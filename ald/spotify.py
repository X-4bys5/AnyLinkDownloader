import base64
import json
import os
import re
import urllib.parse
import urllib.request

from .core import UA
from .pages import fetch_page


# reads spotify links and works out the songs in them, the audio itself comes from youtube
KINDS = ("track", "album", "playlist")
# open/play/embed hosts, an optional intl-xx part and user/<name>/ all show up in links people paste
_URL_RE = re.compile(
    r"^(?:https?://)?(?:open|play|embed)\.spotify\.com/(?:embed/)?(?:intl-[a-z]{2,3}(?:-[a-z]+)?/)?"
    r"(?:(?:user/[^/]+/)?(track|album|playlist|artist|show|episode))/([A-Za-z0-9]{10,30})",
    re.I,
)
_URI_RE = re.compile(r"^spotify:(?:user:[^:]+:)?(track|album|playlist|artist|show|episode):([A-Za-z0-9]{10,30})$", re.I)
_SHORT_RE = re.compile(r"^(?:https?://)?(?:spotify\.link|spotify\.app\.link)/\S+", re.I)


class SpotifyError(Exception):
    def __init__(self, title, text):
        super().__init__(f"{title}: {text}")
        self.title, self.text = title, text


def is_spotify(url):
    u = (url or "").strip()
    return bool(_URL_RE.match(u) or _URI_RE.match(u) or _SHORT_RE.match(u))


def parse(url):
    u = (url or "").strip()
    m = _URL_RE.match(u) or _URI_RE.match(u)
    return (m.group(1).lower(), m.group(2)) if m else None


def resolve_short(url, proxy=None):
    if not url.lower().startswith("http"):
        url = "https://" + url
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy and proxy.startswith("http") else None
    )
    opener = urllib.request.build_opener(*([handler] if handler else []))
    with opener.open(req, timeout=15) as r:
        final = r.geturl()
        if parse(final):
            return final
        # short link didnt end on a normal url so dig the real link out of the page html
        body = r.read(400_000).decode("utf-8", "replace")
    m = re.search(r"https://open\.spotify\.com/(?:intl-[a-z-]+/)?(?:track|album|playlist)/[A-Za-z0-9]{10,30}", body)
    return m.group(0) if m else final


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            yield n
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)


_IMG_HOSTS = ("scdn.co", "spotifycdn.com")


def _best_image(entity):
    srcs = []
    for d in _walk(entity):
        for key in ("sources", "images", "image", "coverArt", "cover_art", "thumbnails"):
            v = d.get(key)
            if isinstance(v, list):
                srcs += [s for s in v if isinstance(s, dict) and s.get("url")]
            elif isinstance(v, dict) and v.get("url"):
                srcs.append(v)
    if srcs:
        mids = [s for s in srcs if 200 <= (s.get("width") or s.get("maxWidth") or 0) <= 700]
        return (mids[0] if mids else srcs[0])["url"]
    found = []
    for d in _walk(entity):
        for v in d.values():
            if (
                isinstance(v, str)
                and v.startswith("http")
                and any(h in v.split("/")[2] for h in _IMG_HOSTS if len(v.split("/")) > 2)
            ):
                found.append(v)
    return found[0] if found else None


def _artists(d):
    a = d.get("artists")
    if isinstance(a, list) and a:
        names = [x.get("name") if isinstance(x, dict) else str(x) for x in a]
        return ", ".join(n for n in names if n)
    return (d.get("subtitle") or d.get("artist") or "").replace("\u00a0", " ").strip()


