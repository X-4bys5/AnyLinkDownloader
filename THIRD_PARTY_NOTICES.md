# Third party software

Any Link Downloader is built on, and the Windows installer bundles, the following projects. Each keeps its own license; please check the linked project for the full text.

| Project | Used for | License |
|---|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | downloading | Unlicense |
| [FFmpeg](https://ffmpeg.org) | merging, converting, Studio | GPL v3 for the bundled build (gyan.dev "essentials") |
| [Deno](https://deno.com) | JavaScript runtime some sites need | MIT |
| [spotDL](https://github.com/spotDL/spotify-downloader) | Spotify song matching | MIT |
| [PyInstaller](https://pyinstaller.org) | packaging | GPL v2 with a bootloader exception that allows bundling |
| [wry](https://github.com/tauri-apps/wry) | optional native window | MIT / Apache-2.0 |
| other Python packages (requests, mutagen, certifi, ...) | various | see each package |

## FFmpeg source

The installer ships an unmodified FFmpeg build downloaded from https://www.gyan.dev/ffmpeg/builds/ (fallback: https://github.com/BtbN/FFmpeg-Builds). FFmpeg's source code for that build is available at https://ffmpeg.org/download.html and from the build page above.
