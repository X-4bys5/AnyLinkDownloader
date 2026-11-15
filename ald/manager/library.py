import os
import subprocess
import sys
import threading
import time

from ..core import file_log
from ..storage import AUDIO_EXT, ID_RE, MEDIA_EXT


class LibraryMixin:
    def _scan_folder(self):
        folder = self.settings["folder"]
        idx = {}
        base = os.path.abspath(folder)
        depth0 = base.count(os.sep)
        for dirpath, dirs, files in os.walk(base):
            if dirpath.count(os.sep) - depth0 >= 3:
                dirs[:] = []
            time.sleep(0)  # gives other threads a turn while a big folder gets scanned
            for name in files:
                low = name.lower()
                # half finished files dont count as saved
                if os.path.splitext(low)[1] not in MEDIA_EXT or any(
                    x in low for x in (".part", ".ytdl", ".temp.", ".turbo")
                ):
                    continue
                m = ID_RE.search(name)
                if m:
                    idx.setdefault(m.group(1), []).append(os.path.join(dirpath, name))
        self.index_cache = (time.monotonic(), folder, idx)
        return idx

    def folder_index(self, force=False):
        folder = self.settings["folder"]
        t, f, idx = self.index_cache
        # full rescan after 15 min, after 20s it refreshes in the background so the saved check stays quick
        if force or f != folder or time.monotonic() - t > 900:
            return self._scan_folder()
        if time.monotonic() - t > 20 and not self._index_busy:
            self._index_busy = True

            def refresh():
                try:
                    self._scan_folder()
                except Exception as e:
                    file_log(f"folder re-scan failed: {e}")
                finally:
                    self._index_busy = False

            threading.Thread(target=refresh, daemon=True, name="index").start()
        return idx

    def _index_add(self, path, vid):
        # new files go straight into the index so the next skip check sees them without a rescan
        t, f, idx = self.index_cache
        if f != self.settings["folder"] or not path:
            self.index_cache = (0, None, {})
            return
        m = ID_RE.search(os.path.basename(path))
        key = m.group(1) if m else (str(vid) if vid else None)
        if key and path not in idx.get(key, []):
            idx.setdefault(key, []).append(path)

    def existing_file(self, vid, kind="video", ext=None):
        if not vid or vid == "NA":  # yt-dlp says NA when there is no id
            return None
        for p in self.folder_index().get(str(vid), []):
            if not os.path.exists(p):
                continue
            e = os.path.splitext(p.lower())[1]
            # same id can be a video and an audio file, only the matching kind counts
            if (kind == "audio") == (e in AUDIO_EXT) and (not ext or e == ext):
                return p
        return None

    def library_view(self):
        out = []
        for e in self.library[:300]:
            p = e.get("path")
            ok = bool(p and os.path.exists(p))
            out.append(dict(e, exists=ok))
        return out

    def forget(self, path=None):
        with self.lock:
            self.library = [] if path is None else [e for e in self.library if e.get("path") != path]
        self._save_library()
        self.touch()

    def _allowed_path(self, path):
        if not path:
            return False
        # only files the app knows about or that sit in the save folder
        ap = os.path.abspath(path)
        known = {os.path.abspath(e["path"]) for e in self.library if e.get("path")}
        known |= {os.path.abspath(j.path) for j in self.jobs if j.path}
        folder = os.path.abspath(self.settings["folder"])
        return ap in known or ap.startswith(folder + os.sep) or ap == folder or ap in self.studio_paths

    def open_path(self, path):
        if not self._allowed_path(path) or not os.path.exists(path):
            return False
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return True
        except Exception as e:
            self.log(f"Couldn't open {path}: {e}")
            return False

    def reveal_path(self, path=None):
        path = path or self.settings["folder"]
        if not self._allowed_path(path):
            return False
        try:
            if os.path.isdir(path):
                os.makedirs(path, exist_ok=True)
                return self.open_path(path)
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
            return True
        except Exception as e:
            self.log(f"Couldn't show {path}: {e}")
            return False
