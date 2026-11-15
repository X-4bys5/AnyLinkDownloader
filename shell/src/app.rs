use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::thread;
use std::time::Duration;

use tao::dpi::LogicalSize;
use tao::event::{Event, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoopBuilder};
use tao::window::{Icon, WindowBuilder};
use wry::{NewWindowResponse, WebContext, WebViewBuilder};

use crate::logic::{decode_icon, host_port, is_internal, log_line, origin, Opts};

#[derive(Debug, Clone, Copy)]
enum Ev {
    Close,
}

// bundled so the exe doesnt need an icon file next to it
const ICON: &[u8] = include_bytes!("../icon.png");

pub fn run(o: Opts) -> Result<(), String> {
    let base = origin(&o.url).ok_or("bad address")?;
    let event_loop = EventLoopBuilder::<Ev>::with_user_event().build();
    let proxy = event_loop.create_proxy();

    let mut wb = WindowBuilder::new()
        .with_title(o.title.clone())
        .with_inner_size(LogicalSize::new(o.width, o.height))
        .with_min_inner_size(LogicalSize::new(720.0, 520.0));
    if let Some((rgba, w, h)) = decode_icon(ICON) {
        if let Ok(icon) = Icon::from_rgba(rgba, w, h) {
            wb = wb.with_window_icon(Some(icon));
        }
    }
    let window = wb.build(&event_loop).map_err(|e| format!("window: {e}"))?;

    let mut ctx = WebContext::new(o.data_dir.as_ref().map(PathBuf::from));

    let ipc_proxy = proxy.clone();
    let base_nav = base.clone();
    let base_new = base.clone();
    let builder = WebViewBuilder::new_with_web_context(&mut ctx)
        .with_url(o.url.clone())
        .with_devtools(cfg!(debug_assertions)) // only in debug builds
        .with_initialization_script("window.__aldShell = true;") // lets the page know its not in a normal browser
        .with_ipc_handler(move |req| {
            if req.body().as_str() == "close" {
                let _ = ipc_proxy.send_event(Ev::Close);
            }
        })
        // anything that isnt our own local page opens in the real browser
        .with_navigation_handler(move |url| {
            if is_internal(&url, &base_nav) {
                true
            } else {
                let _ = webbrowser::open(&url);
                false
            }
        })
        .with_new_window_req_handler(move |url, _features| {
            if is_internal(&url, &base_new) {
                NewWindowResponse::Allow
            } else {
                let _ = webbrowser::open(&url);
                NewWindowResponse::Deny
            }
        });
    let webview = builder
        .build(&window)
        .map_err(|e| format!("web view: {e} (is the Microsoft WebView2 runtime installed?)"))?;

    // watchdog, if python dies the window would just sit there with a dead page
    if let Some((host, port)) = host_port(&o.url) {
        let p = proxy.clone();
        let data = o.data_dir.clone();
        thread::spawn(move || {
            let addr = match (host.as_str(), port)
                .to_socket_addrs()
                .ok()
                .and_then(|mut a| a.next())
            {
                Some(a) => a,
                None => return,
            };
            let (mut seen, mut misses, mut waited) = (false, 0u32, 0u32);
            loop {
                if TcpStream::connect_timeout(&addr, Duration::from_millis(800)).is_ok() {
                    seen = true;
                    misses = 0;
                } else if seen {
                    misses += 1;
                    if misses >= 4 {
                        // about 8s of nothing, not a single slow answer
                        log_line(
                            data.as_deref(),
                            "the engine stopped answering - closing the window",
                        );
                        let _ = p.send_event(Ev::Close);
                        return;
                    }
                } else {
                    waited += 1;
                    if waited >= 30 {
                        // 30 tries 2s apart, so it gets about a minute to start
                        log_line(
                            data.as_deref(),
                            "the engine never answered - closing the window",
                        );
                        let _ = p.send_event(Ev::Close);
                        return;
                    }
                }
                thread::sleep(Duration::from_secs(2));
            }
        });
    }

    let close_proxy = proxy.clone();
    let _keep = ctx; // the web context has to outlive the webview
    event_loop.run(move |event, _target, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            Event::WindowEvent { event: WindowEvent::CloseRequested, .. } => {
                // ask the page first so it can warn about running downloads, no hook means close right away
                let p = close_proxy.clone();
                let sent = webview.evaluate_script_with_callback(
                    "typeof window.__aldClose === 'function' ? (window.__aldClose(), 'hook') : 'none'",
                    move |answer| {
                        if answer.contains("none") {
                            let _ = p.send_event(Ev::Close);
                        }
                    },
                );
                if sent.is_err() {
                    *control_flow = ControlFlow::Exit;
                }
            }
            Event::UserEvent(Ev::Close) => *control_flow = ControlFlow::Exit,
            _ => {}
        }
    });
}
