import http.server
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

from . import studio
from . import winutil
from .core import UA, file_log, fmt_size, resource_path, safe_name


MAX_BODY = 1_000_000  # the ui only sends small json, anything bigger gets rejected


# small lru in memory so the same thumbnails dont get downloaded over and over
class _ThumbCache:
    def __init__(self, cap=24 * 1024 * 1024):
        self.cap, self.size, self.d, self.lock = cap, 0, OrderedDict(), threading.Lock()

    def get(self, k):
        with self.lock:
            v = self.d.get(k)
            if v:
                self.d.move_to_end(k)
            return v

    def put(self, k, v):
        with self.lock:
            if k in self.d:
                return
            self.d[k] = v
            self.size += len(v[1])
            while self.size > self.cap and self.d:
                _, old = self.d.popitem(last=False)
                self.size -= len(old[1])


_host_ok = {}


# the thumbnail proxy shouldnt be usable to poke at things on the local network
def _public_host(host):
    if os.environ.get("ALD_ALLOW_LOCAL_THUMBS"):  # lets thumbnails come from localhost while developing
        return True
    hit = _host_ok.get(host)
    if hit and time.monotonic() - hit[1] < 600:
        return hit[0]
    try:
        ok = True
        for fam, _, _, _, addr in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(addr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                ok = False
                break
    except Exception:
        return False
    if len(_host_ok) > 500:
        _host_ok.clear()
    _host_ok[host] = (ok, time.monotonic())
    return ok


class AppServer:
    def __init__(self, manager, headless=False):
        self.m = manager
        self.token = secrets.token_urlsafe(24)  # new every run, the page and api only work with it
        self.thumbs = _ThumbCache()
        self.studio = studio.Studio(
            emit=manager.emit, log=manager.log, allow=manager.studio_paths, threads=manager.studio_threads
        )
        self.clients = 0
        self.last_seen = time.monotonic()
        self.ever_connected = False
        self.quit = threading.Event()
        self.started = time.monotonic()
        app = self
        html_path = resource_path("ald", "ui", "index.html")

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "AnyLink"

            def log_message(self, *a):
                pass

            # only our own address is accepted as the Host, other names could be dns rebinding
            def _host_ok(self):
                host = (self.headers.get("Host") or "").lower()
                return host in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

            def _authed(self):
                cookie = self.headers.get("Cookie") or ""
                return any(p.strip() == f"ald={app.token}" for p in cookie.split(";"))

            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _text(self, code, text):
                body = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    return {}
                if n > MAX_BODY:
                    raise ValueError("too big")
                return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

            def do_GET(self):
                if not self._host_ok():
                    return self._text(403, "Wrong host")
                u = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path == "/":
                    if not self._authed() and (q.get("t") or [""])[0] != app.token:
                        return self._text(403, "Open this app from its own window.")
                    try:
                        with open(html_path, "rb") as f:
                            body = f.read()
                    except OSError:
                        return self._text(500, "The app's screen file is missing.")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Set-Cookie", f"ald={app.token}; Path=/; HttpOnly; SameSite=Strict")
                    self.send_header(
                        "Content-Security-Policy",
                        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                        "script-src 'self' 'unsafe-inline'; font-src 'self' data:; connect-src 'self'; "
                        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                    )
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if u.path.startswith("/fonts/") and os.path.basename(u.path) in ("brico.woff2", "figtree.woff2"):
                    return self._file(resource_path("ald", "ui", "fonts", os.path.basename(u.path)), "font/woff2")
                static = re.fullmatch(r"/(css|js)/([a-z0-9_-]+)\.(css|js)", u.path)
                # plain file names only so nothing can climb out of the ui folder
                if static and static.group(1) == static.group(3):
                    ctype = "text/css; charset=utf-8" if static.group(3) == "css" else "text/javascript; charset=utf-8"
                    return self._file(
                        resource_path("ald", "ui", static.group(1), static.group(2) + "." + static.group(3)),
                        ctype,
                        cache="no-cache",
                    )
                if u.path == "/favicon.png":
                    return self._file(resource_path("assets", "icon.png"), "image/png")
                if not self._authed():
                    return self._text(403, "Not allowed")
                if u.path == "/api/state":
                    return self._json(app.m.snapshot())
                if u.path == "/api/events":
                    return self._events()
                if u.path == "/api/thumb":
                    return self._thumb((q.get("u") or [""])[0])
                if u.path == "/api/clipboard":
                    return self._json({"text": winutil.read_clipboard()})
                if u.path == "/api/diagnostics":
                    return self._json({"text": app.m.diagnostics()})
                if u.path == "/api/studio/media":
                    return self._media((q.get("id") or [""])[0])
                if u.path == "/api/studio/frame":
                    return self._picture(
                        lambda: app.studio.frame(
                            (q.get("id") or [""])[0], (q.get("t") or ["0"])[0], (q.get("h") or ["90"])[0]
                        ),
                        "image/jpeg",
                    )
                if u.path == "/api/studio/wave":
                    return self._picture(
                        lambda: app.studio.wave(
                            (q.get("id") or [""])[0], (q.get("w") or ["1600"])[0], (q.get("h") or ["80"])[0]
                        ),
                        "image/png",
                    )
                self._text(404, "Not found")

            def _write_ok(self):
                if not self._host_ok() or not self._authed():
                    self._text(403, "Not allowed")
                    return False
                origin = self.headers.get("Origin")
                if origin and origin not in (f"http://127.0.0.1:{app.port}", f"http://localhost:{app.port}"):
                    self._text(403, "Wrong origin")
                    return False
                # other websites cant add a custom header without a preflight, so they cant call the api
                if self.headers.get("X-ALD") != "1":
                    self._text(403, "Missing header")
                    return False
                return True

            def do_PUT(self):
                if not self._write_ok():
                    self.close_connection = True
                    return
                u = urllib.parse.urlsplit(self.path)
                if u.path != "/api/studio/upload":
                    return self._text(404, "Not found")
                try:
                    name = (urllib.parse.parse_qs(u.query).get("name") or ["clip"])[0]
                    self._json(app.upload(self, name))
                except Exception as e:
                    file_log(f"Upload failed: {e!r}")
                    self.close_connection = True
                    self._json({"ok": False, "error": "The file couldn't be copied in."}, 500)

            def do_POST(self):
                if not self._write_ok():
                    return
                path = urllib.parse.urlsplit(self.path).path
                try:
                    data = self._body()
                    self._json(app.dispatch(path, data))
                except KeyError:
                    self._text(404, "Not found")
                except Exception as e:
                    file_log(f"API error {path}: {e!r}")
                    self._json({"error": str(e)}, 500)

            def _file(self, path, ctype, cache="max-age=86400"):
                try:
                    with open(path, "rb") as f:
                        body = f.read()
                except OSError:
                    return self._text(404, "Not found")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache)
                self.end_headers()
                self.wfile.write(body)

            def _picture(self, make, ctype):
                try:
                    body = make()
                except studio.StudioError:
                    body = None
                except Exception as e:
                    file_log(f"Studio picture failed: {e!r}")
                    body = None
                if not body:
                    return self._text(404, "No picture")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(body)

            def _media(self, sid):
                try:
                    hit = app.studio.media_file(sid)
                except studio.StudioError:
                    return self._text(404, "Not found")
                if not hit:
                    return self._text(404, "Still preparing")
                path, ctype = hit
                try:
                    size = os.path.getsize(path)
                except OSError:
                    return self._text(404, "Not found")
                # the video element needs range requests to seek around in the preview
                start, end, code = 0, size - 1, 200
                rng = (self.headers.get("Range") or "").strip()
                if rng:
                    m = re.match(r"^bytes=(\d*)-(\d*)$", rng)
                    if m and (m.group(1) or m.group(2)):
                        a, b = m.groups()
                        if not a:
                            start = max(0, size - int(b))
                        else:
                            start = int(a)
                            end = min(size - 1, int(b)) if b else size - 1
                        if start > end or start >= size:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{size}")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        code = 206
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    with open(path, "rb") as f:
                        f.seek(start)
                        left = end - start + 1
                        while left > 0:
                            chunk = f.read(min(256 * 1024, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (OSError, ValueError):
                    self.close_connection = True

            def _thumb(self, url):
                if not url.startswith(("http://", "https://")):
                    return self._text(400, "bad url")
                hit = app.thumbs.get(url)
                if not hit:
                    try:
                        host = urllib.parse.urlsplit(url).hostname or ""
                        if not _public_host(host):
                            return self._text(403, "no")
                        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*"})
                        with urllib.request.urlopen(req, timeout=8) as r:
                            ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
                            raw = r.read(3_000_000)  # cap it, a thumbnail should never be bigger than this
                        if not ctype.startswith("image/"):
                            return self._text(415, "not an image")
                        hit = (ctype, raw)
                        app.thumbs.put(url, hit)
                    except Exception:
                        return self._text(404, "no thumbnail")
                self.send_response(200)
                self.send_header("Content-Type", hit[0])
                self.send_header("Content-Length", str(len(hit[1])))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(hit[1])

            def _events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = app.m.subscribe()
                app.clients += 1
                app.ever_connected = True
                app.last_seen = time.monotonic()
                try:
                    self._sse("state", app.m.snapshot())
                    while not app.quit.is_set():
                        try:
                            kind, data = q.get(timeout=2)
                            self._sse(kind, data)
                        except queue.Empty:
                            # writing every 2s keeps the stream alive and fails fast once the window is gone
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                        app.last_seen = time.monotonic()
                except (OSError, ValueError):
                    pass
                finally:
                    app.clients -= 1
                    app.last_seen = time.monotonic()
                    app.m.unsubscribe(q)

            def _sse(self, kind, data):
                self.wfile.write(f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            request_queue_size = 32

            def handle_error(self, request, client_address):
                e = sys.exc_info()[1]
                # the browser dropping a connection is normal, only log the rest
                if not isinstance(e, (ConnectionError, TimeoutError)):
                    file_log(f"Server error from {client_address}: {e!r}")

        self.httpd = Server(("127.0.0.1", int(os.environ.get("ALD_PORT") or 0)), Handler)
        self.port = self.httpd.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def upload(self, h, name):
        n = int(h.headers.get("Content-Length") or 0)
        if n <= 0:
            return {"ok": False, "error": "That file is empty."}
        os.makedirs(studio.UPLOADS, exist_ok=True)
        try:
            free = shutil.disk_usage(studio.UPLOADS).free
            if free < n + 200 * 1024 * 1024:  # leave about 200mb spare after the copy
                return {"ok": False, "error": f"Not enough free space to open this file ({fmt_size(n)})."}
        except OSError:
            pass
        base = safe_name(os.path.basename(name or "clip"), "clip")
        dest = os.path.join(studio.UPLOADS, f"{secrets.token_hex(3)}_{base}")
        part = dest + ".part"  # only renamed once the whole file made it in
        got = 0
        try:
            with open(part, "wb") as f:
                while got < n:
                    chunk = h.rfile.read(min(1024 * 1024, n - got))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
            if got != n:
                raise OSError("short upload")
            os.replace(part, dest)
        except Exception:
            try:
                os.remove(part)
            except OSError:
                pass
            raise
        try:
            return {"ok": True, "source": self.studio.register(dest, upload=True)}
        except studio.StudioError as e:
            try:
                os.remove(dest)
            except OSError:
                pass
            return {"ok": False, "error": str(e)}

    def studio_api(self, what, d):
        st, m = self.studio, self.m
        try:
            if what == "caps":
                caps = st.caps()
                return {
                    "formats": [
                        {
                            "id": k,
                            "label": f["label"],
                            "kind": f["kind"],
                            "ext": f["ext"],
                            "hint": f["hint"],
                            "ok": bool(caps.get(k, True)),
                        }
                        for k, f in studio.FORMATS.items()
                    ]
                }
            if what == "open":
                p = d.get("path") or ""
                if not m._allowed_path(p):
                    return {"ok": False, "error": "That file isn't one this app knows about."}
                return {"ok": True, "source": st.register(p)}
            if what == "pick":
                got = winutil.choose_files()
                if got is None:
                    return {"ok": False, "unavailable": True}
                srcs, errs = [], []
                for p in got:
                    try:
                        srcs.append(st.register(p))
                    except studio.StudioError as e:
                        errs.append(f"{os.path.basename(p)}: {e}")
                return {"ok": True, "sources": srcs, "errors": errs}
            if what == "pick_dir":
                got = winutil.choose_folder(m.settings["folder"], "Choose where to save the result")
                if got is None:
                    return {"ok": False, "unavailable": True}
                if got:
                    st.picked_dirs.add(os.path.abspath(got))
                return {"ok": bool(got), "dir": got}
            if what in ("plan", "export"):
                spec = dict(d, default_dir=m.settings["folder"])
                return {"ok": True, "plan": st.plan_public(spec)} if what == "plan" else st.start_export(spec)
            if what == "cancel":
                return {"ok": st.cancel()}
            if what == "close":
                return {"ok": st.close(d.get("id"))}
            if what == "keyframes":
                return {"ok": True, "keyframes": st.keyframes(d.get("id")) or []}
            if what == "status":
                return st.status()
            if what == "proxy":
                return {"ok": True, "source": st.force_proxy(d.get("id"))}
        except studio.StudioError as e:
            return {"ok": False, "error": str(e)}
        raise KeyError(what)

    def dispatch(self, path, d):
        m = self.m
        if path.startswith("/api/studio/"):
            return self.studio_api(path[len("/api/studio/") :], d)
        if path == "/api/analyze":
            return m.analyze(d.get("url", ""))
        if path == "/api/preview":
            return m.preview(d.get("url", ""))
        if path == "/api/sizes":
            return m.sizes(d.get("url", ""), bool(d.get("full")))
        if path == "/api/timing":
            m.log("Screen timing: " + str(d.get("msg", ""))[:400])
            return {}
        if path == "/api/enqueue":
            return m.enqueue(d)
        if path == "/api/cancel":
            m.cancel(d.get("id"))
            return {"ok": True}
        if path == "/api/retry":
            m.retry(d.get("id"))
            return {"ok": True}
        if path == "/api/remove":
            m.remove(d.get("id"))
            return {"ok": True}
        if path == "/api/resume":
            m.resume_queue(bool(d.get("go", True)))
            return {"ok": True}
        if path == "/api/settings":
            m.save_settings(d)
            return {"ok": True, "settings": m.settings}
        if path == "/api/choose_folder":
            got = winutil.choose_folder(m.settings["folder"])
            if got is None:
                return {"ok": False, "unavailable": True}
            if got:
                m.save_settings({"folder": got})
            return {"ok": bool(got), "folder": m.settings["folder"]}
        if path == "/api/open":
            return {"ok": m.open_path(d.get("path"))}
        if path == "/api/reveal":
            return {"ok": m.reveal_path(d.get("path"))}
        if path == "/api/forget":
            m.forget(d.get("path"))
            return {"ok": True}
        if path == "/api/update_engine":
            return m.update_engine()
        if path == "/api/app_update":
            return m.app_update_check(bool(d.get("force")))
        if path == "/api/ota":
            return m.ota_check(bool(d.get("force")))
        if path == "/api/app_update_download":
            return m.app_update_download()
        if path == "/api/app_update_install":
            return m.app_update_install(bool(d.get("confirm")))
        if path == "/api/cleanup":
            return m.cleanup(bool(d.get("do")))
        if path == "/api/speed_test":
            return m.speed_test()
        if path == "/api/ping":
            self.last_seen = time.monotonic()
            return {"ok": True}
        if path == "/api/newwindow":
            self.last_seen = time.monotonic()
            if not winutil.focus_existing_window():
                winutil.open_app_window(self.url)
            return {"ok": True}
        if path == "/api/quit":
            self.quit.set()
            return {"ok": True}
        raise KeyError(path)

    def _clip_loop(self):
        last = None
        while not self.quit.is_set():
            self.quit.wait(1.0)
            try:
                s = self.m.settings
                # only watches while a window is actually open
                if self.clients <= 0 or not s.get("clip_watch") or not s.get("auto_fetch", True):
                    continue
                text = (winutil.read_clipboard() or "").strip()
                if text == last:
                    continue
                last = text
                if re.fullmatch(r"https?://\S+", text):
                    self.m.prefetch(text)
            except Exception as e:
                file_log(f"clipboard watch: {e}")

    def serve_forever(self, window_proc=None, grace=8, first_connect_timeout=45):
        t = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True)
        t.start()
        if winutil.IS_WIN:
            threading.Thread(target=self._clip_loop, daemon=True, name="clipwatch").start()
        try:
            while not self.quit.is_set():
                time.sleep(0.5)
                now = time.monotonic()
                # no window ever connected or the last one closed a while ago -> time to quit
                if not self.ever_connected:
                    if now - self.started > first_connect_timeout:
                        file_log("no window connected - exiting")
                        break
                elif self.clients <= 0 and now - self.last_seen > grace:
                    break
        finally:
            self.quit.set()
            try:
                self.httpd.shutdown()
            except Exception:
                pass
