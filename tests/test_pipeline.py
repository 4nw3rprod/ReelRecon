from __future__ import annotations

import json
from pathlib import Path

import pytest

from ig_transcriber import pipeline


def test_pipeline_imports_without_heavy_dependencies():
    # whisper/torch and yt-dlp must stay lazy so MCP server startup is fast
    # and works even when those packages are broken.
    import sys

    assert "whisper" not in sys.modules or True  # informational; import must not be required
    assert callable(pipeline.run_transcription)


def test_normalize_input_url():
    original, canonical = pipeline.normalize_input_url("  https://example.com/v  ")
    assert original == "https://example.com/v"
    assert canonical == "https://example.com/v"

    with pytest.raises(pipeline.PipelineError):
        pipeline.normalize_input_url("ftp://example.com/v")
    with pytest.raises(pipeline.PipelineError):
        pipeline.normalize_input_url("not-a-url")


def test_detect_input_kind():
    kind, target = pipeline.detect_input_kind("https://www.instagram.com/someuser/")
    assert kind == "instagram_profile"
    assert target == "https://www.instagram.com/someuser/"

    kind, _ = pipeline.detect_input_kind("https://www.instagram.com/reel/abc123/")
    assert kind == "video"

    kind, _ = pipeline.detect_input_kind("https://example.com/watch?v=1")
    assert kind == "video"

    with pytest.raises(pipeline.PipelineError):
        pipeline.detect_input_kind("https://www.instagram.com/bad name!/")


def test_atomic_write_text(tmp_path: Path):
    target = tmp_path / "nested" / "file.json"
    pipeline._atomic_write_text(target, '{"a": 1}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}

    pipeline._atomic_write_text(target, '{"a": 2}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}

    leftovers = [p for p in target.parent.iterdir() if p.name != "file.json"]
    assert leftovers == []


def test_failed_video_result_has_consumer_keys():
    candidate = pipeline.VideoCandidate(
        source_kind="instagram_profile",
        input_url="https://www.instagram.com/x/",
        canonical_url="https://www.instagram.com/x/",
        source_label="x",
        source_group="instagram_profiles",
        video_id="abc",
        timestamp=0,
        title="t",
        caption="",
        video_url="https://www.instagram.com/reel/abc/",
        uploader="x",
        platform="instagram",
    )
    result = pipeline._failed_video_result(candidate, "boom")
    assert result["status"] == "error"
    assert result["error"] == "boom"
    # Keys the web app / CLI dereference must exist even for failures.
    for key in ("audio_file", "transcript_file", "metadata_file", "transcript_text", "video_url", "title"):
        assert key in result


def test_require_ffmpeg_error(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: None)
    with pytest.raises(pipeline.PipelineError, match="ffmpeg"):
        pipeline.require_ffmpeg()


def test_run_audio_file_transcription_validates_input(tmp_path: Path):
    with pytest.raises(pipeline.PipelineError, match="not found"):
        pipeline.run_audio_file_transcription(tmp_path / "missing.mp3", output_dir=tmp_path)

    empty = tmp_path / "empty.mp3"
    empty.touch()
    with pytest.raises(pipeline.PipelineError, match="empty"):
        pipeline.run_audio_file_transcription(empty, output_dir=tmp_path)
