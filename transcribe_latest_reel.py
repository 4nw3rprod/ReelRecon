#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ig_transcriber import PipelineError, run_transcription


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Accept an Instagram profile URL or a direct video URL, "
            "download audio, and transcribe with Whisper."
        )
    )
    parser.add_argument("input_url", help="Instagram profile URL or direct video URL")
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="Base directory for generated files. Default: %(default)s",
    )
    parser.add_argument(
        "--model",
        default="base",
        help="Whisper model name. Examples: tiny, base, small, medium, large",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional Whisper language hint, e.g. en, hi, es",
    )
    parser.add_argument(
        "--frames",
        action="store_true",
        help="Also download each video and save key frames (one per shot change) for visual analysis",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=12,
        help="Most frames to keep per video with --frames. Default: %(default)s",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result to stdout",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        result = run_transcription(
            args.input_url,
            output_dir=args.output_dir,
            model_name=args.model,
            language=args.language,
            reuse_existing=True,
            include_frames=args.frames,
            max_frames=args.max_frames,
        )
    except PipelineError as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Input kind: {result['input_kind']}")
        print(f"Canonical URL: {result['canonical_url']}")
        print(f"Videos processed: {result['completed_videos']}/{result['total_videos']}")
        print(f"Manifest file: {result['manifest_file']}")
        for video in result["videos"]:
            print(f"- {video.get('title')}")
            print(f"  Video URL: {video.get('video_url')}")
            if video.get("status") == "ok":
                print(f"  Transcript: {video.get('transcript_file')}")
                if video.get("frames"):
                    print(f"  Frames: {len(video['frames'])} in {Path(video['frames'][0]['file']).parent}")
                elif video.get("frames_error"):
                    print(f"  Frames failed: {video['frames_error']}")
            else:
                print(f"  FAILED: {video.get('error', 'unknown error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
