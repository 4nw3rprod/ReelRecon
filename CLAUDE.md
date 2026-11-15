# Claude Usage

Use this repository to:

- fetch the latest 10 videos from a public Instagram profile and transcribe them, or
- transcribe a single direct video URL, or
- transcribe a local uploaded audio file.

There is also a local web app for interactive use and progress tracking. The frontend is a Vite React app built with shadcn/ui components and served by the FastAPI backend after build.
There is also an MCP server so Claude or other MCP-compatible clients can operate the tool directly.

AI insights are generated with GroqCloud when `GROQ_API_KEY` is available. The app falls back to local heuristic insights if Groq is unavailable.

## Install

Run:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then connect Instagram, which profile scans require (Instagram hides profiles from logged-out requests):

```bash
.venv/bin/python -m ig_transcriber.login     # or: npx -y reelrecon login
```

It is interactive (browser choice, OS keychain prompt), so the user must run it in their own terminal; agents should not run it for them. It saves the session to `~/.reelrecon/instagram_cookies.txt` (mode 600), which every run uses automatically. `--status` checks it, `--logout` deletes it. If `check_health` lists "not connected to Instagram", ask the user to run it.

Optional:

```bash
cp .env.example .env.local
```

Then set `GROQ_API_KEY` in `.env.local`.

Requirements:

- `ffmpeg` available on `PATH`
- network access enabled
- public Instagram profile URL

## Preferred command

Use JSON mode so stdout is machine-readable:

```bash
./run_latest_reel_transcription.sh "https://www.instagram.com/<username>/" --json
```

Optional:

```bash
./run_latest_reel_transcription.sh "https://www.instagram.com/reel/<id>/" --json --model small --language en
```

## MCP

Preferred command for MCP clients:

```bash
./run_mcp_server.sh
```

Or, without a local clone (Node 18+, Python 3.10+, ffmpeg required; first run provisions a Python env in `~/.reelrecon`):

```bash
npx -y reelrecon
```

This starts the server over stdio. The MCP surface exposes:

- `transcribe_input`
- `transcribe_local_audio`
- `list_recent_batches`
- `read_batch_manifest`
- `read_video_output`
- `get_video_frames`
- `analyze_video`
- `compare_hooks`
- `check_health`

MCP tools never raise for expected failures: every tool returns `status: "ok"` or `status: "error"` with `error_type`, `error`, and usually a `hint`. Use `check_health` to diagnose setup problems (whisper/yt-dlp/ffmpeg availability, output directory writability, job activity). Use `include_transcript_text=false` or `max_transcript_chars` to keep tool responses small; full transcripts stay on disk and behind the transcript resources. In multi-video batches a failing video is recorded with `status: "error"` and counted in `failed_videos` instead of aborting the batch. Server limits (job timeout, concurrency, upload size) are tunable via `REELRECON_*` environment variables (legacy `IG_TRANSCRIBER_*` names still work) documented in the README.

Resources:

- `reelrecon://server`
- `reelrecon://recent-batches`
- `reelrecon://manifest/{source_group}/{source_label}`
- `reelrecon://transcript/{source_group}/{source_label}/{video_id}`

If an MCP client needs HTTP instead of stdio:

```bash
./run_mcp_server.sh --transport streamable-http --host 127.0.0.1 --port 8001
```

Then connect the client to `http://127.0.0.1:8001/mcp`.

## UI

Start the local app:

```bash
./run_ui.sh
```

The launcher picks an open localhost port and opens the browser automatically. If needed, read the URL from terminal output.
It also builds the frontend before starting the server.

## Success contract

On success, stdout is a single JSON object with:

- `status`
- `input_kind`
- `input_url`
- `canonical_url`
- `total_videos`
- `completed_videos`
- `videos`
- `ai_overview`
- `manifest_file`

Each item in `videos` includes transcript paths, metadata paths, detected language, and `ai_insights`.

For any question about why a video works, its hook, its visuals, its cover, or the creator's strategy, use `analyze_video` (one video: `input_url` or saved ids, plus `question`) or `compare_hooks` (a saved batch). Both return a JSON packet with `analysis_method` (the method in `skills/reelrecon-hook-analyzer/SKILL.md`) followed by labelled images: the cover, then hook-window frames and shot frames. Follow that method: evidence for every claim, strong/adequate/weak bands, never numeric scores, and persuasion levers described as techniques rather than judgements of the creator. Pass `include_method=false` on follow-up calls to save tokens.

For raw frames only, `get_video_frames(source_group, source_label, video_id)` downloads the video once and returns its key frames as images: the opening frame plus one per shot change (sampled about every 3 seconds when there are few cuts), 512px wide, at most `max_frames` (default 12). Each frame has `time_seconds` and `spoken` (the words said at that moment). `transcribe_input(include_frames=true)` (CLI: `--frames`) saves frames for every video in a batch as file paths without returning images. Frames live in each video's `frames/` folder.

Batch results also include `performance`: each video scored against the batch median (`metric` is `views` when every video has a view count, otherwise `engagement` = likes + comments, which Instagram still shows to logged-out requests). `performance.outliers` lists videos at 2x the median or more, with the words spoken in their first 3 seconds. Each video carries `metrics` (views, likes, comments, duration_seconds; `null` when hidden) and `speech` (opening_line, first_3s, speech_starts_at, words_per_minute). Timed Whisper segments are saved in each video's `metadata.json`.

## Failure contract

On failure, the command exits non-zero.

If `--json` is used, stdout includes:

```json
{"status":"error","error":"..."}
```

Human-readable error details are also written to stderr.

## Notes

- Public profiles only (private accounts are refused even if the logged-in user follows them).
- Profile scans read the latest 10 reels from the profile's Reels tab (pinned reels skipped) via Instagram's web GraphQL query, logged in. If they fail with "unexpected response", Instagram rotated the query ID: set `REELRECON_REELS_DOC_ID` or update the default in `ig_transcriber/pipeline.py`.
- Local audio uploads bypass Instagram entirely.
- Profile scans need the saved login (above). Single public reels usually work without it. `REELRECON_COOKIES_FILE` / `REELRECON_COOKIES_FROM_BROWSER` override the saved login.
- The wrapper prefers Python 3.11 when available to avoid `yt-dlp` Python 3.9 deprecation noise.
- The wrapper prefers the repo-local `.venv` first when present.
