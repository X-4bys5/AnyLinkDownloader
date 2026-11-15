# everything about looking up a link: fetching info, sizes, playlists and the page sniffing fallback
import concurrent.futures
import copy
import html as htmllib
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from .. import engine
from .. import network
from .. import pages
from .. import spotify
from ..core import UA, file_log, norm, safe_clone
from ..errors import SIGNIN_ERRORS, is_unreachable
from ..formats import _size_guess, compute_sizes, fmt_duration
from ..jobs import JobLogger
from ..pages import fetch_page, find_media_urls, page_title
from ..settings import EXTRAS, INFO_TTL, META_KEEP, PLAYLIST_CAP, _write_json
from ..urls import _YT_CHANNEL_RE, _YT_ID_RE, classify_url, is_youtube, normalize_url, thumb_of, youtube_list_id


class AnalyzeMixin:
    QUICK_BUDGET = 7.0  # seconds the quick fetch gets before we use the full way

    # once one client answers the others get about as long as it took, between these two
    STRAGGLER_WAIT = 8.0
    STRAGGLER_MIN = 2.0

    def _extract_opts(self, url, st, noplaylist, logger):
        opts = self._base_opts(st)
        opts.update(
            {
                "logger": logger,
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": noplaylist,
                "extract_flat": "in_playlist",
                "playlistend": PLAYLIST_CAP,
            }
        )
        first = self.attempt_plan(url)[0][1]
        if "extractor_args" in first:
            opts["extractor_args"] = first["extractor_args"]
        # hls list is only needed for live streams, _analyze asks again with it if nothing came back
        if is_youtube(url) and not st.get("with_hls") and self.settings.get("skip_m3u8", False):
            ea = dict(opts.get("extractor_args") or {})
            yt = dict(ea.get("youtube") or {})
            yt["skip"] = sorted(set(yt.get("skip") or []) | {"hls"})
            ea["youtube"] = yt
            opts["extractor_args"] = ea
        return opts

    def _wide_clients(self, url, st):
        try:
            # several clients at once is only done on youtube with a js runtime and no browser cookies
            if not is_youtube(url) or not engine.js_runtime() or st.get("browser") not in (None, "", "none"):
                return None
            base = list(engine.yt_dlp.extractor.youtube.YoutubeIE._DEFAULT_CLIENTS)
            out = []
            for c in base + (["tv", "web_safari"] if self.settings.get("find_4k") else []):
                if isinstance(c, str) and c not in out:
                    out.append(c)
            return out if len(out) > 1 else None
        except Exception:
            return None

    @staticmethod
    def _merge_formats(infos):
        infos = [i for i in infos if i and i.get("formats")]
        if not infos:
            return None
        merged = infos[0]
        seen = {f.get("format_id") for f in merged["formats"]}
        by_id = {f.get("format_id"): f for f in merged["formats"]}
        for other in infos[1:]:
            for f in other["formats"]:
                fid = f.get("format_id")
                if fid not in seen:
                    seen.add(fid)
                    merged["formats"].append(f)
                    by_id[fid] = f
                else:
                    kept = by_id.get(fid)
                    if kept is not None and not kept.get("filesize") and f.get("filesize"):
                        kept["filesize"] = f["filesize"]
        return merged

    def _quick_client(self):
        try:
            c = tuple(engine.yt_dlp.extractor.youtube.YoutubeIE._DEFAULT_JSLESS_CLIENTS)
            return c[0] if c else None
        except Exception:
            return None

    @staticmethod
    def _quick_enough(info):
        # only enough with audio and 3+ video heights up to 720p, else the full way gives a better list
        fm = list((info or {}).get("formats") or [])
        vh = {
            f["height"]
            for f in fm
            if f.get("height") and f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")
        }
        has_audio = any(f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none") for f in fm)
        return bool(has_audio and len(vh) >= 3 and max(vh, default=0) >= 720)

    def _extract_quick(self, url, st, strict=False):
        qc = self._quick_client()
        if not qc:
            return None

        t0 = time.monotonic()
        opts = self._extract_opts(url, st, True, JobLogger(trace=True))
        yt_args = dict(
            ((opts.get("extractor_args") or {}).get("youtube") or {}), player_client=[qc], player_skip=["webpage"]
        )
        opts["extractor_args"] = dict(opts.get("extractor_args") or {}, youtube=yt_args)
        opts.update(socket_timeout=6, retries=1, extractor_retries=1)

        def run():
            with self._ydl(opts) as ydl:
                return ydl.extract_info(url, download=False)

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="quick")
        try:
            info = ex.submit(run).result(timeout=self.QUICK_BUDGET)
        except Exception as e:
            if strict and is_unreachable(e):
                raise
            why = norm(e)[:90] or type(e).__name__
            self.log(f"Quick fetch ({qc}) didn't work ({why}, {time.monotonic() - t0:.1f}s) - using the full way.")
            return None
        finally:
            ex.shutdown(wait=False)
        if not self._quick_enough(info):
            self.log(f"Quick fetch ({qc}) gave an incomplete list ({time.monotonic() - t0:.1f}s) - using the full way.")
            return None
        self.log(f"Fetch: quick client ({qc}) answered in {time.monotonic() - t0:.1f}s")
        return info

    def _extract_wide(self, url, st, say=None, strict=False):
        # find_4k needs the extra clients so it wins over plain_fetch
        if self.settings.get("plain_fetch") and not self.settings.get("find_4k"):
            return None
        clients = self._wide_clients(url, st)
        if not clients:
            return None
        if self.settings.get("fast_fetch") and not self.settings.get("find_4k"):
            quick = self._extract_quick(url, st, strict)
            if quick is not None:
                return quick

        t0 = time.monotonic()
        loggers = [JobLogger(trace=True) for _ in clients]

        def one(n):
            opts = self._extract_opts(url, st, True, loggers[n])
            args = {
                "youtube": dict(((opts.get("extractor_args") or {}).get("youtube") or {}), player_client=[clients[n]])
            }
            opts["extractor_args"] = dict(opts.get("extractor_args") or {}, **args)
            with self._ydl(opts) as ydl:
                return ydl.extract_info(url, download=False)

        results, failed, errors = [None] * len(clients), [], []
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(clients), thread_name_prefix="client")
        try:
            futs = {ex.submit(one, n): n for n in range(len(clients))}
            pending, first_ok = set(futs), None
            while pending:
                if first_ok is None:
                    wait = None
                else:
                    # with find_4k the slow clients are the whole point so we wait the full time
                    grace = (
                        self.STRAGGLER_WAIT
                        if self.settings.get("find_4k")
                        else min(self.STRAGGLER_WAIT, max(self.STRAGGLER_MIN, first_ok - t0))
                    )
                    wait = max(0.0, first_ok + grace - time.monotonic())
                done, pending = concurrent.futures.wait(
                    pending, timeout=wait, return_when=concurrent.futures.FIRST_COMPLETED
                )
                if not done:
                    break
                for f in done:
                    n = futs[f]
                    try:
                        results[n] = f.result()
                        if first_ok is None and results[n] and results[n].get("formats"):
                            first_ok = time.monotonic()
                    except Exception as e:
                        errors.append(e)
                        failed.append(f"{clients[n]}: {norm(e)[:80]}")
            for f in pending:
                f.cancel()
                failed.append(f"{clients[futs[f]]}: still waiting, left out")
        finally:
            ex.shutdown(wait=False)  # clients we left out just finish in the background
        info = self._merge_formats(results)
        took = time.monotonic() - t0
        got = sum(1 for r in results if r and r.get("formats"))
        if info is None:
            self.log(f"Fetch: no client answered ({took:.1f}s). " + "; ".join(failed)[:300])
            # every client wanted a sign-in, raise that one so it doesnt turn into a vague failure
            if (
                errors
                and len(errors) == len(clients)
                and all(any(m in str(e).lower() for m in SIGNIN_ERRORS) for e in errors)
            ):
                raise errors[0]
            # unreachable from all of them, _extract can then try the other routes
            if strict and errors and all(is_unreachable(e) for e in errors):
                raise errors[0]
            return None
        self.log(
            f"Fetch: {got}/{len(clients)} YouTube clients answered at once in {took:.1f}s"
            + (f" (no answer from: {'; '.join(failed)[:200]})" if failed else "")
        )
        if took > 6:
            slow = max(loggers, key=lambda lg: len(lg.timeline() or ""))
            self.log("Slow fetch - where the time went: " + (slow.timeline() or "n/a"))
        return info

    def _extract(self, url, st, noplaylist, say=None):
        st = self._apply_cached_route(url, st)
        t0 = time.monotonic()
        logger = JobLogger(trace=True)
        opts = self._extract_opts(url, st, noplaylist, logger)
        try:
            if noplaylist:
                wide = self._extract_wide(url, st, say, strict=True)
                if wide is not None:
                    return wide, st
            with self._ydl(opts) as ydl:
                res = ydl.extract_info(url, download=False)
            self.log(f"Fetch: one ordinary yt-dlp run took {time.monotonic() - t0:.1f}s")
            if time.monotonic() - t0 > 6:
                self.log(
                    f"Slow fetch ({time.monotonic() - t0:.1f}s) - where the time went: " + (logger.timeline() or "n/a")
                )
            return res, st
        except Exception as first_err:
            # cant reach the site but the internet works, so test the routes and retry with the best one
            if not st.get("auto_route") or not is_unreachable(first_err) or not network.internet_ok():
                raise
            best = self._find_route(url, st, say)
            if not best:
                raise
            st = dict(st)
            st["proxy"] = best
            opts["proxy"] = best
            if noplaylist:
                try:
                    wide = self._extract_wide(url, st, say)
                except Exception:
                    wide = None
                if wide is not None:
                    return wide, st
            with self._ydl(opts) as ydl:
                return ydl.extract_info(url, download=False), st

    def analyze(self, raw_url):
        key = normalize_url(raw_url) or str(raw_url).strip()
        with self.lock:
            flight = self.inflight.get(key)
            leader = flight is None
            if leader:
                flight = self.inflight[key] = {"done": threading.Event(), "res": None}
        # same link already being fetched (prefetch for example), wait for that one
        if not leader:
            flight["done"].wait(120)
            if flight["res"] is not None:
                return copy.deepcopy(flight["res"])
        t0 = time.monotonic()
        self._tls.tm = [("start", t0)]
        try:
            res = self._analyze(raw_url)
            self._mark("done")
            self.log(f"Fetch took {time.monotonic() - t0:.1f} s ({(res or {}).get('type')}) for {key[:80]}")
            self.log("Fetch timeline (s): " + self._tm_line())
        except Exception:
            res = None
            raise
        finally:
            if leader:
                flight["res"] = res
                with self.lock:
                    self.inflight.pop(key, None)
                flight["done"].set()
        return res

    def _analyze(self, raw_url):
        url = normalize_url(raw_url)
        if not url:
            return {
                "type": "error",
                "title": "That doesn't look like a link",
                "text": "Paste a web address that starts with http or https.",
                "action": None,
            }
        if pages.is_non_media_url(url):
            return {
                "type": "error",
                "title": "That's not a video or audio link",
                "text": "This app downloads videos and music. Paste the link of the page with the video, or a direct .mp4 / .mp3 link.",
                "action": None,
            }
        st = self._st()
        self._mark("setup")
        if spotify.is_spotify(url) or spotify.is_spotify(raw_url):
            return self._analyze_spotify(url if spotify.is_spotify(url) else raw_url.strip(), st)
        want = classify_url(url)
        if want == "video":
            cinfo = self._cached_info(url, self._apply_cached_route(url, st))
            if cinfo is not None:
                self.log("Fetch: same link again - answered from memory")
                return self._video_result(url, cinfo, self._apply_cached_route(url, st), cached=True)
        # a bare channel link gives a list of tabs, the videos tab is what they want
        target = url.rstrip("/") + "/videos" if _YT_CHANNEL_RE.match(url) else url
        tries = [url] if target == url else [target, url]
        info = None

        def run(u, skip):
            if not self._warm_done.is_set() and is_youtube(u):
                self._warm_done.wait(25)
                self._mark("wait-warmup")
            with self.guard:
                self._mark("wait-engine")
                i, s2 = self._extract(u, dict(st, with_hls=not skip), noplaylist=(want == "video"))
                self._mark("yt-dlp" + ("" if skip else "+hls"))
                i = self._descend_tabs(u, i, s2)
                self._mark("tabs")
            return i, s2

        for n, u in enumerate(tries):
            skip = bool(self.settings.get("skip_m3u8", False)) and is_youtube(u) and want == "video"
            try:
                try:
                    info, st = run(u, skip)
                    # live streams have no normal formats without the hls list
                    if (
                        skip
                        and info.get("entries") is None
                        and (
                            not info.get("formats")
                            or info.get("is_live")
                            or info.get("live_status") in ("is_live", "is_upcoming", "post_live")
                        )
                    ):
                        self.log("Fetch: nothing without the HLS list (live stream?) - reading it too")
                        info, st = run(u, False)
                except Exception as e0:
                    if not skip or is_unreachable(e0):
                        raise
                    self.log(f"Fetch: failed without the HLS list ({norm(e0)[:120]}) - trying with it")
                    info, st = run(u, False)
                break
            except Exception as e:
                # /videos didnt work so try the plain link, unless the site just cant be reached
                if n + 1 < len(tries) and not is_unreachable(e):
                    continue
                msg = norm(e)
                if "unsupported url" in msg.lower():
                    # yt-dlp doesnt know the site, look for video links in the page itself
                    try:
                        title, cands, _ = self._sniff(url, st.get("proxy"))
                        if cands:
                            return {
                                "type": "page",
                                "url": url,
                                "title": title or url,
                                "count": len(cands),
                                "thumb": None,
                                "uploader": urllib.parse.urlparse(url).netloc,
                            }
                    except Exception:
                        pass
                self.log(f"Could not read {url}: {msg}")
                tip = self.friendly(msg)
                title, text, action = tip or (
                    "Couldn't read that link",
                    "The site didn't give the app a video to download.",
                    "retry",
                )
                return {"type": "error", "title": title, "text": text, "action": action, "detail": msg[:400]}

        if info.get("entries") is not None:
            return self._playlist_result(url, info)
        return self._video_result(url, info, st)

    def _analyze_spotify(self, url, st):
        try:
            data = spotify.fetch(url, proxy=st.get("proxy") or None, settings=self.settings)
        except spotify.SpotifyError as e:
            return {"type": "error", "title": e.title, "text": e.text, "action": "retry", "detail": str(e)[:400]}
        except Exception as e:
            self.log(f"Could not read Spotify link {url}: {norm(e)}")
            tip = self.friendly(norm(e))
            title, text, action = tip or (
                "Couldn't read that Spotify link",
                "Spotify didn't answer. Try again in a moment.",
                "retry",
            )
            return {"type": "error", "title": title, "text": text, "action": action, "detail": norm(e)[:400]}
        idx = self.folder_index()
        entries = []
        for i, t in enumerate(data["tracks"], 1):
            vid = "sp-" + t["id"]
            entries.append(
                {
                    "i": i,
                    "id": vid,
                    "url": "spotify:track:" + t["id"],
                    "title": t["title"],
                    "duration": t["duration"],
                    "duration_text": fmt_duration(t["duration"]),
                    "thumb": t["thumb"] or data.get("thumb"),
                    "uploader": t["artists"],
                    "album": t.get("album") or "",
                    "saved": vid in idx,
                    "gone": False,
                }
            )
        return {
            "type": "playlist",
            "url": url,
            "id": data["id"],
            "title": data["title"],
            "uploader": data["owner"],
            "count": len(entries),
            "entries": entries,
            "capped": bool(data.get("partial")),
            "thumb": data["thumb"] or next((e["thumb"] for e in entries if e["thumb"]), None),
            "audio_only": True,
            "source": "spotify",
            "noun": ("song", "songs"),
            "note": "Saved as tagged MP3s with Spotify's cover art, title, artist and album. The audio comes from YouTube Music (Spotify's own streams are DRM-locked)."
            + (
                " Spotify shows 100 songs per playlist; add your own free Spotify keys in the settings file to get all of them."
                if data.get("partial")
                else ""
            ),
        }

    def _descend_tabs(self, url, info, st):
        # channel pages can come back as a list of tabs, go into the videos one (2 levels max)
        for _ in range(2):
            ents = [e for e in (info.get("entries") or []) if e]
            if not ents or not all(
                (e.get("ie_key") or "").lower() == "youtubetab" or e.get("_type") == "playlist" for e in ents
            ):
                break
            pick = next(
                (e for e in ents if str(e.get("url") or e.get("webpage_url") or "").rstrip("/").endswith("/videos")),
                ents[0],
            )
            nxt = pick.get("url") or pick.get("webpage_url")
            if not nxt:
                break
            info, _ = self._extract(nxt, st, noplaylist=False)
        return info

    def _video_result(self, url, info, st, cached=False):
        heights = sorted(
            {f["height"] for f in info.get("formats", []) if f.get("height") and f.get("vcodec") not in (None, "none")},
            reverse=True,
        )
        vid = info.get("id")
        if not cached:
            with self.lock:
                self._store_info(url, st, info)
        lid = youtube_list_id(url)
        self._mark("pre-result")
        # rough sizes from the format list right away (shown with a ~), the real ones come from _start_sizes
        try:
            quick = {k: v for k, v in _size_guess(info, self.settings["container"]).items() if v}
        except Exception:
            quick = {}
        existing = self.existing_file(vid, "video")
        self._mark("saved-check")
        thumb = thumb_of(info)
        if vid and is_youtube(url) and re.fullmatch(r"[\w-]{11}", str(vid)):
            thumb = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        res = {
            "type": "video",
            "url": info.get("webpage_url") or url,
            "src": url,
            "id": vid,
            "title": info.get("title") or "Untitled",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": info.get("duration") or 0,
            "duration_text": fmt_duration(info.get("duration")),
            "thumb": thumb,
            "heights": heights,
            "sizes": quick,
            "sizes_by": {self.settings["container"]: quick} if quick else {},
            "audio_sizes": {},
            "sizes_pending": self._start_sizes(url, info),
            "live": bool(info.get("is_live")),
            "existing": existing,
            "existing_audio": self.existing_file(vid, "audio"),
            "in_playlist": {"id": lid, "url": f"https://www.youtube.com/playlist?list={lid}"} if lid else None,
        }
        if not cached:
            self._remember(url, res)
        return res

    def _remember(self, url, res):
        m = {
            "title": res["title"],
            "uploader": res["uploader"],
            "thumb": res["thumb"],
            "duration_text": res["duration_text"],
        }
        with self.lock:
            if self.meta.get(url, {}).get("title") == m["title"] and self.meta[url].get("thumb") == m["thumb"]:
                return
            self.meta.pop(url, None)
            self.meta[url] = m
            while len(self.meta) > META_KEEP:
                self.meta.pop(next(iter(self.meta)))
            snap = dict(self.meta)
        threading.Thread(target=lambda: _write_json(self.meta_path, snap), daemon=True, name="meta").start()

    def preview(self, raw_url):
        url = normalize_url(raw_url)
        if not EXTRAS or not url:
            return {}
        hit = self.meta.get(url)
        if hit:
            return dict(hit, known=True)
        if not is_youtube(url) or classify_url(url) != "video":
            return {}
        m = _YT_ID_RE.search(url)
        if not m:
            return {}
        try:
            req = urllib.request.Request(
                "https://www.youtube.com/oembed?format=json&url="
                + urllib.parse.quote(f"https://www.youtube.com/watch?v={m.group(1)}", safe=""),
                headers={"User-Agent": UA},
            )
            proxy = self._st().get("proxy")
            opener = (
                urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                if proxy
                else urllib.request.build_opener()
            )
            with opener.open(req, timeout=4) as r:
                d = json.loads(r.read(200_000).decode("utf-8", "replace"))
        except Exception:
            return {}
        if not d.get("title"):
            return {}
        return {
            "title": d["title"],
            "uploader": d.get("author_name") or "",
            "thumb": f"https://i.ytimg.com/vi/{m.group(1)}/mqdefault.jpg",
            "duration_text": "",
        }

    def prefetch(self, raw_url):
        url = normalize_url(raw_url)
        if (
            not EXTRAS
            or not url
            or not self.settings.get("auto_fetch", True)
            or not is_youtube(url)
            or classify_url(url) != "video"
        ):
            return
        with self.lock:
            if (
                self._prefetching
                or url in self.inflight
                or self._cached_info(url, self._apply_cached_route(url, self._st())) is not None
            ):
                return
            self._prefetching = True

        def run():
            try:
                self.analyze(url)
            except Exception as e:
                file_log(f"prefetch skipped: {e}")
            finally:
                self._prefetching = False

        threading.Thread(target=run, daemon=True, name="prefetch").start()

    def _start_sizes(self, url, info):
        container = self.settings["container"]
        with self.lock:
            old = self.size_jobs.get(url)
            if old is not None and old.get("container") == container and old.get("info") is info:
                return True
            box = {
                "done": threading.Event(),
                "first": threading.Event(),
                "res": None,
                "container": container,
                "info": info,
                "rest": False,
            }
            self.size_jobs[url] = box
            while len(self.size_jobs) > 30:
                self.size_jobs.pop(next(iter(self.size_jobs)))

        def work():
            t_q = time.monotonic()
            with self.size_gate:
                t_g = time.monotonic()
                try:
                    # first pass is only the chosen type so numbers show up fast, rest comes in _sizes_rest
                    box["res"] = compute_sizes(info, container, first_only=True)
                    box["res"]["partial"] = True
                    self.log(
                        f"Sizes (s): queue {t_g - t_q:.2f} | chosen type {time.monotonic() - t_g:.2f} | {len(info.get('formats') or [])} formats"
                    )
                except Exception as e:
                    file_log(f"size estimates failed: {e}")
                finally:
                    box["first"].set()

        threading.Thread(target=work, daemon=True, name="sizes").start()
        return True

    def _sizes_rest(self, box):
        def work():
            with self.size_gate:
                t = time.monotonic()
                try:
                    box["res"] = compute_sizes(box["info"], box["container"])
                    self.log(f"Sizes (s): the other file types {time.monotonic() - t:.2f}")
                except Exception as e:
                    file_log(f"size estimates failed: {e}")
                finally:
                    box["done"].set()

        threading.Thread(target=work, daemon=True, name="sizes-rest").start()

    def sizes(self, url, full=False):
        box = self.size_jobs.get(url)
        if box is None:
            return {"sizes": {}, "sizes_by": {}, "audio_sizes": {}}
        t = time.monotonic()
        box["first"].wait(30)
        if full:
            with self.lock:
                start = not box["rest"]
                box["rest"] = True
            if start:
                self._sizes_rest(box)
            box["done"].wait(60)
        else:
            self.log(f"Sizes served after waiting {time.monotonic() - t:.2f} s")
        return box["res"] or {"sizes": {}, "sizes_by": {}, "audio_sizes": {}}

    def _playlist_result(self, url, info):
        idx = self.folder_index()
        entries = []
        for i, e in enumerate(info.get("entries") or [], 1):
            if not e:
                continue
            u = e.get("url") or e.get("webpage_url") or ""
            vid = e.get("id")
            if (
                not str(u).startswith("http")
                and vid
                and "youtube" in str(e.get("ie_key") or e.get("extractor") or "youtube").lower()
            ):
                u = f"https://www.youtube.com/watch?v={vid}"
            if not str(u).startswith("http"):
                continue
            title = e.get("title") or "Untitled"
            # private and deleted videos still show up in the list with these placeholder titles
            gone = title in ("[Private video]", "[Deleted video]", "[Unavailable]") or e.get("availability") in (
                "private",
                "needs_auth",
                "premium_only",
            )
            entries.append(
                {
                    "i": i,
                    "id": vid,
                    "url": u,
                    "title": title,
                    "duration": e.get("duration") or 0,
                    "duration_text": fmt_duration(e.get("duration")),
                    "thumb": thumb_of(e),
                    "uploader": e.get("uploader") or e.get("channel") or "",
                    "saved": bool(vid and str(vid) in idx),
                    "gone": bool(gone),
                }
            )
        return {
            "type": "playlist",
            "url": url,
            "id": info.get("id"),
            "title": info.get("title") or "Playlist",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "count": len(entries),
            "entries": entries,
            "capped": len(entries) >= PLAYLIST_CAP,
            "thumb": next((e["thumb"] for e in entries if e.get("thumb")), None),
        }

    def _store_info(self, url, st, info):
        if info.get("entries") is not None or not info.get("formats"):
            return
        self.info_cache[url] = {
            "t": time.monotonic(),
            "info": info,
            "sig": (st["browser"], st["ipv4"], st.get("proxy", "")),
        }
        while len(self.info_cache) > 24:
            self.info_cache.pop(next(iter(self.info_cache)))

    def _cached_info(self, url, st):
        c = self.info_cache.get(url)
        if not c or c["sig"] != (st["browser"], st["ipv4"], st.get("proxy", "")):
            return None
        if time.monotonic() - c["t"] > INFO_TTL:
            self.info_cache.pop(url, None)
            return None
        try:
            return safe_clone(c["info"])
        except Exception:
            self.info_cache.pop(url, None)
            return None

    def _sniff(self, url, proxy=None):
        page = fetch_page(url, proxy=proxy)
        title, cands, where = page_title(page), find_media_urls(url, page), url
        if not cands:
            # nothing in the page itself, the player is often an iframe so check the first 4
            for src in pages._IFRAME_RE.findall(page)[:4]:
                sub = urllib.parse.urljoin(url, htmllib.unescape(src))
                if not sub.lower().startswith(("http://", "https://")):
                    continue
                try:
                    sub_page = fetch_page(sub, referer=url, proxy=proxy)
                except Exception:
                    continue
                cands = find_media_urls(sub, sub_page)
                if cands:
                    title, where = title or page_title(sub_page), sub
                    break
        return title, cands, where

    def _prefetch_next(self, current):
        with self.lock:
            nxt = next((j for j in self.jobs if j.status == "queued" and j is not current), None)
        if not nxt or nxt.url in self.info_cache or not nxt.url.startswith("http"):
            return

        def work():
            try:
                with self.guard:
                    info, st = self._extract(nxt.url, self._st(), noplaylist=True)
                self._store_info(nxt.url, st, info)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()
