# settings that shape how yt-dlp gets called: cookies, the retry plan and the blocked site routes
import concurrent.futures
import os
import time
import urllib.parse
import urllib.request

from .. import engine
from .. import network
from ..core import norm
from ..errors import explain, is_dns_error
from ..network import DnsBypassProxy, ROUTE_TTL, detect_local_tor, mask_proxy, parse_proxies, probe_route
from ..settings import PROFILES
from ..urls import is_youtube


class OptionsMixin:
    def _mark(self, label):
        tm = getattr(self._tls, "tm", None)
        if tm is not None:
            tm.append((label, time.monotonic()))

    def _tm_line(self):
        tm = getattr(self._tls, "tm", None) or []
        out = [f"{b[0]} {b[1] - a[1]:.2f}" for a, b in zip(tm, tm[1:])]
        return " | ".join(out) + (f" | TOTAL {tm[-1][1] - tm[0][1]:.2f} s" if len(tm) > 1 else "")

    def _st(self):
        s = self.settings
        proxies = parse_proxies(s["proxies"])
        return {
            "browser": s["browser"],
            "ipv4": s["ipv4"],
            "proxies": proxies,
            "auto_route": s["auto_route"],
            "proxy": "" if s["auto_route"] or not proxies else proxies[0],
        }

    def _cookies_ok(self, browser):
        b = str(browser).lower()
        now = time.monotonic()
        hit = self.cookie_cache.get(b)  # kept for 10 min so we dont reread the browsers cookies
        if hit and now - hit[0] < 600:
            return hit[1]
        try:
            self.cookie_jars[b] = engine.yt_dlp.cookies.extract_cookies_from_browser(b)
            ok = True
        except Exception as e:
            ok = False
            self.cookie_jars.pop(b, None)
            self.log(f"Couldn't read {b.title()}'s sign-in ({norm(e)[:120]}) - downloading without it.")
            if not hit:
                self.toast(
                    "warn",
                    f"Couldn't read {b.title()}'s sign-in (close {b.title()} and try again). Downloading without it for now.",
                )
        self.cookie_cache[b] = (now, ok)
        return ok

    def _base_opts(self, st):
        opts = {
            "color": {"stdout": "never", "stderr": "never"},
            "socket_timeout": 15,
            "cachedir": engine.YTDLP_CACHE,
            "js_runtimes": {"deno": {}, "node": {}, "bun": {}, "quickjs": {}},
        }
        if st["browser"] and st["browser"] != "none" and self._cookies_ok(st["browser"]):
            b = st["browser"].lower()
            opts["cookiesfrombrowser"] = (b,)
            if self.cookie_jars.get(b) is not None:
                opts["_ald_cookiejar"] = self.cookie_jars[b]
        if st["ipv4"]:
            opts["source_address"] = "0.0.0.0"
        if st.get("proxy"):
            opts["proxy"] = st["proxy"]
        return opts

    def _ydl(self, opts):
        opts = dict(opts)
        jar = opts.pop("_ald_cookiejar", None)
        ydl = engine.yt_dlp.YoutubeDL(opts)
        if jar is not None:
            ydl.cookiejar = jar  # already read once in _cookies_ok, dont let yt-dlp open the browser again
        return ydl

    def studio_threads(self):
        t = self.prof["threads"]
        try:
            busy = any(j.status in ("analyzing", "downloading", "processing") for j in self.jobs)
        except Exception:
            busy = False
        if busy:
            half = max(1, (os.cpu_count() or 2) // 2)
            return min(t, half) if t else half
        return t

    @property
    def prof(self):
        return PROFILES.get(self.settings.get("profile"), PROFILES["balanced"])

    def attempt_plan(self, url=None):
        # the plan goes from fast to careful, each entry is one retry in _download
        conns = self.prof["conns"]
        if url and not is_youtube(url):
            imp = {"impersonate": engine.impersonate_target()} if engine.HAVE_IMPERSONATE else {}
            plan = [
                (f"{conns} connections", {"concurrent_fragment_downloads": conns}),
                ("4 connections", {"concurrent_fragment_downloads": 4}),
            ]
            if imp:
                plan.append(("browser disguise", {"concurrent_fragment_downloads": 4, **imp}))
            plan.append(("one connection", {"concurrent_fragment_downloads": 1, **imp}))
            return plan
        clients = ["default", "tv", "web_safari"]

        # dashy makes yt-dlp split plain http formats into fragments so they can download in parallel
        def dashy(n, player_clients=None):
            yt = {"formats": ["dashy"]}
            if player_clients:
                yt["player_client"] = player_clients
            return {"concurrent_fragment_downloads": n, "extractor_args": {"youtube": yt}}

        wide = clients if (engine.js_runtime() and self.settings.get("find_4k")) else None
        # the retries use other clients than the first try, otherwise they would fail the same way
        retry_clients = ["mweb", "web_embedded", "android_vr"] if wide else clients
        return [
            (f"{conns} connections", dashy(conns, wide)),
            ("4 connections", dashy(4, wide)),
            ("other YouTube clients", dashy(4, retry_clients)),
            (
                "one connection",
                {"concurrent_fragment_downloads": 1, "extractor_args": {"youtube": {"player_client": retry_clients}}},
            ),
        ]

    def _dns_bypass_urls(self):
        with self.lock:
            for mode, _ in network.BYPASS_MODES:
                if mode not in self.dns_proxies:
                    self.dns_proxies[mode] = DnsBypassProxy(mode=mode)
                self.dns_proxies[mode].start()
            return {self.dns_proxies[m].url: lbl for m, lbl in network.BYPASS_MODES}

    def _apply_cached_route(self, url, st):
        if not st.get("auto_route"):
            return st
        r = self.routes.get(urllib.parse.urlparse(url).netloc.lower())
        if r and time.monotonic() - r["t"] < ROUTE_TTL:
            st = dict(st)
            st["proxy"] = r["proxy"]
        return st

    def _find_route(self, url, st, say=None):
        host = urllib.parse.urlparse(url).netloc.lower()
        self.routes.pop(host, None)
        self.route_failed = False  # friendly() uses this to tell blocked from just a broken name lookup
        bypass = self._dns_bypass_urls()
        cands = list(bypass) + [p for p in st.get("proxies", []) if p not in bypass]
        for t in detect_local_tor():
            if t not in cands:
                cands.append(t)
        label = lambda p: bypass.get(p) or mask_proxy(p)  # noqa: E731
        self.log(f"Can't reach {host} directly - testing {len(cands)} route(s) at the same time...")
        if say:
            say("Looking for a way through...")

        def one(p):
            try:
                return p, probe_route(url, p), None
            except Exception as e:
                return p, None, norm(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(cands))) as ex:
            results = list(ex.map(one, cands))
        good = []
        for p, score, err in results:
            if score is None:
                self.log(f"   no  {label(p)} - {err[:90]}")
            else:
                self.log(f"   yes {label(p)} - {score:.1f}s")
                good.append((score, p))
        if not good:
            self.route_failed = True
            self.log("No route got through: the connection itself is being cut (not just the name lookup).")
            return None
        score, best = min(good)
        self.log(f"Fastest route: {label(best)} ({score:.1f}s) - using it for {host}.")
        self.routes[host] = {"proxy": best, "t": time.monotonic()}
        return best

    def friendly(self, msg):
        if is_dns_error(msg):
            if network.internet_ok():
                if self.route_failed:
                    return (
                        "This site is blocked on your network",
                        "The app found the site's address and connected, but the connection was cut. Tor Browser, "
                        "a proxy or a VPN will get through.",
                        None,
                    )
                return (
                    "This site can't be reached from your network",
                    "Your internet works, but this site's address can't be looked up here. It's blocked by your "
                    "network, or the site is down. Check by opening the same link in your browser.",
                    None,
                )
            return ("No internet", "The connection dropped. The app carries on by itself when it's back.", "retry")
        return explain(msg)
