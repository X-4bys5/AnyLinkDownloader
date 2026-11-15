import collections
import hashlib
import glob
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time

from . import engine
from .core import DATA_DIR, NO_WINDOW, file_log, fmt_size, safe_name


STUDIO_DIR = os.path.join(DATA_DIR, "studio")
UPLOADS = os.path.join(STUDIO_DIR, "uploads")
WORK = os.path.join(STUDIO_DIR, "work")
THUMBS = os.path.join(STUDIO_DIR, "thumbs")
PROXIES = os.path.join(STUDIO_DIR, "proxy")

MEDIA_EXTS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".flv",
    ".wmv",
    ".ts",
    ".mts",
    ".m2ts",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".ogv",
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".wav",
    ".ogg",
    ".oga",
    ".opus",
    ".wma",
    ".aiff",
    ".aif",
}

MIN_CLIP = 0.04  # seconds, about one frame at 25fps


def _vint(f, keep_marker=False):
    b = f.read(1)
    if not b:
        return None, 0
    first = b[0]
    n = 1
    mask = 0x80
    while n <= 8 and not (first & mask):
        n += 1
        mask >>= 1
    if n > 8:
        raise ValueError("bad EBML number")
    rest = f.read(n - 1)
    if len(rest) != n - 1:
        return None, 0
    val = first if keep_marker else first & (mask - 1)
    for c in rest:
        val = (val << 8) | c
    unknown = (not keep_marker) and val == (1 << (7 * n)) - 1
    return (-1 if unknown else val), n


def free_ram_mb():
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [
                    ("l", ctypes.c_ulong),
                    ("load", ctypes.c_ulong),
                    ("tp", ctypes.c_ulonglong),
                    ("ap", ctypes.c_ulonglong),
                    ("tpf", ctypes.c_ulonglong),
                    ("apf", ctypes.c_ulonglong),
                    ("tv", ctypes.c_ulonglong),
                    ("av", ctypes.c_ulonglong),
                    ("ex", ctypes.c_ulonglong),
                ]

            m = MS()
            m.l = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return m.ap / 1048576.0
            return None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return None


# pulls keyframe times straight out of the mkv, much quicker than ffprobe going over every packet
def mkv_keyframes(path, limit_s=None):
    # elements we step into (segment, info, cluster, tracks, track entry, block group), the rest get skipped
    MASTER = {0x18538067, 0x1549A966, 0x1F43B675, 0x1654AE6B, 0xAE, 0xA0}
    scale, vtrack, cur_tc, track_no, track_type = 1000000, None, 0, None, None
    out, group_ref, group_pts = [], False, None
    with open(path, "rb", buffering=1 << 16) as f:
        if f.read(4) != b"\x1a\x45\xdf\xa3":
            raise ValueError("not Matroska")
        f.seek(0)
        ids = []
        while True:
            pos = f.tell()
            while ids and ids[-1][0] is not None and pos >= ids[-1][0]:
                eid_done = ids.pop()[1]
                if eid_done == 0xA0:
                    if group_pts is not None and not group_ref:
                        out.append(group_pts)
                    group_pts, group_ref = None, False
                elif eid_done == 0xAE:
                    if track_type == 1 and vtrack is None and track_no is not None:
                        vtrack = track_no
                    track_no, track_type = None, None
            eid, n1 = _vint(f, keep_marker=True)
            if eid is None:
                break
            size, n2 = _vint(f)
            if size is None:
                break
            if eid in MASTER:
                if size == -1:
                    raise ValueError("unknown-size element")
                ids.append((f.tell() + size, eid))
                continue
            if size == -1:
                raise ValueError("unknown-size element")
            end = f.tell() + size
            if eid == 0x2AD7B1:
                scale = int.from_bytes(f.read(size), "big")
            elif eid == 0xD7:
                track_no = int.from_bytes(f.read(size), "big")
            elif eid == 0x83:
                track_type = int.from_bytes(f.read(size), "big")
            elif eid == 0xE7:
                cur_tc = int.from_bytes(f.read(size), "big")
            elif eid in (0xA3, 0xA1):
                if vtrack is None:
                    raise ValueError("blocks before tracks")
                head = f.read(min(size, 8))
                tn = 0
                b0 = head[0]
                ln = 1
                m = 0x80
                while ln <= 8 and not (b0 & m):
                    ln += 1
                    m >>= 1
                tn = b0 & (m - 1)
                for c in head[1:ln]:
                    tn = (tn << 8) | c
                if tn == vtrack:
                    rel = int.from_bytes(head[ln : ln + 2], "big", signed=True)
                    flags = head[ln + 2]
                    # laced video is basically never used, bail out and let ffprobe handle the file
                    if flags & 0x06:
                        raise ValueError("laced video blocks")
                    t = (cur_tc + rel) * scale / 1e9
                    if eid == 0xA3:
                        if flags & 0x80:
                            out.append(t)
                    else:
                        # blocks inside a group have no keyframe flag, they only count as one if the group
                        # never gets a reference block (0xFB), checked when the group closes
                        group_pts = t
            elif eid == 0xFB:
                group_ref = True
            f.seek(end)
    if not out:
        raise ValueError("no keyframes found")
    return sorted(out)


class StudioError(Exception):
    pass


class Cancelled(Exception):
    pass


def _fmt(kind, ext, muxer, label, venc=None, aenc=None, vok=None, aok=None, hint=""):
    return dict(kind=kind, ext=ext, muxer=muxer, label=label, venc=venc, aenc=aenc, vok=vok, aok=aok, hint=hint)


_PCM = {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_u8"}
FORMATS = {
    "mp4": _fmt(
        "video",
        "mp4",
        "mp4",
        "MP4",
        "h264",
        "aac",
        {"h264", "hevc", "av1", "mpeg4"},
        {"aac", "mp3", "ac3", "eac3", "opus"},
        "Plays everywhere",
    ),
    "mp4h": _fmt(
        "video",
        "mp4",
        "mp4",
        "MP4 · H.265",
        "hevc",
        "aac",
        {"hevc"},
        {"aac", "mp3", "ac3", "eac3", "opus"},
        "About half the size, a few old players can't open it",
    ),
    "mkv": _fmt("video", "mkv", "matroska", "MKV", "h264", "aac", None, None, "Holds almost anything"),
    "webm": _fmt(
        "video", "webm", "webm", "WebM", "vp9", "opus", {"vp8", "vp9", "av1"}, {"opus", "vorbis"}, "For the web"
    ),
    "mov": _fmt(
        "video",
        "mov",
        "mov",
        "MOV",
        "h264",
        "aac",
        {"h264", "hevc", "mpeg4", "prores"},
        {"aac", "mp3", "alac", "ac3"} | _PCM,
        "Apple / editors",
    ),
    "gif": _fmt("video", "gif", "gif", "GIF", "gif", None, set(), set(), "Short silent loop"),
    "mp3": _fmt("audio", "mp3", "mp3", "MP3", None, "mp3", None, {"mp3"}, "Plays everywhere"),
    "m4a": _fmt("audio", "m4a", "ipod", "M4A (AAC)", None, "aac", None, {"aac", "alac"}, "Phones and Apple"),
    "flac": _fmt("audio", "flac", "flac", "FLAC", None, "flac", None, {"flac"}, "Lossless"),
    "wav": _fmt("audio", "wav", "wav", "WAV", None, "pcm_s16le", None, _PCM, "Uncompressed"),
    "opus": _fmt("audio", "opus", "opus", "Opus", None, "opus", None, {"opus"}, "Small and clean"),
    "ogg": _fmt("audio", "ogg", "ogg", "OGG", None, "vorbis", None, {"vorbis", "opus", "flac"}, "Open format"),
}
SAME_BY_EXT = {
    "mp4": "mp4",
    "m4v": "mp4",
    "mov": "mov",
    "mkv": "mkv",
    "webm": "webm",
    "mp3": "mp3",
    "m4a": "m4a",
    "flac": "flac",
    "wav": "wav",
    "opus": "opus",
    "ogg": "ogg",
    "oga": "ogg",
}
# crf values, lower is better and bigger. hevc adds 5 to these
VIDEO_Q = {"original": 20, "high": 18, "balanced": 22, "small": 27}
VP9_Q = {"original": 28, "high": 24, "balanced": 31, "small": 36}
AUDIO_K = {"original": 192, "high": 256, "balanced": 192, "small": 128}
OPUS_K = {"original": 128, "high": 160, "balanced": 128, "small": 96}
GIF_Q = {"original": (480, 15), "high": (640, 20), "balanced": (480, 15), "small": (320, 12)}
BROWSER_V = {"h264", "vp8", "vp9", "av1"}
BROWSER_A = {"aac", "mp3", "opus", "vorbis", "flac"}
MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".aac": "audio/aac",
}


