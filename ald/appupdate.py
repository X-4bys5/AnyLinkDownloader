import hashlib
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .core import APP_REPO, APP_VERSION, DATA_DIR, FROZEN, NO_WINDOW, file_log

# checks github releases for a newer installer and runs it
API = os.environ.get("ALD_UPDATE_API", "https://api.github.com").rstrip("/")  # local server override for dev
# release downloads redirect to the githubusercontent hosts, so they all have to be allowed
GITHUB_HOSTS = {"api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
CHECK_EVERY = 12 * 3600
UPDATES_DIR = os.path.join(DATA_DIR, "updates")
STATE_PATH = os.path.join(DATA_DIR, "app_update.json")


def _allowed_hosts():
    hosts = set(GITHUB_HOSTS)
    if os.environ.get("ALD_UPDATE_API"):
        hosts.add(urllib.parse.urlparse(API).hostname or "")
    return hosts


def enabled(repo=None):
    return bool(re.fullmatch(r"[\w.-]+/[\w.-]+", repo if repo is not None else APP_REPO))


def version_key(v):
    # tags like v1.2.3-beta, the suffix is dropped and only the first 4 numbers count
    nums = re.findall(r"\d+", str(v).split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(latest, current=APP_VERSION):
    a, b = version_key(latest), version_key(current)
    n = max(len(a), len(b))  # padded with zeros so 1.2 and 1.2.0 are equal
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def pick_asset(assets):
    exes = [
        a for a in assets or [] if str(a.get("name", "")).lower().endswith(".exe") and a.get("browser_download_url")
    ]
    # installers first, portable last, then the biggest file
    exes.sort(
        key=lambda a: (
            not re.search(r"setup|install", a["name"], re.I),
            bool(re.search(r"portable", a["name"], re.I)),
            -int(a.get("size") or 0),
        )
    )
    return exes[0] if exes else None


def _read_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(st):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


# redirects have to pass the same host check as the first url
class _Guard(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _vet(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _vet(url):
    u = urllib.parse.urlparse(url)
    fake = bool(os.environ.get("ALD_UPDATE_API")) and u.hostname == urllib.parse.urlparse(API).hostname
    if (u.scheme != "https" and not (fake and u.scheme == "http")) or (u.hostname or "") not in _allowed_hosts():
        raise RuntimeError(f"refusing to fetch an update from {u.hostname or url}")
    return url


def _open(url, headers=None, timeout=30):
    _vet(url)
    h = {"User-Agent": f"AnyLinkDownloader/{APP_VERSION}", "Accept-Encoding": "identity"}
    h.update(headers or {})
    return urllib.request.build_opener(_Guard).open(urllib.request.Request(url, headers=h), timeout=timeout)


def _release_info(rel):
    asset = pick_asset(rel.get("assets"))
    if not asset:
        return None
    sha = ""
    # github puts a digest on the asset, if not we look for a .sha256 file next to it
    m = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", str(asset.get("digest") or ""))
    if m:
        sha = m.group(1).lower()
    else:
        side = next(
            (a for a in rel.get("assets") or [] if a.get("name", "").lower() == asset["name"].lower() + ".sha256"), None
        )
        if side and side.get("browser_download_url"):
            try:
                with _open(side["browser_download_url"], timeout=15) as r:
                    got = re.search(r"\b[0-9a-fA-F]{64}\b", r.read(4096).decode("utf-8", "replace"))
                sha = got.group(0).lower() if got else ""
            except Exception as e:
                file_log(f"app update: checksum file unreadable: {e}")
    return {
        "tag": str(rel.get("tag_name") or ""),
        "version": ".".join(str(n) for n in version_key(rel.get("tag_name"))),
        "name": re.sub(r"[^\w.\- ()]", "_", os.path.basename(asset["name"])),
        "size": int(asset.get("size") or 0),
        "url": asset["browser_download_url"],
        "sha256": sha,
        "page": rel.get("html_url") or "",
        "notes": (rel.get("body") or "").strip()[:600],  # release notes are only shown short
    }


def check(force=False, repo=None):
    repo = repo if repo is not None else APP_REPO
    out = {"enabled": enabled(repo), "current": APP_VERSION, "latest": APP_VERSION, "newer": False, "release": None}
    if not out["enabled"]:
        return out
    st = _read_state()
    if st.get("repo") != repo:
        st = {}
    now = time.time()
    if force or now - st.get("checked", 0) >= CHECK_EVERY:
        headers = {"Accept": "application/vnd.github+json"}
        if st.get("etag"):
            headers["If-None-Match"] = st["etag"]
        try:
            with _open(f"{API}/repos/{repo}/releases/latest", headers, timeout=15) as r:
                rel = json.loads(r.read().decode("utf-8"))
                st = {"repo": repo, "checked": now, "etag": r.headers.get("ETag"), "release": _release_info(rel)}
        except urllib.error.HTTPError as e:
            if e.code == 304:
                st["checked"] = now
            elif e.code == 404:  # no release published yet
                st = {"repo": repo, "checked": now, "release": None}
            else:
                raise
        _write_state(st)
    rel = st.get("release")
    if rel:
        out.update(latest=rel["version"], newer=is_newer(rel["version"]), release=rel)
    return out


def _path(rel):
    return os.path.join(UPDATES_DIR, re.sub(r"[^\w.\-]", "_", rel["tag"] or rel["version"]), rel["name"])


def _sha256(path, cancel=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            if cancel is not None and cancel.is_set():
                raise InterruptedError("cancelled")
    return h.hexdigest()


def have_file(rel):
    p = _path(rel)
    try:
        if not os.path.isfile(p):
            return None
        if rel["size"] and os.path.getsize(p) != rel["size"]:
            return None
        key = f"{os.path.getsize(p)}:{os.stat(p).st_mtime_ns}"  # so we dont hash the same file every time
        st = _read_state()
        if st.get("verified", {}).get(p) == key:
            return p
        if rel["sha256"] and _sha256(p) != rel["sha256"]:
            return None
        st.setdefault("verified", {})[p] = key
        _write_state(st)
        return p
    except OSError:
        return None


def download(rel, progress=None, cancel=None):
    have = have_file(rel)
    if have:
        return have, False
    dest = _path(rel)
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for stale in (dest,):
        try:
            os.remove(stale)
        except OSError:
            pass
    have_bytes = os.path.getsize(part) if os.path.isfile(part) else 0
    if rel["size"] and have_bytes > rel["size"]:
        os.remove(part)
        have_bytes = 0
    # resumes the .part file, if the server ignores the range (not a 206) we start over
    headers = {"Range": f"bytes={have_bytes}-"} if have_bytes else {}
    h = hashlib.sha256()
    try:
        with _open(rel["url"], headers, timeout=30) as r:
            if have_bytes and r.status == 206:
                with open(part, "rb") as f:
                    for b in iter(lambda: f.read(1 << 20), b""):
                        h.update(b)
                mode = "ab"
            else:
                have_bytes, mode = 0, "wb"
            done = have_bytes
            with open(part, mode) as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("cancelled")
                    b = r.read(1 << 17)
                    if not b:
                        break
                    out.write(b)
                    h.update(b)
                    done += len(b)
                    if progress and rel["size"]:
                        progress(min(1.0, done / rel["size"]))
        size = os.path.getsize(part)
        if rel["size"] and size != rel["size"]:
            raise RuntimeError(f"the download is incomplete ({size} of {rel['size']} bytes)")
        if rel["sha256"] and h.hexdigest() != rel["sha256"]:
            os.remove(part)
            raise RuntimeError("the download is corrupted (checksum does not match), so it was thrown away")
        os.replace(part, dest)
    except InterruptedError:
        raise
    st = _read_state()
    st.setdefault("verified", {})[dest] = f"{os.path.getsize(dest)}:{os.stat(dest).st_mtime_ns}"
    _write_state(st)
    tidy(keep=os.path.dirname(dest))
    return dest, True


def tidy(keep=None):
    try:
        for name in os.listdir(UPDATES_DIR):
            p = os.path.join(UPDATES_DIR, name)
            if keep and os.path.abspath(p) == os.path.abspath(keep):
                continue
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def tidy_if_installed():
    try:
        for name in os.listdir(UPDATES_DIR):
            if not is_newer(name):  # folder is named after the version, not newer means already installed
                shutil.rmtree(os.path.join(UPDATES_DIR, name), ignore_errors=True)
    except OSError:
        pass


def can_install():
    return os.name == "nt" and FROZEN


def installer_args(exe_dir=None):
    d = ntpath.normcase(ntpath.abspath(exe_dir or os.path.dirname(sys.executable)))
    pf = [ntpath.normcase(os.environ.get(k, "")) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    machine = any(p and d.startswith(p) for p in pf)
    # inno setup flags, machine wide only when the app lives under program files
    return [
        "/SILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        "/ALLUSERS" if machine else "/CURRENTUSER",
    ]


def install(path):
    if not can_install():
        raise RuntimeError("the installer update is for the installed Windows app; this copy is running from source")
    if not os.path.isfile(path):
        raise RuntimeError("the installer is missing")
    flags = 0x00000008 | 0x00000200 | NO_WINDOW  # detached + new process group so it survives us closing
    subprocess.Popen([path] + installer_args(), close_fds=True, creationflags=flags, cwd=os.path.dirname(path))
    file_log(f"app update: started {path}")
