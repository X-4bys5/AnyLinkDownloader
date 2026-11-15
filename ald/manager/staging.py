# downloads go into a working folder first and only the finished file moves into the save folder
import os
import re
import shutil
import time

from .. import engine
from ..core import DATA_DIR, MB
from ..settings import STAGE_NAME


class StagingMixin:
    @staticmethod
    def _same_volume(a, b):
        # the last move has to stay on one drive, across drives it turns into a slow copy
        try:
            if os.name == "nt":
                return (
                    os.path.splitdrive(os.path.abspath(a))[0].lower()
                    == os.path.splitdrive(os.path.abspath(b))[0].lower()
                )
            return os.stat(a).st_dev == os.stat(b).st_dev
        except OSError:
            return False

    @staticmethod
    def _title_limit(work, sub="", prefix="", win=None):
        if not (os.name == "nt" if win is None else win):
            return 150
        # windows paths break at about 260 so long titles get cut, 40 is room for the id and extension
        room = 235 - len(work) - len(sub) - len(prefix) - 40
        return max(40, min(150, room))

    @staticmethod
    def _hide(path):
        if os.name == "nt":
            try:
                import ctypes

                ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)
            except Exception:
                pass

    def _stage_root(self, dest):
        default = os.path.join(DATA_DIR, STAGE_NAME)
        try:
            os.makedirs(default, exist_ok=True)
            os.makedirs(dest, exist_ok=True)
            if self._same_volume(default, dest):
                return default
            # other drive than appdata, so use a hidden folder next to the save folder instead
            beside = os.path.join(os.path.dirname(os.path.abspath(dest)), "." + STAGE_NAME)
            os.makedirs(beside, exist_ok=True)
            self._hide(beside)
            if self._same_volume(beside, dest):
                return beside
        except OSError:
            pass
        return default

    def _stage(self, job):
        if not job.stage or not os.path.isdir(job.stage["root"]):
            dest = job.dest or self.settings["folder"]
            root = os.path.join(self._stage_root(dest), job.id)
            job.stage = {"root": root, "work": os.path.join(root, "work"), "done": os.path.join(root, "done")}
        os.makedirs(job.stage["root"], exist_ok=True)
        return job.stage

    def _stage_drop(self, job):
        if job.stage:
            shutil.rmtree(job.stage["root"], ignore_errors=True)
            job.stage = None

    # failed jobs keep their folder so a retry can pick up what is already there
    def _stage_finish(self, job):
        if job.status in ("done", "skipped", "cancelled"):
            self._stage_drop(job)

    def _purge_stages(self):
        roots = {
            os.path.join(DATA_DIR, STAGE_NAME),
            os.path.join(os.path.dirname(os.path.abspath(self.settings["folder"])), "." + STAGE_NAME),
        }
        # leftovers from a crashed run, active (resumed) jobs keep theirs
        for root in roots:
            try:
                for name in os.listdir(root):
                    if re.fullmatch(r"[0-9a-f]{10}", name) and name not in {j.id for j in self.jobs if j.active}:
                        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            except OSError:
                pass

    def _unique_path(self, path):
        base, ext = os.path.splitext(path)
        n = 2
        while os.path.exists(f"{base} ({n}){ext}"):
            n += 1
        return f"{base} ({n}){ext}"

    def _move_into_place(self, job, src, dst):
        deadline = time.monotonic() + 90
        while True:
            try:
                os.replace(src, dst)
                return
            except PermissionError:  # antivirus is probably still scanning the new file
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
            except OSError:
                break  # cant just rename (other drive), copy it instead
        tmp = dst + ".ald-moving"
        total = os.path.getsize(src) or 1
        done = 0
        try:
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    if job.cancel.is_set():
                        raise engine.yt_dlp.utils.DownloadCancelled("Cancelled by user")
                    buf = fi.read(8 * MB)
                    if not buf:
                        break
                    fo.write(buf)
                    done += len(buf)
                    self._set(
                        job,
                        status="processing",
                        phase="merge",
                        message=f"Moving to your folder  ({done * 100 // total}%)",
                        speed=0.0,
                        eta=None,
                    )
            shutil.copystat(src, tmp)
            for n in range(20):
                try:
                    os.replace(tmp, dst)
                    break
                except PermissionError:
                    if n == 19:
                        raise
                    time.sleep(0.5)
            os.remove(src)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _deliver(self, job, src):
        if not src or not os.path.exists(src) or not job.stage:
            return src
        try:
            rel = os.path.relpath(os.path.abspath(src), os.path.abspath(job.stage["done"]))
        except ValueError:
            return src
        if rel == ".." or rel.startswith(".." + os.sep):
            return src
        dest = job.dest or self.settings["folder"]
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst) and not job.force:  # only a forced redownload may replace a file
            dst = self._unique_path(dst)
        self._set(job, status="processing", phase="merge", message="Saving", speed=0.0, eta=None)
        self._move_into_place(job, src, dst)
        self.log(f"Moved to {dst}")
        return dst
