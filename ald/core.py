import collections
import collections.abc
import copy
import os
import re
import sys
import threading
import time
import types


# paths, version and small helpers everything else uses

APP_NAME = "Any Link Downloader"
APP_VERSION = "0.11.8"
APP_REPO = "X-4bys5/AnyLinkDownloader"

FROZEN = bool(getattr(sys, "frozen", False))
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW, stops a console flashing up on windows

MB = 1_048_576
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
ANSI_RE = re.compile(r"\x1b?\[[0-9;]*m")  # yt-dlp messages come with color codes in them


def _data_dir():
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        root = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    # ALD_DATA_DIR is handy for running a second copy without touching your real settings
    return os.environ.get("ALD_DATA_DIR") or os.path.join(root, "AnyLinkDownloader")


DATA_DIR = _data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
LOG_PATH = os.path.join(DATA_DIR, "app.log")
CONFIG_PATH = os.path.join(DATA_DIR, "settings.json")


def resource_path(*parts):
    patch = os.environ.get("ALD_PATCH_ROOT")  # set by ota.py when a quick update is running
    if patch and os.path.exists(os.path.join(patch, *parts)):  # a patch only has ald/, the rest comes from the bundle
        return os.path.join(patch, *parts)
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def safe_clone(obj, _depth=0):
    if _depth > 40:  # bail out instead of recursing forever on a weird nested object
        return obj
    if isinstance(obj, dict):
        return {k: safe_clone(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [safe_clone(v, _depth + 1) for v in obj]
    if isinstance(obj, tuple):
        return tuple(safe_clone(v, _depth + 1) for v in obj)
    if isinstance(obj, (set, frozenset)):
        return type(obj)(safe_clone(v, _depth + 1) for v in obj)
    if isinstance(obj, (types.GeneratorType, collections.abc.Iterator)):
        return [safe_clone(v, _depth + 1) for v in obj]
    if isinstance(obj, (str, bytes, int, float, bool, type(None))) or callable(obj):
        return obj
    try:
        return copy.deepcopy(obj)
    except Exception:
        return obj


def clean(msg):
    return ANSI_RE.sub("", str(msg)).strip()


def norm(msg):
    return re.sub(r"^(ERROR|WARNING):\s*", "", clean(msg))


def version_key(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4])


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


_log_lock = threading.Lock()


def file_log(text):
    try:
        with _log_lock:
            # log is capped, past 1mb we only keep the last 200kb
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
                with open(LOG_PATH, "rb") as f:
                    f.seek(-200_000, os.SEEK_END)
                    tail = f.read()
                with open(LOG_PATH, "wb") as f:
                    f.write(tail)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + text + "\n")
    except Exception:
        pass  # logging should never be the thing that crashes a download


_dns_cache = {}


def safe_name(name, default="video"):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")[:100]  # windows dislikes trailing dots and spaces
    return name or default
