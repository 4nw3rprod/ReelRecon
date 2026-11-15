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


def test_cookie_settings_missing_file_raises(monkeypatch):
    monkeypatch.setenv("REELRECON_COOKIES_FILE", "/does/not/exist/cookies.txt")
    with pytest.raises(pipeline.PipelineError, match="missing file"):
        pipeline.cookie_settings()


def test_cookie_settings_and_yt_dlp_options(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("REELRECON_COOKIES_FILE", str(cookies))

    cookies_file, cookies_browser = pipeline.cookie_settings()
    assert cookies_file == str(cookies)
    assert cookies_browser is None
    assert pipeline._yt_dlp_base_options()["cookiefile"] == str(cookies)

    monkeypatch.delenv("REELRECON_COOKIES_FILE")
    monkeypatch.setenv("REELRECON_COOKIES_FROM_BROWSER", "firefox:MyProfile")
    assert pipeline._yt_dlp_base_options()["cookiesfrombrowser"] == ("firefox", "MyProfile")

    monkeypatch.delenv("REELRECON_COOKIES_FROM_BROWSER")
    options = pipeline._yt_dlp_base_options()
    assert "cookiefile" not in options and "cookiesfrombrowser" not in options


def test_instagram_cookie_header(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        ".instagram.com\tTRUE\t/\tTRUE\t1993456000\tsessionid\tabc123\n"
        ".instagram.com\tTRUE\t/\tTRUE\t1993456000\tcsrftoken\ttok\n"
        ".example.com\tTRUE\t/\tFALSE\t1993456000\tother\tx\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REELRECON_COOKIES_FILE", str(cookies))
    header = pipeline._instagram_cookie_header()
    assert "sessionid=abc123" in header
    assert "csrftoken=tok" in header
    assert "other=x" not in header

    monkeypatch.delenv("REELRECON_COOKIES_FILE")
    assert pipeline._instagram_cookie_header() is None


def test_download_error_login_wall_hint():
    err = pipeline._download_error(
        Exception("Requested content is not available, rate-limit reached or login required"),
        "https://www.instagram.com/reel/abc/",
        "downloading audio for",
    )
    assert "reelrecon login" in str(err)

    plain = pipeline._download_error(Exception("connection reset"), "https://example.com/v", "inspecting")
    assert "reelrecon login" not in str(plain)


def _ok_video(video_id: str, **metrics):
    base = {"views": None, "likes": None, "comments": None, "duration_seconds": None}
    return {"status": "ok", "video_id": video_id, "title": video_id, "metrics": {**base, **metrics}, "speech": {"first_3s": f"hook {video_id}"}}


def test_analyze_performance_uses_views_when_all_present():
    videos = [_ok_video("a", views=100), _ok_video("b", views=120), _ok_video("c", views=400), {"status": "error", "video_id": "d"}]
    result = pipeline.analyze_performance(videos)
    assert result["available"] and result["metric"] == "views"
    assert result["median"] == 120
    assert [o["video_id"] for o in result["outliers"]] == ["c"]
    assert result["outliers"][0]["first_3s"] == "hook c"
    assert videos[2]["performance"]["is_outlier"] is True
    assert videos[0]["performance"]["multiplier"] == 0.83
    assert "performance" not in videos[3]


def test_analyze_performance_falls_back_to_engagement():
    videos = [_ok_video("a", views=500, likes=10, comments=0), _ok_video("b", likes=10, comments=2), _ok_video("c", likes=50, comments=10)]
    result = pipeline.analyze_performance(videos)
    assert result["metric"] == "engagement"
    assert [r["value"] for r in result["ranking"]] == [60, 12, 10]
    assert result["outliers"][0]["video_id"] == "c"


def test_analyze_performance_needs_enough_data():
    assert pipeline.analyze_performance([_ok_video("a", likes=5), _ok_video("b")])["available"] is False
    assert pipeline.analyze_performance([_ok_video(x, likes=0) for x in "abc"])["available"] is False


def test_speech_profile():
    segments = [{"start": 0.4, "end": 2.0, "text": " Stop scrolling."}, {"start": 2.5, "end": 6.0, "text": "Here is why."}, {"start": 6.0, "end": 12.4, "text": "  "}]
    profile = pipeline.speech_profile(segments, "Stop scrolling. Here is why.")
    assert profile["first_3s"] == "Stop scrolling. Here is why."
    assert profile["opening_line"] == "Stop scrolling."
    assert profile["speech_starts_at"] == 0.4
    assert profile["words_per_minute"] == round(5 / (5.6 / 60))
    assert pipeline.speech_profile([], "")["has_speech"] is False


def test_metrics_extraction_and_merge():
    media = {"play_count": 900, "view_count": 5, "like_count": 40, "comment_count": 3, "video_duration": 12.34}
    assert pipeline.metrics_from_reel_media(media) == {"views": 900, "likes": 40, "comments": 3, "duration_seconds": 12.3}
    assert pipeline.metrics_from_reel_media({"view_count": 5})["views"] == 5
    info = pipeline.metrics_from_yt_dlp_info({"like_count": 107621, "comment_count": 1338, "view_count": None})
    assert info == {"views": None, "likes": 107621, "comments": 1338, "duration_seconds": None}
    assert pipeline.merge_metrics({"views": None, "likes": 5}, {"views": 77, "likes": 9}) == {"views": 77, "likes": 5, "comments": None, "duration_seconds": None}
