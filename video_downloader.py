import os
import platform
import shutil
import sys
import threading
import time
import traceback

# the packaged exe has no console so these are None and a stray print would crash
for _n in ("stdout", "stderr"):
    if getattr(sys, _n, None) is None:
        setattr(sys, _n, open(os.devnull, "w", encoding="utf-8"))

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ota  # noqa: E402

ota.activate()  # has to come before the first ald import so a quick update can take its place

from ald import core, engine, winutil  # noqa: E402
from ald.core import APP_NAME, APP_VERSION, DATA_DIR, FROZEN, file_log  # noqa: E402

_MUTEX = []
INSTANCE_FILE = os.path.join(DATA_DIR, "instance.json")


# if a copy is already running we just ask it for a new window instead of starting another
def ask_running_instance():
    import json
    import urllib.request

    try:
        with open(INSTANCE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        req = urllib.request.Request(
            f"http://127.0.0.1:{d['port']}/api/newwindow",
            data=b"{}",
            method="POST",
            headers={"Cookie": f"ald={d['token']}", "X-ALD": "1", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return bool(json.load(r).get("ok"))
    except Exception:
        return False


def claim_start():
    if os.name != "nt":
        return True
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        handle = k32.CreateMutexW(None, False, "Local\\AnyLinkDownloader2.Start")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _MUTEX.append(handle)
    except Exception:
        pass
    return True


def write_instance(port, token):
    import json

    try:
        with open(INSTANCE_FILE, "w", encoding="utf-8") as f:
            json.dump({"port": port, "token": token, "pid": os.getpid()}, f)
    except OSError:
        pass


def clear_instance():
    try:
        os.remove(INSTANCE_FILE)
    except OSError:
        pass


def install_crash_guards():
    def report(where, et, ev, tb):
        file_log(f"INTERNAL ERROR ({where})\n" + "".join(traceback.format_exception(et, ev, tb)))

    sys.excepthook = lambda t, v, tb: report("the app", t, v, tb)
    threading.excepthook = lambda a: report("a background task", a.exc_type, a.exc_value, a.exc_traceback)


def load_engine():
    try:
        return engine.load()
    except Exception as e:
        winutil.message_box(
            f"{APP_NAME} could not start its download engine.\n\n{type(e).__name__}: {e}\n\n"
            "Reinstalling the app fixes this."
        )
        raise SystemExit(1)


def run_selftest(out_path):
    import http.server
    import json
    import subprocess
    import tempfile
    import urllib.request

    lines, bad = [], []

    def check(name, fn):
        try:
            lines.append(f"PASS  {name}: {fn()}")
        except Exception as e:  # noqa: BLE001
            bad.append(name)
            lines.append(f"FAIL  {name}: {type(e).__name__}: {e}")

    def run(cmd, timeout=60):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=core.NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
        return r.stdout

    tmp = tempfile.mkdtemp(prefix="ald_selftest_")
    os.environ["ALD_DATA_DIR"] = os.path.join(tmp, "data")
    clip = os.path.join(tmp, "clip.mp4")
    yt = engine.yt_dlp

    check(
        "build",
        lambda: f"{APP_NAME} {APP_VERSION} | Python {platform.python_version()} | {'packaged' if FROZEN else 'source'}",
    )
    check("yt-dlp", lambda: yt.version.__version__ + (" (updated engine)" if engine.active_engine() else ""))
    check(
        "speed patches",
        lambda: (
            ", ".join(k for k, v in engine._patched.items() if v)
            if all(engine._patched.values())
            else (_ for _ in ()).throw(RuntimeError(str(engine._patched)))
        ),
    )
    check("engine can read links", engine.check_engine_works)

    def ejs():
        import yt_dlp_ejs
        import yt_dlp_ejs.yt.solver  # noqa: F401

        return engine.versions()["ejs"] or "present"

    check("yt-dlp-ejs (YouTube solver scripts)", ejs)
    check(
        "browser disguise (curl_cffi)",
        lambda: "yes" if engine.HAVE_IMPERSONATE else (_ for _ in ()).throw(RuntimeError("missing (optional)")),
    )

    def ff():
        p = shutil.which("ffmpeg")
        if not p:
            raise RuntimeError("ffmpeg not found")
        return p + " | " + run([p, "-version"]).splitlines()[0]

    check("ffmpeg", ff)
    check("ffprobe", lambda: shutil.which("ffprobe") or (_ for _ in ()).throw(RuntimeError("ffprobe not found")))

    def dn():
        p = shutil.which("deno")
        if not p:
            raise RuntimeError("deno not found")
        return p + " | " + run([p, "--version"]).splitlines()[0]

    check("deno (JavaScript runtime)", dn)

    def encode():
        run(
            [
                shutil.which("ffmpeg"),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=160x90:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=1",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                "-shortest",
                clip,
            ]
        )
        return f"made a test clip ({os.path.getsize(clip)} bytes)"

    check("ffmpeg can encode", encode)

    def studio_check():
        import time as _t

        from ald import studio

        src = os.path.join(tmp, "studio_src.mp4")
        # keyframe every second (-g 25 at 25fps), the cut below lands between keyframes
        run(
            [
                shutil.which("ffmpeg"),
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=3:size=160x120:rate=25",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=3",
                "-c:v",
                "libx264",
                "-g",
                "25",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                src,
            ]
        )
        events = []
        st = studio.Studio(emit=lambda k, d: events.append(d))
        sid = st.register(src)["id"]
        outdir = os.path.join(tmp, "studio_out")
        os.makedirs(outdir)
        st.picked_dirs.add(os.path.abspath(outdir))
        st.start_export(
            {"segments": [{"src": sid, "in": 0.52, "out": 2.04}], "format": "mp4", "dir": outdir, "name": "t"}
        )
        end = _t.time() + 60
        while _t.time() < end and not any(e.get("state") in ("done", "error") for e in events):
            _t.sleep(0.1)
        done = [e for e in events if e.get("state") in ("done", "error")]
        if not done or done[-1]["state"] != "done":
            raise RuntimeError(done[-1].get("message") if done else "timed out")
        return f"cut + checked ({done[-1]['size_text']})"

    check("Studio (cut, re-encode, safety check)", studio_check)

    def tls():
        import ssl

        import certifi

        ssl.create_default_context(cafile=certifi.where())
        return certifi.where()

    check("HTTPS certificates", tls)
    check(
        "screen file",
        lambda: (
            f"{os.path.getsize(core.resource_path('ald', 'ui', 'index.html'))} bytes"
            if os.path.exists(core.resource_path("ald", "ui", "index.html"))
            else (_ for _ in ()).throw(RuntimeError("index.html missing"))
        ),
    )
    check(
        "window browser",
        lambda: (
            winutil.find_browser()
            or (_ for _ in ()).throw(RuntimeError("no Edge/Chrome found (the app will use the default browser)"))
        ),
    )

    # downloads a clip from a tiny local web server so it works without internet
    def end_to_end():
        from ald import manager as M
        from ald import server as S

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=tmp, **k)

            def log_message(self, *a):
                pass

        web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        mgr = M.Manager(start_threads=False)
        out = os.path.join(tmp, "saved")
        mgr.save_settings({"folder": out})
        threading.Thread(target=mgr._worker, daemon=True).start()
        srv = S.AppServer(mgr)
        threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.port}"
            cookie = {"Cookie": f"ald={srv.token}", "X-ALD": "1", "Content-Type": "application/json", "Origin": base}

            def api(path, body=None):
                req = urllib.request.Request(
                    base + path,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers=cookie,
                    method="POST" if body is not None else "GET",
                )
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)

            page = urllib.request.urlopen(urllib.request.Request(base + "/?t=" + srv.token), timeout=10).read()
            if b"Any Link" not in page:
                raise RuntimeError("the screen did not load")
            try:  # no cookie has to be refused
                urllib.request.urlopen(base + "/api/state", timeout=5)
                raise RuntimeError("API answered without the secret")
            except urllib.error.HTTPError as e:
                if e.code != 403:
                    raise
            res = api("/api/analyze", {"url": f"http://127.0.0.1:{web.server_address[1]}/clip.mp4"})
            if res.get("type") != "video":
                raise RuntimeError(f"analysis failed: {res}")
            api(
                "/api/enqueue",
                {"items": [{"url": res["url"], "id": res["id"], "title": res["title"]}], "kind": "video"},
            )
            end = time.time() + 60
            while time.time() < end:
                jobs = api("/api/state")["jobs"]
                if jobs and jobs[-1]["status"] in ("done", "failed"):
                    break
                time.sleep(0.3)
            j = api("/api/state")["jobs"][-1]
            if j["status"] != "done":
                raise RuntimeError(f"download ended as {j['status']}: {j.get('error')}")
            return f"analysed, downloaded and saved {os.path.basename(j['path'])}"
        finally:
            mgr.stop.set()
            web.shutdown()
            srv.httpd.shutdown()

    check("whole app (local test download)", end_to_end)

    def online():
        try:
            with urllib.request.urlopen("https://pypi.org/simple/yt-dlp/", timeout=8) as r:
                return f"HTTPS works (HTTP {r.status})"
        except Exception as e:  # noqa: BLE001
            return f"skipped - offline? ({e})"  # being offline shouldnt fail the whole selftest

    check("internet", online)

    # the browser checks are optional so they dont count as a fail
    lines.append(
        "RESULT: "
        + (
            "OK"
            if not [b for b in bad if not b.startswith(("browser disguise", "window browser"))]
            else "FAIL (" + ", ".join(bad) + ")"
        )
    )
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    print("\n".join(lines))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if lines[-1] == "RESULT: OK" else 1


