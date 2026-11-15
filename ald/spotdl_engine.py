import asyncio
import importlib.util
import os
import shutil
import threading

# spotDL keeps global state so only one thing talks to it at a time
_lock = threading.Lock()
_client = None
_client_key = None


def available():
    try:
        return importlib.util.find_spec("spotdl") is not None
    except Exception:
        return False


def enabled(settings):
    return (settings or {}).get("spotify_engine", "auto") != "builtin" and available()


def _creds(settings):
    cid = (settings.get("spotify_client_id") or os.environ.get("SPOTIFY_CLIENT_ID") or "").strip()
    sec = (settings.get("spotify_client_secret") or os.environ.get("SPOTIFY_CLIENT_SECRET") or "").strip()
    if cid and sec:
        return cid, sec
    # no keys set, fall back to the shared ones spotDL ships with
    from spotdl.utils.config import DEFAULT_CONFIG

    return DEFAULT_CONFIG["client_id"], DEFAULT_CONFIG["client_secret"]


def _get_client(settings, proxy):
    global _client, _client_key
    key = (_creds(settings), proxy or "")
    if _client is not None and _client_key == key:
        return _client
    from spotdl import Spotdl

    cid, sec = _creds(settings)
    if _client is not None:
        try:
            # its a singleton, has to be reset or it keeps using the old keys and proxy
            from spotdl.utils.spotify import SpotifyClient

            SpotifyClient._instance = None
        except Exception:
            pass
        _client = None
    dl = {
        "load_config": False,
        "format": "mp3",
        "bitrate": "256k",
        "audio_providers": ["youtube-music", "youtube"],
        "lyrics_providers": [],
        "overwrite": "force",
        "threads": 1,
        "simple_tui": True,
        "print_errors": False,
        "log_level": "CRITICAL",
        "preload": False,
        "scan_for_songs": False,
        "skip_album_art": False,
        "filter_results": True,
        "ffmpeg": shutil.which("ffmpeg") or "ffmpeg",
    }
    if proxy and proxy.startswith("http"):
        dl["proxy"] = proxy
    _client = Spotdl(
        client_id=cid,
        client_secret=sec,
        no_cache=True,
        headless=True,
        downloader_settings=dl,
        loop=asyncio.new_event_loop(),
    )
    _client_key = key
    return _client


def download_track(track_id, out_dir, settings, proxy=None, fmt="mp3"):
    with _lock:
        sd = _get_client(settings, proxy)
        # spotDL is async inside and this runs on a worker thread, so use its own loop
        asyncio.set_event_loop(sd.downloader.loop)
        sd.downloader.settings["format"] = fmt if fmt in ("mp3", "m4a", "flac") else "mp3"
        sd.downloader.settings["output"] = os.path.join(out_dir, "{artists} - {title}.{output-ext}")
        songs = sd.search([f"https://open.spotify.com/track/{track_id}"])
        if not songs:
            raise RuntimeError("spotDL could not read this song from Spotify")
        _song, path = sd.download(songs[0])
    if not path or not os.path.isfile(str(path)):  # no path back means it found no good match
        raise RuntimeError("spotDL found no good match for this song")
    return str(path)


def _song_dict(sg):
    return {
        "id": sg.song_id,
        "title": sg.name,
        "artists": ", ".join(sg.artists or [sg.artist]),
        "duration": int(sg.duration or 0),
        "thumb": sg.cover_url,
        "album": sg.album_name or "",
    }


def fetch_list(kind, sid, settings, proxy=None):
    with _lock:
        _get_client(settings, proxy)
        url = f"https://open.spotify.com/{kind}/{sid}"
        if kind == "track":
            from spotdl.types.song import Song

            sg = Song.from_url(url)
            tr = _song_dict(sg)
            return {
                "kind": kind,
                "id": sid,
                "title": sg.name,
                "owner": tr["artists"],
                "thumb": sg.cover_url,
                "tracks": [tr],
                "partial": False,
            }
        if kind == "playlist":
            from spotdl.types.playlist import Playlist as T
        else:
            from spotdl.types.album import Album as T
        meta, songs = T.get_metadata(url)
    tracks = [_song_dict(x) for x in songs if getattr(x, "song_id", None)]
    return {
        "kind": kind,
        "id": sid,
        "title": meta.get("name") or "Spotify",
        "owner": meta.get("author_name") or meta.get("artist") or "",
        "thumb": meta.get("cover_url") or next((t["thumb"] for t in tracks if t["thumb"]), None),
        "tracks": tracks,
        "partial": False,
    }
