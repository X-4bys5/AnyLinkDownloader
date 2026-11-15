import collections
import math
import re
import subprocess
import time


class Meter:
    # all in seconds. speed is measured over the last WINDOW, the TAU ones are how fast the smoothing reacts
    # (TAU_DOWN is smaller so a slowdown shows up quicker than a speedup)
    WINDOW = 4.0
    MIN_SPAN = 1.5
    TAU_UP = 1.0
    TAU_DOWN = 0.7
    TAU_ETA = 2.0
    STALL = 3.0

    def __init__(self, clock=time.monotonic, min_speed=1024.0):
        self.clock = clock
        self.min_speed = min_speed
        self.reset()

    def reset(self):
        self.samples = collections.deque()
        self.speed = 0.0
        self._eta = None
        self._eta_t = None
        self._last = None
        self._moved = None

    def add(self, done, now=None):
        now = self.clock() if now is None else now
        done = float(done or 0)
        if self.samples and done < self.samples[-1][1]:
            self.reset()  # counter went backwards (new stream or a retry) so start measuring again
        if self._moved is None or not self.samples or done > self.samples[-1][1]:
            self._moved = now
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[1][0] > self.WINDOW:
            self.samples.popleft()
        t0, b0 = self.samples[0]
        span = now - t0
        if span < self.MIN_SPAN:
            return
        inst = max(0.0, (done - b0) / span)
        if self._last is None or not self.speed:
            self.speed = inst
        else:
            tau = self.TAU_DOWN if inst < self.speed else self.TAU_UP
            self.speed += (inst - self.speed) * (1 - math.exp(-max(0.0, now - self._last) / tau))
        self._last = now

    def stalled(self, now=None):
        now = self.clock() if now is None else now
        return self._moved is not None and now - self._moved > self.STALL

    def eta(self, remaining, now=None):
        now = self.clock() if now is None else now
        if remaining is None or remaining <= 0 or self.speed < self.min_speed or self.stalled(now):
            self._eta = None
            return None
        raw = remaining / self.speed
        # eta counts down on its own between updates and only eases towards the new guess so it doesnt jump
        if self._eta is None:
            self._eta = raw
        else:
            dt = max(0.0, now - (self._eta_t if self._eta_t is not None else now))
            counted_down = max(0.0, self._eta - dt)
            self._eta = counted_down + (raw - counted_down) * (1 - math.exp(-dt / self.TAU_ETA))
        self._eta_t = now
        return max(1.0, self._eta)


def audio_convert_seconds(duration, codec):
    if not duration:
        return 0.0
    # rough guess of how many seconds of audio get converted per second
    speed = {"mp3": 35.0, "m4a": 40.0, "opus": 25.0, "vorbis": 20.0, "flac": 90.0, "wav": 400.0}.get(codec, 30.0)
    return max(1.0, duration / speed)


def human_codec(container_or_fmt):
    return str(container_or_fmt or "").upper()


_TIME_RE = re.compile(r"out_time_(?:us|ms)=(\d+)")


def probe(path, ffprobe="ffprobe"):
    import json

    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        j = json.loads(r.stdout or "{}")
    except Exception:
        return None, None, 0.0
    v = next(  # cover art shows up as a video stream too, so attached pics get skipped
        (
            s.get("codec_name")
            for s in j.get("streams", [])
            if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    a = next((s.get("codec_name") for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    try:
        dur = float((j.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return v, a, dur


def run_ffmpeg(cmd, duration, on_progress, cancel=None, clock=time.monotonic):
    # the progress flags have to go before the output file, which is the last item
    full = cmd[:-1] + ["-progress", "pipe:1", "-nostats", "-loglevel", "error"] + cmd[-1:]
    p = subprocess.Popen(
        full,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    t0 = clock()
    errs = []
    import threading

    # stderr gets read in the background, if the pipe fills up ffmpeg just hangs
    def drain():
        for line in p.stderr:
            errs.append(line)

    threading.Thread(target=drain, daemon=True).start()
    meter = Meter(clock, min_speed=0.01)  # speed here is seconds of media per second, not bytes
    try:
        for line in p.stdout:
            if cancel is not None and cancel.is_set():
                p.kill()
                raise RuntimeError("cancelled")
            m = _TIME_RE.match(line.strip())
            if not m or not duration:
                continue
            done = int(m.group(1)) / 1e6
            frac = min(0.999, done / duration)  # 100% only after ffmpeg has really exited
            meter.add(done)
            sp = meter.speed
            eta = meter.eta(max(0.0, duration - done)) if sp else None
            on_progress(frac, (sp if sp else 0.0), eta)
        rc = p.wait()
    finally:
        if p.poll() is None:
            p.kill()
    if rc != 0:
        raise RuntimeError("".join(errs)[-400:] or f"ffmpeg exited with {rc}")
    return clock() - t0
