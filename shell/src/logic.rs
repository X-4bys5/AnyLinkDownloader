use std::io::Write;

pub const DEFAULT_TITLE: &str = "Any Link Downloader";

#[derive(Debug, Clone, PartialEq)]
pub struct Opts {
    pub url: String,
    pub title: String,
    pub data_dir: Option<String>,
    pub width: f64,
    pub height: f64,
}

pub fn parse_args(args: &[String]) -> Result<Opts, String> {
    let mut url: Option<String> = None;
    let mut o = Opts {
        url: String::new(),
        title: DEFAULT_TITLE.to_string(),
        data_dir: None,
        width: 1180.0,
        height: 820.0,
    };
    let mut i = 0;
    while i < args.len() {
        let a = args[i].as_str();
        match a {
            "--title" | "--data" | "--size" => {
                let v = args
                    .get(i + 1)
                    .ok_or_else(|| format!("{a} needs a value"))?
                    .clone();
                match a {
                    "--title" => o.title = v,
                    "--data" => o.data_dir = Some(v),
                    _ => {
                        // clamped so a typo cant open a tiny or giant window
                        let (w, h) = v.split_once('x').ok_or("--size looks like 1180x820")?;
                        o.width = w
                            .trim()
                            .parse::<f64>()
                            .map_err(|_| "bad width")?
                            .clamp(600.0, 8000.0);
                        o.height = h
                            .trim()
                            .parse::<f64>()
                            .map_err(|_| "bad height")?
                            .clamp(400.0, 8000.0);
                    }
                }
                i += 2;
            }
            _ if a.starts_with("--") => return Err(format!("unknown option {a}")),
            _ => {
                if url.is_some() {
                    return Err("only one address is expected".into());
                }
                url = Some(a.to_string());
                i += 1;
            }
        }
    }
    let url = url.ok_or("usage: ald-window <address> [--title T] [--data DIR] [--size WxH]")?;
    // only our own local engine is allowed, never a remote page
    if origin(&url)
        .map(|o| host_port_of_origin(&o).is_some())
        .unwrap_or(false)
        && is_local_host(&url)
    {
        o.url = url;
        Ok(o)
    } else {
        Err("the address must be http://127.0.0.1:<port>/... (the app's own engine)".into())
    }
}

pub fn origin(url: &str) -> Option<String> {
    let (scheme, rest) = url.split_once("://")?;
    if scheme != "http" && scheme != "https" {
        return None;
    }
    let end = rest
        .find(|c| c == '/' || c == '?' || c == '#')
        .unwrap_or(rest.len());
    let host = &rest[..end];
    // user@host urls are refused, http://127.0.0.1@evil.com would otherwise look local
    if host.is_empty() || host.contains('@') {
        return None;
    }
    Some(format!("{scheme}://{host}"))
}

fn host_port_of_origin(origin: &str) -> Option<(String, u16)> {
    let (scheme, host) = origin.split_once("://")?;
    let (h, p) = match host.rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse::<u16>().ok()?),
        None => (host.to_string(), if scheme == "https" { 443 } else { 80 }),
    };
    Some((h, p))
}

fn is_local_host(url: &str) -> bool {
    origin(url)
        .and_then(|o| host_port_of_origin(&o))
        .map(|(h, _)| h == "127.0.0.1" || h == "localhost" || h == "[::1]")
        .unwrap_or(false)
}

pub fn host_port(url: &str) -> Option<(String, u16)> {
    host_port_of_origin(&origin(url)?)
}

pub fn is_internal(url: &str, base_origin: &str) -> bool {
    // about:blank, blob and data urls are the page's own, nothing to hand to the real browser
    if url == "about:blank" || url.starts_with("blob:") || url.starts_with("data:") {
        return true;
    }
    match origin(url) {
        Some(o) => o == base_origin,
        None => false,
    }
}

pub fn decode_icon(bytes: &[u8]) -> Option<(Vec<u8>, u32, u32)> {
    let mut dec = png::Decoder::new(std::io::Cursor::new(bytes));
    dec.set_transformations(png::Transformations::EXPAND);
    let mut reader = dec.read_info().ok()?;
    let size = reader.output_buffer_size()?;
    let mut buf = vec![0u8; size];
    let info = reader.next_frame(&mut buf).ok()?;
    let (w, h) = (info.width, info.height);
    let data = &buf[..info.buffer_size()];
    // 16 bit and indexed pngs arent worth handling, the bundled icon is plain 8 bit rgba
    if info.bit_depth != png::BitDepth::Eight {
        return None;
    }
    let rgba = match info.color_type {
        png::ColorType::Rgba => data.to_vec(),
        png::ColorType::Rgb => data
            .chunks_exact(3)
            .flat_map(|p| [p[0], p[1], p[2], 255])
            .collect(),
        png::ColorType::GrayscaleAlpha => data
            .chunks_exact(2)
            .flat_map(|p| [p[0], p[0], p[0], p[1]])
            .collect(),
        png::ColorType::Grayscale => data.iter().flat_map(|&g| [g, g, g, 255]).collect(),
        png::ColorType::Indexed => return None,
    };
    if rgba.len() != (w as usize) * (h as usize) * 4 {
        return None;
    }
    Some((rgba, w, h))
}

