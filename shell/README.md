# ald-window

small rust program that shows the app's page in the system web view (webview2 on windows) in its own window. python still does all the actual work.

- only opens `http://127.0.0.1:<port>/`
- links to other sites open in your normal browser
- the X button asks the page first, so you get a warning if downloads are running
- closes itself if the app stops answering
- if it cant start (webview2 missing for example) the app falls back to an edge/chrome window

## getting ald-window.exe

option 1: push the project to github, run the "Build native window" workflow, and drop the downloaded `ald-window.exe` into `vendor/bin/`.

option 2: install rust (https://rustup.rs, plus the c++ build tools on windows) and run `build.bat`, it builds this too if it finds `cargo`.

`cargo test` runs the logic tests on any system. set `ALD_NO_SHELL=1` to turn the window off.
