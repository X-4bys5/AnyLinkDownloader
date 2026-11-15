import re

# yt-dlp only gives us error text, so all of this is substring matching on the lowercased message
TRANSIENT = (
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "remote end closed",
    "incompleteread",
    "incomplete read",
    "temporary failure",
    "eof occurred",
    "broken pipe",
    "connection refused",
    "bad gateway",
    "service unavailable",
    "gateway time-out",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "network is unreachable",
)

DNS_ERRORS = (
    "getaddrinfo failed",
    "name or service not known",
    "temporary failure in name resolution",
    "failed to resolve",
    "nodename nor servname",
    "no address associated with hostname",
    "could not resolve host",
    "couldn't resolve host",
)


def is_merge_error(msg):
    low = str(msg).lower()
    # these look like merge errors but re-running the merge cant fix them
    if any(
        x in low
        for x in (
            "is not installed",
            "ffmpeg not found",
            "ffprobe and ffmpeg not found",
            "access is denied",
            "winerror",
            "drm",
            "disk full",
            "no space",
        )
    ):
        return False
    return any(
        x in low
        for x in (
            "postprocessing",
            "merging",
            "ffmpeg exited",
            "conversion failed",
            "could not write header",
            "incorrect codec parameters",
            "invalid data found",
            "moov atom not found",
            "error while decoding",
            "unable to merge",
            "invalid argument",
        )
    )


def is_dns_error(msg):
    low = str(msg).lower()
    return any(t in low for t in DNS_ERRORS)


UNREACHABLE = (
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "connection aborted",
    "remote end closed",
    "eof occurred",
    "ssl",
    "certificate",
    "no route to host",
    "network is unreachable",
)


def is_unreachable(msg):
    low = str(msg).lower()
    return is_dns_error(low) or any(t in low for t in UNREACHABLE)


def is_transient(msg):
    low = str(msg).lower()
    return any(t in low for t in TRANSIENT)


def is_rate_limited(msg):
    low = str(msg).lower()
    return "429" in low or "too many requests" in low


def needs_fallback(msg):
    return bool(
        re.search(
            r"http error (403|429)|forbidden|too many requests|format is not available|requested format|impersonat",
            str(msg).lower(),
        )
    )


