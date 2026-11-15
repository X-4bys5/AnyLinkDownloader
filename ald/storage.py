import os
import re
import time
import uuid

from .core import DATA_DIR


# what yt-dlp leaves behind when a download gets killed, plus our own .ald-moving
_LEFTOVER_RE = re.compile(r"(\.part-Frag\d+|\.part|\.ytdl|\.turbo|\.ald-moving)$", re.I)


def find_leftovers(root, max_depth=3):
    out = []
    root = os.path.abspath(root)
    base = root.count(os.sep)
    for dirpath, dirs, files in os.walk(root):
        if dirpath.count(os.sep) - base >= max_depth:
            dirs[:] = []  # dont go deeper, the save folder can be huge
        for f in files:
            if _LEFTOVER_RE.search(f):
                fp = os.path.join(dirpath, f)
                try:
                    out.append((fp, os.path.getsize(fp)))
                except OSError:
                    pass
    return out


MEDIA_EXT = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".m4v",
    ".avi",
    ".flv",
    ".ts",
    ".mp3",
    ".m4a",
    ".opus",
    ".ogg",
    ".wav",
    ".aac",
    ".flac",
    ".mpg",
    ".mpeg",
    ".3gp",
}

AUDIO_EXT = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac"}

# yt-dlp puts the video id in square brackets right before the extension, this is how we spot saved videos
ID_RE = re.compile(r"\[([^\[\]]{1,200})\]\.[A-Za-z0-9]{2,5}$")


def default_folder():
    home = os.path.expanduser("~")
    for cand in (
        os.path.join(home, "Videos", "Any Link"),
        os.path.join(home, "Downloads", "Any Link"),
        os.path.join(home, "Any Link"),
    ):
        parent = os.path.dirname(cand)
        if os.path.isdir(parent):
            return cand
    return os.path.join(home, "Any Link")


_WRITABLE_CACHE = {}


def writable_folder(path):
    # cached for 30s so we dont create a test file every time this gets called
    hit = _WRITABLE_CACHE.get(path)
    if hit and time.monotonic() - hit[0] < 30 and os.path.isdir(hit[1][0]):
        return hit[1]
    res = _probe_writable(path)
    if res[0] and os.path.isdir(res[0]):
        if len(_WRITABLE_CACHE) > 16:
            _WRITABLE_CACHE.clear()
        _WRITABLE_CACHE[path] = (time.monotonic(), res)
    return res


def _probe_writable(path):
    candidates = [
        path,
        default_folder(),
        os.path.join(os.path.expanduser("~"), "Documents", "Any Link"),
        os.path.join(os.path.expanduser("~"), "Any Link"),
        os.path.join(DATA_DIR, "Downloads"),
    ]
    for i, c in enumerate(x for x in candidates if x):
        try:
            os.makedirs(c, exist_ok=True)
            probe = os.path.join(c, f".write_test_{uuid.uuid4().hex[:8]}")
            with open(probe, "w"):
                pass
            os.remove(probe)
            return c, i > 0  # second value says we had to fall back to a different folder
        except Exception:
            continue
    return path, False
