import contextlib

from . import engine
from .settings import VIDEO_FORMATS


# format choices and size estimates

# preferred codec order when guessing sizes, av1 first
_VCODEC_RANK = ("av01", "vp9.2", "vp9", "vp09", "hev", "h265", "avc", "h264", "vp8")


# guessed bitrates for converted audio, wav and flac get worked out from the sample rate instead
_AUDIO_KBPS = {"mp3": 245, "m4a": 256, "ogg": 192, "opus": 96, "flac": 900, "wav": 1411}


def _fbytes(f, dur):
    # real size if yt-dlp knows it, otherwise bitrate times duration
    n = f.get("filesize") or f.get("filesize_approx")
    if n:
        return int(n)
    br = f.get("tbr") or ((f.get("vbr") or 0) + (f.get("abr") or 0)) or f.get("abr") or f.get("vbr")
    return int(br * 1000 / 8 * dur) if br and dur else 0


def _vrank(f):
    c = str(f.get("vcodec") or "").lower()
    return next((i for i, k in enumerate(_VCODEC_RANK) if c.startswith(k)), len(_VCODEC_RANK))


def video_format_choice(container, height, have_ffmpeg=True):
    h = f"[height<={height}]" if height else ""
    if not have_ffmpeg:  # cant merge without ffmpeg, so only single files that already have both
        return f"best{h}/best", None, None
    fmt = f"bestvideo{h}+bestaudio/best{h}"
    if container == "webm":
        return fmt, ["res", "fps", "ext:webm:webm"], "webm"
    if container in ("mp4", "mov"):
        return fmt, ["res", "fps", "ext:mp4:m4a"], container
    return fmt, None, "mkv"


def audio_format_choice(afmt):
    pref = {"m4a": "bestaudio[ext=m4a]/", "opus": "bestaudio[acodec=opus]/"}.get(afmt, "")
    return pref + "bestaudio/best"


# runs yt-dlp's own format picker on info we already have, so the size matches what it would really download
class _Picker:
    def __init__(self):
        self.y = None

    def __enter__(self):
        yt = engine.yt_dlp
        if yt is not None:
            try:
                self.y = yt.YoutubeDL(
                    {
                        "quiet": True,
                        "no_warnings": True,
                        "skip_download": True,
                        "simulate": True,
                        "listformats_table": False,
                        "color": {"stdout": "never", "stderr": "never"},
                    }
                )
            except Exception:
                self.y = None
        return self

    def __exit__(self, *a):
        try:
            if self.y is not None:
                self.y.close()
        except Exception:
            pass

    def pick(self, info, fmt, sort=None):
        if self.y is None or not info or info.get("entries") is not None or not info.get("formats"):
            return None
        try:
            self.y.params["format"] = fmt
            self.y.format_selector = self.y.build_format_selector(fmt)
            if sort:
                self.y.params["format_sort"] = sort
            else:
                self.y.params.pop("format_sort", None)
            i = {
                k: v
                for k, v in info.items()
                if k not in ("requested_formats", "requested_downloads", "requested_subtitles", "__post_extractor")
            }
            i["formats"] = [dict(x) for x in info["formats"]]
            # fake urls and fields just so the picking works, nothing gets fetched from them
            for x in i["formats"]:
                if not str(x.get("url") or "").lower().startswith(("http://", "https://")):
                    x["url"] = "http://x/" + str(x.get("format_id"))
            for k, v in (
                ("id", "x"),
                ("title", "x"),
                ("extractor", "generic"),
                ("extractor_key", "Generic"),
                ("webpage_url", "http://x/"),
                ("_type", "video"),
            ):
                i.setdefault(k, v)
            r = self.y.process_ie_result(i, download=False)
            chosen = r.get("requested_formats") or [r]
            return [dict(c) for c in chosen if c.get("format_id") is not None] or None
        except Exception:
            return None


def format_bytes(f, dur=0):
    return _fbytes(f, dur)


def chosen_total(chosen, dur=0):
    if not chosen:
        return 0
    sizes = [format_bytes(f, dur) for f in chosen]
    return sum(sizes) if all(sizes) else 0


