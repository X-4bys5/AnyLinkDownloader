import html as htmllib
import re
import urllib.parse
import urllib.request

from .core import UA


# reads media links out of normal web pages

_EXT = r"(?:m3u8|mpd|mp4|webm|mov|m4v|mkv|flv|mp3|m4a|aac|ogg|opus|flac|wav)"

# so .mp4 inside poster.mp4.jpg or .mp4x doesnt count
_NOT_IMG = r"(?![A-Za-z0-9]|\.(?:jpe?g|png|gif|webp|svg))"

_ABS_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>\\()\[\]{}]+?\.""" + _EXT + _NOT_IMG + r"""(?:\?[^\s"'<>\\()\[\]{}]*)?""",
    re.I,
)

_REL_RE = re.compile(
    r"""["']((?:\.{0,2}/)?[^\s"'<>\\]+?\.""" + _EXT + _NOT_IMG + r"""(?:\?[^\s"'<>\\]*)?)["']""",
    re.I,
)

_META_MEDIA_RE = re.compile(
    r"""<meta[^>]+?(?:property|name)\s*=\s*["'](?:og:(?:video|audio)(?::url|:secure_url)?|twitter:player:stream)["'][^>]*?content\s*=\s*["']([^"']+)["']""",
    re.I,
)

_TAG_SRC_RE = re.compile(r"""<(?:video|audio|source)\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)

_IFRAME_RE = re.compile(r"""<iframe[^>]+?src\s*=\s*["']([^"']+)["']""", re.I)

# urls with any of these in them are almost never the real video
_JUNK = ("thumb", "sprite", "poster", "preview", "/ads/", "adserver", "doubleclick", "tracking", "pixel")


def fetch_page(url, referer=None, proxy=None):
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    # urllib cant do socks so only http(s) proxies get used
    handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy and proxy.startswith("http") else None
    )
    opener = urllib.request.build_opener(*([handler] if handler else []))
    with opener.open(req, timeout=15) as r:
        raw = r.read(4_000_000)  # first 4mb is plenty, we only look for links
        charset = r.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def find_media_urls(page_url, text):
    # links inside scripts and json are often escaped like https:\/\/site\/a.mp4
    text = (
        text.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u0026", "&")
        .replace("&amp;", "&")
    )
    scheme = urllib.parse.urlparse(page_url).scheme or "https"
    found = []
    for m in _ABS_RE.finditer(text):
        u = m.group(0)
        found.append(f"{scheme}:{u}" if u.startswith("//") else u)
    for m in _REL_RE.finditer(text):
        found.append(urllib.parse.urljoin(page_url, m.group(1)))
    tagged = []
    for rx in (_META_MEDIA_RE, _TAG_SRC_RE):
        for m in rx.finditer(text):
            u = urllib.parse.urljoin(page_url, m.group(1).strip())
            if u.lower().startswith(("http://", "https://")) and not re.search(
                r"\.(?:jpe?g|png|gif|webp|svg|html?)(?:\?|$)", u, re.I
            ):
                tagged.append(u)
    found = tagged + found  # meta and video/source tags go first, they point at the actual media
    seen, out = set(), []
    for u in found:
        u = u.rstrip(".,;")
        if u in seen or any(j in u.lower() for j in _JUNK):
            continue
        seen.add(u)
        out.append(u)

    def rank(u):
        path = urllib.parse.urlparse(u).path.lower()
        # m3u8 and mpd first, they are usually the real stream
        return 0 if path.endswith(".m3u8") else 1 if path.endswith(".mpd") else 2

    return sorted(out, key=rank)[:8]  # only keep the best few


# links ending in these get turned away before we even ask yt-dlp
_NON_MEDIA_EXT = {
    "pdf",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "txt",
    "csv",
    "rtf",
    "epub",
    "zip",
    "rar",
    "7z",
    "tar",
    "gz",
    "iso",
    "dmg",
    "exe",
    "msi",
    "apk",
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "bmp",
    "svg",
    "ico",
    "css",
    "js",
    "json",
    "xml",
}


def is_non_media_url(url):
    try:
        path = urllib.parse.urlparse(url).path.lower()
    except Exception:
        return False
    return "." in path.rsplit("/", 1)[-1] and path.rsplit(".", 1)[-1] in _NON_MEDIA_EXT


def page_title(text):
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    return re.sub(r"\s+", " ", htmllib.unescape(m.group(1))).strip() if m else ""
