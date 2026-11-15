from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.types import ToolAnnotations

SERVER_NAME = "ReelRecon"
SERVER_VERSION = "1.2.1"

logger = logging.getLogger("reelrecon.mcp")


def _env(name: str, default: str | None = None) -> str | None:
    # REELRECON_* is the primary prefix; the legacy IG_TRANSCRIBER_* prefix
    # remains supported so existing setups keep working after the rename.
    for key in (f"REELRECON_{name}", f"IG_TRANSCRIBER_{name}"):
        value = os.environ.get(key)
        if value is not None:
            return value
    return default


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(int(_env(name, str(default))), minimum)
    except (TypeError, ValueError):
        return default


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = Path(_env("OUTPUT_DIR", str(REPO_ROOT / "outputs"))).expanduser().resolve()
MAX_LIST_LIMIT = 50
MAX_FRAMES_LIMIT = 60
JOB_TIMEOUT_SECONDS = _env_int("JOB_TIMEOUT_SECONDS", 3600, minimum=30)
QUEUE_TIMEOUT_SECONDS = _env_int("QUEUE_TIMEOUT_SECONDS", 900, minimum=5)
MAX_CONCURRENT_JOBS = _env_int("MAX_CONCURRENT_JOBS", 1, minimum=1)
MAX_UPLOAD_BYTES = _env_int("MAX_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024, minimum=1)

# Whisper model names accepted without touching the (heavy) whisper import.
# Extra names can be allowed with REELRECON_EXTRA_MODELS="name1,name2".
KNOWN_WHISPER_MODELS = {
    "tiny",
    "tiny.en",
    "base",
    "base.en",
    "small",
    "small.en",
    "medium",
    "medium.en",
    "large",
    "large-v1",
    "large-v2",
    "large-v3",
    "large-v3-turbo",
    "turbo",
}

_SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9._-]+")

# Serializes/limits transcription jobs so parallel tool calls cannot trample
# each other's output directories or exhaust memory loading Whisper models.
_job_semaphore: asyncio.Semaphore | None = None
_job_semaphore_guard = threading.Lock()
_active_jobs = 0
_abandoned_jobs = 0


def _get_job_semaphore() -> asyncio.Semaphore:
    global _job_semaphore
    with _job_semaphore_guard:
        if _job_semaphore is None:
            _job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        return _job_semaphore


@lru_cache(maxsize=1)
def _pipeline() -> Any:
    """Import the transcription pipeline lazily.

    whisper/torch imports take seconds; deferring them keeps MCP `initialize`
    fast, and an installation problem becomes a structured tool error instead
    of a server that fails to boot.
    """
    from ig_transcriber import pipeline

    return pipeline


def _error(error_type: str, message: str, *, hint: str | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "error_type": error_type, "error": message}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    return payload


def _safe_slug(value: str, fallback: str = "item") -> str:
    # Strip leading/trailing dots too so a slug can never be a path-traversal
    # component like "." or "..".
    slug = _SLUG_PATTERN.sub("-", value.strip()).strip("-.").lower()
    if not slug or set(slug) <= {".", "-"}:
        return fallback
    return slug


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise json.JSONDecodeError("Expected a JSON object", str(path), 0)
    return data


def _within_output_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved != DEFAULT_OUTPUT_DIR and DEFAULT_OUTPUT_DIR not in resolved.parents:
        raise FileNotFoundError(f"Path is outside the output directory: {resolved}")
    return resolved


def _manifest_path(source_group: str, source_label: str) -> Path:
    return _within_output_root(
        DEFAULT_OUTPUT_DIR / _safe_slug(source_group, "group") / _safe_slug(source_label, "source") / "manifest.json"
    )


def _video_dir(source_group: str, source_label: str, video_id: str) -> Path:
    return _within_output_root(
        DEFAULT_OUTPUT_DIR
        / _safe_slug(source_group, "group")
        / _safe_slug(source_label, "source")
        / _safe_slug(video_id, "video")
    )


def _manifest_resource_uri(source_group: str, source_label: str) -> str:
    return f"reelrecon://manifest/{_safe_slug(source_group, 'group')}/{_safe_slug(source_label, 'source')}"


def _transcript_resource_uri(source_group: str, source_label: str, video_id: str) -> str:
    return (
        "reelrecon://transcript/"
        f"{_safe_slug(source_group, 'group')}/{_safe_slug(source_label, 'source')}/{_safe_slug(video_id, 'video')}"
    )


def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _recent_manifest_paths(limit: int) -> list[Path]:
    capped_limit = min(max(limit, 1), MAX_LIST_LIMIT)
    try:
        manifests = sorted(
            DEFAULT_OUTPUT_DIR.glob("*/*/manifest.json"),
            key=_mtime_or_zero,
            reverse=True,
        )
    except OSError as exc:
        logger.warning("Could not scan output directory %s: %s", DEFAULT_OUTPUT_DIR, exc)
        return []
    return manifests[:capped_limit]


