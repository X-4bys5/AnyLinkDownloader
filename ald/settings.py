import json
import os

from .core import file_log


# defaults and the settings file helpers

VIDEO_FORMATS = ("mp4", "mkv", "webm", "mov", "avi")
AUDIO_FORMATS = ("mp3", "m4a", "opus", "flac", "wav", "ogg")
SPOTIFY_FORMATS = ("mp3", "m4a", "flac")
AUDIO_EXTS = {"mp3": ".mp3", "m4a": ".m4a", "opus": ".opus", "flac": ".flac", "wav": ".wav", "ogg": ".ogg"}
AUDIO_CODEC = {"mp3": "mp3", "m4a": "m4a", "opus": "opus", "flac": "flac", "wav": "wav", "ogg": "vorbis"}

MAX_WORKERS = 4
CONNS = 16

# threads 0 means let ffmpeg pick, nice is the low priority mode for slow pcs
PROFILES = {
    "gentle": {"parallel": 2, "conns": 6, "nice": True, "threads": 2},
    "balanced": {"parallel": 3, "conns": CONNS, "nice": False, "threads": 0},
    "max": {"parallel": 4, "conns": 32, "nice": False, "threads": 0},
}

EXTRAS = True
INFO_TTL = 600  # seconds a fetched video info stays cached
META_KEEP = 400  # how many link previews we remember
MAX_NET_RETRIES = 3
PLAYLIST_CAP = 1500  # playlists and channels get cut off after this many entries
SPEED_TEST_URL = "https://speed.cloudflare.com/__down?bytes={n}"
STAGE_NAME = "Downloading"  # name of the working folder, see staging.py


DEFAULTS = {
    "folder": "",
    "theme": "auto",
    "browser": "none",
    "profile": "balanced",
    "auto_fetch": True,
    "clip_watch": True,
    "auto_update": True,
    "auto_route": True,
    "plain_fetch": True,
    "fast_fetch": True,
    "warm_js": True,
    "skip_m3u8": False,
    "find_4k": False,
    "ipv4": False,
    "proxies": "",
    "container": "mp4",
    "audio_format": "mp3",
    "kind": "video",
    "quality": "best",
    "sound": True,
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "spotify_engine": "auto",
    "first_run_done": False,
}


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    try:
        tmp = path + ".tmp"  # write next to it and swap, so a crash mid write cant leave half a file
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, path)
    except Exception as e:
        file_log(f"could not save {path}: {e}")


def installed_browsers():
    # only checks the profile folders exist, doesnt look inside them
    home = os.path.expanduser("~")
    la, ra = os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")
    spots = {
        "firefox": [
            os.path.join(ra, "Mozilla", "Firefox", "Profiles"),
            os.path.join(home, ".mozilla", "firefox"),
            os.path.join(home, "Library", "Application Support", "Firefox"),
        ],
        "edge": [os.path.join(la, "Microsoft", "Edge", "User Data"), os.path.join(home, ".config", "microsoft-edge")],
        "chrome": [
            os.path.join(la, "Google", "Chrome", "User Data"),
            os.path.join(home, ".config", "google-chrome"),
            os.path.join(home, "Library", "Application Support", "Google", "Chrome"),
        ],
        "brave": [
            os.path.join(la, "BraveSoftware", "Brave-Browser", "User Data"),
            os.path.join(home, ".config", "BraveSoftware", "Brave-Browser"),
        ],
    }
    return [name for name, paths in spots.items() if any(p and os.path.isdir(p) for p in paths)]
