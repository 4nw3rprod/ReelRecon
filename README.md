# IG Content Transcriber

This pipeline takes a public Instagram profile URL, a direct video URL, or an uploaded audio file.

- If you pass an Instagram profile URL, it fetches the latest 10 videos and transcribes all of them.
- If you pass a direct video URL, it transcribes that single video.
- If you upload an audio file, it transcribes the audio directly and generates AI insights from the transcript.

It also ships with a local web UI built with React, Vite, Tailwind, and shadcn/ui components for running jobs, watching live progress, opening the generated artifacts, and showing AI insights.
It also ships with an MCP server so other AI clients can operate the pipeline over a standard tool interface.

## Requirements

- Python 3.9+
- `ffmpeg` installed and available on `PATH`

## Install

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Optional for AI insights:

```bash
cp .env.example .env.local
```

Then set `GROQ_API_KEY` in `.env.local`.

## Usage

```bash
./run_latest_reel_transcription.sh "https://www.instagram.com/nike/"
```

Optional flags:

```bash
./run_latest_reel_transcription.sh \
  "https://www.instagram.com/reel/DXR5AB3j78e/" \
  --model small \
  --language en \
  --output-dir outputs
```

Machine-readable output for agent workflows:

```bash
./run_latest_reel_transcription.sh \
  "https://www.instagram.com/nike/" \
  --json
```

## MCP Server

Start the MCP server over stdio:

```bash
./run_mcp_server.sh
```

This is the preferred mode for local MCP clients such as Claude Code, Claude Desktop, and Cursor. Point the client at:

- command: `/Users/anw3r/AI Projects/IG Content Transcriber/run_mcp_server.sh`
- args: none

If your MCP client prefers HTTP instead of stdio:

```bash
./run_mcp_server.sh --transport streamable-http --host 127.0.0.1 --port 8001
```

Then connect the client to:

```text
http://127.0.0.1:8001/mcp
```

MCP tools exposed:

- `transcribe_input`
- `transcribe_local_audio`
- `list_recent_batches`
- `read_batch_manifest`
- `read_video_output`
- `check_health`

MCP resources exposed:

- `ig-transcriber://server`
- `ig-transcriber://recent-batches`
- `ig-transcriber://manifest/{source_group}/{source_label}`
- `ig-transcriber://transcript/{source_group}/{source_label}/{video_id}`

### MCP error contract

Tools never raise for expected failures. Every tool returns a JSON object with `status`: `"ok"` or `"error"`. Errors include `error_type` (`invalid_input`, `dependency_error`, `output_dir_error`, `pipeline_error`, `not_found`, `server_busy`, `timeout`, `internal_error`), a human-readable `error`, and usually a `hint`. Call `check_health` first when transcription tools fail unexpectedly — it reports dependency status (whisper, yt-dlp, ffmpeg), output-directory writability, and job activity.

Large responses can be trimmed with `include_transcript_text=false` or `max_transcript_chars` on the transcription and read tools; full transcripts always remain on disk and via the `ig-transcriber://transcript/...` resources. In multi-video profile batches, a failing video no longer aborts the batch: it is recorded in `videos` with `status: "error"` and counted in `failed_videos`.

### MCP server tuning (environment variables)

- `IG_TRANSCRIBER_OUTPUT_DIR` — output root (default: `<repo>/outputs`)
- `IG_TRANSCRIBER_JOB_TIMEOUT_SECONDS` — hard per-job timeout (default: 3600)
- `IG_TRANSCRIBER_QUEUE_TIMEOUT_SECONDS` — max wait for a job slot (default: 900)
- `IG_TRANSCRIBER_MAX_CONCURRENT_JOBS` — parallel transcription jobs (default: 1)
- `IG_TRANSCRIBER_MAX_UPLOAD_BYTES` — max local audio file size (default: 2 GiB)
- `IG_TRANSCRIBER_EXTRA_MODELS` — comma-separated extra Whisper model names to allow
- `IG_TRANSCRIBER_HTTP_TIMEOUT_SECONDS` — Instagram/Groq/yt-dlp socket timeout (default: 30)
- `IG_TRANSCRIBER_FETCH_RETRIES` — Instagram profile fetch attempts (default: 3)

## Web UI

Start the app:

```bash
./run_ui.sh
```

The launcher will pick an open localhost port and open the browser automatically.
It also installs the frontend dependencies if needed and rebuilds the shadcn dashboard before starting the server.

If you want to disable auto-open:

```bash
./run_ui.sh --no-open
```

If you want to open it manually, use the URL printed in the terminal. Typical URLs are:

```text
http://127.0.0.1:8000
```

The UI lets you:

- submit Instagram profile URLs or direct video URLs
- upload audio files such as `mp3`, `wav`, `m4a`, `aac`, `flac`, `ogg`, and `webm`
- choose the Whisper model and optional language hint
- monitor progress across each pipeline stage
- inspect recent jobs
- open generated audio, transcript, metadata, and manifest files
- review per-video transcripts and AI insights
- keep input, progress, transcript, activity, and history visible in a single fixed dashboard

## Output

Each run writes files under source-based folders such as:

```text
outputs/instagram_profiles/<username>/<video_id>/
outputs/video_urls/<source_label>/<video_id>/
```

Files created:

- `audio.mp3`
- `transcript.txt`
- `metadata.json`
- `manifest.json` at the batch/source level

## Tests

The MCP server and pipeline helpers are covered by a lightweight test suite that only needs `mcp` and `pytest` (no whisper/torch download):

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

## Notes

- This currently supports public Instagram profiles only.
- If Instagram rate-limits or blocks anonymous access, rerun later.
- Uploaded audio does not depend on Instagram and can be transcribed directly from the dashboard upload control.
- Larger Whisper models improve accuracy but take more time and memory.
- For Claude Code or other agent workflows, see `CLAUDE.md`.
- For MCP client setup, use `run_mcp_server.sh`.
- The backend now caches loaded Whisper models and reuses existing transcript outputs for the same latest reel when possible.
- AI insights use GroqCloud when `GROQ_API_KEY` is available. If Groq is unavailable, the app falls back to local heuristic insights so transcription still completes.
