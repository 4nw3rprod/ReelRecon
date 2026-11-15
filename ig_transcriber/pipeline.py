from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import shutil
import socket
import statistics
import subprocess
import tempfile
import time
import warnings
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha1
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL 1.1.1+.*")
warnings.filterwarnings("ignore", message="Support for Python version 3.9 has been deprecated.*")


def _env_str(name: str) -> Optional[str]:
    # REELRECON_* is the primary prefix; the legacy IG_TRANSCRIBER_* prefix
    # remains supported so existing setups keep working after the rename.
    for key in (f"REELRECON_{name}", f"IG_TRANSCRIBER_{name}"):
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    return None


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = _env_str(name) or default
    try:
        return max(int(raw), minimum)
    except (TypeError, ValueError):
        return default


INSTAGRAM_APP_ID = "936619743392459"
DEFAULT_TIMEOUT_SECONDS = _env_int("HTTP_TIMEOUT_SECONDS", 30, minimum=1)
FETCH_RETRY_ATTEMPTS = _env_int("FETCH_RETRIES", 3, minimum=1)
INSTAGRAM_VIDEO_LIMIT = 10
# A video counts as an outlier when its metric is at least this multiple of the batch median.
OUTLIER_MULTIPLIER = 2.0
MIN_VIDEOS_FOR_PERFORMANCE = 3
HOOK_WINDOW_SECONDS = 3.0
# Frame extraction for visual analysis: shot changes at or above SCENE_THRESHOLD become frames.
DEFAULT_MAX_FRAMES = 12
MAX_FRAMES_LIMIT = 60
FRAME_WIDTH = 512
SCENE_THRESHOLD = 0.3
FFMPEG_TIMEOUT_SECONDS = 300
# ffmpeg only ever reads local files we downloaded, and only as plain media: no playlists (HLS/concat)
# that could pull in other local files or URLs from a hostile download.
FFMPEG_INPUT_GUARD = ["-protocol_whitelist", "file", "-format_whitelist", "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,image2"]
MAX_COVER_BYTES = 10 * 1024 * 1024
COVER_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
HOOK_FRAME_STEP_SECONDS = 0.5
DEFAULT_ANALYSIS_SHOT_FRAMES = 8
# The hook-analysis method ships as an installable skill; the MCP tools serve the same file.
METHOD_PATH = Path(__file__).resolve().parent.parent / "skills" / "reelrecon-hook-analyzer" / "SKILL.md"
GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
ProgressCallback = Callable[[str, int, str], None]

STOPWORDS = {
    "a",
    "about",
    "all",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "has",
    "have",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "more",
    "my",
    "not",
    "of",
    "on",
    "or",
    "our",
    "out",
    "so",
    "that",
    "the",
    "their",
    "them",
    "there",
    "these",
    "they",
    "this",
    "to",
    "up",
    "use",
    "was",
    "we",
    "what",
    "when",
    "with",
    "you",
    "your",
}

POSITIVE_WORDS = {
    "amazing",
    "best",
    "better",
    "boost",
    "easy",
    "fast",
    "great",
    "improve",
    "love",
    "powerful",
    "simple",
    "smart",
    "strong",
    "win",
}

NEGATIVE_WORDS = {
    "bad",
    "broken",
    "difficult",
    "fail",
    "hard",
    "issue",
    "mistake",
    "problem",
    "risk",
    "slow",
    "stuck",
    "worse",
}

CTA_PATTERNS = (
    "follow",
    "subscribe",
    "comment",
    "share",
    "like",
    "buy",
    "dm",
    "message me",
    "link in bio",
    "sign up",
    "download",
    "book a call",
    "join",
)


class PipelineError(RuntimeError):
    pass


def _import_whisper() -> Any:
    # Imported lazily: pulling in whisper/torch takes seconds and should not
    # delay (or crash) callers that never transcribe, such as MCP server startup.
    try:
        import whisper
    except Exception as exc:
        raise PipelineError(
            f"The 'openai-whisper' package is not usable: {exc}. "
            "Install dependencies with: pip install -r requirements.txt"
        ) from exc
    return whisper


def _import_yt_dlp() -> Any:
    try:
        from yt_dlp import YoutubeDL
    except Exception as exc:
        raise PipelineError(
            f"The 'yt-dlp' package is not usable: {exc}. "
            "Install dependencies with: pip install -r requirements.txt"
        ) from exc
    return YoutubeDL


def require_ffmpeg() -> str:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise PipelineError(
            "ffmpeg was not found on PATH. It is required for audio extraction and transcription. "
            "Install it (e.g. `apt install ffmpeg` or `brew install ffmpeg`) and retry."
        )
    return ffmpeg_path


def _atomic_write_text(path: Path, text: str) -> None:
    # Write via a temp file in the same directory and atomically replace, so a
    # crash mid-write can never leave a truncated/corrupt file behind.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class VideoCandidate:
    source_kind: str
    input_url: str
    canonical_url: str
    source_label: str
    source_group: str
    video_id: str
    timestamp: int
    title: str
    caption: str
    video_url: str
    uploader: str
    platform: str
    position: int = 1
    total_videos: int = 1
    # views / likes / comments / duration_seconds; None when the source did not expose it.
    metrics: Dict[str, Any] = field(default_factory=dict)
    # The image shown on the profile grid and in shares; may be a custom upload rather than a frame.
    cover_url: str = ""


def _emit(progress_callback: Optional[ProgressCallback], stage: str, percent: int, message: str) -> None:
    if progress_callback is not None:
        progress_callback(stage, percent, message)


def _safe_slug(value: str, fallback: str = "item") -> str:
    # Strip leading/trailing dots too so a slug can never be a path-traversal
    # component like "." or "..".
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.").lower()
    if not slug or set(slug) <= {".", "-"}:
        return fallback
    return slug


def _timestamp_to_iso(timestamp: int) -> Optional[str]:
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _file_sha1(path: Path) -> str:
    digest = sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_instagram_host(netloc: str) -> bool:
    return "instagram.com" in netloc.lower()


def normalize_input_url(input_url: str) -> tuple[str, str]:
    parsed = urlparse(input_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise PipelineError("The input URL must start with http:// or https://")
    return input_url.strip(), parsed.geturl()


def detect_input_kind(input_url: str) -> tuple[str, str]:
    parsed = urlparse(input_url)
    if _is_instagram_host(parsed.netloc):
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise PipelineError("Could not determine the Instagram target from the URL")
        if parts[0] in {"reel", "p", "tv"}:
            return "video", input_url
        username = parts[0].lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9._]+", username):
            raise PipelineError(f"Invalid Instagram username parsed from URL: {username}")
        return "instagram_profile", f"https://www.instagram.com/{username}/"
    return "video", input_url


def _instagram_session_cookies() -> Dict[str, str]:
    """instagram.com cookies from the configured session file (saved login or REELRECON_COOKIES_FILE)."""
    cookies_file, _ = cookie_settings()
    if not cookies_file:
        return {}
    jar = MozillaCookieJar()
    try:
        jar.load(cookies_file, ignore_discard=True, ignore_expires=True)
    except Exception:
        return {}
    return {cookie.name: cookie.value for cookie in jar if "instagram.com" in (cookie.domain or "") and cookie.value}


def _instagram_cookie_header() -> Optional[str]:
    cookies = _instagram_session_cookies()
    return "; ".join(f"{name}={value}" for name, value in cookies.items()) or None


LOGIN_HINT = "Run `npx -y reelrecon login` (or `python -m ig_transcriber.login` from a clone) once to connect your Instagram account."
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
# shortcut: Instagram rotates GraphQL doc IDs every few weeks or months. When profile scans fail with
# "unexpected response", copy the new doc_id of the Reels tab's PolarisProfileReelsTabContentQuery_connection
# request from browser devtools into REELRECON_REELS_DOC_ID, then update this default.
DEFAULT_REELS_DOC_ID = "28237651492507044"


