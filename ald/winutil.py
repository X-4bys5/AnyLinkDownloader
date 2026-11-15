import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

from .core import APP_NAME, DATA_DIR, NO_WINDOW, file_log, resource_path

# window, browser, clipboard and file dialog stuff, mostly windows only
IS_WIN = os.name == "nt"


def find_browser():
    env = os.environ.get("ALD_BROWSER")
    if env and os.path.exists(env):
        return env
    cands = []
    if IS_WIN:
        pf, pf86, la = (os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"))
        for base in (pf86, pf, la):  # edge first, its always there on windows 10 and 11
            if not base:
                continue
            cands += [
                os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                os.path.join(base, "Vivaldi", "Application", "vivaldi.exe"),
                os.path.join(base, "Chromium", "Application", "chrome.exe"),
            ]
        # fallback for browsers installed somewhere unusual
        try:
            import winreg

            for exe in ("msedge.exe", "chrome.exe", "brave.exe"):
                for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                            cands.append(winreg.QueryValueEx(k, "")[0])
                    except OSError:
                        pass
        except Exception:
            pass
    elif sys.platform == "darwin":
        cands += [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    else:
        for n in (
            "microsoft-edge",
            "microsoft-edge-stable",
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "brave-browser",
        ):
            p = shutil.which(n)
            if p:
                cands.append(p)
        cands += ["/opt/google/chrome/chrome"]
    return next((c for c in cands if c and os.path.exists(c)), None)


def find_shell():
    if os.environ.get("ALD_NO_SHELL"):
        return None
    env = os.environ.get("ALD_SHELL")
    if env:
        return env if os.path.isfile(env) else None
    if not IS_WIN:
        return None
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    # the small native window, if its missing we just use the browser instead
    for c in (
        resource_path("bin", "ald-window.exe"),
        os.path.join(here, "ald-window.exe"),
        resource_path("vendor", "bin", "ald-window.exe"),
    ):
        if os.path.isfile(c):
            return c
    return None


SHELL_GRACE = 30  # seconds, if the native window dies sooner than this we assume it couldnt start


def _open_shell_window(url):
    shell = find_shell()
    if not shell:
        return None
    args = [shell, url, "--title", APP_NAME, "--data", os.path.join(DATA_DIR, "webview"), "--size", "1180x820"]
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW if IS_WIN else 0,
        )
    except Exception as e:
        file_log(f"native window could not start ({e}) - using the browser window")
        return None

    def watch():
        t0 = time.monotonic()
        try:
            rc = proc.wait()
        except Exception:
            return
        if rc != 0 and time.monotonic() - t0 < SHELL_GRACE:
            file_log(
                f"native window exited with code {rc} after {time.monotonic() - t0:.0f}s - opening the browser window instead"
            )
            _open_browser_window(url)

    threading.Thread(target=watch, daemon=True, name="shell-watch").start()
    return proc


def open_app_window(url):
    proc = _open_shell_window(url)
    if proc is not None:
        return proc
    return _open_browser_window(url)


def _open_browser_window(url):
    exe = find_browser()
    if exe:
        profile = os.path.join(DATA_DIR, "window")
        args = [
            exe,
            f"--app={url}",
            f"--user-data-dir={profile}",  # own profile so we dont mess with the normal browser
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1180,820",
            "--disable-session-crashed-bubble",
            "--disable-features=Translate,msEdgeSidebarV2",
            "--disable-sync",
            "--disable-background-networking",
            "--password-store=basic",
        ]
        if os.environ.get("ALD_HEADLESS_TEST"):  # dev only, skips the window stuff
            args += ["--headless=new", "--no-sandbox", "--disable-gpu"]
        try:
            return subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
            )
        except Exception:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return None


def message_box(text, title=APP_NAME):
    try:
        if IS_WIN:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)  # 0x10 is the error icon
            return
    except Exception:
        pass
    print(text)