def _manifest_summary(path: Path) -> dict[str, Any]:
    relative = path.relative_to(DEFAULT_OUTPUT_DIR)
    source_group, source_label = relative.parts[0], relative.parts[1]
    summary: dict[str, Any] = {
        "source_group": source_group,
        "source_label": source_label,
        "manifest_file": str(path),
        "manifest_resource": _manifest_resource_uri(source_group, source_label),
        "updated_at": _mtime_or_zero(path),
    }
    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        summary["status"] = "unreadable"
        summary["error"] = f"Manifest could not be read: {exc}"
        return summary

    summary.update(
        {
            "status": "ok",
            "input_kind": payload.get("input_kind"),
            "input_url": payload.get("input_url"),
            "canonical_url": payload.get("canonical_url"),
            "model": payload.get("model"),
            "total_videos": payload.get("total_videos"),
            "completed_videos": payload.get("completed_videos"),
            "failed_videos": payload.get("failed_videos"),
        }
    )
    return summary


def _known_batches(limit: int = 10) -> list[dict[str, str]]:
    batches = []
    for path in _recent_manifest_paths(limit):
        relative = path.relative_to(DEFAULT_OUTPUT_DIR)
        batches.append({"source_group": relative.parts[0], "source_label": relative.parts[1]})
    return batches


def _attach_resource_links(batch_result: dict[str, Any], manifest_path: Path | None = None) -> dict[str, Any]:
    try:
        manifest_file = batch_result.get("manifest_file")
        if manifest_file:
            manifest_path = _within_output_root(Path(manifest_file))
        elif manifest_path is not None:
            manifest_path = _within_output_root(manifest_path)
            batch_result["manifest_file"] = str(manifest_path)

        if manifest_path is not None:
            relative = manifest_path.relative_to(DEFAULT_OUTPUT_DIR)
            source_group, source_label = relative.parts[0], relative.parts[1]
            batch_result["manifest_resource"] = _manifest_resource_uri(source_group, source_label)
            for video in batch_result.get("videos", []):
                if isinstance(video, dict):
                    video["transcript_resource"] = _transcript_resource_uri(
                        source_group,
                        source_label,
                        str(video.get("video_id") or "video"),
                    )
    except (OSError, ValueError, IndexError) as exc:
        # Resource links are a convenience; never let them break a result
        # that the pipeline already produced successfully.
        logger.warning("Could not attach resource links: %s", exc)
    return batch_result


def _shape_batch_result(
    batch_result: dict[str, Any],
    *,
    include_transcript_text: bool,
    max_transcript_chars: int,
) -> dict[str, Any]:
    enriched = _attach_resource_links(batch_result)
    if include_transcript_text and max_transcript_chars <= 0:
        return enriched

    shaped = json.loads(json.dumps(enriched))
    for video in shaped.get("videos", []):
        if not isinstance(video, dict):
            continue
        text = video.get("transcript_text") or ""
        video["transcript_chars"] = len(text)
        if not include_transcript_text:
            video.pop("transcript_text", None)
        elif len(text) > max_transcript_chars:
            video["transcript_text"] = text[:max_transcript_chars]
            video["transcript_text_truncated"] = True
    return shaped


def _clean_url(raw_url: str) -> str:
    # LLM clients routinely wrap URLs in quotes, angle brackets, or markdown.
    url = (raw_url or "").strip().strip("'\"").strip()
    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1].strip()
    return url


def _validate_url(raw_url: str) -> tuple[str | None, dict[str, Any] | None]:
    url = _clean_url(raw_url)
    if not url:
        return None, _error(
            "invalid_input",
            "input_url is empty.",
            hint="Pass a public Instagram profile URL (https://www.instagram.com/<username>/) or a direct video URL.",
        )
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        return None, _error(
            "invalid_input",
            f"input_url must start with http:// or https://, got: {url[:200]}",
            hint="Example: https://www.instagram.com/instagram/ or https://www.instagram.com/reel/<id>/",
        )
    return url, None


def _allowed_models() -> set[str]:
    extra = {
        name.strip()
        for name in (_env("EXTRA_MODELS") or "").split(",")
        if name.strip()
    }
    return KNOWN_WHISPER_MODELS | extra


def _validate_model(model_name: str) -> tuple[str | None, dict[str, Any] | None]:
    name = (model_name or "").strip()
    if not name:
        return "base", None
    allowed = _allowed_models()
    if name not in allowed:
        return None, _error(
            "invalid_input",
            f"Unknown Whisper model: {name!r}",
            hint=f"Valid models: {', '.join(sorted(allowed))}. "
            "Set REELRECON_EXTRA_MODELS to allow additional names.",
        )
    return name, None


def _normalize_language(language: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if language is None:
        return None, None
    lang = language.strip().lower()
    if lang in {"", "auto", "none", "null", "detect", "default"}:
        return None, None
    if not re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})?|[a-z ]{3,30}", lang):
        return None, _error(
            "invalid_input",
            f"Invalid language hint: {language!r}",
            hint="Use an ISO code like 'en', 'es', 'hi', a language name like 'english', or omit it for auto-detection.",
        )
    return lang, None


def _validate_limit(limit: int) -> int:
    try:
        return min(max(int(limit), 1), MAX_LIST_LIMIT)
    except (TypeError, ValueError):
        return 10


def _validate_frame_count(max_frames: int) -> int:
    try:
        return min(max(int(max_frames), 1), MAX_FRAMES_LIMIT)
    except (TypeError, ValueError):
        return 12


def _frame_images(groups: list[tuple[str, list[dict[str, Any]]]]) -> list[Any]:
    """Each frame image preceded by a one-line label, so the model can tie pictures to the JSON packet."""
    content: list[Any] = []
    for label, frames in groups:
        for frame in frames:
            spoken = frame.get("spoken")
            content.append(f"{label} {frame['time_seconds']}s" + (f' | spoken: "{spoken}"' if spoken else " | no speech"))
            content.append(Image(path=frame["file"]))
    return content