def _instagram_request(request: Request, what: str) -> bytes:
    """Fetch from Instagram with retries for rate limits, 5xx and network errors."""
    last_error: Optional[PipelineError] = None
    for attempt in range(1, FETCH_RETRY_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404:
                raise PipelineError(f"Instagram returned 404 for {what}.") from exc
            if exc.code in {401, 403}:
                raise PipelineError(f"Instagram refused the request for {what} (HTTP {exc.code}). " + LOGIN_HINT) from exc
            if exc.code == 429:
                last_error = PipelineError("Instagram rate-limited the request. Wait a few minutes and try again.")
            elif exc.code >= 500:
                last_error = PipelineError(f"Instagram failed with HTTP {exc.code} for {what}")
            else:
                raise PipelineError(f"Instagram returned HTTP {exc.code} for {what}") from exc
        except (URLError, TimeoutError) as exc:
            last_error = PipelineError(f"Network error while fetching {what}: {getattr(exc, 'reason', exc)}")
        if attempt < FETCH_RETRY_ATTEMPTS:
            time.sleep(min(2 ** attempt, 10))
    assert last_error is not None
    raise last_error


@dataclass(frozen=True)
class InstagramProfilePage:
    user_id: str
    lsd: str
    fb_dtsg: str


def _page_token(html: str, name: str) -> Optional[str]:
    match = re.search(r'"%s",\[\],\{"token":"([^"]+)"' % re.escape(name), html)
    return match.group(1) if match else None


def fetch_profile_page(username: str, cookies: Dict[str, str]) -> InstagramProfilePage:
    """Load a profile page as the logged-in user: its numeric ID plus the tokens Instagram's GraphQL calls need."""
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Dest": "document",
    }
    html = _instagram_request(
        Request(f"https://www.instagram.com/{username}/", headers=headers), f"the @{username} profile page"
    ).decode("utf-8", "replace")

    viewer_id = cookies.get("ds_user_id")
    if not viewer_id or viewer_id not in html:
        raise PipelineError("Instagram did not recognise your saved login; it has probably expired. " + LOGIN_HINT)

    name = re.escape(username)
    match = re.search(r'"username":"%s"[^{}]{0,300}?"(?:id|pk)":"(\d+)"' % name, html, re.IGNORECASE) or re.search(
        r'"(?:id|pk)":"(\d+)"[^{}]{0,300}?"username":"%s"' % name, html, re.IGNORECASE
    )
    if not match:
        raise PipelineError(f"Instagram profile not found: https://www.instagram.com/{username}/")

    # Public profiles only, even when the logged-in user follows a private account.
    if re.search(r'"username":"%s"[^{}]{0,400}?"is_private":true|"is_private":true[^{}]{0,400}?"username":"%s"' % (name, name), html, re.IGNORECASE):
        raise PipelineError("This Instagram profile is private. Only public profiles are supported.")

    lsd, fb_dtsg = _page_token(html, "LSD"), _page_token(html, "DTSGInitialData")
    if not lsd or not fb_dtsg:
        raise PipelineError("Instagram's page format changed (missing request tokens). Update ReelRecon and retry.")
    return InstagramProfilePage(user_id=match.group(1), lsd=lsd, fb_dtsg=fb_dtsg)


def _find_key(data: Any, key: str) -> Any:
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for value in data.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def fetch_profile_reels(username: str, canonical_url: str) -> list[Dict[str, Any]]:
    """Newest-first reels of a profile, via the same GraphQL query instagram.com's Reels tab uses."""
    cookies = _instagram_session_cookies()
    if not cookies.get("sessionid"):
        raise PipelineError(
            "Profile scans need your Instagram login: Instagram no longer lists a profile's posts to logged-out visitors. "
            + LOGIN_HINT
        )
    page = fetch_profile_page(username, cookies)
    variables = {
        "data": {"include_feed_video": True, "page_size": 12, "target_user_id": page.user_id},
        "first": 12,
        "id": page.user_id,
        "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": True,
    }
    doc_id = _env_str("REELS_DOC_ID") or DEFAULT_REELS_DOC_ID
    body = urlencode(
        {
            "lsd": page.lsd,
            "fb_dtsg": page.fb_dtsg,
            "fb_api_req_friendly_name": "PolarisProfileReelsTabContentQuery_connection",
            "doc_id": doc_id,
            "variables": json.dumps(variables),
        }
    ).encode("utf-8")
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Origin": "https://www.instagram.com",
        "Referer": f"https://www.instagram.com/{username}/reels/",
        "X-CSRFToken": cookies.get("csrftoken", ""),
        "X-IG-App-ID": INSTAGRAM_APP_ID,
        "X-FB-LSD": page.lsd,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    raw = _instagram_request(
        Request("https://www.instagram.com/api/graphql", data=body, headers=headers, method="POST"),
        f"the @{username} reels list",
    )
    text = raw.decode("utf-8", "replace")
    unexpected = PipelineError(
        f"Instagram returned an unexpected response for the @{username} reels list. Instagram may have rotated its "
        f"query ID ({doc_id}); update ReelRecon or set REELRECON_REELS_DOC_ID."
    )
    if "{" not in text:
        raise unexpected
    try:
        payload = json.loads(text[text.index("{"):])
    except json.JSONDecodeError as exc:
        raise unexpected from exc
    if payload.get("errors") and not payload.get("data"):
        messages = "; ".join(str(err.get("message")) for err in payload["errors"][:2])
        raise PipelineError(f"Instagram rejected the reels query: {messages}. " + str(unexpected))
    edges = _find_key(payload.get("data"), "edges")
    if not isinstance(edges, list):
        raise unexpected
    return [edge["node"].get("media") or edge["node"] for edge in edges if isinstance(edge, dict) and edge.get("node")]


