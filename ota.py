# quick updates for the python and ui side of the app, no installer needed.
# a patch is a signed zip of the ald/ folder. it gets unpacked into the data folder and the app
# uses it instead of the bundled copy from the next start on. big stuff (new yt-dlp bundle, ffmpeg,
# the rust window, new python packages) still goes out as a normal installer release.
# this file stays outside ald/ on purpose, a broken patch can never break the thing that fetches the fix.
import base64
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import json
import os
import re
import shutil
import sys
import zipfile

# paste the public key that `python tools/release.py keygen` prints, empty keeps quick updates off
PUBKEY = "vj8RbRUQkcvPjZGrmXUAk5p03oeW07olMQtJslHINaY="

TAG = "ota"  # github release the patches get uploaded to (a pre-release, so it is never "latest")
MANIFEST = "ald-patch.json"
MAX_JSON = 64 * 1024
MAX_ZIP = 40 * 1024 * 1024
MAX_UNPACKED = 150 * 1024 * 1024
ALLOWED_EXT = {".py", ".html", ".css", ".js", ".woff2", ".png", ".svg", ".ico", ".json", ".txt"}  # no exe, dll or pyd
BOOT_TRIES = 2  # starts a patch gets to finish booting before it is dropped


def canonical(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify(manifest, sig_b64, pubkey=None):
    pub = PUBKEY if pubkey is None else pubkey
    if not pub:
        return False
    try:
        from Cryptodome.Signature import eddsa

        key = eddsa.import_public_key(base64.b64decode(pub))
        eddsa.new(key, "rfc8032").verify(canonical(manifest), base64.b64decode(sig_b64))
        return True
    except Exception:
        return False


def allowed():
    if os.environ.get("ALD_NO_PATCH") or "--no-patch" in sys.argv:  # escape hatch if a patch ever misbehaves
        return False
    # from source the files are already whatever is in the folder, ALD_OTA_FORCE=1 turns it on anyway if you want to try it
    return bool(getattr(sys, "frozen", False)) or bool(os.environ.get("ALD_OTA_FORCE"))


def level():
    try:
        return int(os.environ.get("ALD_PATCH_LEVEL") or 0)
    except ValueError:
        return 0


def _core():
    from ald import core  # whichever copy is loaded right now

    return core


def _root():
    return os.path.join(_core().DATA_DIR, "patch")


def _load():
    try:
        with open(os.path.join(_root(), "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(st):
    os.makedirs(_root(), exist_ok=True)
    p = os.path.join(_root(), "state.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(p + ".tmp", p)


def _log(text):
    try:
        _core().file_log("quick update: " + text)
    except Exception:
        pass


def _drop(st, n, why):
    st["bad"] = (st.get("bad") or [])[-19:] + [n]
    st["active"], st["boots"] = None, 0
    try:
        _save(st)
    except OSError:
        pass
    _log(f"patch {n} dropped, back on the installed files ({why})")


# serves ald and everything under it from the patch folder, ahead of the bundled copy
class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root):
        self.root = root

    def find_spec(self, name, path=None, target=None):
        if name != "ald" and not name.startswith("ald."):
            return None
        return importlib.machinery.PathFinder.find_spec(name, [self.root] if name == "ald" else path)


def _purge():
    for name in [m for m in sys.modules if m == "ald" or m.startswith("ald.")]:
        del sys.modules[name]
    importlib.invalidate_caches()


# has to run before anything from ald is imported, video_downloader.py calls it first thing
def activate():
    if not allowed():
        return 0
    try:
        base = _core().APP_VERSION  # the bundled one, no patch is on the path yet
        st = _load()
    except Exception:
        return 0
    n = st.get("active")
    folder = os.path.join(_root(), str(n)) if n else ""
    if not n or st.get("base") != base or not os.path.isfile(os.path.join(folder, "ald", "core.py")):
        return 0
    if st.get("boots", 0) >= BOOT_TRIES:  # it started twice and never got to mark_good
        _purge()
        _drop(st, n, "it kept failing to start")
        return 0
    st["boots"] = st.get("boots", 0) + 1
    _save(st)
    _purge()
    finder = _Finder(folder)
    sys.meta_path.insert(0, finder)
    os.environ["ALD_PATCH_ROOT"], os.environ["ALD_PATCH_LEVEL"] = folder, str(n)
    try:
        for name in ("ald.core", "ald.engine", "ald.winutil"):  # what video_downloader imports first
            importlib.import_module(name)
    except Exception as e:
        sys.meta_path.remove(finder)
        os.environ.pop("ALD_PATCH_ROOT", None)
        os.environ.pop("ALD_PATCH_LEVEL", None)
        _purge()
        _drop(st, n, f"{type(e).__name__}: {e}")
        return 0
    return n


# the app is up, so the patch is fine
def mark_good():
    try:
        st = _load()
        if st.get("boots"):
            st["boots"] = 0
            _save(st)
    except Exception:
        pass


def enabled():
    try:
        repo = _core().APP_REPO
    except Exception:
        return False
    return bool(PUBKEY) and allowed() and bool(re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""))


# a patch that is downloaded but not running yet, 0 if there is none
def pending():
    try:
        st = _load()
        n = st.get("active")
        return n if n and n != level() and st.get("base") == _core().APP_VERSION else 0
    except Exception:
        return 0


def built():
    try:
        st = _load()
        return st.get("built", "") if st.get("active") else ""
    except Exception:
        return ""


def _get(url, limit):
    from ald import appupdate  # same host checks as the installer updates

    with appupdate._open(url, timeout=20) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("the update file is bigger than it should be")
    return data


def unpack(zip_path, dest, base):
    total = 0
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if not infos or len(infos) > 3000:
            raise RuntimeError("the patch has a strange number of files")
        for i in infos:
            parts = i.filename.split("/")
            ext = os.path.splitext(i.filename)[1].lower()
            if parts[0] != "ald" or ".." in parts or "\\" in i.filename or ":" in i.filename or "" in parts:
                raise RuntimeError(f"unexpected path in the patch: {i.filename}")
            if ext not in ALLOWED_EXT:
                raise RuntimeError(f"unexpected file type in the patch: {i.filename}")
            total += i.file_size
            if total > MAX_UNPACKED:
                raise RuntimeError("the patch unpacks to far more than it should")
            target = os.path.join(dest, *parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(i) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    core_py = os.path.join(dest, "ald", "core.py")
    if not (os.path.isfile(core_py) and os.path.isfile(os.path.join(dest, "ald", "__init__.py"))):
        raise RuntimeError("the patch is missing core files")
    for d, _, files in os.walk(dest):
        for name in files:
            if name.endswith(".py"):
                p = os.path.join(d, name)
                with open(p, "rb") as f:
                    compile(f.read(), p, "exec")  # a syntax error here means we never even stage it
    with open(core_py, encoding="utf-8") as f:
        m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m or m.group(1) != base:
        raise RuntimeError("the patch was built for another version of the app")


def _tidy(keep):
    root = _root()
    try:
        for name in os.listdir(root):
            if name == "state.json" or name in keep:
                continue
            p = os.path.join(root, name)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
    except OSError:
        pass


def update():
    core = _core()
    out = {"enabled": enabled(), "state": "off", "error": ""}
    if not out["enabled"]:
        return out
    out["state"] = "uptodate"
    base = os.environ.get("ALD_OTA_URL") or f"https://github.com/{core.APP_REPO}/releases/download/{TAG}"
    raw = json.loads(_get(f"{base}/{MANIFEST}", MAX_JSON).decode("utf-8"))
    man = raw.get("manifest") if isinstance(raw, dict) else None
    if not isinstance(man, dict) or not verify(man, str(raw.get("sig") or "")):
        raise RuntimeError("the patch isnt signed properly, ignoring it")
    n = int(man["patch"])
    st = _load()
    if st.get("base") != core.APP_VERSION:
        st = {"bad": st.get("bad") or []}  # left over from an older installer
    have = max(level(), st.get("active") or 0)
    if man.get("base") != core.APP_VERSION or n <= have or n in (st.get("bad") or []):
        _tidy({str(level()), str(st.get("active") or "")})
        return out
    name, size = str(man.get("zip") or ""), int(man.get("size") or 0)
    if not re.fullmatch(r"[\w.-]+\.zip", name) or not 0 < size <= MAX_ZIP:
        raise RuntimeError("the patch description doesnt look right")
    root = _root()
    os.makedirs(root, exist_ok=True)
    part = os.path.join(root, f"{n}.zip.part")
    h, got = hashlib.sha256(), 0
    from ald import appupdate

    with appupdate._open(f"{base}/{name}", timeout=30) as r, open(part, "wb") as f:
        while True:
            b = r.read(1 << 17)
            if not b:
                break
            got += len(b)
            if got > size:
                raise RuntimeError("the patch is bigger than announced")
            h.update(b)
            f.write(b)
    if got != size or h.hexdigest() != str(man.get("sha256") or "").lower():
        os.remove(part)
        raise RuntimeError("the patch download is corrupted, thrown away")
    tmp, final = os.path.join(root, f"{n}.tmp"), os.path.join(root, str(n))
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        unpack(part, tmp, core.APP_VERSION)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        os.remove(part)
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    _save(
        {
            "base": core.APP_VERSION,
            "active": n,
            "boots": 0,
            "bad": st.get("bad") or [],
            "built": str(man.get("built") or ""),
            "notes": str(man.get("notes") or "")[:400],
        }
    )
    _tidy({str(level()), str(n)})
    _log(f"patch {n} downloaded ({got // 1024} KB), switches on at the next start")
    out["state"] = "ready"
    return out
