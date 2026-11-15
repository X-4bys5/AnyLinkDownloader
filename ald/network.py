import concurrent.futures
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request

from .core import _dns_cache

# proxy parsing, the dns bypass and internet checks
ROUTE_TTL = 1800


def parse_proxies(text):
    out = []
    for p in re.split(r"[\s,;]+", text or ""):
        p = p.strip()
        if not p:
            continue
        # no scheme given so we try it as an http proxy and as socks5h
        variants = [p] if "://" in p else ["http://" + p, "socks5h://" + p]
        for v in variants:
            if v not in out:
                out.append(v)
    return out


def mask_proxy(p):
    return re.sub(r"//[^@/]*@", "//***@", p)


def detect_local_tor():
    found = []
    for port in (9150, 9050):  # tor browser uses 9150, the tor service 9050
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                found.append(f"socks5h://127.0.0.1:{port}")
        except OSError:
            pass
    return found


def probe_route(url, proxy, timeout=10):
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "socket_timeout": timeout}
    if proxy:
        opts["proxy"] = proxy
    t0, penalty = time.monotonic(), 0.0
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            resp = ydl.urlopen(url)
            resp.read(262144)  # 256kb is enough to see how fast the route is
            resp.close()
        except yt_dlp.networking.exceptions.HTTPError:
            penalty = 5.0  # the route works but an error ranks it lower
    return time.monotonic() - t0 + penalty


# plain ips so looking these up doesnt need dns itself
DOH_ENDPOINTS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/resolve")
UDP_DNS_SERVERS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")


def doh_lookup(host, endpoint, timeout=3):
    req = urllib.request.Request(
        f"{endpoint}?name={urllib.parse.quote(host)}&type=A", headers={"accept": "application/dns-json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
    if not ips:
        raise OSError(f"no address for {host}")
    return ips


def _skip_name(msg, i):
    while True:
        n = msg[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:  # compression pointer, always 2 bytes
            return i + 2
        i += 1 + n


def _parse_a_records(msg, qid):
    if len(msg) < 12 or msg[:2] != qid:
        raise OSError("bad DNS reply")
    rcode = msg[3] & 0x0F
    if rcode != 0:
        raise OSError(f"DNS error {rcode}")
    qd, an = int.from_bytes(msg[4:6], "big"), int.from_bytes(msg[6:8], "big")
    i = 12
    for _ in range(qd):
        i = _skip_name(msg, i) + 4
    ips = []
    for _ in range(an):
        i = _skip_name(msg, i)
        typ = int.from_bytes(msg[i : i + 2], "big")
        rdlen = int.from_bytes(msg[i + 8 : i + 10], "big")
        rdata = msg[i + 10 : i + 10 + rdlen]
        if typ == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in rdata))
        i += 10 + rdlen
    if not ips:
        raise OSError("no A record")
    return ips


def udp_lookup(host, server, port=53, timeout=3):
    qid = os.urandom(2)
    labels = b"".join(bytes([len(p)]) + p for p in (x.encode("idna") for x in host.rstrip(".").split(".")))
    packet = qid + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + labels + b"\x00\x00\x01\x00\x01"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(packet, (server, port))
        msg, _ = s.recvfrom(4096)
    return _parse_a_records(msg, qid)


def smart_resolve(host):
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host) or ":" in host:
        return host
    hit = _dns_cache.get(host)
    if hit and time.monotonic() - hit[1] < 300:  # 5 min cache
        return hit[0]
    # fire them all at once and take whichever answers first
    racers = [
        lambda: doh_lookup(host, DOH_ENDPOINTS[0]),
        lambda: udp_lookup(host, UDP_DNS_SERVERS[0]),
        lambda: doh_lookup(host, DOH_ENDPOINTS[1]),
        lambda: udp_lookup(host, UDP_DNS_SERVERS[1]),
        lambda: udp_lookup(host, UDP_DNS_SERVERS[2]),
    ]
    last = None
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(racers), thread_name_prefix="dns")
    try:
        futs = [pool.submit(fn) for fn in racers]
        for fut in concurrent.futures.as_completed(futs, timeout=12):
            try:
                ips = fut.result()
            except Exception as e:
                last = e
                continue
            if ips:
                _dns_cache[host] = (ips[0], time.monotonic())
                return ips[0]
    except concurrent.futures.TimeoutError as e:
        last = e
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # last resort is the normal system resolver
    try:
        ip = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
        _dns_cache[host] = (ip, time.monotonic())
        return ip
    except Exception as e:
        last = e
    raise OSError(f"could not look up {host}: {last}")


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)  # pass the close on so the other side sees eof
        except OSError:
            pass


BYPASS_MODES = (
    ("plain", "built-in DNS bypass"),
    ("split", "built-in DNS bypass + split packets"),
    ("tlsrec", "built-in DNS bypass + split TLS handshake"),
    ("both", "built-in DNS bypass + both splits"),
)


