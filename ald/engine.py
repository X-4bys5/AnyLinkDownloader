import collections
import concurrent.futures
import gzip
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
import zipfile

from .core import APP_VERSION, DATA_DIR, FROZEN, MB, file_log, resource_path, version_key

# loads yt-dlp, patches its downloaders and handles updating the engine and the helper tools
ENGINE_DIR = os.path.join(DATA_DIR, "engine")
TOOLS_DIR = os.path.join(DATA_DIR, "bin")
STATE_PATH = os.path.join(DATA_DIR, "engine_state.json")
CHECK_EVERY = 12 * 3600  # dont ask pypi more than twice a day
NO_UPDATE = bool(os.environ.get("ALD_NO_UPDATE"))

yt_dlp = None
HAVE_IMPERSONATE = False
_lock = threading.RLock()
_patched = {"turbo": False, "pool": False, "rename": False, "jsc": False}
YTDLP_CACHE = os.path.join(DATA_DIR, "ytdlp_cache")


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        pass


def build_info():
    return _read_json(resource_path("build_info.json"), {})


def bootstrap_path():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    exe_dir = os.path.dirname(sys.executable) if FROZEN else here
    # our own bin folders go before PATH so the bundled ffmpeg and deno win
    wanted = [
        resource_path("bin"),
        os.path.join(exe_dir, "bin"),
        os.path.join(here, "vendor", "bin"),
        TOOLS_DIR,
    ]
    if os.name == "nt":
        wanted += [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"),
            os.path.join(os.path.expanduser("~"), ".deno", "bin"),
        ]
    front, seen = [], set()
    for d in [d for d in wanted if d and os.path.isdir(d)] + [
        p for p in os.environ.get("PATH", "").split(os.pathsep) if p
    ]:
        k = os.path.normcase(os.path.abspath(d))
        if k not in seen:
            seen.add(k)
            front.append(d)
    new_path = os.pathsep.join(front)
    if new_path != os.environ.get("PATH", ""):
        os.environ["PATH"] = new_path


def _engine_dir_version():
    try:
        with open(os.path.join(ENGINE_DIR, "yt_dlp", "version.py"), encoding="utf-8") as f:
            m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", f.read())
        return m.group(1) if m else None
    except Exception:
        return None


def _purge_modules():
    for m in [m for m in sys.modules if m.split(".")[0] in ("yt_dlp", "yt_dlp_ejs")]:
        del sys.modules[m]
    importlib.invalidate_caches()


def _use_engine_dir(on):
    sys.path[:] = [p for p in sys.path if p != ENGINE_DIR]
    if on:
        sys.path.insert(0, ENGINE_DIR)


def active_engine():
    return bool(yt_dlp) and os.path.abspath(os.path.dirname(os.path.dirname(yt_dlp.__file__))) == os.path.abspath(
        ENGINE_DIR
    )


def load():
    global yt_dlp
    bootstrap_path()
    with _lock:
        eng = _engine_dir_version()
        bundled = build_info().get("ytdlp")
        if eng and bundled and version_key(eng) <= version_key(bundled):
            # downloaded engine isnt newer than the bundled one so its just leftover
            shutil.rmtree(ENGINE_DIR, ignore_errors=True)
            eng = None
        if eng and (FROZEN or not _importable_without_engine()):
            _use_engine_dir(True)
        try:
            yt_dlp = _import()
        except Exception:
            if not eng:
                raise
            file_log("downloaded engine failed to import - using the built-in one")
            _use_engine_dir(False)
            _purge_modules()
            shutil.rmtree(ENGINE_DIR, ignore_errors=True)
            yt_dlp = _import()
        apply_patches()
        return yt_dlp


def _importable_without_engine():
    return importlib.util.find_spec("yt_dlp") is not None and not FROZEN


def _import():
    import yt_dlp as y  # noqa: F401
    import yt_dlp.version  # noqa: F401

    return y


