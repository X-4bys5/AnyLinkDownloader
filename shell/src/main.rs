// small launcher, the real window code is in app.rs
#![cfg_attr(windows, windows_subsystem = "windows")]

#[cfg(any(windows, target_os = "macos"))]
mod app;
mod logic;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let opts = match logic::parse_args(&args) {
        Ok(o) => o,
        Err(e) => {
            eprintln!("ald-window: {e}");
            std::process::exit(2); // non-zero within 30s of launch and winutil.py opens the browser window instead
        }
    };
    #[cfg(any(windows, target_os = "macos"))]
    {
        let data = opts.data_dir.clone();
        if let Err(e) = app::run(opts) {
            logic::log_line(data.as_deref(), &format!("could not open the window: {e}"));
            eprintln!("ald-window: {e}");
            std::process::exit(1);
        }
    }
    // linux only builds the logic so cargo test works there
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        let _ = opts;
        eprintln!("ald-window: the native window is only built for Windows (and macOS)");
        std::process::exit(3);
    }
}