class DnsBypassProxy:
    def __init__(self, resolver=None, mode="plain"):
        self.resolver = resolver or smart_resolve
        self.mode = mode
        self.server = None
        self.port = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        if self.server is None:
            outer = self

            class Handler(socketserver.BaseRequestHandler):
                def handle(self):
                    outer._handle(self.request)

            class Server(socketserver.ThreadingTCPServer):
                daemon_threads = True
                allow_reuse_address = True

            self.server = Server(("127.0.0.1", 0), Handler)  # port 0 lets the os pick a free one
            self.port = self.server.server_address[1]
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.url

    def _open(self, host, port):
        return socket.create_connection((self.resolver(host), port), timeout=10)

    def _send_hello(self, client, upstream, buf, host=""):
        client.settimeout(5)
        try:
            if not buf:
                buf = client.recv(4096)
            if not buf or buf[0] != 0x16:  # 0x16 is a tls handshake, anything else goes through untouched
                return buf
            while len(buf) < 5:
                chunk = client.recv(4096)
                if not chunk:
                    return buf
                buf += chunk
            total = 5 + int.from_bytes(buf[3:5], "big")
            while len(buf) < total:
                chunk = client.recv(4096)
                if not chunk:
                    return buf
                buf += chunk
        except socket.timeout:
            return buf
        hello, extra = buf[:total], buf[total:]
        name = host.encode("ascii", "ignore")
        idx = hello.find(name)
        # tries to cut in the middle of the hostname so a filter cant read it in one piece
        cut = idx + max(1, len(name) // 2) if idx > 5 else 6
        cut = max(6, min(cut, total - 1))
        records = [hello]
        if self.mode in ("tlsrec", "both"):
            body, ver = hello[5:], hello[1:3]
            k = cut - 5
            records = [
                b"\x16" + ver + len(body[:k]).to_bytes(2, "big") + body[:k],
                b"\x16" + ver + len(body[k:]).to_bytes(2, "big") + body[k:],
            ]
        if self.mode in ("split", "both"):
            segments = records if len(records) == 2 else [hello[:cut], hello[cut:]]
        else:
            segments = [b"".join(records)]
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for n, seg in enumerate(segments):
            if n:
                time.sleep(0.05)  # small gap so the pieces really go out as separate packets
            upstream.sendall(seg)
        if extra:
            upstream.sendall(extra)
        return b""

    def _handle(self, client):
        upstream = None
        try:
            client.settimeout(30)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = client.recv(8192)
                if not chunk or len(buf) > 65536:
                    return
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            method, target, version = lines[0].decode("latin-1").split(" ", 2)
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                upstream = self._open(host.strip("[]"), int(port or 443))
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                first = rest
                if self.mode != "plain":
                    first = self._send_hello(client, upstream, rest, host.strip("[]"))
            else:
                u = urllib.parse.urlsplit(target)
                upstream = self._open(u.hostname, u.port or 80)
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                # plain http, drop the proxy headers and force the connection closed
                keep = [x for x in lines[1:] if not x.lower().startswith((b"proxy-", b"connection:", b"keep-alive:"))]
                first = (
                    f"{method} {path} {version}".encode("latin-1")
                    + b"\r\n"
                    + b"\r\n".join(keep)
                    + b"\r\nConnection: close\r\n\r\n"
                    + rest
                )
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            if upstream:
                upstream.close()
            return
        try:
            client.settimeout(120)
            upstream.settimeout(120)
            if first:
                upstream.sendall(first)
            t = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
            t.start()
            _pipe(client, upstream)
            t.join(timeout=120)
        except OSError:
            pass
        finally:
            for s in (client, upstream):
                try:
                    s.close()
                except OSError:
                    pass


def set_low_impact(enabled):
    try:
        if sys.platform.startswith("win"):
            import ctypes

            k32 = ctypes.windll.kernel32
            BELOW_NORMAL, NORMAL = 0x00004000, 0x00000020
            return bool(k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL if enabled else NORMAL))
        if enabled:
            os.setpriority(os.PRIO_PROCESS, 0, max(os.getpriority(os.PRIO_PROCESS, 0), 5))
            return True
    except Exception:
        pass
    return False


def internet_ok(timeout=4.0):
    result = threading.Event()

    def ip(host):
        try:
            with socket.create_connection((host, 443), timeout=3):
                result.set()
        except OSError:
            pass

    def name(host):
        try:
            addr = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)[0][4]
            with socket.create_connection(addr[:2], timeout=3):
                result.set()
        except OSError:
            pass

    # some by ip and some by name so broken dns alone doesnt look like no internet
    probes = [(ip, h) for h in ("1.1.1.1", "8.8.8.8", "9.9.9.9")] + [
        (name, h) for h in ("www.microsoft.com", "www.google.com", "www.cloudflare.com")
    ]
    for fn, host in probes:
        threading.Thread(target=fn, args=(host,), daemon=True).start()
    return result.wait(timeout)


def _fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    total = 0
    with urllib.request.urlopen(req, timeout=15) as r:
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
    return total


def measure_line(url, conns=8):
    # one connection first, then all of them at once to compare
    t = time.monotonic()
    n = _fetch_bytes(url)
    single = n / max(time.monotonic() - t, 1e-6)

    got = []

    def worker():
        try:
            got.append(_fetch_bytes(url))
        except Exception:
            pass

    threads = [threading.Thread(target=worker) for _ in range(conns)]
    t = time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    if not got:
        raise RuntimeError("all parallel test connections failed")
    return single, sum(got) / max(time.monotonic() - t, 1e-6), len(got)