def _count(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def metrics_from_reel_media(media: Dict[str, Any]) -> Dict[str, Any]:
    plays = _count(media.get("play_count"))
    duration = media.get("video_duration")
    return {
        "views": plays if plays is not None else _count(media.get("view_count")),
        "likes": _count(media.get("like_count")),
        "comments": _count(media.get("comment_count")),
        "duration_seconds": round(float(duration), 1) if duration else None,
    }


def metrics_from_yt_dlp_info(info: Dict[str, Any]) -> Dict[str, Any]:
    duration = info.get("duration")
    return {
        "views": _count(info.get("view_count")),
        "likes": _count(info.get("like_count")),
        "comments": _count(info.get("comment_count")),
        "duration_seconds": round(float(duration), 1) if duration else None,
    }


def merge_metrics(primary: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("views", "likes", "comments", "duration_seconds")
    return {key: primary.get(key) if primary.get(key) is not None else fallback.get(key) for key in keys}


def _instagram_video_url(shortcode: str, product_type: Optional[str]) -> str:
    if product_type == "clips":
        return f"https://www.instagram.com/reel/{shortcode}/"
    if product_type == "igtv":
        return f"https://www.instagram.com/tv/{shortcode}/"
    return f"https://www.instagram.com/p/{shortcode}/"


def collect_instagram_profile_videos(canonical_url: str) -> list[VideoCandidate]:
    username = urlparse(canonical_url).path.strip("/").split("/")[0]
    candidates: list[VideoCandidate] = []
    for media in fetch_profile_reels(username, canonical_url):
        shortcode = media.get("code")
        # Pinned reels are old picks shown first, not the latest uploads.
        if not shortcode or media.get("clips_tab_pinned_user_ids"):
            continue
        candidates.append(
            VideoCandidate(
                source_kind="instagram_profile",
                input_url=canonical_url,
                canonical_url=canonical_url,
                source_label=username,
                source_group="instagram_profiles",
                video_id=shortcode,
                timestamp=0,  # filled in from yt-dlp when the audio is downloaded
                title=shortcode,
                caption="",
                video_url=_instagram_video_url(shortcode, media.get("product_type")),
                uploader=username,
                platform="instagram",
                metrics=metrics_from_reel_media(media),
                cover_url=str((((media.get("image_versions2") or {}).get("candidates") or [{}])[0]).get("url") or ""),
            )
        )
        if len(candidates) == INSTAGRAM_VIDEO_LIMIT:
            break

    if not candidates:
        raise PipelineError(f"No reels found for @{username}. The account may be private or have no reels.")
    total = len(candidates)
    return [replace(candidate, position=index, total_videos=total) for index, candidate in enumerate(candidates, start=1)]


def reelrecon_home() -> Path:
    return Path(_env_str("HOME") or Path.home() / ".reelrecon").expanduser()


def saved_session_path() -> Path:
    """Where `reelrecon login` stores the user's Instagram session (a cookies.txt file)."""
    return reelrecon_home() / "instagram_cookies.txt"


def session_source() -> Optional[str]:
    """Which Instagram session will be used, if any: env_cookies_file, env_browser, or saved_login."""
    if _env_str("COOKIES_FILE"):
        return "env_cookies_file"
    if _env_str("COOKIES_FROM_BROWSER"):
        return "env_browser"
    if saved_session_path().is_file():
        return "saved_login"
    return None


def cookie_settings() -> tuple[Optional[str], Optional[str]]:
    """Own-session cookies, needed for profile scans now that Instagram hides them from logged-out requests.

    Public reels normally work anonymously (same as playing the video after
    dismissing the login popup in a browser). When Instagram rate-limits or
    login-walls anonymous requests, users can supply their own logged-in
    session: REELRECON_COOKIES_FILE (a Netscape cookies.txt export) or
    REELRECON_COOKIES_FROM_BROWSER (e.g. "chrome", "firefox:ProfileName").
    Without either, the session saved by `reelrecon login` is used when present.
    """
    cookies_file = _env_str("COOKIES_FILE")
    if not cookies_file and not _env_str("COOKIES_FROM_BROWSER") and saved_session_path().is_file():
        return str(saved_session_path()), None
    if cookies_file:
        path = Path(cookies_file).expanduser()
        if not path.is_file():
            raise PipelineError(
                f"REELRECON_COOKIES_FILE points to a missing file: {path}. "
                "Export a cookies.txt from your logged-in browser (e.g. the 'Get cookies.txt' extension) "
                "or unset the variable to use anonymous access."
            )
        cookies_file = str(path)
    return cookies_file, _env_str("COOKIES_FROM_BROWSER")


_LOGIN_WALL_MARKERS = (
    "login required",
    "log in",
    "login",
    "rate-limit",
    "rate limit",
    "requested content is not available",
    "checkpoint required",
    "checkpoint_required",
)


def _download_error(exc: Exception, target_url: str, action: str) -> PipelineError:
    text = str(exc).strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _LOGIN_WALL_MARKERS):
        return PipelineError(
            f"Instagram refused anonymous access while {action} {target_url}: {text} — "
            "this usually means the video is private/removed, or Instagram is rate-limiting anonymous requests. "
            "If the link plays in a private browser window (after dismissing the login popup), wait a few minutes and retry. "
            + (LOGIN_HINT if session_source() is None else "Your saved Instagram session may have expired: run the login command again.")
        )
    return PipelineError(f"Failed while {action} {target_url}: {text}")


def _yt_dlp_base_options() -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": DEFAULT_TIMEOUT_SECONDS,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
    }
    cookies_file, cookies_browser = cookie_settings()
    if cookies_file:
        options["cookiefile"] = cookies_file
    elif cookies_browser:
        options["cookiesfrombrowser"] = tuple(
            part.strip() for part in cookies_browser.split(":") if part.strip()
        )
    return options


def _yt_dlp_extract_info(target_url: str) -> Dict[str, Any]:
    YoutubeDL = _import_yt_dlp()
    options = _yt_dlp_base_options()
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(target_url, download=False)
    except PipelineError:
        raise
    except Exception as exc:
        raise _download_error(exc, target_url, "inspecting") from exc
    if not isinstance(info, dict):
        raise PipelineError(f"Could not extract video information from URL: {target_url}")
    return info


def collect_direct_video(target_url: str) -> list[VideoCandidate]:
    info = _yt_dlp_extract_info(target_url)
    page_url = info.get("webpage_url") or target_url
    uploader = info.get("uploader") or info.get("channel") or info.get("extractor_key") or "video"
    source_label = info.get("uploader_id") or _safe_slug(uploader, "video")
    title = (info.get("title") or info.get("fulltitle") or "Video").strip()
    description = (info.get("description") or "").strip()
    timestamp = int(info.get("timestamp") or 0)
    video_id = str(info.get("id") or _safe_slug(page_url))

    return [
        VideoCandidate(
            source_kind="video_url",
            input_url=target_url,
            canonical_url=page_url,
            source_label=source_label,
            source_group="video_urls",
            video_id=video_id,
            timestamp=timestamp,
            title=title,
            caption=description,
            video_url=target_url,
            uploader=uploader,
            platform=(info.get("extractor_key") or urlparse(target_url).netloc or "video").lower(),
            position=1,
            total_videos=1,
            metrics=metrics_from_yt_dlp_info(info),
            cover_url=str(info.get("thumbnail") or ""),
        )
    ]


def resolve_candidates(input_url: str) -> tuple[str, str, list[VideoCandidate]]:
    normalized_input, canonical = normalize_input_url(input_url)
    input_kind, canonical_target = detect_input_kind(canonical)
    if input_kind == "instagram_profile":
        return input_kind, canonical_target, collect_instagram_profile_videos(canonical_target)
    return input_kind, canonical_target, collect_direct_video(normalized_input)