def focus_existing_window(title=APP_NAME):
    if not IS_WIN:
        return False
    try:
        import ctypes

        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, title)
        if hwnd:
            u.ShowWindow(hwnd, 9)  # SW_RESTORE, in case its minimized
            u.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def read_clipboard():
    try:
        if IS_WIN:
            import ctypes
            from ctypes import wintypes

            # without argtypes the handles get cut to 32 bit on 64 bit python
            u, k = ctypes.windll.user32, ctypes.windll.kernel32
            u.OpenClipboard.argtypes, u.OpenClipboard.restype = [wintypes.HWND], wintypes.BOOL
            u.GetClipboardData.argtypes, u.GetClipboardData.restype = [wintypes.UINT], wintypes.HANDLE
            k.GlobalLock.argtypes, k.GlobalLock.restype = [wintypes.HGLOBAL], ctypes.c_void_p
            k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            if not u.OpenClipboard(None):
                return ""
            try:
                h = u.GetClipboardData(13)  # CF_UNICODETEXT
                if not h:
                    return ""
                p = k.GlobalLock(h)
                if not p:
                    return ""
                try:
                    return (ctypes.wstring_at(p) or "")[:4000]  # a link never needs more than this
                finally:
                    k.GlobalUnlock(h)
            finally:
                u.CloseClipboard()
        cmds = (
            [["pbpaste"]]
            if sys.platform == "darwin"
            else [["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "-b", "-o"]]
        )
        for c in cmds:
            if shutil.which(c[0]):
                r = subprocess.run(c, capture_output=True, text=True, timeout=2)
                if r.returncode == 0:
                    return r.stdout[:4000]
    except Exception:
        pass
    return ""


def _win_pick_folder(title="Choose where videos are saved"):
    import ctypes
    from ctypes import wintypes

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    ole, shell, user = ctypes.windll.ole32, ctypes.windll.shell32, ctypes.windll.user32
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    shell.SHBrowseForFolderW.argtypes, shell.SHBrowseForFolderW.restype = [ctypes.POINTER(BROWSEINFOW)], ctypes.c_void_p
    shell.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoInitializeEx(None, 2)  # apartment threaded, the dialog wants it
    try:
        bi = BROWSEINFOW()
        bi.hwndOwner = user.GetForegroundWindow()
        bi.pszDisplayName = ctypes.cast(ctypes.create_unicode_buffer(260), wintypes.LPWSTR)
        bi.lpszTitle = title
        bi.ulFlags = 0x1 | 0x10 | 0x40  # folders only, edit box, the newer dialog style
        pidl = shell.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return ""
        buf = ctypes.create_unicode_buffer(1024)
        ok = shell.SHGetPathFromIDListW(pidl, buf)
        ole.CoTaskMemFree(pidl)
        return buf.value if ok else ""
    finally:
        ole.CoUninitialize()


# fallback for when the ctypes dialog fails
def _ps_pick_folder(title="Choose where videos are saved"):
    title = title.replace("'", "")  # it goes inside single quotes in the script
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$d.Description = '{title}'; $d.ShowNewFolderButton = $true;"
        "if ($d.ShowDialog() -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],  # winforms dialogs need an STA thread
        capture_output=True,
        text=True,
        timeout=600,
        creationflags=NO_WINDOW,
    )
    return r.stdout.strip()


def choose_folder(initial=None, title="Choose where videos are saved"):
    try:
        if IS_WIN:
            try:
                return _win_pick_folder(title)
            except Exception:
                return _ps_pick_folder(title)
        if sys.platform == "darwin":
            r = subprocess.run(
                ["osascript", "-e", f'POSIX path of (choose folder with prompt "{title}")'],
                capture_output=True,
                text=True,
                timeout=600,
            )
            return r.stdout.strip().rstrip("/") if r.returncode == 0 else ""
        for cmd in (
            ["zenity", "--file-selection", "--directory", f"--title={title}"],
            ["kdialog", "--getexistingdirectory", initial or os.path.expanduser("~")],
        ):
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        pass
    return None


# the dialog wants null separated pairs with a double null at the very end
_FILTER = (
    "Video and audio\0*.mp4;*.m4v;*.mov;*.mkv;*.webm;*.avi;*.flv;*.wmv;*.ts;*.mts;*.m2ts;*.mpg;*.mpeg;*.3gp;*.ogv;"
    "*.mp3;*.m4a;*.aac;*.flac;*.wav;*.ogg;*.oga;*.opus;*.wma;*.aiff\0All files\0*.*\0\0"
)


def _win_pick_files():
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", ctypes.c_void_p),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", ctypes.c_void_p),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    ole, user = ctypes.windll.ole32, ctypes.windll.user32
    comdlg = ctypes.windll.comdlg32
    comdlg.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    comdlg.GetOpenFileNameW.restype = wintypes.BOOL
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx(None, 2)
    try:
        flt = ctypes.create_unicode_buffer(_FILTER, len(_FILTER) + 1)
        n = 65536  # multi select puts every name in this one buffer
        buf = ctypes.create_unicode_buffer(n)
        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        ofn.hwndOwner = user.GetForegroundWindow()
        ofn.lpstrFilter = ctypes.cast(flt, ctypes.c_void_p)
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.cast(buf, ctypes.c_void_p)
        ofn.nMaxFile = n
        ofn.lpstrTitle = "Open videos or audio"
        ofn.Flags = 0x80000 | 0x200 | 0x1000 | 0x800 | 0x8  # explorer style, multi select, must exist, keep cwd
        if not comdlg.GetOpenFileNameW(ctypes.byref(ofn)):
            err = ctypes.windll.comdlg32.CommDlgExtendedError()  # 0 means they just cancelled
            if err:
                raise OSError(f"dialog error {err}")
            return []
        raw = ctypes.wstring_at(ctypes.addressof(buf), n)
        parts = raw.split("\0")
        parts = parts[: parts.index("")] if "" in parts else parts
        # one file comes back as a full path, several come back as folder then the file names
        if len(parts) == 1:
            return [parts[0]]
        return [os.path.join(parts[0], f) for f in parts[1:]]
    finally:
        ole.CoUninitialize()