def _validate_slug_input(value: str, field: str) -> tuple[str | None, dict[str, Any] | None]:
    cleaned = (value or "").strip()
    if not cleaned:
        return None, _error(
            "invalid_input",
            f"{field} is empty.",
            hint="Call list_recent_batches to see the available source_group/source_label values.",
        )
    return cleaned, None


class _ThreadSafeProgress:
    """Progress bridge from the worker thread to the MCP client.

    Every failure mode is swallowed on purpose: a disconnected client or a
    closed event loop must never crash the transcription thread. Updates are
    throttled so long batches do not flood the MCP session with notifications.
    """

    def __init__(self, ctx: Context, loop: asyncio.AbstractEventLoop, *, min_interval: float = 0.5) -> None:
        self._ctx = ctx
        self._loop = loop
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_stage: str | None = None
        self._last_percent = -1
        self._last_sent = 0.0

    def __call__(self, stage: str, percent: int, message: str) -> None:
        now = time.monotonic()
        with self._lock:
            stage_changed = stage != self._last_stage
            if not stage_changed and (percent == self._last_percent or now - self._last_sent < self._min_interval):
                return
            self._last_stage = stage
            self._last_percent = percent
            self._last_sent = now
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._report(stage, float(percent), message, stage_changed),
                self._loop,
            )
        except RuntimeError:
            return
        future.add_done_callback(self._consume_result)

    @staticmethod
    def _consume_result(future: Any) -> None:
        try:
            future.exception()
        except Exception:
            pass

    async def _report(self, stage: str, percent: float, message: str, stage_changed: bool) -> None:
        progress_message = f"[{stage}] {message}"
        try:
            await self._ctx.report_progress(progress=percent, total=100.0, message=progress_message)
            if stage_changed:
                await self._ctx.info(progress_message)
        except Exception:
            logger.debug("Progress notification dropped (client likely disconnected)", exc_info=True)


async def _notify(ctx: Context, level: str, message: str) -> None:
    try:
        if level == "error":
            await ctx.error(message)
        else:
            await ctx.info(message)
    except Exception:
        logger.debug("MCP %s notification dropped", level, exc_info=True)


def _preflight_transcription() -> dict[str, Any] | None:
    try:
        pipeline = _pipeline()
    except Exception as exc:
        return _error(
            "dependency_error",
            f"The transcription pipeline could not be loaded: {exc}",
            hint="Install dependencies with: pip install -r requirements.txt (Python 3.11 recommended).",
        )
    try:
        pipeline.require_ffmpeg()
    except pipeline.PipelineError as exc:
        return _error("dependency_error", str(exc))
    try:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if not os.access(DEFAULT_OUTPUT_DIR, os.W_OK):
            raise PermissionError(f"Not writable: {DEFAULT_OUTPUT_DIR}")
    except OSError as exc:
        return _error(
            "output_dir_error",
            f"The output directory is not usable: {exc}",
            hint="Set REELRECON_OUTPUT_DIR to a writable directory.",
        )
    return None


