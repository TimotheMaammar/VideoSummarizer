# VideoSummarizer - a CLI video summarizer

A small command-line tool that takes a video (from a URL or a local file),
fetches its transcript, and writes a clean, structured **Markdown summary** next
to where you run it.

## Supported sources

- **YouTube** - fast subtitle path (no download).
- **~1800 other sites** via [yt-dlp](https://github.com/yt-dlp/yt-dlp) - Vimeo,
  Twitch, TikTok, X, Dailymotion, SoundCloud, TED, etc. Uses the platform's
  subtitles when available, otherwise transcribes the audio.
- **Local files** - any video/audio file on disk (`.mp4`, `.mkv`, `.mp3`, `.wav`,
  …); transcribed directly.

## How it works

```
input  →  [1] resolve source  →  [2] get transcript  →  [3] summarize (local Claude)  →  summary.md
```

1. **Transcript** - a two-tier strategy per source:
   - **Subtitles** if the platform has them (YouTube via
     [`youtube-transcript-api`](https://pypi.org/project/youtube-transcript-api/);
     other sites via yt-dlp). Fast and free.
   - **Whisper fallback** ([`faster-whisper`](https://github.com/SYSTRAN/faster-whisper))
     otherwise - downloads the audio (or reads your local file) and transcribes
     it. Tries the GPU, falls back to CPU automatically. No system `ffmpeg`
     needed (decoding uses the bundled PyAV).
2. **Summary** - the full transcript is piped to your **local `claude` CLI**
   (Claude Code) in headless mode (`claude -p`). This runs on your **Claude
   Pro/Max subscription** - no API key, no per-token cost. The video's metadata
   (title, channel, and - on non-YouTube sites - tags and description) is passed
   alongside the transcript so Claude can fix proper nouns that auto-captions
   garble phonetically (e.g. a name spelled correctly in the title but mangled in
   the transcript).

## Setup

```bash
pip install -r requirements.txt
```

The script calls your local **Claude Code CLI**. Install the standalone CLI (a
normal, non-packaged binary - the desktop app's bundled copy is unreliable for
headless use):

```powershell
# Windows PowerShell
irm https://claude.ai/install.ps1 | iex
```

Then **reopen PowerShell** (so PATH refreshes) and log in once:

```powershell
claude --version   # confirm it's installed
claude             # then follow the browser prompt, choose your Pro/Max account
```

Login is stored under `~/.claude` and persists. After that, `python summarize.py`
works automatically - no per-run steps. The script finds `claude` on your PATH
(falling back to `~/.local/bin` or the desktop-app copy if needed).

## Usage

Interactive (prompts for the input):

```bash
python summarize.py
```

```
Enter a video URL or file path: https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

One-shot - pass a URL or a local file:

```bash
python summarize.py https://youtu.be/dQw4w9WgXcQ
python summarize.py https://vimeo.com/123456789
python summarize.py "C:\videos\lecture.mp4"
```

Options:

```bash
python summarize.py <input> --lang english          # force the summary language
python summarize.py <input> --output-dir summaries   # write the .md somewhere else
python summarize.py <input> --claude-model sonnet    # pick the model (opus/sonnet/haiku)
python summarize.py <input> --whisper-model medium   # bigger = more accurate, slower
python summarize.py <input> --no-whisper             # subtitles only, no transcription
python summarize.py <input> -y                       # skip the confirmation prompt
```

Before sending the transcript to Claude, the tool prints an estimate of how many
input tokens the request will use and asks for confirmation (Enter = yes). It's a
heads-up on the order of magnitude, not a bill - the subscription isn't per-token.
Pass `-y`/`--yes` to skip the prompt; it's also skipped automatically when input
isn't a terminal (pipes, cron), so automated runs never hang.

Whisper model sizes: `tiny` | `base` | `small` (default) | `medium` | `large-v3`.

## Output structure

Each summary contains: **Summary**, **Key Points**, **Details** (with subheadings),
**Notable Quotes / Data**, and **Takeaways**. Edit `prompts.py` to change the
format or tone - that's the main lever on quality.

## Project structure

```
VideoSummarizer/
├── summarize.py      # CLI + orchestration + the Claude call (entry point)
├── sources.py        # transcript acquisition - the core (dispatch + Whisper)
├── prompts.py        # prompt templates (the quality knobs)
├── requirements.txt  # dependencies
├── .gitignore
└── README.md
```

The guiding principle is **separation of concerns**: `summarize.py` doesn't know
*how* a transcript is obtained, and `sources.py` doesn't know *how* it's
summarized. The summarization backend can be swapped without touching the source
layer, and vice-versa.

Data flow:

```
URL / file ──argparse──▶ resolve() ──▶ TranscriptResult
                                              │
                  SYSTEM_PROMPT + USER_PROMPT(transcript)
                                              │
                              claude -p (stdin) ──▶ Markdown ──▶ file.md
```

## Stack

| Library | Role | Why |
|---|---|---|
| [`youtube-transcript-api`](https://pypi.org/project/youtube-transcript-api/) | YouTube subtitles → clean text | free, instant, no download |
| [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) | metadata + subtitles + audio for ~1800 sites | the de-facto standard, actively maintained |
| [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) | local audio→text transcription | ~4× faster than `openai-whisper`; bundles ffmpeg (PyAV) |
| `claude` CLI (Claude Code) | the LLM that summarizes | runs on your subscription - no per-token cost |

Everything else is the Python **standard library**: `argparse`, `subprocess`,
`urllib`, `pathlib`, `re`, `dataclasses`, `tempfile`. Requires **Python 3.9+**
(`str | None` unions are kept lazy via `from __future__ import annotations`).