def reload(only_if_newer=True):
    global yt_dlp
    with _lock:
        eng = _engine_dir_version()
        if not eng or (only_if_newer and yt_dlp and version_key(eng) <= version_key(yt_dlp.version.__version__)):
            return None
        old = yt_dlp.version.__version__ if yt_dlp else None
        try:
            _use_engine_dir(True)
            _purge_modules()
            yt_dlp = _import()
            _patched.update(turbo=False, pool=False)
            apply_patches()
            check_engine_works()
            file_log(f"engine switched {old} -> {yt_dlp.version.__version__}")
            return yt_dlp.version.__version__
        except Exception as e:
            file_log(f"new engine rejected ({e}); going back to {old}")
            _use_engine_dir(False)
            _purge_modules()
            shutil.rmtree(ENGINE_DIR, ignore_errors=True)
            yt_dlp = _import()
            _patched.update(turbo=False, pool=False)
            apply_patches()
            return None


def check_engine_works():
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        ie = next(i for i in ydl._ies.values() if i.suitable("https://www.youtube.com/watch?v=aqz-KE-bpKQ"))
        assert ie.IE_NAME.startswith("youtube")
    return True


def versions():
    ejs = None
    try:
        spec = importlib.util.find_spec("yt_dlp_ejs")
        if spec and spec.origin:
            with open(os.path.join(os.path.dirname(spec.origin), "_version.py"), encoding="utf-8") as f:
                m = re.search(r"['\"]([0-9][^'\"]*)['\"]", f.read())
            ejs = m.group(1) if m else "present"
    except Exception:
        ejs = "present" if importlib.util.find_spec("yt_dlp_ejs") else None
    return {"ytdlp": yt_dlp.version.__version__ if yt_dlp else None, "ejs": ejs, "updated": active_engine()}


_pool_sizes = threading.local()