async def _run_pipeline_job(
    ctx: Context,
    job_description: str,
    func: Callable[..., dict[str, Any]],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a blocking pipeline call with queueing, a hard timeout, and structured errors."""
    global _active_jobs, _abandoned_jobs

    pipeline = _pipeline()
    semaphore = _get_job_semaphore()
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=QUEUE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _error(
            "server_busy",
            f"The server is already running {MAX_CONCURRENT_JOBS} transcription job(s) and the queue wait "
            f"exceeded {QUEUE_TIMEOUT_SECONDS}s.",
            hint="Retry later, or raise REELRECON_MAX_CONCURRENT_JOBS / REELRECON_QUEUE_TIMEOUT_SECONDS.",
        )

    _active_jobs += 1
    try:
        return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=JOB_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _abandoned_jobs += 1
        message = (
            f"{job_description} exceeded the {JOB_TIMEOUT_SECONDS}s job timeout. "
            "The worker may still be finishing in the background; completed output will appear in list_recent_batches."
        )
        await _notify(ctx, "error", message)
        return _error(
            "timeout",
            message,
            hint="Raise REELRECON_JOB_TIMEOUT_SECONDS for long batches, or use a smaller Whisper model.",
        )
    except pipeline.PipelineError as exc:
        await _notify(ctx, "error", str(exc))
        return _error("pipeline_error", str(exc))
    except Exception as exc:
        logger.exception("Unexpected failure in %s", job_description)
        message = f"Unexpected failure: {exc}"
        await _notify(ctx, "error", message)
        return _error("internal_error", message)
    finally:
        _active_jobs -= 1
        semaphore.release()


def _dependency_status() -> dict[str, Any]:
    status: dict[str, Any] = {}
    try:
        _pipeline()
        status["pipeline"] = "ok"
    except Exception as exc:
        status["pipeline"] = f"error: {exc}"
    for module_name in ("whisper", "yt_dlp", "mcp"):
        try:
            __import__(module_name)
            status[module_name] = "ok"
        except Exception as exc:
            status[module_name] = f"error: {exc}"
    return status


def build_server(*, host: str, port: int, debug: bool) -> FastMCP:
    mcp = FastMCP(
        name=SERVER_NAME,
        instructions=(
            "Use this server to transcribe a direct video URL, the latest 10 videos from a public Instagram "
            "profile, or a local audio file. The main tool is transcribe_input. Use list_recent_batches, "
            "read_batch_manifest, and read_video_output to inspect prior results. For any question about a video's hook, "
            "visuals, or why it performed, call analyze_video (one video) or compare_hooks (a whole batch): they return "
            "frames as images, timed speech, measured signals, performance, and the analysis method to follow. "
            "get_video_frames to see a video's "
            "key frames as images (visual analysis), and check_health to diagnose "
            "setup problems; if it reports no Instagram session, ask the user to run `npx -y reelrecon login` in a "
            "terminal, since profile scans need it. Batch results include a 'performance' block that ranks each video against the batch "
            "median (views, or likes + comments when views are hidden) and lists outliers at 2x or more, with the "
            "words spoken in each video's first 3 seconds; start analysis from the outliers. All tools return JSON objects with a 'status' field: 'ok' on success, or 'error' "
            "with 'error_type', 'error', and usually a 'hint' on failure — tool calls never raise for expected "
            "failures. Long transcriptions report progress notifications; set include_transcript_text=false or "
            "max_transcript_chars to keep responses small."
        ),
        host=host,
        port=port,
        debug=debug,
        log_level="DEBUG" if debug else "ERROR",
        json_response=True,
    )

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.resource("reelrecon://server")
    def server_resource() -> str:
        return _json(
            {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "output_root": str(DEFAULT_OUTPUT_DIR),
                "tools": [
                    "transcribe_input",
                    "transcribe_local_audio",
                    "list_recent_batches",
                    "read_batch_manifest",
                    "read_video_output",
                    "get_video_frames",
                    "analyze_video",
                    "compare_hooks",
                    "check_health",
                ],
                "input_support": {
                    "instagram_profile": "Fetches and transcribes the latest 10 videos from a public Instagram profile.",
                    "video_url": "Transcribes a single direct video URL.",
                    "local_audio": "Transcribes a local audio file path.",
                },
                "performance": "Batch results rank videos against the batch median (views, else likes + comments) and flag outliers at 2x+, with each video's first 3 spoken seconds.",
                "error_contract": {
                    "status": "'ok' or 'error'",
                    "error_type": "invalid_input | dependency_error | output_dir_error | pipeline_error | not_found | server_busy | timeout | internal_error",
                },
                "limits": {
                    "job_timeout_seconds": JOB_TIMEOUT_SECONDS,
                    "queue_timeout_seconds": QUEUE_TIMEOUT_SECONDS,
                    "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
                    "max_upload_bytes": MAX_UPLOAD_BYTES,
                    "max_list_limit": MAX_LIST_LIMIT,
                },
                "prompts": ["hook_analysis"],
                "resources": [
                    "reelrecon://method/hook-analysis",
                    "reelrecon://server",
                    "reelrecon://recent-batches",
                    "reelrecon://manifest/{source_group}/{source_label}",
                    "reelrecon://transcript/{source_group}/{source_label}/{video_id}",
                ],
            }
        )

    @mcp.resource("reelrecon://recent-batches")
    def recent_batches_resource() -> str:
        return _json(
            {
                "output_root": str(DEFAULT_OUTPUT_DIR),
                "batches": [_manifest_summary(path) for path in _recent_manifest_paths(limit=10)],
            }
        )

    @mcp.resource("reelrecon://manifest/{source_group}/{source_label}")
    def manifest_resource(source_group: str, source_label: str) -> str:
        try:
            path = _manifest_path(source_group, source_label)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Invalid manifest location for {source_group}/{source_label}") from exc
        if not path.exists():
            raise FileNotFoundError(f"Manifest not found for {source_group}/{source_label}")
        try:
            payload = _attach_resource_links(_load_json(path), manifest_path=path)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Manifest for {source_group}/{source_label} is corrupt: {exc}") from exc
        return _json(payload)

    @mcp.resource("reelrecon://transcript/{source_group}/{source_label}/{video_id}")
    def transcript_resource(source_group: str, source_label: str, video_id: str) -> str:
        try:
            transcript_path = _video_dir(source_group, source_label, video_id) / "transcript.txt"
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Invalid transcript location for {source_group}/{source_label}/{video_id}") from exc
        if not transcript_path.exists():
            raise FileNotFoundError(f"Transcript not found for {source_group}/{source_label}/{video_id}")
        return transcript_path.read_text(encoding="utf-8", errors="replace")

    @mcp.tool(
        description=(
            "Transcribe a direct video URL or the latest 10 videos from a public Instagram profile URL. "
            "input_url: an Instagram profile URL (https://www.instagram.com/<username>/), a single Instagram "
            "reel/post/tv URL, or any direct video URL yt-dlp supports. model_name: Whisper model "
            "(tiny/base/small/medium/large*, default 'base'). language: optional ISO hint like 'en' (omit for "
            "auto-detect). reuse_existing: reuse cached transcripts for already-processed videos. Set "
            "include_transcript_text=false or max_transcript_chars>0 to shrink the response; full text stays "
            "on disk and via resources. The result's 'performance' block ranks videos by views (or likes + comments) "
            "relative to the batch median and flags outliers; each video also carries 'metrics' and 'speech' "
            "(first_3s, words_per_minute, speech_starts_at). include_frames=true also downloads each video and saves "
            "key frames (one per shot change, max_frames each) as file paths; use get_video_frames to view them. "
            "Long profile batches can take many minutes — progress is streamed via "
            "MCP progress notifications."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def transcribe_input(
        input_url: str,
        ctx: Context,
        model_name: str = "base",
        language: str | None = None,
        reuse_existing: bool = True,
        include_transcript_text: bool = True,
        max_transcript_chars: int = 0,
        include_frames: bool = False,
        max_frames: int = 12,
    ) -> dict[str, Any]:
        url, url_error = _validate_url(input_url)
        if url_error:
            return url_error
        model, model_error = _validate_model(model_name)
        if model_error:
            return model_error
        lang, lang_error = _normalize_language(language)
        if lang_error:
            return lang_error
        preflight_error = _preflight_transcription()
        if preflight_error:
            return preflight_error

        pipeline = _pipeline()
        loop = asyncio.get_running_loop()
        progress_callback = _ThreadSafeProgress(ctx, loop)
        await _notify(ctx, "info", f"Starting transcription for {url}")

        result = await _run_pipeline_job(
            ctx,
            f"Transcription of {url}",
            pipeline.run_transcription,
            url,
            output_dir=DEFAULT_OUTPUT_DIR,
            model_name=model,
            language=lang,
            progress_callback=progress_callback,
            reuse_existing=reuse_existing,
            include_frames=include_frames,
            max_frames=_validate_frame_count(max_frames),
        )
        if result.get("status") != "ok":
            return result

        shaped = _shape_batch_result(
            result,
            include_transcript_text=include_transcript_text,
            max_transcript_chars=max(int(max_transcript_chars), 0),
        )
        try:
            await ctx.report_progress(progress=100.0, total=100.0, message="Transcription completed")
        except Exception:
            pass
        await _notify(ctx, "info", f"Completed transcription for {shaped.get('completed_videos', 0)} video(s)")
        return shaped

    @mcp.tool(
        description=(
            "Transcribe a local audio file path and generate AI insights from the transcript. audio_path must "
            "be an existing readable file on the server host (mp3/wav/m4a/flac/ogg and most ffmpeg-decodable "
            "formats). original_filename: optional display name used to label the output. model_name/language: "
            "same as transcribe_input. Set include_transcript_text=false or max_transcript_chars>0 to shrink "
            "the response."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    )
    async def transcribe_local_audio(
        audio_path: str,
        ctx: Context,
        original_filename: str | None = None,
        model_name: str = "base",
        language: str | None = None,
        include_transcript_text: bool = True,
        max_transcript_chars: int = 0,
    ) -> dict[str, Any]:
        raw_path = (audio_path or "").strip().strip("'\"")
        if not raw_path:
            return _error("invalid_input", "audio_path is empty.", hint="Pass the absolute path of a local audio file.")
        source_path = Path(raw_path).expanduser()
        try:
            source_path = source_path.resolve()
        except OSError as exc:
            return _error("invalid_input", f"audio_path could not be resolved: {exc}")
        if not source_path.exists():
            return _error("not_found", f"Audio file not found: {source_path}")
        if not source_path.is_file():
            return _error("invalid_input", f"audio_path is not a regular file: {source_path}")
        if not os.access(source_path, os.R_OK):
            return _error("invalid_input", f"Audio file is not readable: {source_path}")
        try:
            size = source_path.stat().st_size
        except OSError as exc:
            return _error("invalid_input", f"Audio file could not be inspected: {exc}")
        if size == 0:
            return _error("invalid_input", f"Audio file is empty: {source_path}")
        if size > MAX_UPLOAD_BYTES:
            return _error(
                "invalid_input",
                f"Audio file is {size} bytes, above the {MAX_UPLOAD_BYTES} byte limit.",
                hint="Raise REELRECON_MAX_UPLOAD_BYTES to allow larger files.",
            )

        model, model_error = _validate_model(model_name)
        if model_error:
            return model_error
        lang, lang_error = _normalize_language(language)
        if lang_error:
            return lang_error
        preflight_error = _preflight_transcription()
        if preflight_error:
            return preflight_error

        pipeline = _pipeline()
        loop = asyncio.get_running_loop()
        progress_callback = _ThreadSafeProgress(ctx, loop)
        await _notify(ctx, "info", f"Starting local audio transcription for {source_path}")

        result = await _run_pipeline_job(
            ctx,
            f"Local audio transcription of {source_path}",
            pipeline.run_audio_file_transcription,
            str(source_path),
            original_filename=original_filename,
            output_dir=DEFAULT_OUTPUT_DIR,
            model_name=model,
            language=lang,
            progress_callback=progress_callback,
        )
        if result.get("status") != "ok":
            return result

        shaped = _shape_batch_result(
            result,
            include_transcript_text=include_transcript_text,
            max_transcript_chars=max(int(max_transcript_chars), 0),
        )
        try:
            await ctx.report_progress(progress=100.0, total=100.0, message="Local audio transcription completed")
        except Exception:
            pass
        await _notify(ctx, "info", "Completed local audio transcription")
        return shaped

    @mcp.tool(
        description=(
            "List the most recent saved transcription batches from the local outputs directory. "
            f"limit: 1-{MAX_LIST_LIMIT}, default 10. Corrupt manifests are reported with status 'unreadable' "
            "instead of failing the whole listing."
        ),
        annotations=read_only,
    )
    def list_recent_batches(limit: int = 10) -> dict[str, Any]:
        manifests = [_manifest_summary(path) for path in _recent_manifest_paths(_validate_limit(limit))]
        return {
            "status": "ok",
            "output_root": str(DEFAULT_OUTPUT_DIR),
            "count": len(manifests),
            "batches": manifests,
        }

    @mcp.tool(
        description=(
            "Load a saved batch manifest by source_group and source_label (as returned by list_recent_batches "
            "or a transcribe call). Set include_transcript_text=false to omit per-video transcript text."
        ),
        annotations=read_only,
    )
    def read_batch_manifest(
        source_group: str,
        source_label: str,
        include_transcript_text: bool = True,
        max_transcript_chars: int = 0,
    ) -> dict[str, Any]:
        group, group_error = _validate_slug_input(source_group, "source_group")
        if group_error:
            return group_error
        label, label_error = _validate_slug_input(source_label, "source_label")
        if label_error:
            return label_error

        try:
            path = _manifest_path(group, label)
        except FileNotFoundError as exc:
            return _error("invalid_input", str(exc))
        if not path.exists():
            return _error(
                "not_found",
                f"Manifest not found for {group}/{label}",
                hint="Call list_recent_batches to see what is available.",
                available_batches=_known_batches(),
            )
        try:
            payload = _load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            return _error(
                "internal_error",
                f"Manifest for {group}/{label} could not be read: {exc}",
                hint="Re-run the transcription to regenerate this manifest.",
            )

        payload = _shape_batch_result(
            _attach_resource_links(payload, manifest_path=path),
            include_transcript_text=include_transcript_text,
            max_transcript_chars=max(int(max_transcript_chars), 0),
        )
        return {
            "status": "ok",
            "manifest_file": str(path),
            "manifest_resource": _manifest_resource_uri(group, label),
            "batch": payload,
        }

    @mcp.tool(
        description=(
            "Load the saved transcript and metadata for a single processed video by source_group, source_label, "
            "and video_id (as returned by list_recent_batches / read_batch_manifest). "
            "max_transcript_chars>0 truncates the returned transcript text."
        ),
        annotations=read_only,
    )
    def read_video_output(
        source_group: str,
        source_label: str,
        video_id: str,
        max_transcript_chars: int = 0,
    ) -> dict[str, Any]:
        group, group_error = _validate_slug_input(source_group, "source_group")
        if group_error:
            return group_error
        label, label_error = _validate_slug_input(source_label, "source_label")
        if label_error:
            return label_error
        video, video_error = _validate_slug_input(video_id, "video_id")
        if video_error:
            return video_error

        try:
            run_dir = _video_dir(group, label, video)
        except FileNotFoundError as exc:
            return _error("invalid_input", str(exc))
        transcript_path = run_dir / "transcript.txt"
        metadata_path = run_dir / "metadata.json"
        audio_path = run_dir / "audio.mp3"

        if not metadata_path.exists():
            return _error(
                "not_found",
                f"Video output not found for {group}/{label}/{video}",
                hint="Call read_batch_manifest to see the video_id values in this batch.",
                available_batches=_known_batches(),
            )

        try:
            metadata = _load_json(metadata_path)
        except (OSError, json.JSONDecodeError) as exc:
            return _error(
                "internal_error",
                f"Metadata for {group}/{label}/{video} could not be read: {exc}",
                hint="Re-run the transcription to regenerate this video's outputs.",
            )
        try:
            transcript_text = (
                transcript_path.read_text(encoding="utf-8", errors="replace") if transcript_path.exists() else ""
            )
        except OSError as exc:
            return _error("internal_error", f"Transcript for {group}/{label}/{video} could not be read: {exc}")

        result: dict[str, Any] = {
            "status": "ok",
            "audio_file": str(audio_path) if audio_path.exists() else None,
            "transcript_file": str(transcript_path) if transcript_path.exists() else None,
            "metadata_file": str(metadata_path),
            "transcript_resource": _transcript_resource_uri(group, label, video),
            "transcript_chars": len(transcript_text),
            "transcript_text": transcript_text,
            "metadata": metadata,
        }
        capped = max(int(max_transcript_chars), 0)
        if capped and len(transcript_text) > capped:
            result["transcript_text"] = transcript_text[:capped]
            result["transcript_text_truncated"] = True
        return result

    @mcp.tool(
        description=(
            "Get a processed video's key frames for visual analysis: one frame per shot change plus the opening "
            "frame, downscaled to 512px wide, each with its timestamp and the words spoken at that moment. "
            "Identify the video by source_group, source_label, and video_id (from a transcribe call, "
            "list_recent_batches, or read_batch_manifest). The first call downloads the video; later calls reuse "
            f"it. max_frames: 1-{MAX_FRAMES_LIMIT}, default 12. include_images=false returns only file paths."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def get_video_frames(
        source_group: str,
        source_label: str,
        video_id: str,
        ctx: Context,
        max_frames: int = 12,
        include_images: bool = True,
    ):
        group, group_error = _validate_slug_input(source_group, "source_group")
        if group_error:
            return group_error
        label, label_error = _validate_slug_input(source_label, "source_label")
        if label_error:
            return label_error
        video, video_error = _validate_slug_input(video_id, "video_id")
        if video_error:
            return video_error
        try:
            run_dir = _video_dir(group, label, video)
        except FileNotFoundError as exc:
            return _error("invalid_input", str(exc))
        metadata_path = run_dir / "metadata.json"
        if not metadata_path.exists():
            return _error(
                "not_found",
                f"Video output not found for {group}/{label}/{video}",
                hint="Transcribe the video first (transcribe_input), or call read_batch_manifest for valid video_id values.",
                available_batches=_known_batches(),
            )
        try:
            video_url = _load_json(metadata_path).get("video_url") or ""
        except (OSError, json.JSONDecodeError) as exc:
            return _error("internal_error", f"Metadata for {group}/{label}/{video} could not be read: {exc}")
        preflight_error = _preflight_transcription()
        if preflight_error:
            return preflight_error

        pipeline = _pipeline()

        def extract() -> dict[str, Any]:
            frames = pipeline.frames_for_video(run_dir, video_url, max_frames=_validate_frame_count(max_frames))
            return {"status": "ok", "video_url": video_url, "frame_count": len(frames), "frames": frames}

        result = await _run_pipeline_job(ctx, f"Frame extraction for {group}/{label}/{video}", extract)
        if result.get("status") != "ok" or not include_images:
            return result
        # Text summary first, then one image per frame, in timeline order.
        return [result, *(Image(path=frame["file"]) for frame in result["frames"])]

    @mcp.resource("reelrecon://method/hook-analysis")
    def hook_method_resource() -> str:
        return _pipeline().analysis_method()

    @mcp.prompt(
        name="hook_analysis",
        description="Analyze a reel's hook (or a whole profile's hooks) from frames, timed speech, and real performance.",
    )
    def hook_analysis_prompt(target: str, question: str = "") -> str:
        ask = question.strip() or "Why does this opening work or not, and what is the cheapest improvement?"
        return (
            f"Target: {target}\nQuestion: {ask}\n\n"
            "If the target is a single video URL or saved video, call analyze_video with it and this question. "
            "If it is a profile, call transcribe_input on it and then compare_hooks on the resulting batch. "
            "Then follow this method exactly:\n\n" + _pipeline().analysis_method()
        )

    @mcp.tool(
        description=(
            "Analyze one video for the user's question: its hook (first 3 seconds), visuals, and how sight and "
            "speech line up. Pass input_url (any single reel/video URL; transcribed first if new) or the "
            "source_group/source_label/video_id of a saved video, plus the user's question. Returns a JSON packet "
            "(measured signals, timed speech, performance vs. the creator's median when known, and the analysis "
            "method to follow) followed by the cover image (when available) and labelled frame images: dense frames across the first 3 seconds, then "
            "one per shot. include_method=false skips the method text on follow-up calls."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def analyze_video(
        ctx: Context,
        input_url: str | None = None,
        source_group: str | None = None,
        source_label: str | None = None,
        video_id: str | None = None,
        question: str | None = None,
        model_name: str = "base",
        max_shot_frames: int = 8,
        include_images: bool = True,
        include_method: bool = True,
    ):
        pipeline_error = _preflight_transcription()
        if pipeline_error:
            return pipeline_error
        pipeline = _pipeline()
        shot_limit = _validate_frame_count(max_shot_frames)

        if input_url:
            url, url_error = _validate_url(input_url)
            if url_error:
                return url_error
            try:
                kind, _ = pipeline.detect_input_kind(url)
            except pipeline.PipelineError as exc:
                return _error("invalid_input", str(exc))
            if kind == "instagram_profile":
                return _error(
                    "invalid_input",
                    "analyze_video takes one video; that is a profile URL.",
                    hint="Call transcribe_input on the profile, then compare_hooks with its source_group/source_label.",
                )
            model, model_error = _validate_model(model_name)
            if model_error:
                return model_error

            def run() -> dict[str, Any]:
                batch = pipeline.run_transcription(url, output_dir=DEFAULT_OUTPUT_DIR, model_name=model)
                run_dir = Path(batch["videos"][0]["metadata_file"]).parent
                return pipeline.build_video_analysis(run_dir, question=question, max_shot_frames=shot_limit, include_method=include_method)
        else:
            fields = {"source_group": source_group, "source_label": source_label, "video_id": video_id}
            missing = [name for name, value in fields.items() if not (value or "").strip()]
            if missing:
                return _error(
                    "invalid_input",
                    "Pass input_url, or all of source_group, source_label and video_id.",
                    hint="list_recent_batches and read_batch_manifest show saved videos.",
                )
            try:
                run_dir = _video_dir(source_group, source_label, video_id)
            except FileNotFoundError as exc:
                return _error("invalid_input", str(exc))
            if not (run_dir / "metadata.json").exists():
                return _error("not_found", f"Video output not found for {source_group}/{source_label}/{video_id}", available_batches=_known_batches())

            def run() -> dict[str, Any]:
                return pipeline.build_video_analysis(run_dir, question=question, max_shot_frames=shot_limit, include_method=include_method)

        packet = await _run_pipeline_job(ctx, "Video analysis", run)
        if packet.get("status") != "ok" or not include_images:
            return packet
        content = [packet]
        if packet.get("cover"):
            content += ["cover image (profile grid / share thumbnail)", Image(path=packet["cover"]["file"])]
        return content + _frame_images([("hook frame", packet["hook_frames"]), ("shot frame", packet["shot_frames"])])

    @mcp.tool(
        description=(
            "Compare the hooks of every video in a saved batch (e.g. a profile from transcribe_input): each video's "
            "cover image (together: the creator's profile grid) and first 3 seconds as labelled frames (0s, 1s, 2s, 3s) "
            "with its spoken words, measured signals, and real "
            "performance against the creator's median, plus the analysis method. Use it to find what the "
            f"over-performing openings share. max_videos: 1-{MAX_LIST_LIMIT}, default 10."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def compare_hooks(
        source_group: str,
        source_label: str,
        ctx: Context,
        max_videos: int = 10,
        include_images: bool = True,
        include_method: bool = True,
    ):
        group, group_error = _validate_slug_input(source_group, "source_group")
        if group_error:
            return group_error
        label, label_error = _validate_slug_input(source_label, "source_label")
        if label_error:
            return label_error
        try:
            manifest = _manifest_path(group, label)
        except FileNotFoundError as exc:
            return _error("invalid_input", str(exc))
        if not manifest.exists():
            return _error("not_found", f"Manifest not found for {group}/{label}", hint="Call transcribe_input on the profile first.", available_batches=_known_batches())
        preflight_error = _preflight_transcription()
        if preflight_error:
            return preflight_error
        pipeline = _pipeline()
        packet = await _run_pipeline_job(
            ctx,
            f"Hook comparison for {group}/{label}",
            pipeline.build_hook_comparison,
            manifest.parent,
            max_videos=_validate_limit(max_videos),
            include_method=include_method,
        )
        if packet.get("status") != "ok" or not include_images:
            return packet
        groups = []
        for video in packet["videos"]:
            perf = video.get("performance") or {}
            tag = f"{video['video_id']} ({perf['multiplier']}x median)" if perf.get("multiplier") is not None else video["video_id"]
            groups.append((f"[{tag}] hook frame", video.get("hook_frames") or []))
        content: list[Any] = [packet]
        # Covers first, in batch order: together they are the creator's profile grid.
        for video in packet["videos"]:
            if video.get("cover"):
                perf = video.get("performance") or {}
                tag = f"{video['video_id']} ({perf['multiplier']}x median)" if perf.get("multiplier") is not None else video["video_id"]
                content += [f"[{tag}] cover image", Image(path=video["cover"]["file"])]
        return content + _frame_images(groups)

    @mcp.tool(
        description=(
            "Check server health: dependency status (whisper, yt-dlp, ffmpeg), output directory writability, "
            "GroqCloud configuration, job limits, and current job activity. Call this first when transcription "
            "tools fail unexpectedly."
        ),
        annotations=read_only,
    )
    async def check_health() -> dict[str, Any]:
        dependencies = await asyncio.to_thread(_dependency_status)
        ffmpeg_path = shutil.which("ffmpeg")

        output_root_exists = DEFAULT_OUTPUT_DIR.is_dir()
        output_root_writable = False
        try:
            DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            output_root_exists = True
            output_root_writable = os.access(DEFAULT_OUTPUT_DIR, os.W_OK)
        except OSError:
            pass

        session = None
        try:
            session = _pipeline().session_source()
        except Exception:
            pass

        problems = []
        if session is None:
            problems.append(
                "not connected to Instagram: profile scans will fail. Ask the user to run `npx -y reelrecon login` "
                "in a terminal (it imports their browser's Instagram login); single public reels may still work"
            )
        if not ffmpeg_path:
            problems.append("ffmpeg is not on PATH")
        if not output_root_writable:
            problems.append(f"output directory is not writable: {DEFAULT_OUTPUT_DIR}")
        problems.extend(
            f"{name}: {status}" for name, status in dependencies.items() if str(status).startswith("error")
        )

        return {
            "status": "ok" if not problems else "degraded",
            "problems": problems,
            "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "python_version": sys.version.split()[0],
            "dependencies": dependencies,
            "ffmpeg_path": ffmpeg_path,
            "output_root": str(DEFAULT_OUTPUT_DIR),
            "output_root_exists": output_root_exists,
            "output_root_writable": output_root_writable,
            "saved_batches": len(_recent_manifest_paths(MAX_LIST_LIMIT)),
            "groq_configured": bool(os.environ.get("GROQ_API_KEY")),
            "instagram_cookies_configured": session is not None,
            "instagram_session": session,
            "jobs": {
                "active": _active_jobs,
                "abandoned_after_timeout": _abandoned_jobs,
                "max_concurrent": MAX_CONCURRENT_JOBS,
                "job_timeout_seconds": JOB_TIMEOUT_SECONDS,
                "queue_timeout_seconds": QUEUE_TIMEOUT_SECONDS,
            },
            "allowed_models": sorted(_allowed_models()),
        }

    return mcp


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Expose the IG Content Transcriber pipeline as an MCP server for other AI clients."
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
        help="MCP transport to run. stdio is the default and is the right choice for Claude/Cursor-style integrations.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP transports.")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transports.")
    parser.add_argument("--debug", action="store_true", help="Enable MCP server debug logging.")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    # Anything written to stdout would corrupt the stdio MCP framing; keep all
    # server-side logging on stderr.
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    server = build_server(host=args.host, port=args.port, debug=args.debug)
    try:
        server.run(transport=args.transport)
    except KeyboardInterrupt:
        logger.info("MCP server interrupted; shutting down.")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
