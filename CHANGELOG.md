# Changelog

All notable changes to Any Link Downloader. Versions are `major.minor.patch`; while the major number is 0 the app is still in beta.

## [Unreleased]

## [0.11.8] - 2026-11-15

First public release.

### Added
- Download video or audio from the sites yt-dlp supports, playlists and channels included, with format picking and size shown before downloading.
- Spotify links through spotDL, with a built-in matcher as fallback.
- Studio tab: cut, split, join, rearrange, rotate, mirror, mute and convert (MP4, MKV, WebM, MOV, GIF, MP3, M4A, FLAC, WAV, Opus, OGG).
- Automatic recovery: dropped connections, 403/429 answers, broken yt-dlp, failed merges, antivirus-deleted ffmpeg/deno, interrupted downloads.
- Windows installer built by GitHub Actions, and in-app updates from GitHub releases.
- Signed quick updates for the `ald/` folder, so small fixes don't need a new installer.
- Optional native window (Rust + WebView2).
