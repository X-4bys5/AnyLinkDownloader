# the Manager class: shared state and the background threads, the rest is split into mixins
import os
import queue
import re
import socket
import sys
import threading
import time

import ota
from .. import appupdate
from .. import settings
from .. import engine
from .. import network
from .. import urls
from ..core import APP_NAME, APP_VERSION, CONFIG_PATH, DATA_DIR, MB, file_log, fmt_size, norm
from ..jobs import Job, JobLogger, PUBLIC_JOB_FIELDS, _Guard
from .analyze import AnalyzeMixin
from .downloads import DownloadMixin
from .jobqueue import QueueMixin
from .library import LibraryMixin
from .options import OptionsMixin
from .staging import StagingMixin
from ..network import set_low_impact
from ..settings import (
    AUDIO_FORMATS,
    DEFAULTS,
    EXTRAS,
    MAX_WORKERS,
    PROFILES,
    SPEED_TEST_URL,
    VIDEO_FORMATS,
    _read_json,
    _write_json,
)
from ..storage import default_folder, find_leftovers, writable_folder


class Manager(AnalyzeMixin, OptionsMixin, LibraryMixin, QueueMixin, StagingMixin, DownloadMixin):
    def __init__(self, start_threads=True):
        self.lock = threading.RLock()
        self.guard = _Guard()
        self.settings = dict(DEFAULTS)
        self.cookie_cache = {}
        self.cookie_jars = {}
        self.settings.update({k: v for k, v in _read_json(CONFIG_PATH, {}).items() if k in DEFAULTS})
        folder = self.settings.get("folder") or default_folder()
        self.settings["folder"], moved = writable_folder(folder)
        self.library = [e for e in _read_json(os.path.join(DATA_DIR, "library.json"), []) if isinstance(e, dict)][:500]
        self.jobs = []
        self.subs = []
        self.dirty = threading.Event()
        self.wake = threading.Event()
        self.log_lines = []
        self.routes = {}
        self.dns_proxies = {}
        self.info_cache = {}
        self.inflight = {}
        self.size_jobs = {}
        self.size_gate = threading.Semaphore(1)
        self.route_failed = False
        self.healed_engine = False
        self._swap_after = 0.0
        self.stop = threading.Event()
        self.index_cache = (0, None, {})
        self._warm_done = threading.Event()
        self._warm_done.set()
        self._tls = threading.local()
        self._index_busy = False
        self._prefetching = False
        self.meta_path = os.path.join(DATA_DIR, "meta.json")
        self.meta = {k: v for k, v in _read_json(self.meta_path, {}).items() if isinstance(v, dict)}
        self.last_clip = None
        self.studio_paths = set()
        self.engine_note = None
        self.appu = {"state": "idle", "progress": 0.0, "error": "", "release": None, "enabled": appupdate.enabled()}
        self.appu_cancel = threading.Event()
        self.ota = {"state": "idle", "error": ""}
        self.started = time.time()
        # jobs saved from last time come back as queued and start over from 0
        for e in _read_json(os.path.join(DATA_DIR, "queue.json"), []):
            try:
                j = Job(**{k: v for k, v in e.items() if k in PUBLIC_JOB_FIELDS})
                j.status = "queued"
                j.progress, j.speed, j.eta = 0.0, 0.0, None
                j.message = "Ready to continue"
                self.jobs.append(j)
            except Exception:
                pass
        self.resumable = sum(1 for j in self.jobs if j.active)
        self.paused = self.resumable > 0  # leftovers wait until they pick continue or drop in the ui
        self.log(
            f"{APP_NAME} {APP_VERSION} | yt-dlp {engine.yt_dlp.version.__version__ if engine.yt_dlp else '?'} "
            f"| Python {sys.version.split()[0]}"
        )
        if moved:
            self.toast("warn", f"Couldn't save to the old folder, so videos go to {self.settings['folder']} now.")
        engine.set_rename_wait_callback(lambda: self.log("Windows is holding the file (antivirus scan?), waiting..."))
        if self.settings.get("profile") not in PROFILES:
            self.settings["profile"] = "balanced"
        set_low_impact(self.prof["nice"])
        if start_threads:
            for n in range(MAX_WORKERS):
                threading.Thread(target=self._worker, daemon=True, name=f"worker{n}").start()
            threading.Thread(target=self._ticker, daemon=True, name="ticker").start()
            threading.Thread(target=self._startup_tasks, daemon=True, name="startup").start()
            threading.Thread(target=self._ota_loop, daemon=True, name="quick-update").start()

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    _DROPPABLE = ("state", "log")

    def emit(self, kind, data):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait((kind, data))
                continue
            except queue.Full:
                pass
            # queue is full, drop an old state or log message to make room (state gets resent anyway)
            try:
                with q.mutex:
                    victim = next((it for it in q.queue if it[0] in self._DROPPABLE), None)
                    if victim is None and kind in self._DROPPABLE:
                        continue
                    if victim is None:
                        victim = q.queue[0]
                    q.queue.remove(victim)
                q.put_nowait((kind, data))
            except Exception:
                pass

    def log(self, text):
        file_log(text)
        with self.lock:
            self.log_lines.append(text)
            del self.log_lines[:-800]
        self.emit("log", text)

    def toast(self, kind, text):
        self.emit("toast", {"kind": kind, "text": text})

    def touch(self):
        self.dirty.set()

    def _ticker(self):
        while not self.stop.is_set():
            woke = self.dirty.wait(0.5)
            if not woke and any(j.status == "downloading" for j in self.jobs):
                woke = True
            if woke:
                self.dirty.clear()
                try:
                    self.emit("state", self.snapshot(light=True))
                except Exception:
                    pass
                time.sleep(0.16)

    def snapshot(self, light=False):
        with self.lock:
            jobs = [j.to_dict() for j in self.jobs[-300:]]
        # light is the one that goes out every tick, library and health only go with a full one
        snap = {
            "jobs": jobs,
            "paused": self.paused,
            "folder": self.settings["folder"],
            "app_update": self.app_update_view(),
            "ota": self.ota_view(),
        }
        if not light:
            snap.update(
                {
                    "settings": dict(self.settings),
                    "version": APP_VERSION,
                    "library": self.library_view(),
                    "health": self.health(),
                    "resumable": self.resumable,
                    "platform": sys.platform,
                    "browsers": settings.installed_browsers(),
                }
            )
        return snap

    def health(self):
        v = engine.versions()
        return {
            "ffmpeg": engine.have_tool("ffmpeg"),
            "ffprobe": engine.have_tool("ffprobe"),
            "js": engine.js_runtime(),
            "ytdlp": v["ytdlp"],
            "ejs": v["ejs"],
            "updated": v["updated"],
            "impersonate": engine.HAVE_IMPERSONATE,
            "python": sys.version.split()[0],
            "note": self.engine_note,
        }

    def save_settings(self, patch):
        with self.lock:
            for k, v in (patch or {}).items():
                if k not in DEFAULTS:
                    continue
                if k == "folder":
                    v, _ = writable_folder(str(v))
                    self.index_cache = (0, None, {})  # different folder so the old scan is useless
                elif k == "container":
                    v = str(v).lower() if str(v).lower() in VIDEO_FORMATS else "mp4"
                elif k == "audio_format":
                    v = str(v).lower() if str(v).lower() in AUDIO_FORMATS else "mp3"
                elif k == "profile":
                    v = str(v).lower() if str(v).lower() in PROFILES else "balanced"
                elif isinstance(DEFAULTS[k], bool):
                    v = bool(v)
                else:
                    v = str(v)
                self.settings[k] = v
            _write_json(CONFIG_PATH, self.settings)
        if "profile" in (patch or {}):
            set_low_impact(self.prof["nice"])
            self.wake.set()
        self.touch()
        return self.settings

    def _save_queue(self):
        with self.lock:
            _write_json(os.path.join(DATA_DIR, "queue.json"), [j.persistent() for j in self.jobs if j.active][:300])

    def _save_library(self):
        _write_json(os.path.join(DATA_DIR, "library.json"), self.library[:500])

    def update_engine(self):
        try:
            res = engine.update_now(force=True)
        except Exception as e:
            self.log(f"Update failed: {norm(e)}")
            return {"ok": False, "text": "Couldn't reach the update server. Check your internet and try again."}
        if not res["downloaded"]:
            return {"ok": True, "text": f"Already up to date (yt-dlp {res['latest']})."}
        busy = any(j.active for j in self.jobs)
        if busy:
            # swapping yt-dlp in the middle of a download breaks it, _idle_tasks does it once nothing runs
            return {
                "ok": True,
                "text": f"Downloaded yt-dlp {res['latest']}. It switches on after the current downloads.",
            }
        v = self._swap_engine()
        return {"ok": True, "text": f"Updated to yt-dlp {v or res['latest']}."}

    def app_update_view(self):
        a = self.appu
        rel = a.get("release") or {}
        return {
            "enabled": a["enabled"],
            "state": a["state"],
            "progress": round(a["progress"], 3),
            "error": a["error"],
            "current": APP_VERSION,
            "version": rel.get("version", ""),
            "size": rel.get("size", 0),
            "notes": rel.get("notes", ""),
            "page": rel.get("page", ""),
            "can_install": appupdate.can_install(),
        }

    def ota_view(self):
        o = self.ota
        return {
            "enabled": ota.enabled(),
            "state": "ready" if ota.pending() else o["state"],
            "error": o["error"],
            "running": ota.level(),
            "pending": ota.pending(),
            "built": ota.built(),
        }

    def ota_check(self, force=False):
        if not ota.enabled() or self.ota["state"] == "checking":
            return self.ota_view()
        self.ota.update(state="checking", error="")
        try:
            res = ota.update()
            self.ota.update(state=res["state"])
            if res["state"] == "ready":
                self.log("A quick update was downloaded, it switches on the next time the app opens.")
                self.toast("good", "Update downloaded. It switches on the next time you open the app.")
        except Exception as e:
            self.log(f"Quick update check failed: {norm(e)[:160]}")
            self.ota.update(state="error" if force else "uptodate", error="Couldn't check for updates right now.")
        self.touch()
        return self.ota_view()

    def _ota_loop(self):
        time.sleep(8)  # after the other start up work has had its turn
        while not self.stop.is_set():
            if self.settings["auto_update"] and not engine.NO_UPDATE and ota.enabled():
                self.ota_check()
            # small downloads so this checks more often than the installer, every 3 hours
            for _ in range(3 * 3600 // 5):
                if self.stop.is_set():
                    return
                time.sleep(5)

    def _appu_set(self, **kw):
        self.appu.update(kw)
        self.touch()

    def app_update_check(self, force=False):
        a = self.appu
        if not a["enabled"] or a["state"] in ("downloading", "installing"):
            return self.app_update_view()
        try:
            res = appupdate.check(force=force)
        except Exception as e:
            self.log(f"App update check failed: {norm(e)[:160]}")
            if force:
                self._appu_set(state="error", error="Couldn't reach GitHub. Check your internet and try again.")
            return self.app_update_view()
        rel = res["release"]
        if res["newer"] and rel:
            self._appu_set(
                release=rel,
                error="",
                progress=1.0 if appupdate.have_file(rel) else 0.0,
                state="ready" if appupdate.have_file(rel) else "available",
            )
        else:
            self._appu_set(release=rel, state="uptodate", error="", progress=0.0)
        return self.app_update_view()

    def app_update_download(self):
        a = self.appu
        rel = a.get("release")
        if not rel or a["state"] not in ("available", "error", "ready"):
            return self.app_update_view()
        if a["state"] == "ready" and appupdate.have_file(rel):
            return self.app_update_view()
        self.appu_cancel.clear()
        self._appu_set(state="downloading", progress=0.0, error="")

        def run():
            last = [0.0]

            def prog(f):
                if time.monotonic() - last[0] > 0.2:
                    last[0] = time.monotonic()
                    self._appu_set(progress=f)

            try:
                path, fresh = appupdate.download(rel, prog, self.appu_cancel)
                self.log(
                    f"App update {rel['version']}: {'downloaded' if fresh else 'already on disk, not downloaded again'}"
                )
                self._appu_set(state="ready", progress=1.0)
            except InterruptedError:
                self._appu_set(state="available")
            except Exception as e:
                self.log(f"App update download failed: {norm(e)[:200]}")
                self._appu_set(state="error", error=f"Couldn't download the update: {norm(e)[:140]}")

        threading.Thread(target=run, daemon=True, name="app-update").start()
        return self.app_update_view()

    def app_update_install(self, confirm=False):
        rel = self.appu.get("release")
        path = rel and appupdate.have_file(rel)
        if not path:
            self._appu_set(state="available", progress=0.0)
            return {"ok": False, "text": "The update isn't downloaded yet.", "view": self.app_update_view()}
        busy = sum(1 for j in self.jobs if j.active)
        if busy and not confirm:
            return {
                "ok": False,
                "busy": busy,
                "text": f"{busy} {'download is' if busy == 1 else 'downloads are'} still running.",
                "view": self.app_update_view(),
            }
        try:
            # saved first because the app quits right after the installer starts
            self._save_queue()
            _write_json(CONFIG_PATH, self.settings)
            appupdate.install(path)
        except Exception as e:
            self.log(f"App update install failed: {norm(e)[:200]}")
            return {"ok": False, "text": norm(e)[:200], "view": self.app_update_view()}
        self._appu_set(state="installing")
        return {"ok": True, "quit": True, "view": self.app_update_view()}

    def cleanup(self, do=False):
        found = find_leftovers(self.settings["folder"])
        total = sum(sz for _, sz in found)
        if do:
            gone = 0
            for fp, _ in found:
                try:
                    os.remove(fp)
                    gone += 1
                except OSError:
                    pass
            self.log(f"Deleted {gone} of {len(found)} leftover file(s) ({fmt_size(total)}).")
            return {"count": gone, "size": fmt_size(total), "done": True}
        return {"count": len(found), "size": fmt_size(total), "done": False}

    def speed_test(self):
        try:
            single, multi, n = network.measure_line(SPEED_TEST_URL.format(n=10_000_000))
            return {"ok": True, "single": single / MB, "multi": multi / MB, "mbit": multi * 8 / 1e6, "n": n}
        except Exception as e:
            return {"ok": False, "text": norm(e)}

    def diagnostics(self):
        v = engine.versions()
        s = dict(self.settings)
        s["proxies"] = "(set)" if s.get("proxies") else ""
        return (
            f"{APP_NAME} {APP_VERSION}{' +quick update ' + str(ota.level()) if ota.level() else ''}\nPython {sys.version.split()[0]} on {sys.platform}\n"
            f"yt-dlp {v['ytdlp']} (updated copy: {v['updated']}) | ejs {v['ejs']} | JS runtime: {engine.js_runtime() or 'NONE'} "
            f"| ffmpeg: {'yes' if engine.have_tool('ffmpeg') else 'NO'} | curl_cffi: {engine.HAVE_IMPERSONATE}\n"
            f"Settings: {s}\n\n" + "\n".join(self.log_lines[-300:])
        )

    def _prewarm(self):
        # loading the extractors up front so the first link they paste isnt slow
        if EXTRAS:
            for host in ("www.youtube.com", "i.ytimg.com", "youtubei.googleapis.com"):
                threading.Thread(target=lambda h=host: self._warm_dns(h), daemon=True, name="dns").start()
        try:
            with self.guard:
                with engine.yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": JobLogger()}) as ydl:
                    for key in ("Youtube", "Generic"):
                        ydl.get_info_extractor(key)
            urls.collection_kind("https://warmup.invalid/x")
        except Exception as e:
            file_log(f"prewarm skipped: {e}")

    @staticmethod
    def _warm_dns(host):
        try:
            socket.getaddrinfo(host, 443)
        except Exception:
            pass

    def _warm_index(self):
        try:
            self.folder_index()
        except Exception as e:
            file_log(f"folder scan at start-up skipped: {e}")

    WARM_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

    def _warm_js(self):
        try:
            if not EXTRAS or not self.settings.get("warm_js", True) or not engine.js_runtime():
                return
            t0 = time.monotonic()
            with self.guard:
                self._extract(self.WARM_URL, dict(self._st(), with_hls=False), noplaylist=True)
            self.log(
                f"Warm-up: YouTube's player was prepared in {time.monotonic() - t0:.1f} s (first fetch of this launch skips this)"
            )
        except Exception as e:
            self.log(f"Warm-up skipped: {norm(e)[:160]}")
        finally:
            self._warm_done.set()

    def _startup_tasks(self):
        self._prewarm()
        if EXTRAS and self.settings.get("warm_js", True) and engine.js_runtime():
            self._warm_done.clear()
            threading.Thread(target=self._warm_js, daemon=True, name="warm-js").start()
        try:
            missing = engine.missing_tools()
            if missing:
                self.log(f"Missing parts: {', '.join(missing)} - fetching them.")
                self.toast("info", "Fetching a missing part. This only happens once.")
                left = engine.repair_tools()
                self.toast(
                    "good" if not left else "warn",
                    "All parts are in place."
                    if not left
                    else f"Couldn't fetch: {', '.join(left)}. Some sites may not work.",
                )
        except Exception as e:
            file_log(f"repair failed: {e}")
        try:
            self._purge_stages()
        except Exception:
            pass
        try:
            cutoff = time.time() - 3 * 86400
            for fp, _ in find_leftovers(self.settings["folder"]):
                if re.search(r"\.part-Frag\d+$", fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
        except Exception:
            pass
        try:
            appupdate.tidy_if_installed()
        except Exception:
            pass
        time.sleep(6)  # dont check for updates right at launch
        while not self.stop.is_set():
            if self.settings["auto_update"] and not engine.NO_UPDATE and self.appu["enabled"]:
                try:
                    v = self.app_update_check()
                    if v["state"] in ("available", "ready"):
                        self.log(f"A newer version of the app exists: {v['version']} (you have {APP_VERSION}).")
                except Exception:
                    pass
            if self.settings["auto_update"] and not engine.NO_UPDATE:
                try:
                    res = engine.update_now()
                    if res["downloaded"]:
                        self.log(
                            f"Downloaded a newer download engine (yt-dlp {res['latest']}); switching on at the next quiet moment."
                        )
                except Exception:
                    pass
            # check again in 6 hours, sleeping in 5s steps so shutdown doesnt have to wait for it
            for _ in range(6 * 3600 // 5):
                if self.stop.is_set():
                    return
                time.sleep(5)

    def shutdown(self):
        self.stop.set()
        with self.lock:
            for j in self.jobs:
                if j.active:
                    j.cancel.set()
        self._save_queue()
        _write_json(CONFIG_PATH, self.settings)
