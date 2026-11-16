# Any Link Downloader

[![Release](https://img.shields.io/github/v/release/X-4bys5/AnyLinkDownloader?label=release&v=2)](https://github.com/X-4bys5/AnyLinkDownloader/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/X-4bys5/AnyLinkDownloader/total?v=2)](https://github.com/X-4bys5/AnyLinkDownloader/releases)
[![License](https://img.shields.io/github/license/X-4bys5/AnyLinkDownloader?v=2)](LICENSE)
![Platform](https://img.shields.io/badge/platform-windows%2010%2F11-blue)

windows app for saving videos and audio from a link. paste the link, pick a quality, download. built on yt-dlp and ffmpeg, and it has a small editor (Studio) and spotify song support on top.

the app itself is python. the window is just a local web page, served from `127.0.0.1`, so most of the ui is plain html css js.

## download

get the installer from the [latest release](https://github.com/X-4bys5/AnyLinkDownloader/releases/latest), or [download it directly](https://github.com/X-4bys5/AnyLinkDownloader/releases/latest/download/AnyLinkDownloader-Setup.exe). windows 10 or 11, no python needed. the app updates itself after that.

## what it does

- downloads video or audio from most sites yt-dlp supports, playlists and channels included
- picks formats for you and shows the size before you download (a `~` means its a guess)
- spotify links work through spotDL, see below
- Studio tab for cutting, joining, converting and a few other edits without leaving the app
- updates itself from GitHub releases, small fixes arrive as quick updates without a new installer (see "updating")

## stack

- python 3.10+
- yt-dlp, ffmpeg, and a js runtime like deno (yt-dlp needs one for some sites)
- html css js frontend, no framework
- rust (wry) for the optional native window in `shell/`
- pyinstaller + inno setup for the installer, built by github actions

## setup

```
git clone https://github.com/X-4bys5/AnyLinkDownloader.git
cd AnyLinkDownloader
python -m venv venv
venv\Scripts\activate
pip install -r requirements-build.txt
```

run it:
```
python video_downloader.py
```

`python video_downloader.py --serve` only prints the local address instead of opening a window.

you need ffmpeg and ffprobe on your PATH if you run from source. the installer build bundles them.

## how it works

downloads go into a separate working folder first (`%LOCALAPPDATA%\AnyLinkDownloader\Downloading`, or a hidden `.Downloading` folder next to the save folder if thats on another drive). only the finished file gets moved into the save folder, and an existing name never gets overwritten (`name (2).mp4`).

stuff it handles on its own:

- a video thats already saved is skipped (matched by the video id in the file name)
- dropped connection, it waits and carries on when the internet is back
- 403 or 429 answers step down to fewer connections, then other youtube clients, then one connection
- "unable to extract" downloads a fresh yt-dlp, switches to it without a restart and retries. a broken update gets dropped
- sign-in and age checks borrow the cookies from an installed browser
- failed video + audio merge tries mp4, then mkv, then a fresh download
- blocked sites get tested through a few routes at once
- ffmpeg or deno deleted by antivirus gets fetched again
- an interrupted download is offered again on the next start

sizes come from the exact streams yt-dlp is going to fetch. speed and time left are measured over the last few seconds.

## Studio

trims, splits, deletes, rearranges and joins clips, rotates, mirrors, mutes and converts files (MP4, MKV, WebM, MOV, GIF, MP3, M4A, FLAC, WAV, Opus, OGG). your original is never written to, the result is built as a hidden `.studio-part` file, checked, and only then renamed.

- exact mode is frame accurate, a cut that lands on a keyframe gets copied without re-encoding
- fast mode is always lossless but snaps cuts back to the previous keyframe
- only the first video and first audio track are kept
- hardware encoding is tried first (NVENC, QSV, AMF) and falls back to the cpu. `ALD_NO_HWENC=1` turns it off, `ALD_NO_HWACCEL=1` turns off hardware decoding too

## spotify

spotify links download through spotDL. songs get matched on youtube music and saved as tagged files with cover art (mp3, m4a or flac). if spotDL fails on a song the built in matcher takes over. spotify's own streams are DRM protected so you cant save those, the audio is the youtube music upload.

the public playlist page only shows 100 songs. for longer playlists add `spotify_client_id` and `spotify_client_secret` to `settings.json`. set `"spotify_engine": "builtin"` to skip spotDL.

## updating

the app updates itself from the releases of this repo. the installer is built by github actions, you never build or upload it by hand.

versions look like `0.11.8` (still in beta while the first number is 0). the version lives in one place, `APP_VERSION` in `ald/core.py`, the installer, the exe properties and the release tag all read it from there. changes go in `CHANGELOG.md` under `[Unreleased]`, the release notes in the app are taken from there.

to ship a new version:

```
python tools/bump.py patch --push
```

(`minor` or an exact version like `0.12.0` works too, `--tag` only commits and tags and leaves the push to you.) that commits, tags `vX.Y.Z` and pushes. the **Release** workflow then builds the native window, runs the self test, builds `AnyLinkDownloader-Setup.exe` with its `.sha256` and publishes the release. installed copies pick it up on their own.

if github actions is not available (billing problem, outage), do the same on your own pc with the gh cli (`gh auth login` once):

```
python tools/bump.py patch --tag
python tools/ship.py
```

that builds the installer locally and publishes the release itself. without the gh cli it stops after the build and tells you which two files to upload to the release by hand.

it checks at most every 12 hours and from Settings. only github.com addresses are accepted and no token is stored.

## quick updates (ota)

for small changes (ui tweaks, bug fixes, speed ups) you dont need a new installer. a small signed patch of the `ald/` folder (about 250 KB) goes out and every installed copy picks it up by itself, it switches on the next time the app opens. the installer is still needed for new yt-dlp/ffmpeg bundles, the rust window or new python packages.

setting it up, once:

1. `python tools/release.py keygen` makes your signing key (saved in `~/.ald/ota_key.pem`, back it up and never commit it)
2. paste the public key it prints into `PUBKEY` in `ota.py`
3. add the contents of the private key file as a repo secret called `OTA_SIGNING_KEY` (Settings, Secrets and variables, Actions)
4. ship a normal release, quick updates only work on builds that contain your key

after that every push to `main` that changes something in `ald/` runs the **Quick update** workflow: lint, build, sign, upload to a pre-release called `ota`. without the secret it just skips itself. by hand it still works:

```
python tools/release.py ota "what changed" --publish
```

what keeps it safe:
- the app only accepts patches signed with your key, over the github.com hosts, and only for the exact `APP_VERSION` it was built with (a new installer = new base, old patches are ignored and wiped)
- patches can only contain `ald/` files of known types (no exe, dll or pyd), paths are checked and every .py gets compiled before it is used
- if a patch fails to import, or the app doesnt get through start up twice in a row, it is dropped and the app goes back to the installed files by itself
- `--no-patch` (or `ALD_NO_PATCH=1`) starts the app without any patch if you ever need to
- the installer update check ignores the `ota` release because its a pre-release

it checks every 3 hours (only when "keep itself up to date" is on) and from Settings.

## smaller installer

`python tools/build.py --slim` (or `python tools/ship.py --slim`) leaves ffmpeg and deno out of the installer. the app downloads them by itself the first time it starts (about 120 MB, needs internet once, they stay after updates). the self test still runs with them.

## native window

`shell/` has `ald-window`, a small rust program that shows the app in webview2 instead of an edge/chrome app window. if `vendor/bin/ald-window.exe` is missing or wont start the app just opens the browser window instead. `ALD_NO_SHELL=1` turns it off. more in `shell/README.md`.

## project layout

```
video_downloader.py     entry point: startup, single instance, self test
ota.py                  quick updates: loads/downloads signed patches (stays outside ald/ on purpose)
tools/build.py          one click build: bundle, self test, installer
tools/bump.py           changes the version, tags and pushes a release
tools/release.py        makes the signing key, builds and uploads quick updates
.github/workflows/      ci (lint), release (installer), ota (quick updates), native window
CHANGELOG.md            what changed in each version
ald/
  core.py               paths, version, logging, small helpers
  settings.py           defaults, speed profiles, settings file
  storage.py            save folders, leftover files, file name matching
  urls.py               link classification
  pages.py              pulling media links out of normal web pages
  network.py            proxies, dns, blocked site routes, connectivity
  errors.py             error matching and the plain language messages
  formats.py            format choices and size estimates
  jobs.py               the Job model and its logger
  engine.py             loads yt-dlp, speed patches, engine updater
  progress.py           size, speed and time left
  appupdate.py          self update from github releases
  spotify.py            spotify links, song matching, cover art, tags
  spotdl_engine.py      spotDL integration
  studio.py             editor backend (probe, preview, cut, join, convert)
  server.py             local http server (secret cookie, SSE, range requests)
  winutil.py            windows stuff: browser window, clipboard, folder dialog
  manager/              the "brain", split into mixins
    app.py                state, events, settings, startup and shutdown
    analyze.py            reading a link (video, playlist, channel, spotify)
    options.py            yt-dlp options and blocked site routes
    jobqueue.py           queue, workers, retry, cancel
    staging.py            working folders, moving finished files
    downloads.py          the download itself, repairs and retries
    library.py            saved files and whats already downloaded
  ui/                   index.html, css/, js/, fonts/
shell/                  rust native window
vendor/bin/             ffmpeg, ffprobe, deno, ald-window.exe
```

## known issues

```
- windows smartscreen warns about the installer since its not signed (more info, then run anyway)
- spotify playlists stop at 100 songs without your own client id/secret
- DRM protected streams cant be saved, thats on purpose
- studio only keeps the first video and audio track
```

## license

[MIT](LICENSE). the installer also bundles ffmpeg and deno, see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

log is at `%LOCALAPPDATA%\AnyLinkDownloader\app.log`, and Settings has a "copy report for help" button. only download stuff you have the right to.