def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:
        os.environ["ALD_NO_UPDATE"] = "1"  # dont go downloading a new engine in the middle of a test
        engine.NO_UPDATE = True
        i = argv.index("--selftest")
        out = argv[i + 1] if len(argv) > i + 1 else os.path.join(DATA_DIR, "selftest.txt")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        load_engine()
        raise SystemExit(run_selftest(out))

    if ask_running_instance():
        ota.mark_good()  # we didnt start anything, so this launch must not count against the patch
        return
    if not claim_start():
        ota.mark_good()
        # other copy is probably still starting up, give it a few seconds to be ready
        for _ in range(12):
            time.sleep(0.5)
            if ask_running_instance():
                return
        return
    install_crash_guards()
    load_engine()
    from ald.manager import Manager
    from ald.server import AppServer

    manager = Manager()
    server = AppServer(manager)
    write_instance(server.port, server.token)
    patch = f" +quick update {ota.level()}" if ota.level() else ""
    file_log(f"{APP_NAME} {APP_VERSION}{patch} on {platform.platform()} | window at port {server.port}")
    if "--serve" in argv:
        print(server.url)
        sys.stdout.flush()
    else:
        proc = winutil.open_app_window(server.url)
        if proc is None and not winutil.find_browser():
            file_log("no Chromium browser found - using the default browser")
    ota.mark_good()  # made it to a running server, the quick update (if any) is fine
    try:
        server.serve_forever()
    finally:
        clear_instance()
        manager.shutdown()


if __name__ == "__main__":
    main()
