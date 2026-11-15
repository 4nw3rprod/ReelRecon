from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ig_transcriber import pipeline

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_video(path: Path, sources: list[str], seconds: int = 2) -> Path:
    """Concatenate lavfi sources (each `seconds` long) into one video with hard cuts between them."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for source in sources:
        sep = ":" if "=" in source else "="
        args += ["-f", "lavfi", "-i", f"{source}{sep}duration={seconds}:size=640x360:rate=10"]
    inputs = "".join(f"[{i}]" for i in range(len(sources)))
    args += ["-filter_complex", f"{inputs}concat=n={len(sources)}:v=1", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True)
    return path


def test_extract_frames_one_per_shot_with_spoken_text(tmp_path):
    video = make_video(tmp_path / "video.mp4", ["testsrc", "color=c=red", "smptebars"])
    segments = [{"start": 0.0, "end": 1.5, "text": "Stop scrolling."}, {"start": 4.0, "end": 6.0, "text": "Here's why."}]
    frames = pipeline.extract_frames(video, tmp_path / "frames", segments=segments)
    assert [f["time_seconds"] for f in frames] == [0.0, 2.0, 4.0]
    assert [f["spoken"] for f in frames] == ["Stop scrolling.", None, "Here's why."]
    assert all(Path(f["file"]).exists() for f in frames)
    width = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", frames[0]["file"]], capture_output=True, text=True
    ).stderr
    assert "512x288" in width


def test_extract_frames_caps_count_keeping_opening_frame(tmp_path):
    video = make_video(tmp_path / "video.mp4", ["testsrc", "color=c=red", "smptebars", "color=c=blue", "color=c=green"])
    frames = pipeline.extract_frames(video, tmp_path / "frames", max_frames=3)
    assert [f["time_seconds"] for f in frames] == [0.0, 4.0, 8.0]
    assert len(list((tmp_path / "frames").glob("*.jpg"))) == 3


def test_extract_frames_samples_static_video_evenly(tmp_path):
    video = make_video(tmp_path / "video.mp4", ["color=c=gray"], seconds=7)
    frames = pipeline.extract_frames(video, tmp_path / "frames")
    assert len(frames) == 3  # one frame about every 3 seconds
    assert frames[0]["time_seconds"] == 0.0


def test_frames_for_video_saves_into_metadata_and_attach_survives_errors(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    make_video(run_dir / "video.mp4", ["testsrc", "color=c=red"])
    (run_dir / "metadata.json").write_text(json.dumps({"video_url": "https://example.com/v", "segments": []}))
    frames = pipeline.frames_for_video(run_dir, "https://example.com/v")  # video already on disk: no download
    assert len(frames) == 2
    assert json.loads((run_dir / "metadata.json").read_text())["frames"] == frames

    result = pipeline.attach_frames({"metadata_file": str(tmp_path / "missing" / "metadata.json"), "video_url": ""})
    assert result["frames"] == [] and "no video URL" in result["frames_error"]


def test_opening_frame_skips_black_fade_in(tmp_path):
    video = make_video(tmp_path / "video.mp4", ["color=c=black", "testsrc", "smptebars"])
    frames = pipeline.extract_frames(video, tmp_path / "frames")
    assert frames[0]["time_seconds"] >= 2.0  # first real picture, not the black opening
    assert [f["time_seconds"] for f in frames] == [2.0, 4.0]
