import threading
import time
import uuid

from . import progress
from .core import norm


# the engine only gets swapped when nothing is using yt-dlp, downloads take a normal "with guard" and the
# swap takes exclusive
class _Guard:
    def __init__(self):
        self.cv = threading.Condition()
        self.n = 0
        self.blocked = False

    def idle(self):
        with self.cv:
            return self.n == 0 and not self.blocked

    def __enter__(self):
        with self.cv:
            while self.blocked:
                self.cv.wait(1)
            self.n += 1

    def __exit__(self, *a):
        with self.cv:
            self.n -= 1
            self.cv.notify_all()

    def exclusive(self, timeout=20):
        # ok on the returned thing says if everyone actually finished before the timeout
        guard = self

        class _Ctx:
            ok = False

            def __enter__(self_):
                with guard.cv:
                    guard.blocked = True
                    end = time.monotonic() + timeout
                    while guard.n > 0 and time.monotonic() < end:
                        guard.cv.wait(0.25)
                    self_.ok = guard.n == 0
                return self_

            def __exit__(self_, *a):
                with guard.cv:
                    guard.blocked = False
                    guard.cv.notify_all()

        return _Ctx()


class Cancelled(Exception):
    pass


class JobLogger:
    def __init__(self, write=None, trace=False):
        self.write = write or (lambda m: None)
        self.last_error = None
        self.errors = []
        self.t0 = time.monotonic()
        self.trace = [] if trace else None

    def timeline(self, limit=14):
        out = []
        for i, (t, m) in enumerate(self.trace or []):
            nxt = self.trace[i + 1][0] if i + 1 < len(self.trace) else time.monotonic() - self.t0
            out.append((nxt - t, t, m))
        return "; ".join(f"+{t:.1f}s {m[:60]} ({d:.1f}s)" for d, t, m in sorted(out, reverse=True)[:limit] if d >= 0.8)

    def debug(self, msg):
        if self.trace is not None and len(self.trace) < 400:
            m = norm(msg)
            # only the [extractor] style lines are kept, enough to see where the time went
            if m.startswith("[") and not m.startswith("[debug"):
                self.trace.append((time.monotonic() - self.t0, m))
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        m = norm(msg)
        if "falling back on generic information extractor" in m.lower():
            return  # just noise for us
        self.write(f"! {m}")

    def error(self, msg):
        m = norm(msg)
        self.errors.append(m)
        if "unsupported url" in m.lower():
            return  # not a real failure yet, we still try scanning the page for media links
        self.last_error = m
        self.write(f"x {m}")


# the part of a job the ui gets to see
PUBLIC_JOB_FIELDS = (
    "id",
    "url",
    "title",
    "uploader",
    "thumb",
    "duration",
    "kind",
    "height",
    "sub",
    "index",
    "status",
    "progress",
    "speed",
    "eta",
    "downloaded",
    "total",
    "message",
    "error",
    "path",
    "size",
    "created",
    "finished",
    "vid",
    "attempt",
    "force",
    "container",
    "afmt",
    "expected",
    "exact",
    "phase",
    "took",
    "note",
)


class Job:
    def __init__(self, **kw):
        self.id = uuid.uuid4().hex[:10]
        self.url = ""
        self.title = ""
        self.uploader = ""
        self.album = ""
        self.thumb = None
        self.duration = 0
        self.kind = "video"
        self.height = None
        self.container = "mp4"
        self.afmt = "mp3"
        self.expected = 0
        self.sizes = {}
        self.sub = ""
        self.index = None
        self.vid = None
        self.force = False
        self.status = "queued"
        self.progress = 0.0
        self.speed = 0.0
        self.eta = None
        self.downloaded = 0
        self.total = 0
        self.message = "Waiting in line"
        self.error = None
        self.path = None
        self.size = 0
        self.created = time.time()
        self.finished = None
        self.attempt = ""
        self.exact = False
        self.phase = ""
        self.took = 0
        self.note = ""
        self.plan = None
        self.seen_ids = set()
        self.sure = {}
        self.last_data = 0.0
        self.cap = 0.985  # download progress stops here, the rest is for merging and converting
        self.meter = progress.Meter()
        self.cancel = threading.Event()
        self.seen = set()
        self.streams = {}
        self.dest = ""
        self.stage = None
        self.__dict__.update(kw)

    def to_dict(self):
        d = {k: getattr(self, k) for k in PUBLIC_JOB_FIELDS}
        # no new data for a few seconds, dont show a stale speed
        if self.status == "downloading" and self.last_data and time.monotonic() - self.last_data > progress.Meter.STALL:
            d["speed"], d["eta"], d["message"] = 0.0, None, "Waiting for data"
        return d

    def persistent(self):
        d = self.to_dict()
        # anything that was mid way comes back as queued after a restart
        if d["status"] in ("downloading", "analyzing", "processing", "waiting"):
            d["status"] = "queued"
        return d

    @property
    def active(self):
        return self.status in ("queued", "analyzing", "downloading", "processing", "waiting")