def _ms(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0
    # some sources give milliseconds and some seconds, anything over 1000 counts as ms
    return int(round(v / 1000.0)) if v > 1000 else int(v)


def _track_from(d, fallback_thumb=None):
    uri = d.get("uri") or ""
    tid = uri.rsplit(":", 1)[-1] if uri.startswith("spotify:track:") else (d.get("id") or "")
    title = (d.get("title") or d.get("name") or "").strip()
    if not title or not re.fullmatch(r"[A-Za-z0-9]{10,30}", tid or ""):
        return None
    return {
        "id": tid,
        "title": title,
        "artists": _artists(d),
        "duration": _ms(d.get("duration") or d.get("duration_ms")),
        "thumb": _best_image(d) or fallback_thumb,
        "album": ((d.get("album") or {}).get("name") if isinstance(d.get("album"), dict) else "") or "",
    }


def parse_embed(html, kind, sid):
    # the embed page is a next.js page, everything we need sits in this script tag
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise SpotifyError(
            "Couldn't read that Spotify link", "Spotify didn't send the song list. Try again in a minute."
        )
    data = json.loads(m.group(1))
    entity = None
    for d in _walk(data):
        if isinstance(d.get("entity"), dict) and (
            d["entity"].get("trackList") is not None or d["entity"].get("name") or d["entity"].get("title")
        ):
            entity = d["entity"]
            break
    if entity is None:
        raise SpotifyError(
            "Couldn't read that Spotify link", "It may be private, deleted, or not a song, album or playlist link."
        )
    cover = _best_image(entity)
    title = (entity.get("name") or entity.get("title") or "Spotify").strip()
    owner = entity.get("subtitle") if kind != "track" else _artists(entity)
    tracks = []
    tl = entity.get("trackList")
    if isinstance(tl, list):
        for t in tl:
            if isinstance(t, dict):
                tr = _track_from(t, cover)
                if tr:
                    tracks.append(tr)
    elif kind == "track":
        tr = _track_from({**entity, "uri": f"spotify:track:{sid}"}, cover)
        if tr:
            tracks.append(tr)
    return {
        "kind": kind,
        "id": sid,
        "title": title,
        "owner": (owner or "").strip(),
        "thumb": cover,
        "tracks": tracks,
        "partial": False,
    }


def parse_meta(html, kind, sid):
    import html as H

    def tag(name):
        m = re.search(
            r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']*)["\']' % re.escape(name), html, re.I
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']%s["\']' % re.escape(name), html, re.I
        )
        return H.unescape(m.group(1)).strip() if m else ""

    title, desc, img = tag("og:title"), tag("og:description"), tag("og:image")
    if kind != "track" or not title:
        raise SpotifyError(
            "Couldn't read that Spotify link", "Spotify didn't send the song list. Try again in a minute."
        )
    # the description mixes the artist with the year and a "song" label, filtered out below
    parts = [x.strip() for x in re.split(r"\s[\u00b7\u2022|-]\s", desc) if x.strip()]
    artists = next(
        (x for x in parts if x.lower() != title.lower() and not re.fullmatch(r"\d{4}", x) and "song" not in x.lower()),
        "",
    )
    tr = {"id": sid, "title": title, "artists": artists, "duration": 0, "thumb": img or None, "album": ""}
    return {
        "kind": kind,
        "id": sid,
        "title": title,
        "owner": artists,
        "thumb": img or None,
        "tracks": [tr],
        "partial": False,
    }


def _api_creds(settings=None):
    s = settings or {}
    cid = s.get("spotify_client_id") or os.environ.get("SPOTIFY_CLIENT_ID") or ""
    sec = s.get("spotify_client_secret") or os.environ.get("SPOTIFY_CLIENT_SECRET") or ""
    return (cid.strip(), sec.strip()) if cid and sec else None


def _http_json(url, headers=None, data=None, proxy=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy and proxy.startswith("http") else None
    )
    opener = urllib.request.build_opener(*([handler] if handler else []))
    with opener.open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_api(kind, sid, creds, proxy=None, http=_http_json, cap=1500):
    # client credentials flow, no login needed but private playlists still wont work
    tok = http(
        "https://accounts.spotify.com/api/token",
        data=b"grant_type=client_credentials",
        proxy=proxy,
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )["access_token"]
    auth = {"Authorization": f"Bearer {tok}"}
    base = "https://api.spotify.com/v1"

    def conv(t, album=None):
        if not t or not t.get("id"):
            return None
        al = album or t.get("album") or {}
        imgs = al.get("images") or []
        return {
            "id": t["id"],
            "title": t.get("name") or "",
            "artists": ", ".join(a.get("name", "") for a in t.get("artists", [])),
            "duration": _ms(t.get("duration_ms")),
            "thumb": (imgs[len(imgs) // 2]["url"] if imgs else None),
            "album": al.get("name") or "",
        }

    if kind == "track":
        t = http(f"{base}/tracks/{sid}", headers=auth, proxy=proxy)
        tr = conv(t)
        return {
            "kind": kind,
            "id": sid,
            "title": t.get("name") or "Spotify",
            "owner": tr["artists"] if tr else "",
            "thumb": tr and tr["thumb"],
            "tracks": [tr] if tr else [],
            "partial": False,
        }
    head = http(
        f"{base}/{kind}s/{sid}?fields=name,owner(display_name),artists(name),images"
        if kind == "playlist"
        else f"{base}/albums/{sid}",
        headers=auth,
        proxy=proxy,
    )
    imgs = head.get("images") or []
    # api page limits, 100 for playlist tracks and 50 for album tracks
    tracks, url = (
        [],
        (f"{base}/playlists/{sid}/tracks?limit=100" if kind == "playlist" else f"{base}/albums/{sid}/tracks?limit=50"),
    )
    while url and len(tracks) < cap:
        page = http(url, headers=auth, proxy=proxy)
        for it in page.get("items", []):
            t = it.get("track") if kind == "playlist" else it
            tr = conv(t, album=head if kind == "album" else None)
            if tr:
                tracks.append(tr)
        url = page.get("next")
    owner = (head.get("owner") or {}).get("display_name") or ", ".join(
        a.get("name", "") for a in head.get("artists", [])
    )
    return {
        "kind": kind,
        "id": sid,
        "title": head.get("name") or "Spotify",
        "owner": owner,
        "thumb": imgs[len(imgs) // 2]["url"] if imgs else None,
        "tracks": tracks,
        "partial": False,
    }


def oembed_thumb(kind, sid, proxy=None, http=None):
    try:
        d = (http or _http_json)(
            "https://open.spotify.com/oembed?url="
            + urllib.parse.quote(f"https://open.spotify.com/{kind}/{sid}", safe=""),
            proxy=proxy,
        )
        u = d.get("thumbnail_url") or ""
        return u if u.startswith("http") else None
    except Exception:
        return None


def fetch(url, proxy=None, settings=None):
    u = url.strip()
    if _SHORT_RE.match(u):
        u = resolve_short(u, proxy)
    p = parse(u)
    if not p:
        raise SpotifyError(
            "That Spotify link isn't one I can read", "Paste a song, album or playlist link from Spotify."
        )
    kind, sid = p
    if kind not in KINDS:
        raise SpotifyError(
            "Spotify artist and podcast pages can't be used",
            "Open the album or playlist you want and paste that link instead.",
        )
    creds = _api_creds(settings)
    err = None
    result = None
    try:
        result = parse_embed(fetch_page(f"https://open.spotify.com/embed/{kind}/{sid}", proxy=proxy), kind, sid)
    except SpotifyError as e:
        err = e
    except Exception as e:
        err = SpotifyError("Couldn't reach Spotify", "Check your internet and try again.")
        err.__cause__ = e
    # embed only seems to give the first 100 playlist tracks (thats why partial is set below)
    if creds and (result is None or (kind == "playlist" and len(result["tracks"]) >= 100)):
        try:
            return fetch_api(kind, sid, creds, proxy)
        except Exception:
            pass
    if result is None or not result["tracks"]:
        # embed gave nothing, try spotDL next and the meta tags after that
        try:
            from . import spotdl_engine

            if spotdl_engine.available():
                alt = spotdl_engine.fetch_list(kind, sid, settings or {}, proxy)
                if alt["tracks"]:
                    alt["partial"] = False
                    return alt
        except Exception:
            pass
        if kind == "track":
            try:
                result = parse_meta(fetch_page(f"https://open.spotify.com/embed/track/{sid}", proxy=proxy), kind, sid)
            except Exception:
                pass
    if result is None:
        raise err
    if not result["tracks"]:
        raise SpotifyError("No songs found", "That link has no songs I can see. Private playlists can't be read.")
    result["partial"] = kind == "playlist" and len(result["tracks"]) >= 100
    if not result.get("thumb"):
        result["thumb"] = oembed_thumb(kind, sid, proxy)
    if kind in ("track", "album"):
        for t in result["tracks"]:
            t["thumb"] = t.get("thumb") or result.get("thumb")
    return result


# words that mean its probably not the studio version, penalised below unless the song title has them too
_BAD = re.compile(
    r"\b(live|cover|karaoke|reaction|sped up|speed up|slowed|reverb|nightcore|8d|instrumental|remix|tutorial|lesson|"
    r"piano version|acoustic|lyrics video|full album|compilation|mashup)\b",
    re.I,
)


def _tokens(s):
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", (s or "").lower())
    return {w for w in re.findall(r"[\w']+", s) if len(w) > 1}


def search_queries(title, artists):
    # youtube music songs tab first, plain youtube search second, only the first artist is used
    first = (artists or "").split(",")[0].strip()
    q = f"{first} {title}".strip()
    return [
        f"https://music.youtube.com/search?q={urllib.parse.quote_plus(q)}#songs",
        f"ytsearch6:{first} - {title} audio".strip(),
    ]


def pick_match(cands, title, artists, duration):
    want_t, want_a = _tokens(title), _tokens(artists)
    best, best_score = None, -1e9
    for e in cands or []:
        if not e or not (e.get("id") or e.get("url")):
            continue
        et = e.get("title") or ""
        blob = _tokens(et + " " + (e.get("uploader") or e.get("channel") or "") + " " + (e.get("artist") or ""))
        score = 0.0
        if want_t:
            score += 4.0 * len(want_t & blob) / len(want_t)
        if want_a:
            score += 2.0 * len(want_a & blob) / len(want_a)
        d = e.get("duration")
        if duration and d:
            diff = abs(d - duration)
            # length is the best signal, a few seconds off is the same song, 30+ is a different version
            score += 5.0 if diff <= 3 else 3.5 if diff <= 8 else 1.5 if diff <= 15 else -4.0 if diff > 30 else -1.0
        if _BAD.search(et) and not _BAD.search(title or ""):
            score -= 3.0
        if best is None or score > best_score:
            best, best_score = e, score
    if best is None:
        return None
    # nothing shares a word with the title and the score is low, better to fail than grab the wrong song
    if (
        want_t
        and not (want_t & _tokens((best.get("title") or "") + " " + (best.get("uploader") or "")))
        and best_score < 3
    ):
        return None
    return best


# cover urls have a size code after the image id, 1e02 is 300px, 4851 is 64px and b273 is the 640px one
_SIZE_RE = re.compile(r"(ab67616d0000)(?:1e02|4851)", re.I)


def upgrade_cover(url):
    return _SIZE_RE.sub(r"\1b273", url or "")


def _get_image(url, proxy):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy and proxy.startswith("http") else None
    )
    with urllib.request.build_opener(*([handler] if handler else [])).open(req, timeout=15) as r:
        data = r.read(8_000_000)
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg" if data[:3] == b"\xff\xd8\xff" else None
    return (data, mime) if data and mime else (None, None)


def _fetch_cover(cover_url, proxy):
    if not (cover_url and cover_url.startswith("http")):
        return None, None
    big = upgrade_cover(cover_url)
    for u in [big, cover_url] if big != cover_url else [cover_url]:
        try:
            data, mime = _get_image(u, proxy)
            if data:
                return data, mime
        except Exception:
            continue
    return None, None


def embed_tags(path, title, artists, album="", cover_url=None, proxy=None, track_no=None):
    ext = os.path.splitext(path)[1].lower()
    data, mime = _fetch_cover(cover_url, proxy)
    if ext == ".m4a":
        from mutagen.mp4 import MP4, MP4Cover

        f = MP4(path)
        if title:
            f["\xa9nam"] = [title]
        if artists:
            f["\xa9ART"] = [artists]
        if album:
            f["\xa9alb"] = [album]
        if track_no:
            f["trkn"] = [(int(track_no), 0)]
        if data:
            f["covr"] = [
                MP4Cover(data, imageformat=MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG)
            ]
        f.save()
        return
    if ext == ".flac":
        from mutagen.flac import FLAC, Picture

        f = FLAC(path)
        for k, v in (
            ("title", title),
            ("artist", artists),
            ("album", album),
            ("tracknumber", str(track_no) if track_no else ""),
        ):
            if v:
                f[k] = [v]
        if data:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, mime, "Cover", data
            f.clear_pictures()
            f.add_picture(pic)
        f.save()
        return
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK, ID3NoHeaderError

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    if title:
        tags.setall("TIT2", [TIT2(encoding=3, text=title)])
    if artists:
        tags.setall("TPE1", [TPE1(encoding=3, text=artists)])
    if album:
        tags.setall("TALB", [TALB(encoding=3, text=album)])
    if track_no:
        tags.setall("TRCK", [TRCK(encoding=3, text=str(track_no))])
    if data:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    tags.save(path, v2_version=3)  # id3v2.3, windows explorer and older players dont read 2.4 properly
