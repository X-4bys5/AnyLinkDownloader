import threading
import time
import traceback

from .. import engine
from ..core import file_log, norm, safe_name, version_key
from ..jobs import Job
from ..settings import AUDIO_FORMATS, SPOTIFY_FORMATS, VIDEO_FORMATS
from ..storage import writable_folder


class QueueMixin:
    def enqueue(self, payload):
        items = payload.get("items") or []
        if not items:
            return {"added": 0}
        kind = "audio" if payload.get("kind") == "audio" else "video"
        if any(str(it.get("id") or "").startswith("sp-") for it in items):
            kind = "audio"
        height = payload.get("height")
        height = int(height) if str(height or "").isdigit() else None
        pl = payload.get("playlist") or {}
        sub = safe_name(pl.get("title") or "", "Playlist") if pl else ""
        force = bool(payload.get("force"))
        container = str(payload.get("container") or self.settings["container"]).lower()
        container = container if container in VIDEO_FORMATS else "mp4"
        afmt = str(payload.get("audio_format") or self.settings.get("audio_format") or "mp3").lower()
        allowed = SPOTIFY_FORMATS if any(str(it.get("id") or "").startswith("sp-") for it in items) else AUDIO_FORMATS
        afmt = afmt if afmt in allowed else "mp3"
        folder, moved = writable_folder(self.settings["folder"])
        if folder != self.settings["folder"]:
            self.save_settings({"folder": folder})
            self.toast("warn", f"Couldn't write to the old folder - saving to {folder} instead.")
        added = skipped = 0
        with self.lock:
            # anything already queued or running is skipped, matched by url and by video id
            live = {(j.url, j.kind) for j in self.jobs if j.active} | {
                (j.vid, j.kind) for j in self.jobs if j.active and j.vid
            }
            for it in items:
                url = it.get("url")
                if not url or (url, kind) in live or (it.get("id") and (it.get("id"), kind) in live):
                    skipped += 1
                    continue
                self.jobs.append(
                    Job(
                        url=url,
                        title=it.get("title") or url,
                        uploader=it.get("uploader") or "",
                        thumb=it.get("thumb"),
                        album=it.get("album") or "",
                        duration=it.get("duration") or 0,
                        vid=it.get("id"),
                        kind=kind,
                        height=height,
                        container=container,
                        afmt=afmt,
                        sub=sub,
                        index=(it.get("i") if pl else None),
                        force=force or bool(it.get("force")),
                    )
                )
                added += 1
            self.paused = False  # adding something un-pauses, paused is just the resume prompt at start
        self._save_queue()
        self.touch()
        self.wake.set()
        return {"added": added, "skipped": skipped}

    def _find(self, jid):
        with self.lock:
            return next((j for j in self.jobs if j.id == jid), None)

    def cancel(self, jid=None):
        with self.lock:
            targets = [j for j in self.jobs if j.active and (jid is None or j.id == jid)]
        for j in targets:
            j.cancel.set()
            if j.status == "queued":
                j.status, j.message, j.finished = "cancelled", "Cancelled", time.time()
            else:
                j.message, j.speed, j.eta = "Stopping", 0.0, None
        self._save_queue()
        self.touch()

    def retry(self, jid):
        j = self._find(jid)
        if j and not j.active:
            j.cancel = threading.Event()
            j.status, j.message, j.error, j.progress, j.speed, j.eta = "queued", "Waiting in line", None, 0.0, 0.0, None
            j.streams, j.seen, j.finished, j.force = {}, set(), None, True
            self.paused = False
            self._save_queue()
            self.touch()
            self.wake.set()

    def remove(self, jid=None):
        with self.lock:
            gone = [j for j in self.jobs if not j.active and (jid is None or j.id == jid)]
            self.jobs = [j for j in self.jobs if j.active or (jid is not None and j.id != jid)]
        for j in gone:
            self._stage_drop(j)
        self.touch()

    def resume_queue(self, go=True):
        with self.lock:
            if not go:
                for j in self.jobs:
                    if j.active:
                        j.status, j.message, j.finished = "cancelled", "Dropped", time.time()
            self.paused = False
            self.resumable = 0
        self._save_queue()
        self.touch()
        self.wake.set()

    def _set(self, job, **kw):
        for k, v in kw.items():
            setattr(job, k, v)
        self.touch()

    def _next_job(self):
        with self.lock:
            if self.paused:
                return None
            # the profile decides how many run at once, not the number of worker threads
            running = sum(1 for j in self.jobs if j.status in ("analyzing", "downloading", "processing"))
            if running >= self.prof["parallel"]:
                return None
            job = next((j for j in self.jobs if j.status == "queued"), None)
            if job is not None:
                job.status = "analyzing"  # set under the lock so two workers cant take the same job
            return job

    def _worker(self):
        while not self.stop.is_set():
            job = self._next_job()
            if job is None:
                self._idle_tasks()
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            try:
                self._run(job)
            except Exception as e:
                file_log("WORKER ERROR\n" + traceback.format_exc())
                self._fail(
                    job, "Something unexpected happened", "The app logged the details and moves on.", "retry", str(e)
                )
            finally:
                self._stage_finish(job)
                self._save_queue()
                self.touch()

    def _idle_tasks(self):
        # only swaps the engine between jobs, a swap in the middle of a download breaks it
        now = time.monotonic()
        if now < self._swap_after:
            return
        newer = engine._engine_dir_version()
        if not (newer and engine.yt_dlp and version_key(newer) > version_key(engine.yt_dlp.version.__version__)):
            return
        if not self.guard.idle():
            self._swap_after = now + 5
            return
        if self._swap_engine() is None:
            self._swap_after = now + 30

    def _swap_engine(self):
        with self.guard.exclusive(3) as g:
            if g.ok:
                v = engine.reload()
                if v:
                    self.log(f"Download engine refreshed to yt-dlp {v}.")
                    self.touch()
                    return v
        return None

    def _heal_engine(self):
        try:
            res = engine.update_now(force=True)
            self.log(f"Engine check: latest yt-dlp is {res['latest']} (downloaded: {res['downloaded']}).")
            if res["downloaded"]:
                for _ in range(3):
                    if self._swap_engine():
                        break
        except Exception as e:
            self.log(f"Engine refresh failed (offline?): {norm(e)}")
