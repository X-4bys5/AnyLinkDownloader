import re


# link classification, video vs playlist/channel

_YT_RE = re.compile(r"(?:^|//|\.)(?:youtube\.com|youtu\.be|youtube-nocookie\.com)(?:[/:?#]|$)", re.I)


def is_youtube(url):
    return bool(_YT_RE.search(str(url)))


# checked against the extractor name after the colon, like Site:playlist
_COLLECTION_RE = re.compile(
    r"(tab|playlist|user|channel|search|profile|feed|favou?rites|hashtag|collection|album|series|season"
    r"|show|sets?|likes|uploads|videos|category|tag|community|group|list)",
    re.I,
)


def collection_kind(url):
    try:
        if is_youtube(url) and re.search(r"[?&]v=[\w-]{6,}|youtu\.be/", url):
            return None
        # imported here on purpose, the engine can swap yt_dlp for a newer copy while the app is running
        from yt_dlp.extractor import gen_extractor_classes

        for ie in gen_extractor_classes():
            if ie.IE_NAME == "generic":  # nothing specific matched, treat it as a single video
                return None
            if ie.suitable(url):
                parts = ie.IE_NAME.split(":")[1:]
                return ie.IE_NAME if any(_COLLECTION_RE.search(x) for x in parts) else None
    except Exception:
        pass
    return None


_YT_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{11})(?![\w-])")

_YT_CHANNEL_RE = re.compile(
    r"^https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)/?$", re.I
)


def classify_url(url):
    if is_youtube(url):
        if re.search(r"[?&]v=[\w-]{6,}|youtu\.be/|/shorts/|/live/|/embed/", url):
            return "video"
        if re.search(r"[?&]list=|/playlist|/@|/channel/|/c/|/user/|/results\?", url):
            return "playlist"
        return "video"
    return "playlist" if collection_kind(url) else "video"


def youtube_list_id(url):
    m = re.search(r"[?&]list=([\w-]+)", url or "")
    # skip the auto mixes (RD) and the liked / watch later lists
    if m and not m.group(1).startswith(("RD", "LL", "WL")) and is_youtube(url):
        return m.group(1)
    return None


def normalize_url(text):
    t = (text or "").strip().strip("<>\"'")  # links pasted from chats come wrapped in <> or quotes
    m = re.search(r"https?://\S+", t)
    if m:
        return m.group(0).rstrip(".,;)")
    if re.fullmatch(r"(www\.)?[\w.-]+\.[a-z]{2,}(/\S*)?", t, re.I):
        return "https://" + t
    return None


def thumb_of(info):
    t = info.get("thumbnail")
    if not t:
        ths = [x for x in (info.get("thumbnails") or []) if x.get("url")]
        # a mid sized one is enough, no need for the huge ones
        mids = [x for x in ths if 200 <= (x.get("width") or 0) <= 700]
        t = (mids[-1] if mids else ths[-1])["url"] if ths else None
    if not t and info.get("id") and is_youtube(info.get("url") or info.get("webpage_url") or ""):
        t = f"https://i.ytimg.com/vi/{info['id']}/mqdefault.jpg"
    return t