def _ps_pick_files():
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.OpenFileDialog; $d.Multiselect = $true;"
        "$d.Filter = 'Video and audio|*.mp4;*.m4v;*.mov;*.mkv;*.webm;*.avi;*.flv;*.wmv;*.ts;*.mts;*.mpg;*.mpeg;*.3gp;*.mp3;*.m4a;"
        "*.aac;*.flac;*.wav;*.ogg;*.opus|All files|*.*';"
        "if ($d.ShowDialog() -eq 'OK') { [Console]::Out.Write(($d.FileNames -join \"`n\")) }"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        text=True,
        timeout=600,
        creationflags=NO_WINDOW,
    )
    return [x for x in r.stdout.splitlines() if x.strip()]


def choose_files(initial=None):
    try:
        if IS_WIN:
            try:
                return _win_pick_files()
            except Exception:
                return _ps_pick_files()
        if sys.platform == "darwin":
            script = (
                'set fs to choose file with prompt "Open videos or audio" with multiple selections allowed\n'
                'set out to ""\nrepeat with f in fs\nset out to out & POSIX path of f & linefeed\nend repeat\nout'
            )
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
            return [x for x in r.stdout.splitlines() if x.strip()] if r.returncode == 0 else []
        for cmd in (
            ["zenity", "--file-selection", "--multiple", "--separator=\n", "--title=Open videos or audio"],
            ["kdialog", "--getopenfilename", initial or os.path.expanduser("~"), "--multiple", "--separate-output"],
        ):
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                return [x for x in r.stdout.splitlines() if x.strip()] if r.returncode == 0 else []
    except Exception:
        pass
    return None