pub fn log_line(data_dir: Option<&str>, msg: &str) {
    // a log that cant be written should never take the window down, so every error is ignored
    let Some(dir) = data_dir else { return };
    let _ = std::fs::create_dir_all(dir);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(std::path::Path::new(dir).join("shell.log"))
    {
        let _ = writeln!(f, "{msg}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn a(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn reads_the_command_line() {
        let o = parse_args(&a(&[
            "http://127.0.0.1:5123/?t=abc",
            "--title",
            "My App",
            "--data",
            "C:\\x\\webview",
            "--size",
            "1000x700",
        ]))
        .unwrap();
        assert_eq!(o.url, "http://127.0.0.1:5123/?t=abc");
        assert_eq!(o.title, "My App");
        assert_eq!(o.data_dir.as_deref(), Some("C:\\x\\webview"));
        assert_eq!((o.width, o.height), (1000.0, 700.0));
        let d = parse_args(&a(&["http://localhost:80/"])).unwrap();
        assert_eq!(d.title, DEFAULT_TITLE);
        assert_eq!((d.width, d.height), (1180.0, 820.0));
    }

    #[test]
    fn refuses_anything_that_is_not_the_local_engine() {
        for bad in [
            "https://example.com/",
            "http://192.168.1.5:8000/",
            "file:///C:/x.html",
            "javascript:alert(1)",
            "127.0.0.1:5000",
            "http://user@127.0.0.1:5/",
        ] {
            assert!(parse_args(&a(&[bad])).is_err(), "{bad} should be refused");
        }
        assert!(parse_args(&a(&[])).is_err());
        assert!(parse_args(&a(&["http://127.0.0.1:1/", "http://127.0.0.1:2/"])).is_err());
        assert!(parse_args(&a(&["http://127.0.0.1:1/", "--bogus"])).is_err());
        assert!(parse_args(&a(&["http://127.0.0.1:1/", "--size", "wide"])).is_err());
        assert!(parse_args(&a(&["http://127.0.0.1:1/", "--title"])).is_err());
    }

    #[test]
    fn sizes_are_kept_sensible() {
        let o = parse_args(&a(&["http://127.0.0.1:1/", "--size", "10x10"])).unwrap();
        assert_eq!((o.width, o.height), (600.0, 400.0));
    }

    #[test]
    fn origins_and_ports() {
        assert_eq!(
            origin("http://127.0.0.1:5000/a?b#c").as_deref(),
            Some("http://127.0.0.1:5000")
        );
        assert_eq!(
            origin("http://127.0.0.1:5000").as_deref(),
            Some("http://127.0.0.1:5000")
        );
        assert_eq!(origin("ftp://x/"), None);
        assert_eq!(
            host_port("http://127.0.0.1:5123/?t=1"),
            Some(("127.0.0.1".to_string(), 5123))
        );
        assert_eq!(
            host_port("http://localhost/"),
            Some(("localhost".to_string(), 80))
        );
    }

    #[test]
    fn links_stay_or_leave() {
        let base = "http://127.0.0.1:5123";
        assert!(is_internal("http://127.0.0.1:5123/api/x?y=1", base));
        assert!(is_internal("about:blank", base));
        assert!(is_internal("blob:http://127.0.0.1:5123/uuid", base));
        assert!(!is_internal("https://www.youtube.com/watch?v=1", base));
        assert!(!is_internal("http://127.0.0.1:9999/", base));
        assert!(!is_internal("http://127.0.0.1:5123.evil.com/", base));
        assert!(!is_internal("javascript:alert(1)", base));
    }

    #[test]
    fn the_icon_decodes() {
        let (px, w, h) =
            decode_icon(include_bytes!("../icon.png")).expect("icon.png should decode");
        assert!(w > 0 && h > 0);
        assert_eq!(px.len(), (w * h * 4) as usize);
        assert!(decode_icon(b"not a png").is_none());
    }

    #[test]
    fn logging_never_panics() {
        log_line(None, "nothing");
        let d = std::env::temp_dir().join("ald_window_test_log");
        let _ = std::fs::remove_dir_all(&d);
        log_line(Some(d.to_str().unwrap()), "hello");
        assert_eq!(
            std::fs::read_to_string(d.join("shell.log")).unwrap().trim(),
            "hello"
        );
        let _ = std::fs::remove_dir_all(&d);
    }
}