def ensure_run_dir(base_output_dir: Path, candidate: VideoCandidate) -> Path:
    run_dir = base_output_dir / candidate.source_group / _safe_slug(candidate.source_label) / _safe_slug(candidate.video_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _paths_for_run(run_dir: Path) -> tuple[Path, Path, Path]:
    return run_dir / "audio.mp3", run_dir / "transcript.txt", run_dir / "metadata.json"


def _paths_for_batch(base_output_dir: Path, source_group: str, source_label: str) -> tuple[Path, Path]:
    group_dir = base_output_dir / source_group / _safe_slug(source_label)
    group_dir.mkdir(parents=True, exist_ok=True)
    return group_dir, group_dir / "manifest.json"


def _copy_uploaded_audio(source_audio_path: Path, run_dir: Path) -> Path:
    suffix = source_audio_path.suffix.lower() or ".bin"
    destination = run_dir / f"audio{suffix}"
    if destination.exists():
        return destination
    shutil.copy2(source_audio_path, destination)
    return destination


def _sentence_split(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    parts = re.split(r"(?<=[.!?])\s+", normalized)
    return [part.strip() for part in parts if part.strip()]


def _top_keywords(*texts: str, limit: int = 8) -> list[str]:
    words: list[str] = []
    for text in texts:
        words.extend(re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", text.lower()))
    counts = Counter(word for word in words if word not in STOPWORDS)
    return [word for word, _ in counts.most_common(limit)]


def _sentiment(text: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", text.lower())
    positive = sum(word in POSITIVE_WORDS for word in words)
    negative = sum(word in NEGATIVE_WORDS for word in words)
    if positive > negative:
        return "positive"
    if negative > positive:
        return "negative"
    return "neutral"


def _cta_detected(text: str) -> Optional[str]:
    lowered = text.lower()
    for pattern in CTA_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def _strip_json_fence(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _groq_chat(messages: list[Dict[str, str]]) -> Optional[str]:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None

    request = Request(
        GROQ_BASE_URL,
        data=json.dumps(
            {
                "model": os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL),
                "messages": messages,
                "temperature": 0.2,
            }
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except Exception:
        return None

    choices = payload.get("choices") or []
    if not choices:
        return None
    return choices[0].get("message", {}).get("content")


def _heuristic_video_ai_insights(transcript_text: str, caption: str, title: str) -> Dict[str, Any]:
    sentences = _sentence_split(transcript_text)
    summary_sentences = sentences[:2] or _sentence_split(caption)[:2] or ([title] if title else [])
    keywords = _top_keywords(title, caption, transcript_text)
    hook = sentences[0] if sentences else (caption.splitlines()[0].strip() if caption.strip() else title)
    cta = _cta_detected(f"{caption}\n{transcript_text}")
    insight_focus = keywords[:3] if keywords else ["content", "message", "audience"]
    summary = " ".join(summary_sentences).strip()

    return {
        "summary": summary,
        "hook": hook,
        "keywords": keywords,
        "sentiment": _sentiment(f"{caption}\n{transcript_text}"),
        "cta": cta,
        "title_suggestions": [
            f"{title or hook}: what matters most",
            f"{' / '.join(insight_focus)} breakdown",
            f"The core idea behind {insight_focus[0] if insight_focus else 'this video'}",
        ],
        "content_angles": [
            f"Turn {insight_focus[0] if insight_focus else 'the topic'} into a carousel or thread.",
            f"Clip the opening hook: {hook[:120] if hook else title}.",
            f"Use {insight_focus[1] if len(insight_focus) > 1 else insight_focus[0] if insight_focus else 'the message'} as the CTA angle for a follow-up post.",
        ],
    }


def generate_video_ai_insights(transcript_text: str, caption: str, title: str) -> Dict[str, Any]:
    fallback = _heuristic_video_ai_insights(transcript_text, caption, title)
    content = _groq_chat(
        [
            {
                "role": "system",
                "content": (
                    "You generate structured creator insights for short-form video transcripts. "
                    "Return only valid JSON with keys: summary, hook, keywords, sentiment, cta, "
                    "title_suggestions, content_angles. keywords/title_suggestions/content_angles must be arrays."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "title": title,
                        "caption": caption,
                        "transcript": transcript_text[:12000],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    )
    if not content:
        return {**fallback, "provider": "heuristic"}

    try:
        parsed = json.loads(_strip_json_fence(content))
    except json.JSONDecodeError:
        return {**fallback, "provider": "heuristic"}

    return {
        "summary": str(parsed.get("summary") or fallback["summary"]).strip(),
        "hook": str(parsed.get("hook") or fallback["hook"]).strip(),
        "keywords": [str(item).strip() for item in (parsed.get("keywords") or fallback["keywords"]) if str(item).strip()][:8],
        "sentiment": str(parsed.get("sentiment") or fallback["sentiment"]).strip().lower(),
        "cta": str(parsed.get("cta")).strip() if parsed.get("cta") not in {None, "", "null"} else fallback["cta"],
        "title_suggestions": [str(item).strip() for item in (parsed.get("title_suggestions") or fallback["title_suggestions"]) if str(item).strip()][:3],
        "content_angles": [str(item).strip() for item in (parsed.get("content_angles") or fallback["content_angles"]) if str(item).strip()][:3],
        "provider": "groq",
    }


def _heuristic_batch_ai_overview(videos: list[Dict[str, Any]]) -> Dict[str, Any]:
    transcripts = [video.get("transcript_text", "") for video in videos]
    captions = [video.get("caption", "") for video in videos]
    keywords = _top_keywords(*transcripts, *captions, limit=12)
    hooks = [video.get("ai_insights", {}).get("hook") for video in videos if video.get("ai_insights", {}).get("hook")]
    ctas = [video.get("ai_insights", {}).get("cta") for video in videos if video.get("ai_insights", {}).get("cta")]

    return {
        "summary": f"Processed {len(videos)} videos. The strongest recurring topics were {', '.join(keywords[:5]) or 'the uploaded themes'}.",
        "recurring_keywords": keywords,
        "top_hooks": hooks[:5],
        "cta_patterns": Counter(ctas).most_common(5),
        "video_titles": [video.get("title") for video in videos],
    }


def generate_batch_ai_overview(videos: list[Dict[str, Any]]) -> Dict[str, Any]:
    fallback = _heuristic_batch_ai_overview(videos)
    content = _groq_chat(
        [
            {
                "role": "system",
                "content": (
                    "You generate concise batch insights across multiple short-form video transcripts. "
                    "When videos carry a performance.multiplier (their result divided by the batch median), "
                    "base the summary on what the high-multiplier videos do differently, especially their first_3s hook. "
                    "Return only valid JSON with keys: summary, recurring_keywords, top_hooks, cta_patterns, video_titles. "
                    "recurring_keywords and top_hooks must be arrays of strings. cta_patterns must be an array of [string, number] pairs."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "videos": [
                            {
                                "title": video.get("title"),
                                "caption": video.get("caption"),
                                "transcript": video.get("transcript_text", "")[:4000],
                                "insights": video.get("ai_insights", {}),
                                "performance": video.get("performance"),
                                "speech": video.get("speech"),
                            }
                            for video in videos
                        ]
                    },
                    ensure_ascii=False,
                ),
            },
        ]
    )
    if not content:
        return {**fallback, "provider": "heuristic"}

    try:
        parsed = json.loads(_strip_json_fence(content))
    except json.JSONDecodeError:
        return {**fallback, "provider": "heuristic"}

    cta_patterns_raw = parsed.get("cta_patterns") or fallback["cta_patterns"]
    cta_patterns: list[tuple[str, int]] = []
    for item in cta_patterns_raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            cta_patterns.append((str(item[0]).strip(), int(item[1])))

    return {
        "summary": str(parsed.get("summary") or fallback["summary"]).strip(),
        "recurring_keywords": [str(item).strip() for item in (parsed.get("recurring_keywords") or fallback["recurring_keywords"]) if str(item).strip()][:12],
        "top_hooks": [str(item).strip() for item in (parsed.get("top_hooks") or fallback["top_hooks"]) if str(item).strip()][:5],
        "cta_patterns": cta_patterns or fallback["cta_patterns"],
        "video_titles": [str(item).strip() for item in (parsed.get("video_titles") or fallback["video_titles"]) if str(item).strip()],
        "provider": "groq",
    }


def speech_profile(segments: Iterable[Dict[str, Any]], transcript_text: str) -> Dict[str, Any]:
    """Timing facts from Whisper segments: when speech starts, what is said in the first seconds, pace."""
    spoken = [seg for seg in segments or [] if str(seg.get("text") or "").strip()]
    words = len(transcript_text.split())
    if not spoken:
        return {"has_speech": False, "opening_line": None, "first_3s": None, "speech_starts_at": None, "words": words, "words_per_minute": None}
    first_start = float(spoken[0].get("start") or 0.0)
    spoken_seconds = float(spoken[-1].get("end") or 0.0) - first_start
    first_3s = " ".join(str(seg["text"]).strip() for seg in spoken if float(seg.get("start") or 0.0) < HOOK_WINDOW_SECONDS)
    return {
        "has_speech": True,
        "opening_line": str(spoken[0]["text"]).strip(),
        "first_3s": first_3s or None,
        "speech_starts_at": round(first_start, 1),
        "words": words,
        "words_per_minute": round(words / (spoken_seconds / 60)) if spoken_seconds > 0 else None,
    }


def build_video_result(
    candidate: VideoCandidate,
    audio_path: Path,
    transcript_path: Path,
    metadata_path: Path,
    whisper_result: Dict[str, Any],
    *,
    model_name: str,
    cached: bool,
    metrics: Optional[Dict[str, Any]] = None,
    speech: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    transcript_text = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""
    ai_insights = generate_video_ai_insights(transcript_text, candidate.caption, candidate.title)

    return {
        "status": "ok",
        "source_kind": candidate.source_kind,
        "platform": candidate.platform,
        "source_label": candidate.source_label,
        "position": candidate.position,
        "total_videos": candidate.total_videos,
        "video_id": candidate.video_id,
        "title": candidate.title,
        "uploader": candidate.uploader,
        "input_url": candidate.input_url,
        "canonical_url": candidate.canonical_url,
        "video_url": candidate.video_url,
        "cover_url": candidate.cover_url,
        "caption": candidate.caption,
        "taken_at_timestamp": candidate.timestamp,
        "taken_at_iso": _timestamp_to_iso(candidate.timestamp),
        "audio_file": str(audio_path),
        "transcript_file": str(transcript_path),
        "metadata_file": str(metadata_path),
        "detected_language": whisper_result.get("language"),
        "model": model_name,
        "cached": cached,
        "transcript_text": transcript_text,
        "metrics": metrics if metrics is not None else merge_metrics(candidate.metrics, {}),
        "speech": speech if speech is not None else speech_profile(whisper_result.get("segments") or [], transcript_text),
        "ai_insights": ai_insights,
    }


def _failed_video_result(candidate: VideoCandidate, error: str) -> Dict[str, Any]:
    return {
        "status": "error",
        "error": error,
        "source_kind": candidate.source_kind,
        "platform": candidate.platform,
        "source_label": candidate.source_label,
        "position": candidate.position,
        "total_videos": candidate.total_videos,
        "video_id": candidate.video_id,
        "title": candidate.title,
        "uploader": candidate.uploader,
        "input_url": candidate.input_url,
        "canonical_url": candidate.canonical_url,
        "video_url": candidate.video_url,
        "caption": candidate.caption,
        "taken_at_timestamp": candidate.timestamp,
        "taken_at_iso": _timestamp_to_iso(candidate.timestamp),
        "audio_file": None,
        "transcript_file": None,
        "metadata_file": None,
        "detected_language": None,
        "cached": False,
        "transcript_text": "",
        "ai_insights": None,
    }


def _load_cached_video_result(candidate: VideoCandidate, run_dir: Path, model_name: str) -> Optional[Dict[str, Any]]:
    audio_path, transcript_path, metadata_path = _paths_for_run(run_dir)
    if not (audio_path.exists() and transcript_path.exists() and metadata_path.exists()):
        return None

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    if not candidate.caption and metadata.get("caption"):
        candidate = replace(
            candidate,
            caption=metadata["caption"],
            title=metadata.get("title") or candidate.title,
            timestamp=int(metadata.get("taken_at_timestamp") or candidate.timestamp),
        )
    return build_video_result(
        candidate,
        audio_path,
        transcript_path,
        metadata_path,
        {"language": metadata.get("detected_language")},
        model_name=model_name,
        cached=True,
        metrics=merge_metrics(candidate.metrics, metadata.get("metrics") or {}),
        speech=metadata.get("speech") or speech_profile(metadata.get("segments") or [], transcript_path.read_text(encoding="utf-8")),
    )


def download_audio(candidate: VideoCandidate, run_dir: Path) -> tuple[Path, Dict[str, Any]]:
    """Download audio; also return yt-dlp's info dict (empty if the audio was already on disk)."""
    audio_path, _, _ = _paths_for_run(run_dir)
    if audio_path.exists() and audio_path.stat().st_size > 0:
        return audio_path, {}

    require_ffmpeg()
    YoutubeDL = _import_yt_dlp()

    output_template = str(run_dir / "%(id)s.%(ext)s")
    options = {
        **_yt_dlp_base_options(),
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(candidate.video_url, download=True)
    except PipelineError:
        raise
    except Exception as exc:
        raise _download_error(exc, candidate.video_url, "downloading audio for") from exc
    if not isinstance(info, dict) or not info.get("id"):
        raise PipelineError(f"yt-dlp did not return download metadata for {candidate.video_url}")

    downloaded_audio_path = run_dir / f"{info['id']}.mp3"
    if not downloaded_audio_path.exists():
        candidates = sorted(run_dir.glob(f"{info['id']}.*"))
        if not candidates:
            raise PipelineError("yt-dlp finished without producing an audio file")
        downloaded_audio_path = candidates[0]

    if downloaded_audio_path != audio_path:
        audio_path.unlink(missing_ok=True)
        downloaded_audio_path.replace(audio_path)

    if not audio_path.exists() or audio_path.stat().st_size == 0:
        audio_path.unlink(missing_ok=True)
        raise PipelineError(f"Downloaded audio for {candidate.video_url} is empty")

    return audio_path, info


def _existing_video(run_dir: Path) -> Optional[Path]:
    for path in sorted(run_dir.glob("video.*")):
        if path.suffix not in {".part", ".ytdl"} and path.stat().st_size > 0:
            return path
    return None


def download_video(video_url: str, run_dir: Path) -> Path:
    """Download the video stream once (smallest stream of at least 480p; frames are downscaled anyway)."""
    existing = _existing_video(run_dir)
    if existing:
        return existing
    require_ffmpeg()
    YoutubeDL = _import_yt_dlp()
    options = {
        **_yt_dlp_base_options(),
        "format": "wv*[height>=480]/bv*/b",
        "outtmpl": str(run_dir / "video.%(ext)s"),
        "noplaylist": True,
    }
    try:
        with YoutubeDL(options) as ydl:
            ydl.extract_info(video_url, download=True)
    except PipelineError:
        raise
    except Exception as exc:
        raise _download_error(exc, video_url, "downloading video for") from exc
    found = _existing_video(run_dir)
    if not found:
        raise PipelineError(f"yt-dlp finished without producing a video file for {video_url}")
    return found


def _ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    guarded: list[str] = []
    for arg in args:
        if arg == "-i":
            guarded += FFMPEG_INPUT_GUARD
        guarded.append(arg)
    try:
        return subprocess.run(
            [require_ffmpeg(), "-hide_banner", "-nostdin", *guarded],
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"ffmpeg took longer than {FFMPEG_TIMEOUT_SECONDS}s extracting frames") from exc


def _video_duration(video_path: Path) -> float:
    # `ffmpeg -i` with no output exits non-zero but still prints the duration.
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", _ffmpeg(["-i", str(video_path)]).stderr)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _leading_black_seconds(video_path: Path) -> float:
    """How long the video opens on black (fade-ins), so the opening frame shows the first real picture."""
    result = _ffmpeg(["-i", str(video_path), "-vf", "blackdetect=d=0:pix_th=0.10", "-an", "-f", "null", "-"])
    match = re.search(r"black_start:0(?:\.0+)?\s+black_end:([\d.]+)", result.stderr)
    return float(match.group(1)) if match else 0.0


def _spoken_at(time_seconds: float, segments: Iterable[Dict[str, Any]]) -> Optional[str]:
    for segment in segments or []:
        if float(segment.get("start") or 0) <= time_seconds < float(segment.get("end") or 0):
            return str(segment.get("text") or "").strip() or None
    return None


def _spread_evenly(items: list, count: int) -> list:
    """At most `count` items: always the first and last, the rest evenly spaced."""
    if len(items) <= count:
        return list(items)
    if count <= 1:
        return list(items[:1])
    keep = {round(i * (len(items) - 1) / (count - 1)) for i in range(count)}
    return [item for index, item in enumerate(items) if index in keep]


def extract_frames(
    video_path: Path,
    frames_dir: Path,
    *,
    max_frames: int = DEFAULT_MAX_FRAMES,
    segments: Iterable[Dict[str, Any]] = (),
) -> list[Dict[str, Any]]:
    """Cut a video into JPEG frames for visual analysis.

    Takes the first frame plus one frame per shot change; a video with few cuts
    is sampled evenly instead (about one frame every 3 seconds). Never returns
    more than max_frames: the opening frame is always kept and the rest are
    spread evenly. Each frame carries the words being spoken at that moment.
    """
    max_frames = max(1, int(max_frames))
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)
    scale = f"scale='min({FRAME_WIDTH},iw)':-2"
    start = _leading_black_seconds(video_path)

    # First frame after any black opening, then one frame per shot change.
    result = _ffmpeg([
        "-i", str(video_path),
        "-vf", f"select='gte(t\\,{start:.3f})*(isnan(prev_selected_t)+gt(scene\\,{SCENE_THRESHOLD}))',showinfo,{scale}",
        "-vsync", "vfr", "-q:v", "4",
        str(frames_dir / "scene_%03d.jpg"),
    ])
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise PipelineError(f"ffmpeg could not extract frames from {video_path.name}: {detail}")
    times = [float(value) for value in re.findall(r"pts_time:([\d.]+)", result.stderr)]
    shots = list(zip(times, sorted(frames_dir.glob("scene_*.jpg"))))

    duration = _video_duration(video_path)
    if len(shots) < min(3, max_frames) and duration > 0:
        # Few or no cuts (talking head, single shot): sample evenly instead.
        for _, path in shots:
            path.unlink(missing_ok=True)
        shots = []
        count = max(1, min(max_frames, math.ceil(duration / 3)))
        for index in range(count):
            time_seconds = start + (duration - start) * index / count
            out = frames_dir / f"even_{index:03d}.jpg"
            _ffmpeg(["-ss", f"{time_seconds:.2f}", "-i", str(video_path), "-frames:v", "1", "-vf", scale, "-q:v", "4", str(out)])
            if out.exists():
                shots.append((time_seconds, out))

    if len(shots) > max_frames:
        kept = _spread_evenly(shots, max_frames)
        for shot in shots:
            if shot not in kept:
                shot[1].unlink(missing_ok=True)
        shots = kept

    segment_list = list(segments or [])
    return [
        {"index": index, "time_seconds": round(time_seconds, 2), "file": str(path), "spoken": _spoken_at(time_seconds, segment_list)}
        for index, (time_seconds, path) in enumerate(shots)
    ]


def hook_window_frames(
    video_path: Path,
    frames_dir: Path,
    *,
    step: float = HOOK_FRAME_STEP_SECONDS,
    segments: Iterable[Dict[str, Any]] = (),
) -> list[Dict[str, Any]]:
    """Frames every `step` seconds across the first 3 seconds, black openings included (they are findings)."""
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)
    duration = _video_duration(video_path) or HOOK_WINDOW_SECONDS
    scale = f"scale='min({FRAME_WIDTH},iw)':-2"
    segment_list = list(segments or [])
    frames: list[Dict[str, Any]] = []
    steps = int(round(HOOK_WINDOW_SECONDS / step))
    for index in range(steps + 1):
        time_seconds = round(index * step, 2)
        if time_seconds >= duration:
            break
        out = frames_dir / f"hook_{index:02d}.jpg"
        _ffmpeg(["-ss", f"{time_seconds:.2f}", "-i", str(video_path), "-frames:v", "1", "-vf", scale, "-q:v", "4", str(out)])
        if out.exists():
            frames.append({"index": len(frames), "time_seconds": time_seconds, "file": str(out), "spoken": _spoken_at(time_seconds, segment_list)})
    return frames


def video_signals(metadata: Dict[str, Any], shots: list[Dict[str, Any]], duration: float) -> Dict[str, Any]:
    """Measured facts about the opening and pacing; the analyst quotes these rather than guessing."""
    speech = metadata.get("speech") or {}
    segments = metadata.get("segments") or []
    hook_words = sum(
        len(str(seg.get("text") or "").split()) for seg in segments if float(seg.get("start") or 0) < HOOK_WINDOW_SECONDS
    )
    return {
        "duration_seconds": round(duration, 1),
        "has_speech": speech.get("has_speech"),
        "speech_starts_at": speech.get("speech_starts_at"),
        "first_3s_speech": speech.get("first_3s"),
        "words_in_hook_window": hook_words,
        "words_per_minute": speech.get("words_per_minute"),
        "shot_count": len(shots),
        "hook_window_shot_changes": sum(1 for shot in shots if 0 < shot["time_seconds"] < HOOK_WINDOW_SECONDS),
        "average_shot_seconds": round(duration / len(shots), 1) if shots and duration else None,
    }


def analysis_method() -> str:
    """The hook-analysis method (the skill's body, without its frontmatter)."""
    try:
        text = METHOD_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def _is_public_https_url(url: str) -> bool:
    """Only https URLs whose host resolves solely to public addresses (no loopback, private, link-local)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)}
    except (socket.gaierror, UnicodeError):
        return False
    return bool(addresses) and all(ipaddress.ip_address(address.split("%")[0]).is_global for address in addresses)


class _PublicRedirectsOnly(HTTPRedirectHandler):
    """Check each redirect hop before it is requested, so a public URL cannot bounce into the local network."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_public_https_url(newurl):
            raise URLError(f"refused redirect to non-public URL: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_image(url: str, destination: Path) -> bool:
    """Fetch a cover image and save it downscaled to FRAME_WIDTH as JPEG. False on any failure.

    The URL comes from third-party page data, so it is treated as hostile: https to public hosts only,
    image content types only, size-capped, and decoded by ffmpeg as a single still image.
    """
    # shortcut: the host is checked before urlopen re-resolves it (DNS-rebinding window); acceptable
    # because the response must also be a size-capped image; pin the resolved IP if this server is exposed.
    if not _is_public_https_url(url):
        return False
    raw: Optional[Path] = None
    try:
        opener = build_opener(_PublicRedirectsOnly())
        with opener.open(Request(url, headers={"User-Agent": BROWSER_USER_AGENT}), timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            extension = COVER_EXTENSIONS.get(content_type)
            if not extension:
                return False
            data = response.read(MAX_COVER_BYTES + 1)
        if not data or len(data) > MAX_COVER_BYTES:
            return False
        raw = destination.with_name(f"cover_source{extension}")
        raw.write_bytes(data)
        _ffmpeg(["-y", "-f", "image2", "-i", str(raw), "-frames:v", "1", "-vf", f"scale='min({FRAME_WIDTH},iw)':-2", "-q:v", "4", str(destination)])
        return destination.exists() and destination.stat().st_size > 0
    except (OSError, URLError, ValueError, PipelineError):
        destination.unlink(missing_ok=True)
        return False
    finally:
        if raw is not None:
            raw.unlink(missing_ok=True)


def fetch_cover(run_dir: Path, metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The video's cover image (grid thumbnail), downloaded once. None when unavailable; never raises."""
    cover = run_dir / "cover.jpg"
    if cover.exists() and cover.stat().st_size > 0:
        return {"file": str(cover)}
    # Instagram CDN links are signed and expire, so fall back to a fresh lookup of the video page.
    if metadata.get("cover_url") and _download_image(metadata["cover_url"], cover):
        return {"file": str(cover)}
    if metadata.get("video_url"):
        try:
            fresh = _yt_dlp_extract_info(metadata["video_url"]).get("thumbnail")
        except PipelineError:
            fresh = None
        if fresh and _download_image(str(fresh), cover):
            return {"file": str(cover)}
    return None


def _load_metadata(run_dir: Path) -> Dict[str, Any]:
    try:
        return json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _batch_performance(run_dir: Path, video_id: str) -> Optional[Dict[str, Any]]:
    """This video's standing in its saved batch (from manifest.json), if it was scored."""
    try:
        manifest = json.loads((run_dir.parent / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    batch = manifest.get("performance") or {}
    if not batch.get("available"):
        return None
    for video in manifest.get("videos") or []:
        if video.get("video_id") == video_id and video.get("performance"):
            ranking = [item.get("video_id") for item in batch.get("ranking") or []]
            return {
                **video["performance"],
                "batch_median": batch.get("median"),
                "rank_in_batch": ranking.index(video_id) + 1 if video_id in ranking else None,
                "videos_scored": batch.get("videos_scored"),
            }
    return None


def _shots_and_signals(run_dir: Path, metadata: Dict[str, Any]) -> tuple[Path, list[Dict[str, Any]], Dict[str, Any]]:
    video_url = metadata.get("video_url") or ""
    if not video_url:
        raise PipelineError("This item has no video (uploaded audio cannot be analyzed visually).")
    video_path = download_video(video_url, run_dir)
    # Uncapped so signals count every cut; only an even spread of these is shown as images.
    shots = extract_frames(video_path, run_dir / "frames", max_frames=1_000_000, segments=metadata.get("segments") or [])
    return video_path, shots, video_signals(metadata, shots, _video_duration(video_path))


def build_video_analysis(
    run_dir: Path,
    *,
    question: Optional[str] = None,
    max_shot_frames: int = DEFAULT_ANALYSIS_SHOT_FRAMES,
    include_method: bool = True,
) -> Dict[str, Any]:
    """Everything an analyst needs for one video: hook-window frames, shot frames, timed speech, signals, performance."""
    metadata = _load_metadata(run_dir)
    if not metadata:
        raise PipelineError(f"No processed video in {run_dir}; transcribe it first.")
    video_path, shots, signals = _shots_and_signals(run_dir, metadata)
    segments = metadata.get("segments") or []
    packet: Dict[str, Any] = {
        "status": "ok",
        "question": question,
        "video": {key: metadata.get(key) for key in ("video_id", "title", "caption", "uploader", "video_url", "taken_at_iso")},
        "signals": signals,
        "metrics": metadata.get("metrics"),
        "performance": _batch_performance(run_dir, str(metadata.get("video_id") or "")),
        "speech_segments": segments,
        "hook_frames": hook_window_frames(video_path, run_dir / "hook_frames", segments=segments),
        "shot_frames": _spread_evenly(shots, max(1, max_shot_frames)),
        "cover": fetch_cover(run_dir, metadata),
    }
    if not segments:
        packet["transcript_text"] = (run_dir / "transcript.txt").read_text(encoding="utf-8") if (run_dir / "transcript.txt").exists() else ""
    if include_method:
        packet["analysis_method"] = analysis_method()
    return packet


def build_hook_comparison(batch_dir: Path, *, max_videos: int = INSTAGRAM_VIDEO_LIMIT, include_method: bool = True) -> Dict[str, Any]:
    """The first 3 seconds of every video in a saved batch, side by side with real performance."""
    try:
        manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"No readable batch manifest in {batch_dir}: {exc}") from exc
    performance = manifest.get("performance") or {}
    entries: list[Dict[str, Any]] = []
    for video in [v for v in manifest.get("videos") or [] if v.get("status") == "ok"][: max(1, max_videos)]:
        video_id = str(video.get("video_id") or "")
        run_dir = batch_dir / _safe_slug(video_id)
        entry: Dict[str, Any] = {
            "video_id": video_id,
            "title": video.get("title"),
            "video_url": video.get("video_url"),
            "performance": video.get("performance"),
        }
        try:
            metadata = _load_metadata(run_dir) or video
            video_path, _, entry["signals"] = _shots_and_signals(run_dir, metadata)
            entry["hook_frames"] = hook_window_frames(
                video_path, run_dir / "hook_frames", step=1.0, segments=metadata.get("segments") or []
            )
            entry["cover"] = fetch_cover(run_dir, metadata)
        except PipelineError as exc:
            entry["error"] = str(exc)
            entry["hook_frames"] = []
        entries.append(entry)
    packet: Dict[str, Any] = {
        "status": "ok",
        "input_url": manifest.get("input_url"),
        "performance_summary": {key: performance.get(key) for key in ("available", "metric", "metric_note", "median", "outlier_threshold", "videos_scored", "reason")},
        "videos": entries,
    }
    if include_method:
        packet["analysis_method"] = analysis_method()
    return packet


def frames_for_video(run_dir: Path, video_url: str, *, max_frames: int = DEFAULT_MAX_FRAMES) -> list[Dict[str, Any]]:
    """Download a processed video's footage and cut it into frames; the list is saved into metadata.json."""
    if not video_url:
        raise PipelineError("This item has no video URL (uploaded audio has no frames).")
    metadata_path = run_dir / "metadata.json"
    metadata: Dict[str, Any] = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = {}
    video_path = download_video(video_url, run_dir)
    max_frames = max(1, min(int(max_frames), MAX_FRAMES_LIMIT))
    frames = extract_frames(video_path, run_dir / "frames", max_frames=max_frames, segments=metadata.get("segments") or [])
    if metadata:
        metadata["frames"] = frames
        _atomic_write_text(metadata_path, json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    return frames


def attach_frames(video_result: Dict[str, Any], *, max_frames: int = DEFAULT_MAX_FRAMES) -> Dict[str, Any]:
    """Add frames to a finished video result; a frame failure never loses the transcript."""
    try:
        video_result["frames"] = frames_for_video(
            Path(video_result["metadata_file"]).parent, video_result.get("video_url") or "", max_frames=max_frames
        )
    except PipelineError as exc:
        video_result["frames"] = []
        video_result["frames_error"] = str(exc)
    return video_result


def available_whisper_models() -> list[str]:
    whisper = _import_whisper()
    return list(whisper.available_models())


@lru_cache(maxsize=4)
def load_whisper_model(model_name: str) -> Any:
    whisper = _import_whisper()
    return whisper.load_model(model_name)


def transcribe_audio(
    audio_path: Path,
    model_name: str,
    language: Optional[str],
    progress_callback: Optional[ProgressCallback] = None,
    percent_range: tuple[int, int] = (55, 85),
) -> Dict[str, Any]:
    require_ffmpeg()
    start, end = percent_range
    midpoint = start + math.floor((end - start) * 0.25)
    _emit(progress_callback, "loading_model", midpoint, f"Loading Whisper model '{model_name}'")
    try:
        model = load_whisper_model(model_name)
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"Failed to load Whisper model '{model_name}': {exc}") from exc

    _emit(progress_callback, "transcribing", end, "Transcribing audio with Whisper")
    try:
        result = model.transcribe(str(audio_path), fp16=False, language=language, verbose=None)
    except Exception as exc:
        raise PipelineError(f"Whisper transcription failed: {exc}") from exc
    if not isinstance(result, dict):
        raise PipelineError("Whisper returned an unexpected transcription result")
    return result


def write_video_outputs(
    candidate: VideoCandidate,
    run_dir: Path,
    audio_path: Path,
    whisper_result: Dict[str, Any],
    model_name: str,
    metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _, transcript_path, metadata_path = _paths_for_run(run_dir)
    transcript_text = (whisper_result.get("text") or "").strip()
    _atomic_write_text(transcript_path, transcript_text + ("\n" if transcript_text else ""))

    video_result = build_video_result(
        candidate,
        audio_path,
        transcript_path,
        metadata_path,
        whisper_result,
        model_name=model_name,
        cached=False,
        metrics=metrics,
    )

    metadata = {
        **video_result,
        # Timed segments stay on disk only; they are too large for tool responses.
        "segments": [
            {"start": round(float(seg.get("start") or 0.0), 2), "end": round(float(seg.get("end") or 0.0), 2), "text": str(seg.get("text") or "").strip()}
            for seg in whisper_result.get("segments") or []
        ],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    _atomic_write_text(metadata_path, json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    return video_result


def process_video(
    candidate: VideoCandidate,
    *,
    output_dir: Path,
    model_name: str,
    language: Optional[str],
    progress_callback: Optional[ProgressCallback],
    reuse_existing: bool,
) -> Dict[str, Any]:
    run_dir = ensure_run_dir(output_dir, candidate)

    if reuse_existing:
        cached = _load_cached_video_result(candidate, run_dir, model_name)
        if cached is not None:
            return cached
    else:
        # Force-fresh: wipe the candidate's run_dir so download_audio re-pulls the
        # source instead of returning the stale MP3 sitting in the same dir from a
        # prior run. Without this, two IG URLs that yt-dlp normalises to the same
        # `info["id"]` would both serve the first call's transcript.
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
        run_dir = ensure_run_dir(output_dir, candidate)

    audio_path, info = download_audio(candidate, run_dir)
    metrics = merge_metrics(candidate.metrics, metrics_from_yt_dlp_info(info))
    if info and not candidate.caption:
        # Profile reels arrive without caption or date; yt-dlp's page data has both.
        caption = (info.get("description") or "").strip()
        candidate = replace(
            candidate,
            caption=caption,
            title=caption.splitlines()[0].strip() if caption else candidate.title,
            timestamp=int(info.get("timestamp") or candidate.timestamp),
        )
    if info and not candidate.cover_url:
        candidate = replace(candidate, cover_url=str(info.get("thumbnail") or ""))
    whisper_result = transcribe_audio(audio_path, model_name, language, progress_callback=progress_callback)
    _emit(
        progress_callback,
        "generating_insights",
        min(95, 15 + math.floor((candidate.position / max(candidate.total_videos, 1)) * 75)),
        f"Generating AI insights for video {candidate.position}/{candidate.total_videos}",
    )
    return write_video_outputs(candidate, run_dir, audio_path, whisper_result, model_name, metrics)


def analyze_performance(videos: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Score each video against the batch median and flag outliers.

    Uses views when every video has them; otherwise likes + comments, which
    Instagram still exposes to anonymous requests. Adds a "performance" key to
    each scored video in place.
    """
    ok = [video for video in videos if video.get("status") == "ok"]
    metrics = [video.get("metrics") or {} for video in ok]
    if ok and all(m.get("views") is not None for m in metrics):
        metric, values = "views", [m["views"] for m in metrics]
    else:
        metric = "engagement"
        values = [
            (m["likes"] or 0) + (m.get("comments") or 0) if m.get("likes") is not None else None
            for m in metrics
        ]
    scored = [(video, value) for video, value in zip(ok, values) if value is not None]
    if len(scored) < MIN_VIDEOS_FOR_PERFORMANCE:
        return {
            "available": False,
            "reason": f"Needs at least {MIN_VIDEOS_FOR_PERFORMANCE} videos with view or like counts; got {len(scored)}.",
        }
    median = statistics.median(value for _, value in scored)
    if median <= 0:
        return {"available": False, "reason": f"The median {metric} across the batch is zero."}

    ranking = []
    for video, value in scored:
        multiplier = round(value / median, 2)
        video["performance"] = {
            "metric": metric,
            "value": value,
            "multiplier": multiplier,
            "is_outlier": multiplier >= OUTLIER_MULTIPLIER,
        }
        speech = video.get("speech") or {}
        ranking.append(
            {
                "video_id": video.get("video_id"),
                "title": video.get("title"),
                "video_url": video.get("video_url"),
                "value": value,
                "multiplier": multiplier,
                "first_3s": speech.get("first_3s"),
                "words_per_minute": speech.get("words_per_minute"),
                "duration_seconds": (video.get("metrics") or {}).get("duration_seconds"),
            }
        )
    ranking.sort(key=lambda item: item["multiplier"], reverse=True)
    return {
        "available": True,
        "metric": metric,
        "metric_note": "views" if metric == "views" else "likes + comments (view counts were not available for every video)",
        "median": median,
        "outlier_threshold": OUTLIER_MULTIPLIER,
        "videos_scored": len(scored),
        "outliers": [item for item in ranking if item["multiplier"] >= OUTLIER_MULTIPLIER],
        "ranking": ranking,
    }


def write_batch_manifest(
    batch_result: Dict[str, Any],
    base_output_dir: Path,
    source_group: str,
    source_label: str,
) -> Path:
    _, manifest_path = _paths_for_batch(base_output_dir, source_group, source_label)
    _atomic_write_text(manifest_path, json.dumps(batch_result, indent=2, ensure_ascii=False) + "\n")
    return manifest_path


def run_audio_file_transcription(
    audio_path: str | Path,
    *,
    original_filename: Optional[str] = None,
    output_dir: str | Path = "outputs",
    model_name: str = "base",
    language: Optional[str] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    source_audio_path = Path(audio_path).expanduser().resolve()
    if not source_audio_path.exists() or not source_audio_path.is_file():
        raise PipelineError(f"Audio file not found: {source_audio_path}")
    if source_audio_path.stat().st_size <= 0:
        raise PipelineError("The uploaded audio file is empty.")
    if not os.access(source_audio_path, os.R_OK):
        raise PipelineError(f"Audio file is not readable: {source_audio_path}")

    filename = (original_filename or source_audio_path.name).strip() or source_audio_path.name
    title = Path(filename).stem or "Uploaded audio"
    try:
        file_hash = _file_sha1(source_audio_path)[:10]
    except OSError as exc:
        raise PipelineError(f"Could not read audio file {source_audio_path}: {exc}") from exc
    source_label = _safe_slug(f"{title}-{file_hash}", "audio-upload")
    timestamp = int(source_audio_path.stat().st_mtime)

    candidate = VideoCandidate(
        source_kind="audio_upload",
        input_url=filename,
        canonical_url=f"upload://{source_label}",
        source_label=source_label,
        source_group="audio_uploads",
        video_id=source_label,
        timestamp=timestamp,
        title=title,
        caption="",
        video_url="",
        uploader="uploaded audio",
        platform="local_audio",
        position=1,
        total_videos=1,
    )

    output_root = Path(output_dir).expanduser().resolve()
    run_dir = ensure_run_dir(output_root, candidate)

    _emit(progress_callback, "validating", 4, "Validating uploaded audio file")
    _emit(progress_callback, "preparing_audio", 18, "Staging uploaded audio for transcription")
    staged_audio_path = _copy_uploaded_audio(source_audio_path, run_dir)

    whisper_result = transcribe_audio(
        staged_audio_path,
        model_name,
        language,
        progress_callback=progress_callback,
        percent_range=(22, 82),
    )
    _emit(progress_callback, "generating_insights", 92, "Generating AI insights from uploaded audio")
    video_result = write_video_outputs(
        candidate,
        run_dir,
        staged_audio_path,
        whisper_result,
        model_name,
    )

    batch_result = {
        "status": "ok",
        "input_kind": "audio_upload",
        "input_url": filename,
        "canonical_url": candidate.canonical_url,
        "model": model_name,
        "language_hint": language,
        "total_videos": 1,
        "completed_videos": 1,
        "failed_videos": 0,
        "videos": [video_result],
        "ai_overview": generate_batch_ai_overview([video_result]),
    }

    manifest_path = write_batch_manifest(
        batch_result,
        output_root,
        candidate.source_group,
        candidate.source_label,
    )
    batch_result["manifest_file"] = str(manifest_path)
    _emit(progress_callback, "completed", 100, "Completed transcription for uploaded audio")
    return batch_result


def run_transcription(
    input_url: str,
    *,
    output_dir: str | Path = "outputs",
    model_name: str = "base",
    language: Optional[str] = None,
    progress_callback: Optional[ProgressCallback] = None,
    reuse_existing: bool = True,
    include_frames: bool = False,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> Dict[str, Any]:
    _emit(progress_callback, "validating", 4, "Validating input URL")
    input_kind, canonical_input, candidates = resolve_candidates(input_url)
    output_root = Path(output_dir).expanduser().resolve()

    if input_kind == "instagram_profile":
        _emit(progress_callback, "collecting_videos", 12, f"Collected the latest {len(candidates)} videos from the Instagram profile")
    else:
        _emit(progress_callback, "collecting_videos", 12, "Resolved the video URL")

    video_results: list[Dict[str, Any]] = []
    total = len(candidates)

    for index, candidate in enumerate(candidates, start=1):
        base_percent = 15 + math.floor(((index - 1) / total) * 75)
        _emit(
            progress_callback,
            "downloading_audio",
            base_percent,
            f"Processing video {index}/{total}: {candidate.title[:80]}",
        )
        try:
            video_result = process_video(
                candidate,
                output_dir=output_root,
                model_name=model_name,
                language=language,
                progress_callback=progress_callback,
                reuse_existing=reuse_existing,
            )
            if include_frames:
                _emit(progress_callback, "extracting_frames", base_percent, f"Extracting frames from video {index}/{total}")
                attach_frames(video_result, max_frames=max_frames)
        except PipelineError as exc:
            # In a multi-video batch, one broken video should not abort the
            # remaining downloads. Record the failure and keep going.
            if total == 1:
                raise
            video_result = _failed_video_result(candidate, str(exc))
            _emit(
                progress_callback,
                "video_failed",
                base_percent,
                f"Skipping video {index}/{total} after error: {exc}",
            )
        video_results.append(video_result)
        completed_percent = 15 + math.floor((index / total) * 75)
        status_message = (
            f"Finished video {index}/{total}"
            if total > 1
            else "Transcript ready"
        )
        _emit(progress_callback, "writing_files", completed_percent, status_message)

    successful_videos = [video for video in video_results if video.get("status") == "ok"]
    if not successful_videos:
        errors = "; ".join(
            str(video.get("error")) for video in video_results if video.get("error")
        )
        raise PipelineError(f"All {total} videos failed to transcribe. Errors: {errors or 'unknown'}")

    failed_count = total - len(successful_videos)
    batch_result = {
        "status": "ok",
        "input_kind": input_kind,
        "input_url": input_url,
        "canonical_url": canonical_input,
        "model": model_name,
        "language_hint": language,
        "total_videos": total,
        "completed_videos": len(successful_videos),
        "failed_videos": failed_count,
        "videos": video_results,
        "performance": analyze_performance(video_results),
        "ai_overview": generate_batch_ai_overview(successful_videos),
    }

    manifest_path = write_batch_manifest(
        batch_result,
        output_root,
        candidates[0].source_group,
        candidates[0].source_label,
    )
    batch_result["manifest_file"] = str(manifest_path)
    completed_message = f"Completed transcription for {len(successful_videos)} video(s)"
    if failed_count:
        completed_message += f" ({failed_count} failed)"
    _emit(progress_callback, "completed", 100, completed_message)
    return batch_result