def size_estimates(info, container="mp4", memo=None, pk=None):
    guess = _size_guess(info, container)
    dur = info.get("duration") or 0
    heights = sorted(
        {f["height"] for f in (info.get("formats") or []) if f.get("height") and f.get("vcodec") not in (None, "none")},
        reverse=True,
    )
    if not heights:
        return guess
    have_ff = engine.have_tool("ffmpeg")
    out = {}
    memo = {} if memo is None else memo
    with contextlib.nullcontext(pk) if pk is not None else _Picker() as pick:
        for key in ["best"] + heights:
            fmt, sort, _ = video_format_choice(container, None if key == "best" else key, have_ff)
            mk = (fmt, tuple(sort or ()))  # same format string gives the same answer, dont pick twice
            if mk not in memo:
                memo[mk] = chosen_total(pick.pick(info, fmt, sort), dur)
            out[str(key)] = memo[mk] or guess.get(str(key), 0)
    return {k: v for k, v in out.items() if v}


def compute_sizes(info, container, first_only=False):
    memo = {}
    with _Picker() as pk:
        which = [container] if first_only else list(VIDEO_FORMATS)
        sizes_by = {c: (size_estimates(info, c, memo, pk) if c != "avi" else {}) for c in which}
    return {"sizes": sizes_by.get(container) or {}, "sizes_by": sizes_by, "audio_sizes": audio_size_estimates(info)}


# fallback for when the picker cant give a size
def _size_guess(info, container="mp4"):
    dur = info.get("duration") or 0
    fm = [f for f in (info.get("formats") or []) if f.get("url") or f.get("format_id")]
    isv = lambda f: f.get("vcodec") not in (None, "none")  # noqa: E731
    isa = lambda f: f.get("acodec") not in (None, "none")  # noqa: E731
    vext, aext = {"mp4": ("mp4", "m4a"), "mov": ("mp4", "m4a"), "webm": ("webm", "webm")}.get(container, (None, None))
    auds = [f for f in fm if isa(f) and not isv(f) and _fbytes(f, dur)]
    auds.sort(key=lambda f: (f.get("ext") == aext if aext else False, f.get("abr") or 0), reverse=True)
    audio = _fbytes(auds[0], dur) if auds else 0
    by_h = {}
    for f in fm:
        h = f.get("height")
        if h and isv(f) and _fbytes(f, dur):
            by_h.setdefault(h, []).append(f)
    out = {}
    for h, cands in by_h.items():
        cands.sort(key=lambda f: (f.get("ext") == vext if vext else False, -_vrank(f), _fbytes(f, dur)), reverse=True)
        f = cands[0]
        out[h] = _fbytes(f, dur) + (0 if isa(f) else audio)
    if out:
        out["best"] = out[max(k for k in out if k != "best")]
    return {str(k): v for k, v in out.items()}


def audio_size_estimates(info):
    dur = info.get("duration") or 0
    out = {}
    with _Picker() as pk:
        best = (pk.pick(info, "bestaudio/best") or [None])[0] or {}
        asr = int(best.get("asr") or 44100)
        ch = int(best.get("audio_channels") or 2)
        for fmt, kbps in _AUDIO_KBPS.items():
            copy = None
            if fmt in ("m4a", "opus"):
                c = (pk.pick(info, audio_format_choice(fmt)) or [None])[0]
                if c and (
                    (fmt == "m4a" and c.get("ext") == "m4a")
                    or (fmt == "opus" and str(c.get("acodec") or "").startswith("opus"))
                ):
                    copy = _fbytes(c, dur)
            if copy:  # source is already that codec so it gets copied, the real size is fine
                out[fmt] = copy
            elif fmt == "wav":
                out[fmt] = int(dur * asr * ch * 2 + 44) if dur else 0  # 16 bit pcm plus the wav header
            elif fmt == "flac":
                out[fmt] = int(dur * asr * ch * 2 * 0.6) if dur else 0  # flac is about 60% of wav, a guess
            else:
                out[fmt] = int(kbps * 1000 / 8 * dur) if dur else 0
    return out


def fmt_duration(sec):
    if not sec:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def quality_label(h):
    return f"{h}p"