# first match wins, so the specific ones have to stay above the generic ones
ERROR_TIPS = [
    (
        ("drm protected", "drm-protected", "drm protection", "widevine", "playready", "fairplay"),
        (
            "Protected video",
            "This video is encrypted (Netflix, Disney+ and most paid streaming). This app can't and won't open "
            "those. Use the service's own offline download.",
            None,
        ),
    ),
    (
        ("sign in to confirm", "not a bot", "confirm you're not", "confirm you\u2019re not"),  # curly apostrophe too
        (
            "YouTube wants to know you're human",
            "Sign in to YouTube in your browser, then let this app borrow that sign-in.",
            "cookies",
        ),
    ),
    (
        ("age-restricted", "age restricted", "confirm your age", "inappropriate for some users"),
        (
            "Age-restricted video",
            "YouTube only shows this to signed-in adults. Let the app borrow your browser's sign-in.",
            "cookies",
        ),
    ),
    (
        ("private video",),
        (
            "Private video",
            "Only its owner can watch this. If it's yours, let the app borrow your browser's sign-in.",
            "cookies",
        ),
    ),
    (
        (
            "--cookies",
            "login required",
            "requires login",
            "you need to log in",
            "need to be logged in",
            "authentication required",
            "registered users",
        ),
        (
            "This site wants you signed in",
            "Sign in on the site in your browser, then let the app borrow that sign-in.",
            "cookies",
        ),
    ),
    (
        ("members-only", "members only", "join this channel"),
        (
            "Members-only video",
            "Sign in with a member account in your browser, then let the app borrow that sign-in.",
            "cookies",
        ),
    ),
    (
        ("not available in your country", "blocked it in your country", "geo restrict", "geo-restrict"),
        (
            "Not available in your country",
            "The uploader blocked this video where you are. A downloader can't change that.",
            None,
        ),
    ),
    (
        ("live event will begin", "premieres in", "this live event", "is upcoming"),
        (
            "Hasn't started yet",
            "This is a live stream or premiere that hasn't begun. Try again once it's live.",
            "retry",
        ),
    ),
    (
        (
            "video unavailable",
            "this video is not available",
            "has been removed",
            "no longer available",
            "has been terminated",
            "video has been deleted",
        ),
        ("Video is gone", "It was deleted, made private or blocked. There's nothing to download any more.", None),
    ),
    (
        ("unsupported url",),
        (
            "No video found on that page",
            "The page may only show its video after you sign in or press play. Open it in your browser first, "
            "or copy the direct link to the video.",
            None,
        ),
    ),
    (
        ("http error 404", "404: not found", "error 404", "not found"),
        (
            "That page doesn't exist",
            "The link may be mistyped, cut off when you copied it, or the video was removed.",
            None,
        ),
    ),
    (
        ("unable to extract", "nsig", "signature extraction", "player response", "failed to extract", "unable to find"),
        (
            "The site changed something",
            "The download engine needs a refresh. The app will update it and try again by itself.",
            "update",
        ),
    ),
    (
        ("ffmpeg is not installed", "ffmpeg not found", "ffprobe and ffmpeg not found"),
        ("A needed part is missing", "The app is fetching its video-joining tool. Try again in a minute.", "retry"),
    ),
    (
        ("no space left", "errno 28", "not enough space", "disk full"),
        ("Your disk is full", "Free up some space or save to another drive.", "folder"),
    ),
    (
        ("winerror 5", "winerror 32", "access is denied"),
        (
            "Windows is still holding the file",
            "Usually antivirus scanning it. The video is complete: look in the folder for a file ending in "
            '.temp.mkv and remove ".temp" from the name.',
            None,
        ),
    ),
    (
        (
            "postprocessing",
            "merging",
            "ffmpeg exited",
            "conversion failed",
            "could not write header",
            "incorrect codec parameters",
            "invalid data found",
            "moov atom not found",
        ),
        (
            "Couldn't join video and sound",
            "Even after the automatic repairs. Check that the drive isn't full and try again.",
            "retry",
        ),
    ),
    (
        ("permission denied", "errno 13", "read-only file system"),
        ("Can't save in that folder", "The app isn't allowed to write there. Pick another folder.", "folder"),
    ),
    (
        ("429", "too many requests"),
        ("Slow down", "The site says too many requests. Wait a few minutes and try again.", "retry"),
    ),
    (
        ("http error 403", "forbidden"),
        (
            "The site refused the download",
            "The app already tried its usual workarounds. Signing in through your browser often fixes it.",
            "cookies",
        ),
    ),
    (
        (
            "getaddrinfo",
            "name or service not known",
            "network is unreachable",
            "no route to host",
            "temporary failure in name resolution",
            "timed out",
        ),
        ("No internet", "The connection dropped. The app waits and carries on by itself when it's back.", "retry"),
    ),
]


def explain(msg):
    low = str(msg).lower()
    for needles, tip in ERROR_TIPS:
        if any(n in low for n in needles):
            return tip
    return None


SIGNIN_ERRORS = (
    "sign in to confirm",
    "not a bot",
    "confirm you're not",
    "confirm you\u2019re not",
    "age-restricted",
    "age restricted",
    "confirm your age",
    "inappropriate for some users",
    "login required",
    "requires login",
    "you need to log in",
    "need to be logged in",
    "authentication required",
    "private video",
    "members-only",
    "members only",
    "registered users",
    "--cookies",
)


EXTRACT_ERRORS = (
    "unable to extract",
    "nsig",
    "signature extraction",
    "player response",
    "failed to extract",
    "unable to find",
)
