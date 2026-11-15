from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ig_transcriber import pipeline
from tests.test_frames import make_video

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    # Cover lookups fall back to yt-dlp; keep tests off the network.
    def no_network(url):
        raise pipeline.PipelineError("offline")

    monkeypatch.setattr(pipeline, "_yt_dlp_extract_info", no_network)


def test_spread_evenly_keeps_first_and_last():
    assert pipeline._spread_evenly(list(range(10)), 4) == [0, 3, 6, 9]
    assert pipeline._spread_evenly([1, 2], 5) == [1, 2]
    assert pipeline._spread_evenly([1, 2, 3], 1) == [1]


def test_analysis_method_is_the_skill_body():
    method = pipeline.analysis_method()
    assert method.startswith("# ReelRecon Hook Analyzer")
    assert "name: reelrecon-hook-analyzer" not in method  # frontmatter stripped
    for section in ("Four gates", "Seven dimensions", "The cover", "Creator strategy and psychology", "Nativa Labs"):
        assert section in method


def test_video_signals():
    metadata = {
        "speech": {"has_speech": True, "speech_starts_at": 0.4, "first_3s": "Stop scrolling.", "words_per_minute": 180},
        "segments": [{"start": 0.4, "end": 2.0, "text": "Stop scrolling now."}, {"start": 3.5, "end": 5.0, "text": "Later words."}],
    }
    shots = [{"time_seconds": 0.0}, {"time_seconds": 1.2}, {"time_seconds": 2.5}, {"time_seconds": 7.0}]
    signals = pipeline.video_signals(metadata, shots, 12.0)
    assert signals["words_in_hook_window"] == 3
    assert signals["hook_window_shot_changes"] == 2
    assert signals["shot_count"] == 4 and signals["average_shot_seconds"] == 3.0


def _saved_video(root: Path, video_id: str, sources: list[str], performance=None) -> Path:
    run_dir = root / video_id.lower()
    run_dir.mkdir(parents=True)
    make_video(run_dir / "video.mp4", sources)
    segments = [{"start": 0.0, "end": 2.5, "text": "Watch this."}]
    (run_dir / "metadata.json").write_text(json.dumps({
        "video_id": video_id, "title": "t", "video_url": f"https://example.com/{video_id}", "segments": segments,
        "speech": {"has_speech": True, "speech_starts_at": 0.0, "first_3s": "Watch this.", "words_per_minute": 120},
        "metrics": {"views": 100},
    }))
    return run_dir


@needs_ffmpeg
def test_build_video_analysis_packet(tmp_path):
    run_dir = _saved_video(tmp_path / "batch", "VID1", ["testsrc", "color=c=red", "smptebars"])
    (tmp_path / "batch" / "manifest.json").write_text(json.dumps({
        "performance": {"available": True, "median": 50, "videos_scored": 3, "ranking": [{"video_id": "X"}, {"video_id": "VID1"}]},
        "videos": [{"video_id": "VID1", "performance": {"metric": "views", "value": 100, "multiplier": 2.0, "is_outlier": True}}],
    }))
    packet = pipeline.build_video_analysis(run_dir, question="why?", max_shot_frames=2)
    assert packet["question"] == "why?"
    assert [f["time_seconds"] for f in packet["hook_frames"]] == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    assert packet["hook_frames"][0]["spoken"] == "Watch this."
    assert [f["time_seconds"] for f in packet["shot_frames"]] == [0.0, 4.0]  # 3 shots spread to 2
    assert packet["signals"]["shot_count"] == 3 and packet["signals"]["hook_window_shot_changes"] == 1
    assert packet["performance"]["rank_in_batch"] == 2 and packet["performance"]["batch_median"] == 50
    assert packet["cover"] is None  # no cover URL and offline
    assert "Four gates" in packet["analysis_method"]
    assert "analysis_method" not in pipeline.build_video_analysis(run_dir, include_method=False)


@needs_ffmpeg
def test_cover_is_reused_and_hook_comparison(tmp_path):
    batch = tmp_path / "batch"
    a = _saved_video(batch, "A1", ["testsrc", "color=c=red"])
    _saved_video(batch, "B2", ["color=c=gray"])
    shutil.copy(next((a).glob("video.mp4")), a / "cover.jpg")  # any non-empty file stands in for a cached cover
    (batch / "manifest.json").write_text(json.dumps({
        "input_url": "https://www.instagram.com/x/",
        "performance": {"available": True, "metric": "views", "median": 100, "videos_scored": 2},
        "videos": [
            {"video_id": "A1", "status": "ok", "performance": {"multiplier": 2.5}},
            {"video_id": "B2", "status": "ok", "performance": {"multiplier": 0.4}},
            {"video_id": "C3", "status": "error"},
        ],
    }))
    packet = pipeline.build_hook_comparison(batch)
    assert [v["video_id"] for v in packet["videos"]] == ["A1", "B2"]
    first = packet["videos"][0]
    assert [f["time_seconds"] for f in first["hook_frames"]] == [0.0, 1.0, 2.0, 3.0]
    assert first["cover"] == {"file": str(a / "cover.jpg")}
    assert packet["videos"][1]["cover"] is None
    assert packet["performance_summary"]["median"] == 100