def _tool(name):
    engine.bootstrap_path()
    return shutil.which(name) or name


BELOW_NORMAL = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS, so a long export doesnt make the pc lag


def _low(cmd):
    if os.name == "nt":
        return cmd, NO_WINDOW | BELOW_NORMAL
    nice = shutil.which("nice")
    return (([nice, "-n", "10"] + list(cmd)) if nice else cmd), NO_WINDOW


def _run(cmd, timeout=60, low=True):
    cmd, flags = _low(cmd) if low else (cmd, NO_WINDOW)
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags, stdin=subprocess.DEVNULL)


def _rate(s):
    try:
        a, b = str(s).split("/")
        return float(a) / float(b) if float(b) else 0.0
    except Exception:
        try:
            return float(s)
        except Exception:
            return 0.0


def _flt(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def probe(path):
    try:
        r = _run([_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], 90)
        j = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except FileNotFoundError:
        raise StudioError("ffmpeg is missing, so Studio can't open files.")
    except Exception:
        raise StudioError("This file can't be read. It may be damaged or not a video or audio file.")
    streams = j.get("streams") or []
    fmt = j.get("format") or {}
    # mp3/m4a cover art shows up as a video stream, dont count it as video
    vs = [s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")]
    as_ = [s for s in streams if s.get("codec_type") == "audio"]
    if not vs and not as_:
        raise StudioError("There's no video or audio in this file.")
    v = vs[0] if vs else None
    a = as_[0] if as_ else None
    dur = _flt(fmt.get("duration")) or _flt((v or a).get("duration"))
    if dur <= 0:
        raise StudioError("Couldn't work out how long this file is.")
    info = {
        "duration": dur,
        "start": _flt(fmt.get("start_time")),
        "size": int(_flt(fmt.get("size"))),
        "bitrate": int(_flt(fmt.get("bit_rate"))),
        "container": fmt.get("format_name", ""),
        "has_video": bool(v),
        "has_audio": bool(a),
        "audio_tracks": len(as_),
        "vcodec": v.get("codec_name") if v else None,
        "acodec": a.get("codec_name") if a else None,
        "pix_fmt": v.get("pix_fmt") if v else None,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "fps_str": "30",
        # containers we trust for fast seeking and lossless cuts
        "seek_ok": any(k in (fmt.get("format_name") or "") for k in ("mp4", "matroska", "webm")),
        "rotation": 0,
        "sample_rate": int(_flt(a.get("sample_rate"))) if a else 0,
        "channels": int(a.get("channels") or 0) if a else 0,
        "hdr": False,
    }
    if v:
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        # rotation is a tag on older files and in side data on newer ones
        rot = 0
        try:
            rot = int(float((v.get("tags") or {}).get("rotate") or 0))
        except Exception:
            pass
        for sd in v.get("side_data_list") or []:
            if "rotation" in sd:
                rot = int(round(_flt(sd["rotation"])))
        rot %= 360
        if rot in (90, 270):
            w, h = h, w
        fr = v.get("avg_frame_rate") or "0/0"
        fps = _rate(fr) or _rate(v.get("r_frame_rate"))
        # some files report 0/0 or silly frame rates, assume 30
        if not (1 <= fps <= 240):
            fps, fr = 30.0, "30"
        vs0 = v.get("start_time")
        info["vstart"] = _flt(vs0, None) if vs0 not in (None, "", "N/A") else None
        info.update(
            width=w,
            height=h,
            fps=round(fps, 6),
            fps_str=fr if _rate(v.get("avg_frame_rate")) else (v.get("r_frame_rate") or "30"),
            rotation=rot,
            hdr=(v.get("color_transfer") in ("smpte2084", "arib-std-b67")),
        )
    return info


class Studio:
    def __init__(self, emit=None, log=None, allow=None, threads=None):
        self.emit = emit or (lambda kind, data: None)
        self.threads = threads or (lambda: 0)
        self.log = log or file_log
        self.allow = allow if allow is not None else set()
        self.lock = threading.RLock()
        self.sources = {}
        self.by_path = {}
        self.picked_dirs = set()
        self.job = None
        self.last_event = None
        self._deferred_rm = []
        # kept small so thumbnails and waveforms dont eat the cpu while someone is editing
        self._thumb_sem = threading.Semaphore(2)
        self._wave_sem = threading.Semaphore(1)
        self._scan_sem = threading.Semaphore(1)
        self._prep_lock = threading.Lock()
        self._caps = None
        threading.Thread(target=self._tidy, daemon=True, name="studio-tidy").start()

    def _tidy(self):
        try:
            for d in (UPLOADS, WORK):
                shutil.rmtree(d, ignore_errors=True)
            cutoff = time.time() - 14 * 86400  # thumbs and proxies get cleared after 2 weeks
            for base in (THUMBS, PROXIES):
                if not os.path.isdir(base):
                    continue
                for n in os.listdir(base):
                    p = os.path.join(base, n)
                    try:
                        if os.path.getmtime(p) < cutoff:
                            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
                    except OSError:
                        pass
        except Exception:
            pass

    @staticmethod
    def caps():
        out = getattr(Studio, "_caps_cache", None)
        if out is not None:
            return out
        have = set()
        try:
            r = _run([_tool("ffmpeg"), "-hide_banner", "-encoders"], 30)
            for line in r.stdout.decode("utf-8", "replace").splitlines():
                p = line.split()
                if len(p) >= 2 and re.match(r"^[VAS][.A-Z]{5}$", p[0]):
                    have.add(p[1])
        except Exception:
            pass
        need = {
            "mp4": ["libx264", "aac"],
            "mp4h": ["libx265", "aac"],
            "mkv": ["libx264", "aac"],
            "webm": ["libvpx-vp9", "libopus"],
            "mov": ["libx264", "aac"],
            "gif": ["gif"],
            "mp3": ["libmp3lame"],
            "m4a": ["aac"],
            "flac": ["flac"],
            "wav": ["pcm_s16le"],
            "opus": ["libopus"],
            "ogg": ["libvorbis"],
        }
        res = {}
        for k, enc in need.items():
            ok = all(e in have for e in enc) if have else True
            # no libvorbis is fine, ogg falls back to opus when its missing
            if k == "ogg" and not ok and "libopus" in have:
                ok = True
            res[k] = ok
        Studio._caps_cache = res
        return res

    def register(self, path, upload=False):
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise StudioError("That file isn't there any more.")
        with self.lock:
            sid = self.by_path.get(path)
            if sid and sid in self.sources:
                return self.public(sid)
        info = probe(path)
        st = os.stat(path)
        sid = secrets.token_hex(5)
        key = hashlib.sha1(f"{path}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8", "replace")).hexdigest()[:16]
        src = {
            "id": sid,
            "path": path,
            "name": os.path.basename(path),
            "info": info,
            "upload": upload,
            "key": key,
            "kf": None,
            "kf_lock": threading.Lock(),
            "kf_run": False,
            "kf_failed": False,
            "kf_done": threading.Event(),
            "preview": None,
            "prep": "idle",
            "prep_pct": 0.0,
            "mtime": st.st_mtime,
            "size": st.st_size,
        }
        src["mode"] = self._preview_mode(src)
        with self.lock:
            other = self.by_path.get(path)
            if other and other in self.sources:
                return self.public(other)
            self.sources[sid] = src
            self.by_path[path] = sid
        threading.Thread(target=self._warm, args=(sid,), daemon=True, name="studio-warm").start()
        return self.public(sid)

    def public(self, sid):
        s = self._src(sid)
        i = s["info"]
        return {
            "id": sid,
            "name": s["name"],
            "path": s["path"] if not s["upload"] else "",
            "upload": s["upload"],
            "duration": i["duration"],
            "width": i["width"],
            "height": i["height"],
            "fps": i["fps"],
            "vcodec": i["vcodec"],
            "acodec": i["acodec"],
            "has_video": i["has_video"],
            "has_audio": i["has_audio"],
            "audio_tracks": i["audio_tracks"],
            "size": s["size"],
            "hdr": i["hdr"],
            "rotation": i["rotation"],
            "mode": s["mode"],
            "prep": s["prep"],
            "kind": "video" if i["has_video"] else "audio",
        }

    def _src(self, sid):
        s = self.sources.get(sid)
        if not s:
            raise StudioError("That file was closed. Open it again.")
        if not os.path.isfile(s["path"]):
            raise StudioError(f"{s['name']} has been moved or deleted.")
        return s

    def close(self, sid):
        with self.lock:
            s = self.sources.pop(sid, None)
            if s:
                self.by_path.pop(s["path"], None)
            job = self.job
            in_use = bool(
                s
                and job
                and job.get("running")
                and job.get("plan")
                and any(g["src"]["id"] == sid for g in job["plan"]["segs"])
            )
            if s and s["upload"] and in_use:
                # export is still reading this upload, delete it after the export instead
                self._deferred_rm.append(s["path"])
        if s and s["upload"] and not in_use:
            try:
                os.remove(s["path"])
            except OSError:
                pass
        return True

    def _warm(self, sid):
        s = self.sources.get(sid)
        if s and s["mode"] != "direct":
            self._prepare(sid)
        self.emit("studio", {"t": "ready", "id": sid})
        if s and s["mode"] == "direct":
            time.sleep(2.0)  # let the player start loading before the scan hits the disk
        try:
            self.keyframes(sid)
        except Exception as e:
            self.log(f"Studio keyframe scan failed: {e}")
        self.emit("studio", {"t": "kf", "id": sid})

    def _kf_start(self, s):
        with s["kf_lock"]:
            if s["kf"] is not None or s["kf_run"] or s["kf_failed"]:
                return
            s["kf_run"] = True
            s["kf_done"].clear()
        threading.Thread(target=self._kf_scan, args=(s,), daemon=True, name="studio-kf").start()

    def _kf_scan_mkv(self, s):
        try:
            i = s["info"]
            if not i.get("has_video"):
                return None
            with open(s["path"], "rb") as f:
                if f.read(4) != b"\x1a\x45\xdf\xa3":
                    return None
            ts = mkv_keyframes(s["path"])
            # last keyframe way past the end means we misread something, use the ffprobe path instead
            if i["duration"] and ts[-1] > i["duration"] + i["start"] + 2.0:
                return None
            out = sorted(round(t - i["start"], 4) for t in ts)
            if out and out[0] > 0.5:
                out.insert(0, 0.0)
            r = _run(
                [
                    _tool("ffprobe"),
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    # only the first 20s, we just need the pts/dts gap
                    "-read_intervals",
                    "%+20",
                    "-show_entries",
                    "packet=pts_time,dts_time,flags",
                    "-of",
                    "csv=p=0",
                    s["path"],
                ],
                120,
            )
            # b-frame delay, how far pts sits after dts on a keyframe (_run_copy uses it as lead)
            delay = 0.0
            for line in r.stdout.decode("utf-8", "replace").splitlines():
                p = line.strip().split(",")
                if len(p) >= 3 and "K" in p[2]:
                    t = _flt(p[0], None) if p[0] not in ("", "N/A") else None
                    d = _flt(p[1], None) if p[1] not in ("", "N/A") else None
                    if t is not None and d is not None and d <= t:
                        delay = max(delay, t - d)
            return out, min(delay, 1.0)
        except Exception as e:
            self.log(f"Studio: quick keyframe read didn't work for {s['name']} ({str(e)[:80]}), using ffprobe")
            return None

    def _kf_scan(self, s):
        try:
            with self._scan_sem:
                i = s["info"]
                fast = self._kf_scan_mkv(s) if not os.environ.get("ALD_NO_MKVSCAN") else None
                if fast is not None:
                    s["kf"], s["kf_delay"] = fast
                    return
                r = _run(
                    [
                        _tool("ffprobe"),
                        "-v",
                        "error",
                        "-select_streams",
                        "v:0",
                        "-show_entries",
                        "packet=pts_time,dts_time,flags",
                        "-of",
                        "csv=p=0",
                        s["path"],
                    ],
                    900,
                )
                out, delay = [], 0.0
                for line in r.stdout.decode("utf-8", "replace").splitlines():
                    p = line.strip().split(",")
                    if len(p) < 3 or "K" not in p[2]:
                        continue
                    t = _flt(p[0], None) if p[0] not in ("", "N/A") else None
                    d = _flt(p[1], None) if p[1] not in ("", "N/A") else None
                    if t is None:
                        t = d
                    if t is not None:
                        out.append(round(t - i["start"], 4))
                        if d is not None and d <= t:
                            delay = max(delay, t - d)
                out.sort()
                if out and out[0] > 0.5:
                    out.insert(0, 0.0)
                s["kf"] = out
                s["kf_delay"] = min(delay, 1.0)
        except Exception as e:
            s["kf_failed"] = True
            self.log(f"Studio keyframe scan failed for {s['name']}: {e}")
        finally:
            s["kf_run"] = False
            s["kf_done"].set()

    def keyframes(self, sid, wait=None):
        s = self._src(sid)
        if s["kf"] is not None:
            return s["kf"]
        if not s["info"]["has_video"]:
            s["kf"] = []
            return s["kf"]
        self._kf_start(s)
        if wait is None or wait > 0:
            s["kf_done"].wait(wait)
        return s["kf"]

    # direct = play the original, remux = copy the streams into a clean mp4, proxy = small 480p re-encode
    def _preview_mode(self, s):
        i = s["info"]
        if os.environ.get("ALD_STUDIO_PROXY"):
            return "proxy"
        ext = os.path.splitext(s["path"])[1].lower()
        # a start offset throws the player timeline off so it never plays direct
        if abs(i["start"]) > 0.1:
            return "remux" if self._copyable_preview(i) else "proxy"
        if not i["has_video"]:
            a_ok = i["acodec"] in BROWSER_A or (i["acodec"] or "").startswith("pcm_")
            if a_ok and ext in (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".oga", ".opus", ".aac", ".webm"):
                return "direct"
            return "proxy"
        # browsers only play 8 bit 4:2:0 here, and 4096 wide is the most we try directly
        v_ok = (
            i["vcodec"] in BROWSER_V and (i["pix_fmt"] or "yuv420p") in ("yuv420p", "yuvj420p") and i["width"] <= 4096
        )
        a_ok = (not i["has_audio"]) or i["acodec"] in BROWSER_A
        if not (v_ok and a_ok):
            return "proxy"
        if ext in (".mp4", ".m4v", ".mov") and "mp4" in i["container"] or ext == ".mov" and "mov" in i["container"]:
            if i["vcodec"] in ("h264", "av1", "vp9") and i["acodec"] in (None, "aac", "mp3", "opus", "flac"):
                return "direct"
        if ext == ".webm" and i["vcodec"] in ("vp8", "vp9", "av1") and i["acodec"] in (None, "opus", "vorbis"):
            return "direct"
        return "remux" if self._copyable_preview(i) else "proxy"

    @staticmethod
    def _copyable_preview(i):
        return (
            i["vcodec"] in ("h264", "vp9", "av1")
            and i["pix_fmt"] in ("yuv420p", "yuvj420p")
            and (not i["has_audio"] or i["acodec"] in ("aac", "mp3", "opus"))
        )

    def _prep_workers(self, info, mode, webm):
        if mode != "proxy" or webm or not info["has_video"] or info["duration"] < 40 or os.environ.get("ALD_NO_SPLIT"):
            return 1
        cores = os.cpu_count() or 2
        busy = self.threads() and self.threads() <= max(1, cores // 2)
        n = max(1, min(4, cores // 3, 2 if busy else 4))
        free = free_ram_mb()
        # rough ram per slice, 120mb plus about 40 frames of yuv420 (w*h*1.5 bytes each)
        per = 120 + (info.get("width") or 1920) * (info.get("height") or 1080) * 1.5 * 40 / 1048576.0
        if free is None:
            free = 3000.0  # cant tell, assume 3gb
        return max(1, min(n, int(free * 0.2 // per)))

    def _prepare_split(self, s, build, ok, part, n, pct):
        dur = s["info"]["duration"]
        info = s["info"]
        fps = max(1.0, info.get("fps") or 25.0)
        vs = info.get("vstart")
        v0 = vs if vs is not None else info["start"]
        # slice borders sit half a frame off the frame grid so no frame lands in two slices
        edge = [None] + [v0 + (round((dur * k / n) * fps) - 0.5) / fps for k in range(1, n)] + [v0 + dur + 5.0]
        files = [f"{part}.c{k}.mp4" for k in range(n)]
        done = [0.0] * n
        errs = []

        def work(k):
            try:
                lo, hi = edge[k], edge[k + 1]
                L = max(0.1, min(dur, hi - (lo if lo is not None else v0)))

                def cb(p, **_):
                    done[k] = p * L
                    pct(min(0.99, sum(done) / dur))

                rc, err = self._ff(build(files[k], (lo, hi)), L, cb, None)
                # gpu decode fails on some files, retry the slice on the cpu
                if rc != 0 or not ok(files[k]):
                    rc, err = self._ff(build(files[k], (lo, hi), use_hw=False), L, cb, None)
                if rc != 0 or not ok(files[k]):
                    errs.append(err or "slice failed")
                done[k] = L
            except Exception as e:
                errs.append(str(e))

        ts = [threading.Thread(target=work, args=(k,), daemon=True, name="studio-slice") for k in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        try:
            if errs:
                raise StudioError(errs[0])
            lst = part + ".txt"
            with open(lst, "w", encoding="utf-8") as f:
                for fn in files:
                    # concat list wants forward slashes and the quotes escaped like this
                    f.write("file '" + fn.replace("\\", "/").replace("'", "'\\''") + "'\n")
            rc, err = self._ff(
                [
                    _tool("ffmpeg"),
                    "-hide_banner",
                    "-nostdin",
                    "-y",
                    "-v",
                    "error",
                    "-progress",
                    "pipe:1",
                    "-nostats",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    lst,
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    "-f",
                    "mp4",
                    part,
                ],
                dur,
                lambda p, **_: None,
                None,
            )
            if rc != 0 or not ok(part):
                raise StudioError(err or "join failed")
        finally:
            for fn in files + [part + ".txt"]:
                try:
                    os.remove(fn)
                except OSError:
                    pass

    def _prepare(self, sid):
        s = self.sources.get(sid)
        if not s:
            return
        with self._prep_lock:
            if sid not in self.sources:
                return
            i = s["info"]
            webm = bool(os.environ.get("ALD_STUDIO_PROXY"))
            ext = ".webm" if webm else (".m4a" if not i["has_video"] else ".mp4")
            os.makedirs(PROXIES, exist_ok=True)
            out = os.path.join(PROXIES, f"{s['key']}-{s['mode']}{ext}")
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                s["preview"], s["prep"] = out, "ready"
                return
            s["prep"], s["prep_pct"] = "working", 0.0
            self.emit("studio", {"t": "prep", "id": sid, "state": "working", "pct": 0})
            part = out + ".part"
            can_hw = bool(i["has_video"] and s["mode"] != "remux" and not os.environ.get("ALD_NO_HWACCEL"))

            def build(dst, ss=None, use_hw=True):
                cmd = [
                    _tool("ffmpeg"),
                    "-hide_banner",
                    "-nostdin",
                    "-y",
                    "-v",
                    "error",
                    "-progress",
                    "pipe:1",
                    "-nostats",
                    *(["-threads", str(self.threads())] if self.threads() else []),
                    *(["-hwaccel", "auto"] if (can_hw and use_hw) else []),
                    # seek 1.5s early and keep the timestamps, select below trims to the exact frame
                    *(
                        ["-ss", f"{max(0.0, (ss[0] or 0.0) - 1.5 - i['start']):.3f}", "-copyts"]
                        if ss is not None
                        else []
                    ),
                    "-i",
                    s["path"],
                ]
                if not i["has_video"]:
                    cmd += ["-vn", "-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-f", "ipod"]
                elif s["mode"] == "remux":
                    cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-sn", "-dn", "-f", "mp4"]
                else:
                    # 480p max, -2 keeps the width even for x264
                    vf = "scale=-2:'min(480,ih)',format=yuv420p"
                    if ss is not None:
                        lo, hi = ss
                        sel = (
                            "select='"
                            + (f"gte(t\\,{lo:.5f})*" if lo is not None and lo > 0 else "")
                            + f"lt(t\\,{hi:.5f})'"
                        )
                        vf = f"{sel},setpts=PTS-STARTPTS," + vf
                        cmd += [
                            "-af",
                            (
                                "atrim="
                                + (f"start={lo:.5f}:" if lo is not None else "")
                                + f"end={hi:.5f},asetpts=PTS-STARTPTS"
                            ),
                        ]
                    if webm:
                        cmd += [
                            "-map",
                            "0:v:0",
                            "-map",
                            "0:a:0?",
                            "-vf",
                            vf,
                            "-c:v",
                            "libvpx",
                            "-deadline",
                            "realtime",
                            "-cpu-used",
                            "8",
                            "-b:v",
                            "900k",
                            "-g",
                            "12",
                            "-c:a",
                            "libopus",
                            "-b:a",
                            "64k",
                            "-f",
                            "webm",
                        ]
                    else:
                        # preview only needs to scrub smoothly, so ultrafast + fastdecode
                        # and a keyframe every 12 frames
                        cmd += [
                            "-map",
                            "0:v:0",
                            "-map",
                            "0:a:0?",
                            "-vf",
                            vf,
                            "-c:v",
                            "libx264",
                            "-preset",
                            "ultrafast",
                            "-fps_mode",
                            "passthrough",
                            "-tune",
                            "fastdecode",
                            "-crf",
                            "28",
                            "-g",
                            "12",
                            "-keyint_min",
                            "12",
                            "-sc_threshold",
                            "0",
                            "-c:a",
                            "aac",
                            "-b:a",
                            "96k",
                            "-movflags",
                            "+faststart",
                            "-f",
                            "mp4",
                        ]
                return cmd + [dst]

            def ok(f):
                return os.path.isfile(f) and os.path.getsize(f) > 0

            def pct(p, **_):
                s["prep_pct"] = p
                self.emit("studio", {"t": "prep", "id": sid, "state": "working", "pct": round(p, 3)})

            def single():
                rc, err = self._ff(build(part), i["duration"], pct, None)
                if (rc != 0 or not ok(part)) and can_hw:
                    self.log(f"Studio: hardware decoding didn't work for {s['name']}, using the processor instead")
                    rc, err = self._ff(build(part, use_hw=False), i["duration"], pct, None)
                if rc != 0 or not ok(part):
                    raise StudioError(err or "preview failed")

            try:
                n = self._prep_workers(i, s["mode"], webm)
                if n > 1:
                    try:
                        self._prepare_split(s, build, ok, part, n, pct)
                    except Exception as e:
                        self.log(f"Studio: split preview didn't work ({str(e)[:120]}), doing it in one piece")
                        for f in glob.glob(part + ".c*"):
                            try:
                                os.remove(f)
                            except OSError:
                                pass
                        single()
                else:
                    single()
                os.replace(part, out)
                s["preview"], s["prep"] = out, "ready"
            except Exception as e:
                self.log(f"Studio preview failed for {s['name']}: {e}")
                s["prep"] = "failed"
                try:
                    os.remove(part)
                except OSError:
                    pass
            self.emit("studio", {"t": "prep", "id": sid, "state": s["prep"], "pct": 1})

    def force_proxy(self, sid):
        s = self._src(sid)
        if s["mode"] == "proxy" and s["prep"] in ("working", "ready"):
            return self.public(sid)
        s["mode"], s["prep"], s["preview"], s["prep_pct"] = "proxy", "working", None, 0.0
        threading.Thread(target=self._prepare, args=(sid,), daemon=True, name="studio-proxy").start()
        return self.public(sid)

    def media_file(self, sid):
        s = self._src(sid)
        path = s["path"] if s["mode"] == "direct" else s["preview"]
        if not path or not os.path.isfile(path):
            return None
        ext = os.path.splitext(path)[1].lower()
        return path, MIME.get(ext, "video/mp4")

    def frame(self, sid, t, h=90):
        s = self._src(sid)
        i = s["info"]
        if not i["has_video"]:
            return None
        h = max(24, min(720, int(h)))
        t = round(max(0.0, min(_flt(t), max(0.0, i["duration"] - 0.05))), 1)
        d = os.path.join(THUMBS, s["key"])
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, f"{int(round(t * 10))}_{h}.jpg")
        if not os.path.isfile(fp):
            with self._thumb_sem:
                if not os.path.isfile(fp):
                    tmp = fp + f".{secrets.token_hex(3)}.tmp"
                    # seeking right at the end can give nothing back, so the second try goes 0.6s earlier
                    for tt in (t, max(0.0, t - 0.6)):
                        cmd = [
                            _tool("ffmpeg"),
                            "-hide_banner",
                            "-nostdin",
                            "-v",
                            "error",
                            "-ss",
                            f"{tt:.3f}",
                            "-i",
                            s["path"],
                            "-frames:v",
                            "1",
                            "-vf",
                            f"scale=-2:{h}",
                            "-c:v",
                            "mjpeg",
                            "-q:v",
                            "5",
                            "-f",
                            "image2",
                            "-y",
                            tmp,
                        ]
                        try:
                            _run(cmd, 30)
                        except Exception:
                            pass
                        if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                            break
                    if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                        os.replace(tmp, fp)
                    else:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
        try:
            with open(fp, "rb") as f:
                return f.read()
        except OSError:
            return None

    def wave(self, sid, w=1600, h=80):
        s = self._src(sid)
        if not s["info"]["has_audio"]:
            return None
        w, h = max(200, min(4000, int(w))), max(24, min(400, int(h)))
        d = os.path.join(THUMBS, s["key"])
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, f"wave_{w}x{h}.png")
        if not os.path.isfile(fp):
            with self._wave_sem:
                if not os.path.isfile(fp):
                    tmp = fp + f".{secrets.token_hex(3)}.tmp"
                    # sqrt scale so quiet parts still show up, colorkey turns the black background
                    # transparent
                    fc = (
                        f"[0:a:0]aformat=channel_layouts=mono,showwavespic=s={w}x{h}:colors=white:scale=sqrt,"
                        f"format=rgba,colorkey=black:0.08:0.1[o]"
                    )
                    try:
                        _run(
                            [
                                _tool("ffmpeg"),
                                "-hide_banner",
                                "-nostdin",
                                "-v",
                                "error",
                                "-i",
                                s["path"],
                                "-filter_complex",
                                fc,
                                "-map",
                                "[o]",
                                "-frames:v",
                                "1",
                                "-c:v",
                                "png",
                                "-f",
                                "image2",
                                "-y",
                                tmp,
                            ],
                            300,
                        )
                    except Exception:
                        pass
                    if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                        os.replace(tmp, fp)
                    else:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
        try:
            with open(fp, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _same_format(self, info, path):
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if info["has_video"]:
            f = SAME_BY_EXT.get(ext)
            return f if f in ("mp4", "mov", "mkv", "webm") else "mp4"
        f = SAME_BY_EXT.get(ext)
        return f if f in ("mp3", "m4a", "flac", "wav", "opus", "ogg") else "m4a"

    def build_plan(self, spec, kf_wait=0.0):
        pending = False
        segs_in = spec.get("segments") or []
        if not segs_in:
            raise StudioError("Add a clip to the timeline first.")
        if len(segs_in) > 400:
            raise StudioError("That's a lot of clips. Try exporting in two parts.")
        quality = spec.get("quality") if spec.get("quality") in VIDEO_Q else "original"
        mode = "fast" if spec.get("mode") == "fast" else "exact"
        maxh = int(_flt(spec.get("maxh"), 0))
        noaudio = bool(spec.get("noaudio"))

        segs = []
        for n, s in enumerate(segs_in):
            src = self._src(str(s.get("src")))
            i = src["info"]
            a = max(0.0, _flt(s.get("in")))
            b = min(i["duration"], _flt(s.get("out"), i["duration"]))
            if b - a < MIN_CLIP:
                raise StudioError(f"Clip {n + 1} is too short to export.")
            rot = int(_flt(s.get("rot"))) % 360
            if rot not in (0, 90, 180, 270):
                rot = 0
            segs.append(
                {
                    "src": src,
                    "info": i,
                    "in": a,
                    "out": b,
                    "rot": rot,
                    "hflip": bool(s.get("hflip")),
                    "vflip": bool(s.get("vflip")),
                    "mute": bool(s.get("mute")),
                }
            )

        kinds = {bool(g["info"]["has_video"]) for g in segs}
        if len(kinds) > 1:
            raise StudioError("Audio-only files can't be mixed with videos. Put them in separate projects.")
        has_video = kinds.pop()

        fid = spec.get("format") or "same"
        if fid == "same":
            fid = self._same_format(segs[0]["info"], segs[0]["src"]["path"])
        if fid not in FORMATS:
            raise StudioError("Pick an output format.")
        F = FORMATS[fid]
        if not self.caps().get(fid, True):
            raise StudioError(f"This computer's ffmpeg can't make {F['label']}.")
        if F["kind"] == "video" and not has_video:
            raise StudioError("That's an audio file, so pick an audio format (MP3, M4A, FLAC...).")
        out_video = F["kind"] == "video"
        gif = fid == "gif"

        any_audio = any(g["info"]["has_audio"] and not g["mute"] for g in segs)
        want_audio = bool(any_audio and not noaudio and not gif)
        transformed = any(g["rot"] or g["hflip"] or g["vflip"] for g in segs)
        edited = (
            transformed
            or len(segs) > 1
            or any(g["in"] > 0.001 or g["out"] < g["info"]["duration"] - 0.001 for g in segs)
        )
        notes, why = [], []

        # can this be done without re-encoding at all
        copy = True
        if quality != "original":
            copy = False
            why.append("you chose a quality / size setting")
        if maxh:
            copy = False
            why.append("you chose a smaller resolution")
        if gif:
            copy = False
        if transformed:
            copy = False
            why.append("something is rotated or flipped")
        muted = [g for g in segs if g["mute"] or not g["info"]["has_audio"]]
        if want_audio and muted:
            copy = False
            why.append("some clips have no sound")

        def accepts(g):
            i = g["info"]
            if out_video and (F["vok"] is not None) and i["vcodec"] not in F["vok"]:
                return False
            if want_audio and F["aok"] is not None and i["acodec"] not in F["aok"]:
                return False
            if not out_video and (not i["has_audio"] or (F["aok"] is not None and i["acodec"] not in F["aok"])):
                return False
            return True

        if copy and not all(accepts(g) for g in segs):
            copy = False
            why.append(f"it isn't in a format {F['label']} can hold as-is")
        # concat copy only works if every clip has the exact same stream setup
        if copy and len(segs) > 1:
            sig = {
                (
                    g["info"]["vcodec"],
                    g["info"]["width"],
                    g["info"]["height"],
                    g["info"]["pix_fmt"],
                    round(g["info"]["fps"], 2),
                    g["info"]["acodec"],
                    g["info"]["sample_rate"],
                    g["info"]["channels"],
                )
                for g in segs
            }
            if len(sig) > 1:
                copy = False
                why.append("the clips differ in size, speed or sound format")

        eff = []
        for g in segs:
            a, b = g["in"], g["out"]
            ea, eb, shift = a, b, 0.0
            if has_video:
                fps = g["info"]["fps"] or 25.0
                tol = 0.5 / fps
                if copy and not g["info"]["seek_ok"] and a > tol:
                    copy = False
                    why.append(
                        "this kind of file ("
                        + os.path.splitext(g["src"]["name"])[1].lstrip(".").upper()
                        + ") can't be cut losslessly in the middle"
                    )
                if copy and (a > tol or b < g["info"]["duration"] - tol * 2):
                    kfs = self.keyframes(g["src"]["id"], wait=kf_wait)
                    if kfs is None:
                        copy = False
                        if g["src"]["kf_failed"]:
                            why.append("this file's keyframes couldn't be read")
                        else:
                            pending = True
                            why.append("the file's cut points are still being read")
                    elif not kfs and a > tol:
                        copy = False
                        why.append("this file has no keyframes to cut on")
                if copy:
                    kfs = g["src"]["kf"] or []
                    ktol = (1.0 / fps) if mode == "fast" else tol
                    prev = 0.0
                    for k in kfs:
                        if k <= a + ktol:
                            prev = k
                        else:
                            break
                    on_key = abs(prev - a) <= tol or a <= tol
                    # fast snaps back to the keyframe before the cut and keeps the gap as the shift
                    if mode == "fast":
                        ea, shift = prev, max(0.0, a - prev)
                        if a <= tol:
                            ea, shift = 0.0, 0.0
                    elif not on_key:  # exact only copies if the cut already sits on a keyframe
                        copy = False
                        why.append("the start of a cut isn't on a keyframe")
                    elif a > tol:
                        ea = prev
                    if mode == "exact" and copy:
                        at_end = b >= g["info"]["duration"] - tol * 2
                        if not at_end:
                            copy = False
                            why.append("the end of a cut isn't on a keyframe")
            eff.append((ea, eb, shift))
        if not copy:
            eff = [(g["in"], g["out"], 0.0) for g in segs]

        canvas = None
        if has_video and out_video and not copy:
            g0 = segs[0]
            w, h = g0["info"]["width"], g0["info"]["height"]
            if g0["rot"] in (90, 270):
                w, h = h, w
            cap_h = maxh
            cap_w = GIF_Q[quality][0] if gif else 0
            if cap_h and h > cap_h:
                w, h = w * cap_h / h, cap_h
            if cap_w and w > cap_w:
                w, h = cap_w, h * cap_w / w
            # x264 wants even sizes
            canvas = (max(2, int(round(w / 2)) * 2), max(2, int(round(h / 2)) * 2))

        fps_out = None
        if has_video and out_video and not copy:
            g0 = segs[0]["info"]
            fps_out = g0["fps_str"]
            if gif:
                fps_out = str(GIF_Q[quality][1])

        out_dur = sum(b - a for a, b, _ in eff)
        total_src = sum(g["info"]["duration"] for g in segs) or 1

        est = 0
        if copy:
            for g, (a, b, _) in zip(segs, eff):
                est += g["src"]["size"] * (b - a) / max(g["info"]["duration"], 0.1)
            if not out_video and has_video:
                est = sum(((g["info"]["bitrate"] or 128000) * 0.1) / 8 * (b - a) for g, (a, b, _) in zip(segs, eff))
        else:
            if out_video:
                src_br = (sum(g["src"]["size"] for g in segs) * 8 / total_src) or 4_000_000
                # rough share of the source bitrate each quality lands on, just guesses
                f = {"original": 0.8, "high": 0.9, "balanced": 0.5, "small": 0.3}[quality]
                if fid == "mp4h":
                    f *= 0.6
                if fid == "webm":
                    f *= 0.8
                if canvas:
                    g0 = segs[0]["info"]
                    f *= min(1.0, (canvas[0] * canvas[1]) / max(1, g0["width"] * g0["height"]))
                est = (src_br * f + (192_000 if want_audio else 0)) * out_dur / 8
                if gif:
                    est = canvas[0] * canvas[1] * GIF_Q[quality][1] * out_dur * 0.05
            else:
                k = {
                    "mp3": AUDIO_K[quality],
                    "m4a": AUDIO_K[quality],
                    "opus": OPUS_K[quality],
                    "ogg": AUDIO_K[quality],
                    "flac": 700,
                    "wav": 1411,
                }.get(fid, 192)
                est = k * 1000 / 8 * out_dur

        if copy:
            notes.append("Lossless: nothing is re-encoded, so quality is untouched and it finishes in moments.")
        if mode == "fast" and copy:
            sh = [s for _, _, s in eff if s > 0.02]
            if sh:
                notes.append(
                    f"Lossless cuts start on the nearest keyframe, up to {max(sh):.2f} s earlier than where you cut."
                )
        if (
            not copy
            and out_video
            and F["venc"] not in ("gif",)
            and why
            and quality == "original"
            and not maxh
            and edited
        ):
            notes.append("Re-encoded for frame-accurate cuts at high quality" + (f" ({why[0]})" if why else "") + ".")
        if segs[0]["info"]["hdr"] and not copy and out_video and fid not in ("mp4h", "mkv"):
            notes.append("This looks like HDR video. Converting to a standard format can make the colors look flat.")
        if any(g["info"]["audio_tracks"] > 1 for g in segs):
            notes.append("Only the first audio track is kept.")
        if noaudio and any(g["info"]["has_audio"] for g in segs):
            notes.append("Sound is removed.")

        first = segs[0]["src"]
        out_dir = (spec.get("dir") or "").strip()
        default_dir = None
        if out_dir and os.path.abspath(out_dir) in self.picked_dirs and os.path.isdir(out_dir):
            default_dir = out_dir
        elif not first["upload"]:
            default_dir = os.path.dirname(first["path"])
        out_dir = default_dir or (spec.get("default_dir") or os.path.expanduser("~"))
        base = safe_name(spec.get("name") or "", "")
        if not base:
            stem = os.path.splitext(first["name"])[0]
            same_ext = os.path.splitext(first["name"])[1].lower().lstrip(".") == F["ext"]
            base = safe_name(f"{stem} (edited)" if (edited or same_ext) else stem, "video")
        return {
            "segs": segs,
            "eff": eff,
            "fid": fid,
            "F": F,
            "copy": copy,
            "mode": mode,
            "quality": quality,
            "maxh": maxh,
            "canvas": canvas,
            "fps": fps_out,
            "out_dur": out_dur,
            "est": int(est),
            "notes": notes,
            "why": why,
            "want_audio": want_audio,
            "out_video": out_video,
            "has_video": has_video,
            "gif": gif,
            "out_dir": out_dir,
            "base": base,
            "pending": pending,
        }

    def plan_public(self, spec):
        p = self.build_plan(spec)
        final = self._unique(p["out_dir"], p["base"], p["F"]["ext"])
        return {
            "lossless": p["copy"],
            "format": p["fid"],
            "label": p["F"]["label"],
            "ext": p["F"]["ext"],
            "duration": p["out_dur"],
            "est_size": p["est"],
            "est_text": "about " + fmt_size(p["est"]) if p["est"] else "",
            "notes": p["notes"],
            "why": p["why"][:1],
            "dir": p["out_dir"],
            "name": os.path.basename(final),
            "canvas": f"{p['canvas'][0]}×{p['canvas'][1]}" if p["canvas"] else "",
            "shifts": [round(s, 3) for _, _, s in p["eff"]],
            "pending": p["pending"],
        }

    @staticmethod
    def _unique(folder, base, ext):
        p = os.path.join(folder, f"{base}.{ext}")
        n = 2
        while os.path.exists(p):
            p = os.path.join(folder, f"{base} ({n}).{ext}")
            n += 1
        return p

    def _ff(self, cmd, total, on_pct, job):
        cmd, flags = _low(cmd)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=flags
        )
        if job is not None:
            job["proc"] = proc
        err = collections.deque(maxlen=40)

        # stderr has to be read the whole time or ffmpeg stalls when the pipe fills up
        def drain():
            for ln in proc.stderr:
                err.append(ln.decode("utf-8", "replace").rstrip())

        threading.Thread(target=drain, daemon=True).start()
        last = 0.0
        speed = 0.0
        start = time.monotonic()
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if job is not None and job.get("cancel"):
                    break
                # out_time_ms is actually microseconds too (ffmpeg quirk), so both get the same division
                if line.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        t = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    now = time.monotonic()
                    if now - last >= 0.2 and total > 0:
                        last = now
                        p = max(0.0, min(0.995, t / total))
                        elapsed = now - start
                        eta = (elapsed / p - elapsed) if p > 0.02 else None
                        on_pct(p, speed=speed, eta=eta, t=t)
                elif line.startswith("speed="):
                    try:
                        speed = float(line.split("=", 1)[1].rstrip("x ") or 0)
                    except ValueError:
                        pass
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if job is not None:
            job["proc"] = None
            if job.get("cancel"):
                raise Cancelled()
        text = "\n".join(l for l in err if l)
        return proc.returncode, text[-600:]

    def start_export(self, spec):
        job = {"id": secrets.token_hex(4), "running": True, "cancel": False, "proc": None, "plan": None}
        with self.lock:
            if self.job and self.job.get("running"):
                raise StudioError("An export is already running.")
            self.job = job
        try:
            p = self.build_plan(spec, kf_wait=120)
            os.makedirs(p["out_dir"], exist_ok=True)
            if not os.access(p["out_dir"], os.W_OK):
                raise StudioError("Can't save to that folder. Pick another one.")
            # 40% extra because the estimate is a guess, plus the temp cuts when joining copies,
            # plus 50mb of breathing room
            need = int(p["est"] * 1.4) + (p["est"] if p["copy"] and len(p["segs"]) > 1 else 0) + 50 * 1024 * 1024
            try:
                free = shutil.disk_usage(p["out_dir"]).free
                if free < need:
                    raise StudioError(
                        f"Not enough free space: this needs about {fmt_size(need)} and only {fmt_size(free)} is free."
                    )
            except OSError:
                pass
        except BaseException:
            job["running"] = False
            raise
        job["plan"] = p
        threading.Thread(target=self._export_thread, args=(job,), daemon=True, name="studio-export").start()
        return {"ok": True, "id": job["id"]}

    def status(self):
        j = self.job
        return {"ok": True, "running": bool(j and j.get("running")), "event": self.last_event}

    def cancel(self):
        j = self.job
        if j and j.get("running"):
            j["cancel"] = True
            pr = j.get("proc")
            if pr and pr.poll() is None:
                try:
                    pr.terminate()
                except Exception:
                    pass
        return True

    def _evt(self, job, **kw):
        evt = {"t": "export", "job": job["id"], **kw}
        self.last_event = evt
        self.emit("studio", evt)

    def _export_thread(self, job):
        p = job["plan"]
        F = p["F"]
        # hidden temp file next to the final one, only renamed after the check passes
        part = os.path.join(p["out_dir"], f".{p['base']}.{job['id']}.studio-part")
        work = os.path.join(WORK, job["id"])
        t0 = time.monotonic()
        try:
            self._evt(job, state="running", pct=0, stage="Starting")
            os.makedirs(work, exist_ok=True)

            def prog(base, span, label):
                def f(pct, speed=0.0, eta=None, t=0.0):
                    self._evt(job, state="running", pct=round(base + span * pct, 4), speed=speed, eta=eta, stage=label)

                return f

            if p["copy"]:
                self._run_copy(job, p, part, work, prog)
            else:
                self._run_encode(job, p, part, prog)
            self._evt(job, state="running", pct=0.99, stage="Checking the result")
            self._verify(job, p, part)
            final = self._unique(p["out_dir"], p["base"], F["ext"])
            os.replace(part, final)
            self.allow.add(os.path.abspath(final))
            size = os.path.getsize(final)
            self._evt(
                job,
                state="done",
                pct=1,
                path=final,
                name=os.path.basename(final),
                size=size,
                size_text=fmt_size(size),
                lossless=p["copy"],
                seconds=round(time.monotonic() - t0, 1),
            )
            self.log(
                f"Studio: saved {final} ({fmt_size(size)}, {'lossless' if p['copy'] else 're-encoded'}, "
                f"{time.monotonic() - t0:.1f}s)"
            )
        except Cancelled:
            self._evt(job, state="cancelled")
        except StudioError as e:
            self.log(f"Studio export failed: {e}")
            self._evt(job, state="error", message=str(e))
        except Exception as e:
            self.log(f"Studio export crashed: {e!r}")
            self._evt(
                job, state="error", message="Something went wrong while exporting. Your original files are untouched."
            )
        finally:
            job["running"] = False
            for pth in [part] + self._deferred_rm:
                try:
                    os.remove(pth)
                except OSError:
                    pass
            self._deferred_rm = []
            shutil.rmtree(work, ignore_errors=True)

    def _run_copy(self, job, p, part, work, prog):
        F = p["F"]
        segs, eff = p["segs"], p["eff"]
        total = sum(b - a for a, b, _ in eff) or 1
        done = 0.0
        multi = len(segs) > 1
        cuts = []
        for n, (g, (a, b, _)) in enumerate(zip(segs, eff)):
            dur = b - a
            out = os.path.join(work, f"cut{n}.{F['ext']}") if multi else part
            cut = a > 0.001
            if not g["info"]["has_video"]:
                seek_in, drop_to, lead = (a if cut else 0.0), 0.0, 0.0
            else:
                # lead is the keyframe delay we measured (0.25 if the scan never got it) plus a little extra
                kd = g["src"].get("kf_delay")
                lead = (min(1.0, kd if kd is not None else 0.25) + 0.02) if cut else 0.0
                seek_in = max(0.0, a - 2.0 - lead) if cut else 0.0
                drop_to = max(0.0, a - lead - seek_in) if cut else 0.0
            cmd = [
                _tool("ffmpeg"),
                "-hide_banner",
                "-nostdin",
                "-y",
                "-v",
                "error",
                "-progress",
                "pipe:1",
                "-nostats",
                *(["-ss", f"{seek_in:.4f}"] if seek_in > 0 else []),
                "-i",
                g["src"]["path"],
                *(["-ss", f"{drop_to:.4f}"] if drop_to > 0 else []),
                "-t",
                f"{dur + lead:.4f}",
            ]
            if p["out_video"]:
                cmd += ["-map", "0:v:0"]
            else:
                cmd += ["-vn"]
            cmd += ["-map", "0:a:0"] if p["want_audio"] else ["-an"]
            cmd += ["-sn", "-dn", "-c", "copy", "-map_chapters", "-1", "-avoid_negative_ts", "make_zero"]
            if F["muxer"] in ("mp4", "mov", "ipod") and not multi:
                cmd += ["-movflags", "+faststart"]
            cmd += ["-f", F["muxer"], out]
            # with several clips cutting is the first 85% of the bar and joining is the rest
            base, span = done / total, (b - a) / total
            rc, err = self._ff(
                cmd, dur, prog(base * (0.85 if multi else 1), span * (0.85 if multi else 1), "Cutting"), job
            )
            if rc != 0 or not os.path.isfile(out) or os.path.getsize(out) == 0:
                raise StudioError(
                    "Couldn't cut this file without re-encoding. Try the 'Exact' option or a different format."
                    + (f" ({err.splitlines()[-1][:120]})" if err else "")
                )
            cuts.append(out)
            done += dur
        if multi:
            lst = os.path.join(work, "list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for c in cuts:
                    f.write("file '" + c.replace("\\", "/").replace("'", r"'\''") + "'\n")
            cmd = [
                _tool("ffmpeg"),
                "-hide_banner",
                "-nostdin",
                "-y",
                "-v",
                "error",
                "-progress",
                "pipe:1",
                "-nostats",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                lst,
                "-map",
                "0",
                "-c",
                "copy",
                "-map_chapters",
                "-1",
            ]
            if F["muxer"] in ("mp4", "mov", "ipod"):
                cmd += ["-movflags", "+faststart"]
            cmd += ["-f", F["muxer"], part]
            rc, err = self._ff(cmd, total, prog(0.85, 0.15, "Joining"), job)
            if rc != 0 or not os.path.isfile(part) or os.path.getsize(part) == 0:
                raise StudioError("Couldn't join these clips without re-encoding. Try a different format or quality.")

    def _run_encode(self, job, p, part, prog):
        F = p["F"]
        segs, eff = p["segs"], p["eff"]
        q = p["quality"]
        # nudge the trim edges back a hair so float error cant add or drop a frame
        eps = min(0.002, 0.25 / (segs[0]["info"]["fps"] or 25.0)) if p["has_video"] else 0.0
        a_rate, a_layout = 48000, "stereo"
        if p["want_audio"]:
            ainfo = next(g["info"] for g in segs if g["info"]["has_audio"])
            if F["aenc"] not in ("opus",) and ainfo["sample_rate"] in (44100, 48000) and p["fid"] != "webm":
                a_rate = ainfo["sample_rate"]
            a_layout = "mono" if ainfo["channels"] == 1 else "stereo"

        cmd = [_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-nostats"]
        if self.threads():
            cmd += ["-threads", str(self.threads())]
        graph, vlabels, alabels, durs, ins = [], [], [], [], []
        for n, (g, (a, b, _)) in enumerate(zip(segs, eff)):
            # seek in about a second early (fast) and let trim do the exact cut, only where seeking works
            s_in = max(0.0, a - 1.0) if g["info"]["seek_ok"] else 0.0
            t0, t1 = a - s_in - eps, b - s_in - eps
            dur = b - a
            durs.append(dur)
            ins.append(
                ((["-ss", f"{s_in:.4f}"] if s_in > 0 else []) + ["-t", f"{b - s_in + 0.5:.4f}"], g["src"]["path"])
            )
            if p["out_video"]:
                vf = [f"trim=start={t0:.4f}:end={t1:.4f}", "setpts=PTS-STARTPTS"]
                if g["hflip"]:
                    vf.append("hflip")
                if g["vflip"]:
                    vf.append("vflip")
                vf += {0: [], 90: ["transpose=1"], 180: ["transpose=1", "transpose=1"], 270: ["transpose=2"]}[g["rot"]]
                w, h = g["info"]["width"], g["info"]["height"]
                if g["rot"] in (90, 270):
                    w, h = h, w
                cw, ch = p["canvas"]
                if (w, h) != (cw, ch):
                    vf.append(f"scale={cw}:{ch}:force_original_aspect_ratio=decrease:flags=bicubic")
                    vf.append(f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black")
                vf += ["setsar=1", f"fps={p['fps']}", "format=yuv420p"]
                graph.append(f"[{n}:v:0]{','.join(vf)}[v{n}]")
                vlabels.append(f"[v{n}]")
            if p["want_audio"]:
                if g["info"]["has_audio"] and not g["mute"]:
                    # pad or trim the audio to exactly the clip length so audio and video dont drift
                    # apart after the concat
                    af = (
                        f"[{n}:a:0]atrim=start={t0:.4f}:end={t1:.4f},asetpts=PTS-STARTPTS,aresample={a_rate},"
                        f"aformat=sample_fmts=fltp:channel_layouts={a_layout},apad=whole_dur={dur:.4f},atrim=0:{dur:.4f}"
                    )
                    graph.append(af + f"[a{n}]")
                else:
                    graph.append(f"anullsrc=r={a_rate}:cl={a_layout},atrim=0:{dur:.4f},asetpts=PTS-STARTPTS[a{n}]")
                alabels.append(f"[a{n}]")
        nseg = len(segs)
        parts = []
        for n in range(nseg):
            if p["out_video"]:
                parts.append(vlabels[n])
            if p["want_audio"]:
                parts.append(alabels[n])
        v_out = "[vo]" if p["out_video"] else ""
        a_out = "[ao]" if p["want_audio"] else ""
        if nseg == 1:
            if p["out_video"]:
                graph.append(f"{vlabels[0]}null{v_out}")
            if p["want_audio"]:
                graph.append(f"{alabels[0]}anull{a_out}")
        else:
            graph.append(
                "".join(parts)
                + f"concat=n={nseg}:v={1 if p['out_video'] else 0}:a={1 if p['want_audio'] else 0}"
                + v_out
                + a_out
            )
        # gif needs its own palette or the colors come out awful
        if p["gif"]:
            graph.append(
                "[vo]split[s0][s1];[s0]palettegen=stats_mode=diff[pal];[s1][pal]paletteuse=dither=bayer:bayer_scale=5[vg]"
            )
        mid = ["-filter_complex", ";".join(graph)]
        if p["out_video"]:
            mid += ["-map", "[vg]" if p["gif"] else "[vo]"]
        if p["want_audio"]:
            mid += ["-map", "[ao]"]
        mid += ["-sn", "-dn", "-map_chapters", "-1", "-map_metadata", "-1"]

        ve = F["venc"] if (p["out_video"] and not p["gif"]) else None
        ve_sw, ve_hw, hw_name = [], [], None
        if ve == "h264":
            ve_sw = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(VIDEO_Q[q]), "-profile:v", "high"]
        elif ve == "hevc":
            # x265 crf runs about 5 above x264 for the same look, and apple players want the hvc1 tag
            ve_sw = [
                "-c:v",
                "libx265",
                "-preset",
                "veryfast",
                "-crf",
                str(VIDEO_Q[q] + 5),
                "-tag:v",
                "hvc1",
                # x265 ignores -v error and prints its own log otherwise
                "-x265-params",
                "log-level=error",
            ]
        elif ve == "vp9":
            # b:v 0 is needed or vp9 caps quality at a default bitrate instead of using crf
            ve_sw = [
                "-c:v",
                "libvpx-vp9",
                "-b:v",
                "0",
                "-crf",
                str(VP9_Q[q]),
                "-deadline",
                "good",
                "-cpu-used",
                "4",
                "-row-mt",
                "1",
            ]
        # gpu encoders dont take crf, each has its own knob (cq, global_quality, qp)
        if ve in ("h264", "hevc"):
            hw_name = self._hw_encoder(ve)
            if hw_name:
                cq = VIDEO_Q[q] + (5 if ve == "hevc" else 0)
                tag = ["-tag:v", "hvc1"] if ve == "hevc" else []
                if hw_name.endswith("_nvenc"):
                    ve_hw = ["-c:v", hw_name, "-preset", "p4", "-rc", "vbr", "-cq", str(cq), "-b:v", "0", *tag]
                elif hw_name.endswith("_qsv"):
                    ve_hw = ["-c:v", hw_name, "-preset", "veryfast", "-global_quality", str(cq), *tag]
                else:
                    ve_hw = [
                        "-c:v",
                        hw_name,
                        "-quality",
                        "speed",
                        "-rc",
                        "cqp",
                        "-qp_i",
                        str(cq),
                        "-qp_p",
                        str(cq),
                        *tag,
                    ]
        post = []
        if p["gif"]:
            post += ["-loop", "0"]
        if p["want_audio"]:
            ae = F["aenc"]
            if ae == "aac":
                post += ["-c:a", "aac", "-b:a", f"{AUDIO_K[q]}k"]
            elif ae == "mp3":
                post += ["-c:a", "libmp3lame", "-b:a", f"{AUDIO_K[q]}k"]
            elif ae == "opus":
                post += ["-c:a", "libopus", "-b:a", f"{OPUS_K[q]}k"]
            elif ae == "vorbis":
                if "libvorbis" in self._encoders():
                    post += ["-c:a", "libvorbis", "-b:a", f"{AUDIO_K[q]}k"]
                else:
                    post += ["-c:a", "libopus", "-b:a", f"{OPUS_K[q]}k"]
            elif ae == "flac":
                post += ["-c:a", "flac", "-compression_level", "5"]
            elif ae == "pcm_s16le":
                post += ["-c:a", "pcm_s16le"]
        if F["muxer"] in ("mp4", "mov", "ipod"):
            post += ["-movflags", "+faststart"]
        post += ["-f", F["muxer"] if F["ext"] != "ogg" else "ogg", part]
        total = sum(durs) or 1

        def assemble(hwdec, venc):
            c = list(cmd)
            for opts, path in ins:
                c += opts + (["-hwaccel", "auto"] if (hwdec and p["out_video"]) else []) + ["-i", path]
            return c + mid + venc + post

        # first try is the gpu (decode, plus encode if a gpu encoder works), last resort is plain cpu
        use_hw = not os.environ.get("ALD_NO_HWACCEL")
        attempts = []
        if use_hw and p["out_video"]:
            attempts.append((True, ve_hw or ve_sw, "graphics chip" if ve_hw else "processor, graphics-chip decoding"))
        attempts.append((False, ve_sw, "processor"))
        err, rc = "", 1
        for k, (hwdec, venc, label) in enumerate(attempts):
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
            rc, err = self._ff(assemble(hwdec, venc), total, prog(0.0, 0.985, "Exporting"), job)
            if rc == 0 and os.path.isfile(part) and os.path.getsize(part) > 0:
                if ve_hw:
                    self.log(
                        f"Studio: exported using the {label}"
                        if k == 0
                        else "Studio: exported using the processor (graphics chip didn't work)"
                    )
                break
            if job.get("cancel"):
                raise Cancelled()
            if k < len(attempts) - 1:
                self.log(
                    f"Studio: the {label} couldn't do this export, trying the processor: {(err.splitlines() or [''])[-1][:140]}"
                )
        if rc != 0 or not os.path.isfile(part) or os.path.getsize(part) == 0:
            self.log(f"Studio ffmpeg failed: {err}")
            tail = (err.splitlines() or [""])[-1][:160]
            raise StudioError("ffmpeg couldn't make this file." + (f" ({tail})" if tail else ""))

    def _hw_encoder(self, ve):
        if os.environ.get("ALD_NO_HWENC") or os.environ.get("ALD_NO_HWACCEL"):
            return None
        cache = self.__dict__.setdefault("_hw_enc_cache", {})
        if ve in cache:
            return cache[ve]
        found = None
        names = self._encoders()
        for cand in {"h264": ("h264_nvenc", "h264_qsv", "h264_amf"), "hevc": ("hevc_nvenc", "hevc_qsv", "hevc_amf")}[
            ve
        ]:
            if cand not in names:
                continue
            # being listed in ffmpeg doesnt mean the gpu is there, so run a tiny test encode first
            try:
                r = _run(
                    [
                        _tool("ffmpeg"),
                        "-hide_banner",
                        "-nostdin",
                        "-v",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "color=c=black:s=640x360:r=25:d=0.4",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:v",
                        cand,
                        "-f",
                        "null",
                        "-",
                    ],
                    25,
                )
                if r.returncode == 0:
                    found = cand
                    break
            except Exception:
                continue
        cache[ve] = found
        if found:
            self.log(f"Studio: graphics-chip {ve.upper()} encoding is available ({found})")
        return found

    def _encoders(self):
        if not hasattr(self, "_enc_names"):
            names = set()
            try:
                r = _run([_tool("ffmpeg"), "-hide_banner", "-encoders"], 30)
                for line in r.stdout.decode("utf-8", "replace").splitlines():
                    pp = line.split()
                    if len(pp) >= 2 and re.match(r"^[VAS][.A-Z]{5}$", pp[0]):
                        names.add(pp[1])
            except Exception:
                pass
            self._enc_names = names
        return self._enc_names

    def _verify(self, job, p, part):
        try:
            info = probe(part)
        except StudioError:
            raise StudioError("The result couldn't be read back, so it wasn't saved. Your original is untouched.")
        want = p["out_dur"]
        nseg = len(p["segs"])
        fps = (p["segs"][0]["info"]["fps"] or 25.0) if p["has_video"] else 0
        if p["copy"]:
            # lossless cuts land on keyframes so the length can be off by a lot more
            tol = 0.5 + 0.01 * want + (0.2 * nseg)
        else:
            tol = 0.15 + 0.005 * want + (2.0 / fps if fps else 0.1) * nseg
        if abs(info["duration"] - want) > tol:
            raise StudioError(
                f"The result came out {info['duration']:.1f} s long instead of {want:.1f} s, so it wasn't saved. "
                "Your original is untouched."
            )
        if p["out_video"] and not info["has_video"]:
            raise StudioError("The result has no picture, so it wasn't saved. Your original is untouched.")
        if p["want_audio"] and not info["has_audio"]:
            raise StudioError("The result has no sound, so it wasn't saved. Your original is untouched.")
        d = info["duration"]
        # decode the first and last 2s with -xerror, catches a broken file without reading all of it
        for start, length in ((0.0, min(2.0, d)), (max(0.0, d - 2.0), 2.0)):
            cmd = [
                _tool("ffmpeg"),
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{length:.3f}",
                "-i",
                part,
                "-f",
                "null",
                "-",
            ]
            try:
                r = _run(cmd, 120)
            except subprocess.TimeoutExpired:
                continue
            # any stderr output at all counts as a fail
            if r.returncode != 0 or r.stderr.strip():
                self.log(f"Studio verify failed: {r.stderr.decode('utf-8', 'replace')[:300]}")
                raise StudioError(
                    "The result didn't pass the safety check (it didn't play back cleanly), so it wasn't "
                    "saved. Your original is untouched. Try a different format or the 'Exact' option."
                )
        if job.get("cancel"):
            raise Cancelled()