def _stream_workers(conns, streams, audio_only):
    if streams > 1 and audio_only:
        return max(2, conns // 4)
    return conns


def _share_connections(fm):
    # video and audio download together and each makes its own pool, this splits the connection limit between them
    orig = fm.FragmentFD.download_and_append_fragments_multiple
    if getattr(orig, "_ald", False):
        return

    def multi(self, *args, **kw):
        sizes = None
        try:
            conns = int(self.params.get("concurrent_fragment_downloads") or 1)
            if len(args) > 1 and conns > 1:
                sizes = collections.deque(
                    _stream_workers(conns, len(args), str((a[2] or {}).get("vcodec") or "") == "none") for a in args
                )
        except Exception:
            sizes = None
        _pool_sizes.q = sizes
        try:
            return orig(self, *args, **kw)
        finally:
            _pool_sizes.q = None

    multi._ald = True
    fm.FragmentFD.download_and_append_fragments_multiple = multi


_RANGE_RE = re.compile(r"([?&]range=)(\d+)-(\d+)")


def _resplit_ranges(fragments, pool):
    if not isinstance(fragments, (list, tuple)) and type(fragments).__name__ != "LazyList":
        return None
    if pool < 2 or not 2 <= len(fragments) <= 3000:
        return None
    # only touch plain back to back ranges of one url, anything else is left alone
    frags = list(fragments)
    if not all(isinstance(f, dict) and set(f) <= {"url"} and isinstance(f.get("url"), str) for f in frags):
        return None
    spans = []
    for f in frags:
        m = _RANGE_RE.search(f["url"])
        if not m or len(_RANGE_RE.findall(f["url"])) != 1:
            return None
        spans.append((int(m.group(2)), int(m.group(3))))
    base = [_RANGE_RE.sub(r"\1", f["url"]) for f in frags]
    if len(set(base)) != 1 or any(spans[i + 1][0] != spans[i][1] + 1 for i in range(len(spans) - 1)):
        return None
    first, last = spans[0][0], spans[-1][1]
    total = last - first + 1
    piece = min(10 * MB, max(2 * MB, -(-total // (pool * 2))))  # -(-a // b) is ceil, pieces 2 to 10mb
    n = -(-total // piece)
    if n <= len(frags):
        return None
    piece = -(-total // n)
    out = []
    for i in range(n):
        a = first + i * piece
        b = last if i == n - 1 else a + piece - 1
        out.append({"url": _RANGE_RE.sub(lambda m, a=a, b=b: f"{m.group(1)}{a}-{b}", frags[0]["url"])})
    return out


# a few huge ranges leave most connections idle, so cut them into more pieces
def _split_dash_pieces(dm):
    orig = dm.DashSegmentsFD.real_download
    if getattr(orig, "_ald", False):
        return

    def real_download(self, filename, info_dict):
        try:
            conns = int(self.params.get("concurrent_fragment_downloads") or 1)
            fmts = info_dict.get("requested_formats") or [info_dict]
            if not info_dict.get("is_live") and not self.params.get("test"):
                for f in fmts:
                    new = _resplit_ranges(
                        f.get("fragments"), _stream_workers(conns, len(fmts), str(f.get("vcodec") or "") == "none")
                    )
                    if new:
                        f["fragments"] = new
        except Exception:
            pass
        return orig(self, filename, info_dict)

    real_download._ald = True
    dm.DashSegmentsFD.real_download = real_download


class _BoundedPool(concurrent.futures.ThreadPoolExecutor):
    def __init__(self, max_workers=None, *a, **k):
        sizes = getattr(_pool_sizes, "q", None)
        if sizes:
            max_workers = sizes.popleft()
        super().__init__(max_workers, *a, **k)

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        # stock map queues every fragment up front, this keeps only a small window in flight
        window = max(4, int(self._max_workers * 1.5))
        pending = collections.deque()
        try:
            for args in zip(*iterables):
                pending.append(self.submit(fn, *args))
                if len(pending) >= window:
                    yield pending.popleft().result(timeout)
            while pending:
                yield pending.popleft().result(timeout)
        finally:
            for f in pending:
                f.cancel()


def _sparse(f):
    if not sys.platform.startswith("win"):  # windows only, other filesystems do this by themselves
        return
    try:
        import ctypes
        import msvcrt

        ret = ctypes.c_ulong(0)
        ctypes.windll.kernel32.DeviceIoControl(
            msvcrt.get_osfhandle(f.fileno()), 0x900C4, None, 0, None, 0, ctypes.byref(ret), None
        )
    except Exception:
        pass


# antivirus or the indexer grabs the file right after ffmpeg writes it, then the rename fails with
# PermissionError on windows. so we just keep retrying for a while
class _RetryOS:
    MAX_WAIT = 90  # seconds
    on_wait = None

    def __getattr__(self, name):
        return getattr(os, name)

    def _retry(self, fn, src, dst):
        delay, deadline, warned = 0.2, time.monotonic() + self.MAX_WAIT, False
        while True:
            try:
                return fn(src, dst)
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                if not warned and self.on_wait:
                    warned = True
                    self.on_wait()
                time.sleep(delay)
                delay = min(delay * 1.5, 2.0)

    def replace(self, src, dst):
        return self._retry(os.replace, src, dst)

    def rename(self, src, dst):
        return self._retry(os.rename, src, dst)


def apply_patches():
    global HAVE_IMPERSONATE
    ytd = yt_dlp
    # every patch is optional, if yt-dlp changes its internals we just go without it
    try:
        import yt_dlp.downloader.fragment as fm

        if not getattr(fm, "_vd_bounded", False):
            futures = types.SimpleNamespace(**vars(fm.concurrent.futures))
            futures.ThreadPoolExecutor = _BoundedPool
            fm.concurrent = types.SimpleNamespace(futures=futures)
            fm._vd_bounded = True
        _share_connections(fm)
        import yt_dlp.downloader.dash as dashm

        _split_dash_pieces(dashm)
        _patched["pool"] = True
    except Exception:
        _patched["pool"] = False
    try:
        import yt_dlp.downloader as dm

        dm.PROTOCOL_MAP["http"] = dm.PROTOCOL_MAP["https"] = make_turbo(ytd)
        _patched["turbo"] = True
    except Exception as e:
        file_log(f"turbo downloader unavailable: {e}")
        _patched["turbo"] = False
    try:
        import yt_dlp.postprocessor.ffmpeg as ff

        if not isinstance(ff.os, _RetryOS):
            ff.os = _RetryOS()
        _patched["rename"] = True
    except Exception:
        _patched["rename"] = False
    try:
        import yt_dlp.extractor.youtube.jsc._builtin.ejs as _ejs

        _ejs.EJSBaseJCP._ENABLE_PREPROCESSED_PLAYER_CACHE = True
        _patched["jsc"] = True
    except Exception:
        _patched["jsc"] = False
    prune_player_cache()
    # impersonation needs curl_cffi installed on top of yt-dlp supporting it
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget  # noqa: F401

        HAVE_IMPERSONATE = importlib.util.find_spec("curl_cffi") is not None
    except Exception:
        HAVE_IMPERSONATE = False


def prune_player_cache(keep=3, root=None):
    try:
        d = os.path.join(root or YTDLP_CACHE, "challenge-solver")
        files = [os.path.join(d, n) for n in os.listdir(d) if n.startswith("player")]
        files.sort(key=os.path.getmtime, reverse=True)
        for f in files[keep:]:
            try:
                os.remove(f)
            except OSError:
                pass
        return len(files[keep:])
    except OSError:
        return 0


def impersonate_target():
    from yt_dlp.networking.impersonate import ImpersonateTarget

    return ImpersonateTarget()


def set_rename_wait_callback(fn):
    try:
        import yt_dlp.postprocessor.ffmpeg as ff

        if isinstance(ff.os, _RetryOS):
            ff.os.on_wait = fn
    except Exception:
        pass


def make_turbo(ytd):
    from yt_dlp.downloader.http import HttpFD as _HttpFD
    from yt_dlp.networking import Request as _Request
    from yt_dlp.utils.networking import HTTPHeaderDict as _HeaderDict

    yt_dlp = ytd  # noqa: F841  (used by the class below)

    class TurboHttpFD(_HttpFD):
        MIN_SIZE = 8 * MB  # below this the extra requests arent worth it

        @staticmethod
        def _turbo_name(filename):
            return filename + ".turbo"

        def _drop_part(self, filename):
            try:
                os.remove(self._turbo_name(filename))
            except OSError:
                pass

        def real_download(self, filename, info_dict):
            try:
                done = self._turbo(filename, info_dict)
            except yt_dlp.utils.DownloadCancelled:
                self._drop_part(filename)
                raise
            except Exception as e:
                self._drop_part(filename)
                self.report_warning(f"Multi-connection download failed ({str(e)[:80]}); using one connection.")
                done = None
            return super().real_download(filename, info_dict) if done is None else done

        def _turbo(self, filename, info_dict):
            if filename == "-" or info_dict.get("is_live") or self.params.get("test"):
                return None
            conns = int(self.params.get("concurrent_fragment_downloads") or 1)
            if conns < 2 or info_dict.get("http_chunk_size") or self.params.get("http_chunk_size"):
                return None
            part = self.temp_name(filename)
            # half done normal download, let yt-dlp resume it
            if os.path.exists(part) and os.path.getsize(part) > 0:
                return None
            tmp = self._turbo_name(filename)
            self._drop_part(filename)
            known = info_dict.get("filesize")
            if known and 0 < int(known) < self.MIN_SIZE:
                return None
            url = info_dict["url"]
            headers = _HeaderDict({"Accept-Encoding": "identity"}, info_dict.get("http_headers"))
            ext = {}
            target = self._get_impersonate_target(info_dict)
            if target is not None:
                ext["impersonate"] = target

            def request(a, b):
                h = _HeaderDict(headers)
                h["Range"] = f"bytes={a}-{b}"
                return _Request(url, headers=h, extensions=ext)

            # ask for 1 byte first to see if the server really does ranges and how big the file is
            with self.ydl.urlopen(request(0, 0)) as r:
                m = re.fullmatch(r"bytes\s+0-0/(\d+)", (r.headers.get("Content-Range") or "").strip())
                ok = r.status == 206
                r.read(1)
            if not ok or not m or int(m.group(1)) < self.MIN_SIZE:
                return None
            total = int(m.group(1))

            self.report_destination(filename)
            chunk = max(2 * MB, min(16 * MB, total // (conns * 3)))
            ranges = collections.deque((a, min(a + chunk, total) - 1) for a in range(0, total, chunk))
            conns = min(conns, len(ranges))
            with open(tmp, "wb") as f:
                _sparse(f)
                f.truncate(total)
            lock, stop = threading.Lock(), threading.Event()
            state = {"done": 0, "err": None}

            def fetch(f, a, b):
                pos, tries = a, 0
                while pos <= b and not stop.is_set():
                    try:
                        with self.ydl.urlopen(request(pos, b)) as r:
                            if r.status != 206:
                                raise OSError(f"server answered {r.status} to a range request")
                            while pos <= b and not stop.is_set():
                                data = r.read(min(65536, b - pos + 1))
                                if not data:
                                    raise OSError("connection closed early")
                                f.seek(pos)
                                f.write(data)
                                pos += len(data)
                                with lock:
                                    state["done"] += len(data)
                    except Exception:
                        if stop.is_set():
                            return
                        tries += 1
                        if tries > 5:
                            raise
                        time.sleep(min(0.4 * 2 ** (tries - 1), 6))

            def worker():
                try:
                    with open(tmp, "r+b") as f:
                        while not stop.is_set():
                            with lock:
                                if not ranges:
                                    return
                                a, b = ranges.popleft()
                            fetch(f, a, b)
                except BaseException as e:
                    with lock:
                        state["err"] = state["err"] or e
                    stop.set()

            threads = [threading.Thread(target=worker, daemon=True) for _ in range(conns)]
            t0 = last_t = time.monotonic()
            last_done, speed = 0, None
            for t in threads:
                t.start()
            try:
                while any(t.is_alive() for t in threads):
                    time.sleep(0.25)
                    now, done = time.monotonic(), state["done"]
                    inst = (done - last_done) / max(now - last_t, 1e-6)
                    # smoothed so the ui number doesnt jump around
                    speed = inst if speed is None else speed * 0.7 + inst * 0.3
                    last_done, last_t = done, now
                    self._hook_progress(
                        {
                            "status": "downloading",
                            "filename": filename,
                            "tmpfilename": tmp,
                            "downloaded_bytes": done,
                            "total_bytes": total,
                            "speed": speed,
                            "eta": (total - done) / speed if speed else None,
                            "elapsed": now - t0,
                            "turbo_conns": conns,
                        },
                        info_dict,
                    )
            except BaseException:
                stop.set()
                end = time.monotonic() + 1.5
                for t in threads:
                    t.join(timeout=max(0.05, end - time.monotonic()))
                raise
            for t in threads:
                t.join()
            if state["err"]:
                raise state["err"]
            if state["done"] != total:
                raise OSError("download incomplete")
            self.try_rename(tmp, filename)
            self._hook_progress(
                {
                    "status": "finished",
                    "filename": filename,
                    "downloaded_bytes": total,
                    "total_bytes": total,
                    "elapsed": time.monotonic() - t0,
                },
                info_dict,
            )
            return True

    return TurboHttpFD


def _http_json(url, etag=None, timeout=15):
    headers = {
        "User-Agent": f"AnyLinkDownloader/{APP_VERSION}",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    if etag:
        headers["If-None-Match"] = etag
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8")), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        raise


def _install_wheel(release, dest):
    # yt-dlp ships a pure python wheel so the universal one is the one we want
    entry = next(
        (
            f
            for f in release["urls"]
            if f.get("packagetype") == "bdist_wheel" and f["filename"].endswith("none-any.whl")
        ),
        None,
    )
    if entry is None:
        raise RuntimeError("no universal wheel published for " + release["info"]["name"])
    fd, tmp = tempfile.mkstemp(suffix=".whl")
    os.close(fd)
    try:
        h = hashlib.sha256()
        with urllib.request.urlopen(entry["url"], timeout=30) as r, open(tmp, "wb") as out:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                h.update(chunk)
                out.write(chunk)
        want = (entry.get("digests") or {}).get("sha256")
        if want and h.hexdigest() != want:
            raise RuntimeError("download is corrupted (checksum mismatch)")
        with zipfile.ZipFile(tmp) as z:
            z.extractall(dest)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def update_available(force=False):
    st = _read_json(STATE_PATH, {})
    now = time.time()
    current = yt_dlp.version.__version__
    pending = _engine_dir_version()
    best_have = max([current] + ([pending] if pending else []), key=version_key)
    if not force and now - st.get("checked", 0) < CHECK_EVERY and st.get("latest"):
        return st["latest"], version_key(st["latest"]) > version_key(best_have)
    data, etag = _http_json("https://pypi.org/pypi/yt-dlp/json", st.get("etag") if not force else None)
    latest = data["info"]["version"] if data else st.get("latest") or current
    _write_json(STATE_PATH, {"checked": now, "etag": etag, "latest": latest})
    return latest, version_key(latest) > version_key(best_have)


def download_update(latest):
    release, _ = _http_json(f"https://pypi.org/pypi/yt-dlp/{latest}/json")
    new_dir = ENGINE_DIR + ".new"
    shutil.rmtree(new_dir, ignore_errors=True)
    os.makedirs(new_dir, exist_ok=True)
    try:
        _install_wheel(release, new_dir)
        # yt-dlp pins an exact yt-dlp-ejs version, so grab that one if we dont already have it
        pin = next((r for r in (release["info"].get("requires_dist") or []) if r.lower().startswith("yt-dlp-ejs")), "")
        m = re.search(r"==\s*([0-9][^\s;,)]*)", pin)
        have_ejs = versions().get("ejs")
        if not (m and have_ejs == m.group(1)):
            ejs_release, _ = _http_json(
                f"https://pypi.org/pypi/yt-dlp-ejs/{m.group(1)}/json" if m else "https://pypi.org/pypi/yt-dlp-ejs/json"
            )
            _install_wheel(ejs_release, new_dir)
        if not os.path.isfile(os.path.join(new_dir, "yt_dlp", "version.py")):
            raise RuntimeError("the downloaded engine is incomplete")
        # built next to it and swapped in last so a failed download never leaves a half engine
        shutil.rmtree(ENGINE_DIR, ignore_errors=True)
        os.replace(new_dir, ENGINE_DIR)
    except Exception:
        shutil.rmtree(new_dir, ignore_errors=True)
        raise
    return latest


def update_now(force=False):
    if NO_UPDATE:
        return {"latest": yt_dlp.version.__version__, "downloaded": False}
    latest, newer = update_available(force)
    if not newer:
        return {"latest": latest, "downloaded": False}
    download_update(latest)
    return {"latest": latest, "downloaded": True}


FFMPEG_MIRRORS = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
)


def have_tool(name):
    return shutil.which(name) is not None


def js_runtime():
    return next((n for n in ("deno", "node", "bun", "qjs") if have_tool(n)), None)


def missing_tools():
    out = []
    if not have_tool("ffmpeg") or not have_tool("ffprobe"):
        out.append("ffmpeg")
    if not js_runtime():
        out.append("deno")
    return out


def _download(url, dest, progress=None):
    req = urllib.request.Request(url, headers={"User-Agent": "AnyLinkDownloader"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)


def _extract_exes(zip_path, names, dest):
    found = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename).lower()
            if base in names:
                with z.open(info) as src, open(os.path.join(dest, base), "wb") as out:
                    shutil.copyfileobj(src, out)
                found += 1
    return found


def repair_tools(progress=None):
    need = missing_tools()
    if not need or os.name != "nt":
        return need
    os.makedirs(TOOLS_DIR, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="ald_fix_")
    try:
        if "ffmpeg" in need:
            z = os.path.join(tmp, "ffmpeg.zip")
            for url in FFMPEG_MIRRORS:
                try:
                    _download(url, z, (lambda f: progress("ffmpeg", f)) if progress else None)
                    if _extract_exes(z, {"ffmpeg.exe", "ffprobe.exe"}, TOOLS_DIR) >= 2:  # need both
                        break
                except Exception as e:
                    file_log(f"ffmpeg mirror failed: {url} {e}")
        if "deno" in need:
            arch = "aarch64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "ARM64" else "x86_64"
            z = os.path.join(tmp, "deno.zip")
            try:
                _download(
                    f"https://github.com/denoland/deno/releases/latest/download/deno-{arch}-pc-windows-msvc.zip",
                    z,
                    (lambda f: progress("deno", f)) if progress else None,
                )
                _extract_exes(z, {"deno.exe"}, TOOLS_DIR)
            except Exception as e:
                file_log(f"deno download failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    bootstrap_path()
    return missing_tools()
