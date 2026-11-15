import json
import os
import re
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = os.path.abspath(SPECPATH)
datas, binaries, hiddenimports = [], [], []

# these go in with collect_all so their data files and lazily imported modules make it into the exe
PACKAGES = [
    "yt_dlp",
    "yt_dlp_ejs",
    "certifi",
    "curl_cffi",
    "websockets",
    "brotli",
    "Cryptodome",
    "mutagen",
    "spotdl",
    "spotipy",
    "ytmusicapi",
    "rapidfuzz",
    "pykakasi",
    "slugify",
    "bs4",
    "syncedlyrics",
    "platformdirs",
    "rich",
    "requests",
    "urllib3",
    "charset_normalizer",
    "idna",
]
for pkg in PACKAGES:
    try:
        if pkg == "Cryptodome":  # its SelfTest modules arent needed
            d, b, h = collect_data_files(pkg), [], collect_submodules(pkg, filter=lambda n: "SelfTest" not in n)
        else:
            d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    # optional ones like curl_cffi or spotdl might not be installed, just leave them out
    except Exception as exc:
        print(f"[spec] note: {pkg} not included ({exc})")
hiddenimports = [h for h in hiddenimports if "SelfTest" not in h]

import yt_dlp.version as _v

# the app reads this to know which yt-dlp version was bundled (engine.py)
os.makedirs(os.path.join(ROOT, "build"), exist_ok=True)
info_path = os.path.join(ROOT, "build", "build_info.json")
with open(info_path, "w") as f:
    json.dump({"ytdlp": _v.__version__, "python": sys.version.split()[0]}, f)
datas += [(info_path, ".")]

# file properties (right click the exe -> Details) get the real version instead of being empty
version_file = None
try:
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
    )

    with open(os.path.join(ROOT, "ald", "core.py"), encoding="utf-8") as f:
        _ver = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M).group(1)
    _nums = tuple(([int(x) for x in re.findall(r"\d+", _ver)[:4]] + [0, 0, 0, 0])[:4])
    _vi = VSVersionInfo(
        ffi=FixedFileInfo(filevers=_nums, prodvers=_nums, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "X-4bys5"),
                StringStruct("FileDescription", "Any Link Downloader"),
                StringStruct("FileVersion", _ver),
                StringStruct("InternalName", "AnyLinkDownloader"),
                StringStruct("OriginalFilename", "AnyLinkDownloader.exe"),
                StringStruct("ProductName", "Any Link Downloader"),
                StringStruct("ProductVersion", _ver),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
    version_file = os.path.join(ROOT, "build", "version_info.txt")
    with open(version_file, "w", encoding="utf-8") as f:
        f.write(str(_vi))
except Exception as exc:  # never worth failing a build over
    print(f"[spec] note: no version info in the exe ({exc})")
    version_file = None

datas += [(os.path.join(ROOT, "assets"), "assets"), (os.path.join(ROOT, "ald", "ui"), os.path.join("ald", "ui"))]
bin_dir = os.path.join(ROOT, "vendor", "bin")
if os.path.isdir(bin_dir):
    if os.environ.get("ALD_SLIM") == "1":
        # slim build: ffmpeg and deno are not packed, the app fetches them on its first start (engine.repair_tools)
        win = os.path.join(bin_dir, "ald-window.exe")
        if os.path.isfile(win):
            datas += [(win, "bin")]
    else:
        datas += [(bin_dir, "bin")]
else:
    print("[spec] WARNING: vendor/bin is missing - the app would ship WITHOUT ffmpeg/deno")

a = Analysis(
    [os.path.join(ROOT, "video_downloader.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + collect_submodules("ald"),
    excludes=[  # not used by the app, leaving them out keeps the build smaller
        "Cryptodome.SelfTest",
        "tkinter",
        "_tkinter",
        "customtkinter",
        "PIL",
        "playwright",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "IPython",
        "pytest",
        "notebook",
        "PyQt5",
        "PyQt6",
        "PySide6",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AnyLinkDownloader",
    console=False,  # window app, no black console box
    upx=False,
    version=version_file,
    icon=os.path.join(ROOT, "assets", "icon.ico") if sys.platform.startswith("win") else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="AnyLinkDownloader")
