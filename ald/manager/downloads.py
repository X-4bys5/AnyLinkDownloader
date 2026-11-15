import copy
import os
import shutil
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request

from .. import engine
from .. import settings
from .. import network
from .. import progress
from .. import spotdl_engine
from .. import spotify
from ..core import MB, UA, file_log, fmt_size, norm, safe_name
from ..errors import (
    EXTRACT_ERRORS,
    SIGNIN_ERRORS,
    is_dns_error,
    is_merge_error,
    is_rate_limited,
    is_transient,
    is_unreachable,
    needs_fallback,
)
from ..formats import (
    _Picker,
    audio_format_choice,
    audio_size_estimates,
    format_bytes,
    size_estimates,
    video_format_choice,
)
from ..jobs import JobLogger
from ..settings import AUDIO_CODEC, AUDIO_EXTS, MAX_NET_RETRIES
from ..storage import ID_RE, MEDIA_EXT, writable_folder


class DownloadMixin:
    PP_TEXT = {
        "Merger": ("merge", "Joining video and audio"),
        "FFmpegMerger": ("merge", "Joining video and audio"),
        "FFmpegExtractAudio": ("convert", "Converting the sound"),
        "FFmpegVideoRemuxer": ("merge", "Finishing the file"),
        "FFmpegMetadata": ("merge", "Adding the details"),
        "MoveFiles": ("merge", "Saving"),
        "MetadataParser": ("merge", "Adding the details"),
    }

    def _fail(self, job, title, text, action, detail=""):
        self._set(
            job,
            status="failed",
            message=title,
            finished=time.time(),
            error={"title": title, "text": text, "action": action, "detail": (detail or "")[:500]},
        )

    def _run(self, job):
        self._set(job, status="analyzing", message="Getting ready...", progress=0.0, speed=0.0, eta=None, error=None)
        # matched by the video id in the file name, retry sets force so it downloads again anyway
        if not job.force:
            have = self.existing_file(job.vid, job.kind, AUDIO_EXTS.get(job.afmt) if job.kind == "audio" else None)
            if have:
                self.log(f"Already saved, skipping: {have}")
                self._set(
                    job,
                    status="skipped",
                    message="Already saved",
                    path=have,
                    progress=1.0,
                    finished=time.time(),
                    size=os.path.getsize(have),
                )
                return
        folder, _ = writable_folder(self.settings["folder"])
        if folder != self.settings["folder"]:
            self.save_settings({"folder": folder})
        job.dest = folder
        stage = self._stage(job)
        try:
            if shutil.disk_usage(folder).free < 300 * MB or shutil.disk_usage(stage["root"]).free < 300 * MB:
                return self._fail(job, "Your disk is full", "Free up some space or save to another drive.", "folder")
        except Exception:
            pass
        if job.kind != "audio" and not engine.have_tool("ffmpeg"):
            self._set(job, message="Fetching a missing part...")
            engine.repair_tools()
        if job.url.startswith("spotify:track:"):
            job.kind = "audio"
            # with spotDL on a failed youtube match isnt an error yet, spotDL gets a go first
            if not self._resolve_spotify(job, quiet=spotdl_engine.enabled(self.settings)):
                if job.cancel.is_set():
                    self._set(job, status="cancelled", message="Cancelled", finished=time.time(), speed=0.0)
                    return
                if not spotdl_engine.enabled(self.settings):
                    return
                t0 = time.monotonic()
                if self._download_spotdl(job, timeout=90):
                    self._finish_ok(job, t0)
                elif job.cancel.is_set():
                    self._set(job, status="cancelled", message="Cancelled", finished=time.time(), speed=0.0)
                else:
                    self._fail(
                        job,
                        *getattr(job, "sp_err", ("Couldn't find this song", "No good match was found.", "retry", "")),
                    )
                return
        self._prefetch_next(job)
        result = self._download(job)
        if result == "heal":
            self.healed_engine = True  # only once per run so a broken site cant keep re-fetching yt-dlp
            self._set(job, status="analyzing", message="Refreshing the download engine...", progress=0.0)
            self._heal_engine()
            result = self._download(job, healed=True)
        if result == "heal":
            result = "failed"
        if result == "cancelled":
            self._set(job, status="cancelled", message="Cancelled", finished=time.time(), speed=0.0)

    def _download_opts(self, job, container, st, logger):
        audio_only = job.kind == "audio"
        stage = self._stage(job)
        name = f"{job.index:03d} - " if job.index else ""
        # names go through yt-dlp's template so a real % in a title or folder has to be doubled
        sub = job.sub.replace("%", "%%") if job.sub else ""
        template = os.path.join(
            sub, name + "%%(title).%dB [%%(id)s].%%(ext)s" % self._title_limit(stage["work"], sub, name)
        )
        sp_tags = []
        if str(job.vid or "").startswith("sp-") and job.title:
            label = safe_name(
                f"{job.uploader.split(',')[0].strip()} - {job.title}" if job.uploader else job.title, "song"
            )
            template = os.path.join(sub, name + label.replace("%", "%%") + f" [{job.vid}].%(ext)s")
            lit = lambda x: x.replace("%", "%%")  # noqa: E731
            sp_tags = (
                [(lit(job.title), "%(title)s"), (lit(job.uploader), "%(artist)s")]
                if job.uploader
                else [(lit(job.title), "%(title)s")]
            )
        opts = self._base_opts(st)
        opts.update(
            {
                "outtmpl": template,
                "paths": {"home": stage["done"], "temp": stage["work"]},
                "noplaylist": True,
                "progress_hooks": [self._hook(job)],
                "postprocessor_hooks": [self._pp_hook(job)],
                "post_hooks": [lambda fn: setattr(job, "path", fn)],
                "logger": logger,
                "retries": 20,  # yt-dlp retries by itself first, our retry plan starts after these run out
                "fragment_retries": 20,
                "file_access_retries": 20,
                # windows keeps files locked for a bit (antivirus), so renames fail and get retried
                "retry_sleep_functions": {"file_access": lambda n: min(0.5 * (n + 1), 3)},
                "windowsfilenames": True,
                "ignoreerrors": False,
                "postprocessors": [],
                "overwrites": True if job.force else None,
                "updatetime": False,
            }
        )
        have_ffmpeg = engine.have_tool("ffmpeg")
        if audio_only:
            if not have_ffmpeg:
                raise RuntimeError("ffmpeg is not installed - MP3 conversion needs it")
            codec = AUDIO_CODEC.get(job.afmt, "mp3")
            opts["format"] = audio_format_choice(job.afmt)
            if sp_tags:
                MetadataParserPP = engine.yt_dlp.postprocessor.MetadataParserPP
                opts["postprocessors"].append(
                    {
                        "key": "MetadataParser",
                        "when": "pre_process",
                        "actions": [(MetadataParserPP.interpretter, a, b) for a, b in sp_tags],
                    }
                )
            extract = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
            if codec == "mp3":
                extract["preferredquality"] = "0"
            elif codec == "m4a":
                extract["preferredquality"] = "256"
            elif codec == "vorbis":
                extract["preferredquality"] = "6"
            opts["postprocessors"] += [extract, {"key": "FFmpegMetadata"}]
        else:
            fmt, sort, merge = video_format_choice(container, job.height, have_ffmpeg)
            opts["format"] = fmt
            if sort:
                opts["format_sort"] = sort
            if merge:
                opts["merge_output_format"] = merge
            if have_ffmpeg and container in ("mp4", "mkv", "mov"):
                opts["postprocessors"].append({"key": "FFmpegVideoRemuxer", "preferedformat": container})
        if self.prof["threads"]:
            opts.setdefault("postprocessor_args", {}).setdefault("ffmpeg", []).extend(
                ["-threads", str(self.prof["threads"])]
            )
        return opts

    def _hook(self, job):
        def hook(d):
            if job.cancel.is_set():
                raise engine.yt_dlp.utils.DownloadCancelled("Cancelled by user")
            try:
                self._record(job, d)
            except Exception:
                pass

        return hook

    def _pp_hook(self, job):
        def hook(d):
            if d.get("status") == "started":
                phase, text = self.PP_TEXT.get(d.get("postprocessor"), ("merge", "Finishing up"))
                if phase == "convert" and job.kind == "audio":
                    text = f"Converting to {job.afmt.upper()}"
                self._set(
                    job,
                    status="processing",
                    phase=phase,
                    message=text,
                    progress=max(job.progress, job.cap),
                    speed=0.0,
                    eta=None,
                )

        return hook

    def _record(self, job, d):
        info = d.get("info_dict") or {}
        if d["status"] == "finished":
            job.speed = 0.0
            job.message = "Almost there..."
            self.touch()
            return
        if d["status"] != "downloading":
            return
        key = d.get("filename") or str(info.get("format_id"))
        fid = str(info.get("format_id"))
        plan = job.plan or {}
        done = int(d.get("downloaded_bytes") or 0)
        exact = d.get("total_bytes") or info.get("filesize") or 0
        planned = (plan.get("sizes") or {}).get(fid, 0)
        guess = d.get("total_bytes_estimate") or info.get("filesize_approx") or 0
        prev_done, prev_total = job.sizes.get(key, (0, 0))
        # exact size first, then what we planned from the format list, then yt-dlp's own estimate
        total = exact or planned or guess or prev_total
        job.sizes[key] = (max(done, prev_done), max(total, done))  # estimates jump around so these only go up
        job.seen_ids.add(fid)
        job.sure[key] = bool(exact) or (bool(planned) and not plan.get("approx"))
        has_v = info.get("vcodec") not in (None, "none")
        has_a = info.get("acodec") not in (None, "none")
        got = sum(dn for dn, _ in job.sizes.values())
        known = sum(t for _, t in job.sizes.values())
        # streams that havent started yet still count toward the total or the bar jumps back when audio starts
        unseen = sum(n for f, n in (plan.get("sizes") or {}).items() if f not in job.seen_ids)
        # no plan and only the video so far, so guess audio adds about 6%
        if not plan and has_v and not has_a and len(job.sizes) < 2:
            unseen = int(known * 0.06)
        whole = max(known + unseen, got)
        job.exact = all(job.sure.values()) and (bool(plan) or has_v == has_a or len(job.sizes) >= 2)
        frac = got / whole if whole else 0.0
        if not whole and d.get("fragment_count"):
            frac = (d.get("fragment_index") or 0) / d["fragment_count"]
        job.status, job.phase = "downloading", "download"
        job.progress = min(job.cap, max(job.progress, frac * job.cap))
        if got > job.downloaded:
            job.last_data = time.monotonic()
        job.downloaded, job.total = got, whole
        job.meter.add(got)
        job.speed = job.meter.speed
        job.eta = job.meter.eta(whole - got) if whole > got else None
        job.message = "Downloading"
        if key not in job.seen:
            job.seen.add(key)
            kind = "audio" if (has_a and not has_v) else "video"
            self.log(
                f"{kind}: format {info.get('format_id')} | {info.get('protocol')} | "
                f"{info.get('resolution') or info.get('format_note') or '?'}"
                + (f" | {d['turbo_conns']} connections at once" if d.get("turbo_conns") else "")
            )
        self.touch()

    def _reset_progress(self, job):
        job.streams, job.seen, job._kinds, job.sizes = {}, set(), set(), {}
        job.seen_ids, job.sure = set(), {}
        job.meter.reset()
        job.last_data = 0.0
        job.progress, job.downloaded, job.speed, job.eta = 0.0, 0, 0.0, None

    def _make_plan(self, job, cinfo, container):
        job.plan, job.cap = None, 0.985
        if not cinfo:
            return
        dur = cinfo.get("duration") or job.duration or 0
        if job.kind == "audio":
            fmt, sort = audio_format_choice(job.afmt), None
        else:
            fmt, sort, _ = video_format_choice(container, job.height, engine.have_tool("ffmpeg"))
        with _Picker() as pk:
            chosen = pk.pick(cinfo, fmt, sort)
        if not chosen:
            return
        sizes = {str(f.get("format_id")): format_bytes(f, dur) for f in chosen}
        if not all(sizes.values()):
            return
        exact = all(f.get("filesize") for f in chosen)
        heavy = False
        if job.kind == "video" and container in ("webm", "avi") and engine.have_tool("ffmpeg"):
            vc = next((str(f.get("vcodec") or "") for f in chosen if f.get("vcodec") not in (None, "none")), "")
            ac = next((str(f.get("acodec") or "") for f in chosen if f.get("acodec") not in (None, "none")), "")
            # vp9/av1 + opus go into webm as they are, anything else gets re-encoded in _convert_video
            native = container == "webm" and vc.startswith(("vp9", "vp8", "av01")) and ac.startswith(("opus", "vorbis"))
            heavy = not native
        job.plan = {
            "sizes": sizes,
            "total": sum(sizes.values()),
            "heavy": heavy,
            "approx": not exact,
            "desc": " + ".join(
                f"{f.get('format_id')} {str(f.get('vcodec') if f.get('vcodec') not in (None, 'none') else f.get('acodec'))[:5]} "
                f"{f.get('height') or ''}{'p' if f.get('height') else ''} {f.get('protocol') or ''} "
                f"{'exact' if f.get('filesize') else 'guessed'} {fmt_size(sizes[str(f.get('format_id'))])}"
                for f in chosen
            ),
        }
        if heavy:
            job.cap = 0.5  # a real re-encode takes long so half the bar is kept for it
        job.total = job.plan["total"]
        job.exact = exact

    def _sleep(self, job, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if job.cancel.is_set():
                return True
            time.sleep(0.1)
        return job.cancel.is_set()

    def _wait_for_internet(self, job):
        self._set(job, status="waiting", message="Waiting for the internet...", speed=0.0, eta=None)
        self.log("Internet is down - waiting for it to come back.")
        end = time.monotonic() + 1800
        while time.monotonic() < end and not job.cancel.is_set():
            if network.internet_ok():
                self.log("Internet is back.")
                return True
            time.sleep(3)
        return False

    def _remove_stream_files(self, job):
        st = self._stage(job)
        n = 0
        for d in (st["work"], st["done"]):
            for dirpath, _, files in os.walk(d):
                n += len(files)
            shutil.rmtree(d, ignore_errors=True)
        return n

    def _download(self, job, healed=False):
        yt = engine.yt_dlp
        logger = JobLogger(self.log)
        st = self._apply_cached_route(job.url, self._st())
        attempts = self.attempt_plan(job.url)  # more connections first, then fewer, other clients, then one
        audio_only = job.kind == "audio"
        t0 = time.monotonic()
        self._reset_progress(job)
        try:
            cinfo = self._cached_info(job.url, st)
            if cinfo is None:  # nothing cached, the size plan needs the formats so fetch like analyze does
                try:
                    with self.guard:
                        wide = self._extract_wide(job.url, st)
                except Exception:
                    wide = None
                if wide is not None:
                    self._store_info(job.url, st, wide)
                    cinfo = self._cached_info(job.url, st)
            self._make_plan(job, cinfo, job.container)
            if cinfo and job.kind == "audio":
                job.expected = audio_size_estimates(cinfo).get(job.afmt, 0)
            elif cinfo:
                job.expected = size_estimates(cinfo, job.container).get(str(job.height) if job.height else "best", 0)
            self.log(f"Downloading: {job.url}")
            i = net = announced = 0
            sniffed = rerouted = fresh_done = single_stream = False
            cur_container = job.container
            cookie_left = None
            while True:
                label, extra = attempts[i]
                if i != announced:
                    announced = i
                    self.log(f"Retry {i + 1}/{len(attempts)}: {label}")
                    self._set(job, message=f"Trying another way ({label})", progress=0.0, attempt=label)
                self._reset_progress(job)
                logger.errors.clear()
                opts = self._download_opts(job, cur_container, st, logger)
                opts.update(extra)
                if single_stream:
                    # last resort after merging failed twice, ask for one ready made file
                    opts["format"] = f"best[height<={job.height}]/best" if job.height else "best"
                    opts.pop("merge_output_format", None)
                    opts.pop("format_sort", None)
                elif i == len(attempts) - 1 and job.height and not audio_only:
                    opts["format"] += "/bv*+ba/b"  # last try takes anything if the exact height isnt there
                # only the first try reuses the fetched info, the retries use other clients so it wouldnt fit
                cached = self._cached_info(job.url, st) if i == 0 else None
                try:
                    with self.guard:
                        with self._ydl(opts) as ydl:
                            if cached is not None:
                                try:
                                    ydl.process_ie_result(cached, download=True)
                                except (TypeError, copy.Error) as reuse_err:
                                    if "pickle" not in str(reuse_err) and not isinstance(reuse_err, copy.Error):
                                        raise
                                    self.info_cache.pop(job.url, None)
                                    ydl.download([job.url])
                            else:
                                ydl.download([job.url])
                    break
                except yt.utils.DownloadCancelled:
                    raise
                except Exception as e:
                    if job.cancel.is_set():
                        raise yt.utils.DownloadCancelled("Cancelled by user")
                    err = str(e)
                    low = err.lower()
                    if needs_fallback(err):
                        self.info_cache.pop(job.url, None)
                    # sign-in or age check, try each installed browser's cookies and start the plan over
                    if any(s in low for s in SIGNIN_ERRORS) and st["browser"] in ("none", "") and cookie_left is None:
                        cookie_left = settings.installed_browsers()
                    if cookie_left:
                        b = cookie_left.pop(0)
                        self.log(f"This needs a signed-in browser - trying {b.title()}'s sign-in...")
                        self._set(job, message=f"Using {b.title()}'s sign-in", progress=0.0)
                        st = dict(st, browser=b)
                        i = announced = 0
                        self.info_cache.pop(job.url, None)
                        continue
                    # couldnt read the cookies (browser still open?), remember that and go on without
                    if st["browser"] not in ("none", "") and "cookie" in low:
                        self.cookie_cache[str(st["browser"]).lower()] = (time.monotonic(), False)
                        st = dict(st, browser="none")
                        i = announced = 0
                        self.info_cache.pop(job.url, None)
                        continue
                    # merge failed: mkv first, then a fresh download, then one ready made stream
                    if not audio_only and not single_stream and is_merge_error(err):
                        if cur_container not in ("mkv", "avi"):
                            self.log(f"Couldn't merge into {cur_container.upper()} - re-merging as MKV (same quality).")
                            cur_container = "mkv"
                        elif not fresh_done:
                            fresh_done = True
                            gone = self._remove_stream_files(job)
                            self.log(f"Merging failed - removed {gone} damaged file(s), downloading fresh...")
                        else:
                            single_stream = True
                            self._remove_stream_files(job)
                            self.log("Merging failed again - falling back to one ready-made stream.")
                        self._set(job, message="Repairing...", progress=0.0)
                        continue
                    if not healed and not self.healed_engine and any(s in low for s in EXTRACT_ERRORS):
                        return "heal"  # _run gets a fresh yt-dlp and calls us again
                    # no extractor for the site, read the page for video links ourselves (once)
                    if not sniffed and "unsupported url" in low:
                        sniffed = True
                        if self._sniff_and_download(job, cur_container, st):
                            break
                        raise
                    # connection errors while the internet is really down: wait for it, dont use up attempts
                    if (is_transient(err) or is_dns_error(err)) and not network.internet_ok():
                        if self._wait_for_internet(job):
                            self._set(job, message="Back online - continuing")
                            continue
                        raise (
                            yt.utils.DownloadCancelled("Cancelled by user")
                            if job.cancel.is_set()
                            else RuntimeError("No internet connection")
                        )
                    if is_transient(err) and net < MAX_NET_RETRIES:
                        net += 1
                        wait = 2**net
                        self.log(f"Network hiccup - retrying in {wait}s ({net}/{MAX_NET_RETRIES})")
                        self._set(job, message=f"Connection hiccup - retrying in {wait}s")
                        if self._sleep(job, wait):
                            raise yt.utils.DownloadCancelled("Cancelled by user")
                        continue
                    # site cant be reached but the internet works so its probably blocked, test routes (once)
                    if st.get("auto_route") and not rerouted and is_unreachable(err) and network.internet_ok():
                        rerouted = True
                        best = self._find_route(job.url, st, lambda m: self._set(job, message=m))
                        if best and best != st.get("proxy"):
                            st = dict(st, proxy=best)
                            self.info_cache.pop(job.url, None)
                            net = 0
                            continue
                    if i + 1 < len(attempts) and needs_fallback(err):
                        if is_rate_limited(err):
                            self.log("The site says slow down - waiting 5s before the next try...")
                            self._set(job, message="The site asked us to slow down...")
                            if self._sleep(job, 5):
                                raise yt.utils.DownloadCancelled("Cancelled by user")
                        i += 1
                        continue
                    raise
            return self._finish_ok(job, t0)
        except yt.utils.DownloadCancelled:
            return "cancelled"
        except Exception as e:
            if job.cancel.is_set():
                return "cancelled"
            msg = norm(e)
            file_log("DOWNLOAD FAILED\n" + traceback.format_exc())
            tip = self.friendly(msg)
            title, text, action = tip or (
                "That didn't work",
                "The app tried every way it knows. Open Help to send the details.",
                "retry",
            )
            self.log(f"Failed: {msg}")
            self._fail(job, title, text, action, msg)
            return "failed"

    def _sniff_and_download(self, job, container, st):
        yt = engine.yt_dlp
        try:
            title, cands, referer = self._sniff(job.url, st.get("proxy"))
        except Exception as e:
            self.log(f"Couldn't read the page directly: {norm(e)}")
            return False
        if not cands:
            return False
        self.log(
            f"No dedicated support for this site, but found {len(cands)} video link(s) on the page - trying them..."
        )
        name = safe_name(title, urllib.parse.urlparse(job.url).netloc or "video").replace("%", "%%")
        for n, cand in enumerate(cands, 1):
            if job.cancel.is_set():
                raise yt.utils.DownloadCancelled("Cancelled by user")
            self._reset_progress(job)
            job.plan, job.cap = None, 0.985
            opts = self._download_opts(job, container, st, JobLogger())
            opts.update(
                {
                    "outtmpl": name + " [web].%(ext)s",
                    "http_headers": {"User-Agent": UA, "Referer": referer},
                    "concurrent_fragment_downloads": self.prof["conns"],
                }
            )
            try:
                with self.guard:
                    with self._ydl(opts) as ydl:
                        ydl.download([cand])
                return True
            except yt.utils.DownloadCancelled:
                raise
            except Exception:
                self.log(f"   link {n}/{len(cands)} didn't work, trying the next...")
        return False

    def _convert_video(self, job, src, container):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return src
        vc, ac, dur = progress.probe(src, shutil.which("ffprobe") or "ffprobe")
        dur = dur or job.duration or 0
        label = container.upper()
        if container == "webm" and vc in ("vp8", "vp9", "av1") and ac in ("opus", "vorbis"):
            codec, text = ["-c", "copy"], f"Packing as {label}"
        elif container == "webm":
            codec, text = (
                [
                    "-c:v",
                    "libvpx-vp9",
                    "-crf",
                    "32",
                    "-b:v",
                    "0",
                    "-row-mt",
                    "1",
                    "-cpu-used",
                    "4",
                    "-deadline",
                    "good",
                    "-c:a",
                    "libopus",
                ],
                f"Converting to {label}",
            )
        else:
            codec, text = (
                ["-c:v", "libxvid", "-vtag", "XVID", "-qscale:v", "5", "-c:a", "libmp3lame", "-b:a", "192k"],
                f"Converting to {label}",
            )
        threads = ["-threads", str(self.prof["threads"])] if self.prof["threads"] else []
        base = os.path.splitext(src)[0]
        tmp, dst = base + ".converting." + container, base + "." + container
        lo = max(job.progress, job.cap if (job.plan or {}).get("heavy") else 0.9)
        self.log(f"{text}: {os.path.basename(src)} ({vc or '?'} + {ac or '?'})")

        def on_progress(frac, x, eta):
            self._set(
                job,
                status="processing",
                phase="convert",
                progress=lo + (0.995 - lo) * frac,
                speed=0.0,
                eta=eta,
                message=text + (f"  ({x:.1f}x speed)" if x else ""),
            )

        self._set(job, status="processing", phase="convert", progress=lo, speed=0.0, eta=None, message=text)
        try:
            progress.run_ffmpeg(
                [ffmpeg, "-y", "-i", src, "-map", "0:v:0?", "-map", "0:a:0?", "-map_metadata", "0"]
                + codec
                + threads
                + [tmp],
                dur,
                on_progress,
                cancel=job.cancel,
            )
            for n in range(20):  # antivirus can still be scanning the new file, retry the rename for ~10s
                try:
                    os.replace(tmp, dst)
                    break
                except PermissionError:
                    if n == 19:
                        raise
                    time.sleep(0.5)
            try:
                os.remove(src)
            except OSError:
                pass
            return dst
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            if job.cancel.is_set():
                raise engine.yt_dlp.utils.DownloadCancelled("Cancelled by user")
            self.log(f"Couldn't convert to {label}: {norm(e)} - keeping {os.path.splitext(src)[1].upper().lstrip('.')}")
            job.note = f"Saved as {os.path.splitext(src)[1].upper().lstrip('.')}: {label} conversion didn't work"
            self.toast("warn", job.note)
            return src

    def _finish_ok(self, job, t0):
        path = job.path
        if (not path or not os.path.exists(path)) and job.stage:
            for dirpath, _, files in os.walk(job.stage["done"]):
                hit = next((f for f in files if os.path.splitext(f.lower())[1] in MEDIA_EXT and ID_RE.search(f)), None)
                if hit:
                    path = job.path = os.path.join(dirpath, hit)
                    break
        if not path or not os.path.exists(path):
            self.folder_index(force=True)
            path = self.existing_file(job.vid, job.kind) or path
        if (
            job.kind == "video"
            and job.container in ("webm", "avi")
            and path
            and os.path.exists(path)
            and not path.lower().endswith("." + job.container)
        ):
            path = self._convert_video(job, path, job.container)
            job.path = path
        if (
            str(job.vid or "").startswith("sp-")
            and path
            and path.lower().endswith((".mp3", ".m4a", ".flac"))
            and os.path.exists(path)
            and not getattr(job, "tagged", False)
        ):
            try:
                px = self._st().get("proxy") or None
                cover = spotify.oembed_thumb("track", str(job.vid)[3:], px) or job.thumb
                if cover and cover != job.thumb:
                    self._set(job, thumb=cover)
                spotify.embed_tags(path, job.title, job.uploader, job.album, cover, px, track_no=job.index)
                self.log(
                    f"Cover art embedded from {cover}" if cover else "No cover picture could be found for this song."
                )
            except Exception as e:
                self.log(f"Couldn't embed the cover art: {norm(e)}")
        if path and os.path.exists(path):
            path = job.path = self._deliver(job, path)
        size = os.path.getsize(path) if path and os.path.exists(path) else 0
        secs = time.monotonic() - t0
        self.log(f"Finished in {secs:.0f}s | {fmt_size(size)} | {path or '?'}")
        # how far off the size we showed was, handy for tuning the guesses
        try:
            plan = job.plan or {}
            if size and (job.expected or plan.get("total")):
                shown = job.expected or 0
                off = f"{(size - shown) / shown * 100:+.0f}%" if shown else "n/a"
                self.log(
                    f"Size check: shown ~{fmt_size(shown)} | planned {fmt_size(plan.get('total') or 0)} "
                    f"({'exact' if not plan.get('approx') else 'guessed'}) | real {fmt_size(size)} | off {off} | {plan.get('desc') or 'no plan'}"
                )
        except Exception:
            pass
        entry = {
            "path": path,
            "title": job.title,
            "thumb": job.thumb,
            "size": size,
            "t": time.time(),
            "kind": job.kind,
            "url": job.url,
            "id": job.vid,
            "uploader": job.uploader,
        }
        with self.lock:
            self.library = ([entry] + [e for e in self.library if e.get("path") != path])[:500]
        self._save_library()
        self._index_add(path, job.vid)
        self._set(
            job,
            status="done",
            message="Saved",
            progress=1.0,
            speed=0.0,
            eta=None,
            path=path,
            size=size,
            total=size or job.total,
            exact=True,
            phase="",
            took=round(secs, 1),
            finished=time.time(),
        )
        return "ok"

    def _download_spotdl(self, job, timeout=300):
        tid = job.url.rsplit(":", 1)[-1]
        stage = self._stage(job)
        dest = os.path.join(stage["done"], job.sub) if job.sub else stage["done"]
        self._set(job, status="downloading", message="Trying another way...", progress=0.05, speed=0.0, eta=None)
        box = {}
        tmp = None
        try:
            os.makedirs(dest, exist_ok=True)
            os.makedirs(stage["work"], exist_ok=True)
            tmp = tempfile.mkdtemp(prefix="spdl-", dir=stage["work"])

            # runs in a thread so we can stop waiting on timeout or cancel (the thread itself keeps going)
            def work():
                try:
                    box["path"] = spotdl_engine.download_track(
                        tid, tmp, self.settings, self._st().get("proxy") or None, job.afmt
                    )
                except BaseException as e:  # noqa: BLE001
                    box["err"] = e

            th = threading.Thread(target=work, daemon=True)
            th.start()
            t_end = time.monotonic() + timeout
            while th.is_alive():
                if job.cancel.is_set() or time.monotonic() > t_end:
                    return False
                th.join(0.3)
                self._set(job, message=f"Trying another way... {int(time.monotonic() - (t_end - timeout))}s")
            if "err" in box or not box.get("path"):
                self.log(
                    f"spotDL could not get {job.uploader} - {job.title}: {norm(box.get('err') or 'no file')} - using the built-in matcher"
                )
                return False
            src = box["path"]
            ext = os.path.splitext(src)[1] or AUDIO_EXTS.get(job.afmt, ".mp3")
            prefix = f"{job.index:03d} - " if job.index else ""
            label = safe_name(
                f"{job.uploader.split(',')[0].strip()} - {job.title}" if job.uploader else job.title, "song"
            )
            final = os.path.join(dest, f"{prefix}{label} [{job.vid}]{ext}")
            os.replace(src, final)
            job.path = final
            job.tagged = True
            self.log(f"spotDL saved {final}")
            return True
        except Exception as e:
            self.log(f"spotDL failed: {norm(e)} - using the built-in matcher")
            return False
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    def _resolve_spotify(self, job, quiet=False):
        self._set(job, message="Finding it on YouTube Music...")
        st = dict(self._st(), browser="none")
        title, artists = job.title, job.uploader
        cands, last = [], None
        for q in spotify.search_queries(title, artists):
            if job.cancel.is_set():
                return False
            try:
                with self.guard:
                    info, st = self._extract(q, st, noplaylist=False)
                cands = [e for e in (info.get("entries") or []) if e]
                hit = spotify.pick_match(cands, title, artists, job.duration)
                if hit:
                    vid = hit.get("id")
                    url = (
                        hit.get("url")
                        if str(hit.get("url") or "").startswith("http")
                        else f"https://www.youtube.com/watch?v={vid}"
                    )
                    self.log(f"Spotify '{artists} - {title}' -> {url}")
                    self._set(job, url=url, message="Getting ready...")
                    return True
            except Exception as e:
                last = e
                self.log(f"YouTube Music search failed ({q[:40]}...): {norm(e)}")
        if last is not None and not cands:
            tip = self.friendly(norm(last))
            err = (
                *(tip or ("Couldn't search YouTube Music", "The search didn't work. Try again in a moment.", "retry")),
                norm(last),
            )
        else:
            err = ("Couldn't find this song", f'No good match for "{artists} - {title}" on YouTube Music.', "retry", "")
        if quiet:
            job.sp_err = err
        else:
            self._fail(job, *err[:3], detail=err[3])
        return False
