#!/usr/bin/env python3
"""VideoSummarizer - a CLI tool that summarizes a video into a Markdown file.

Works with YouTube, ~1800 other sites supported by yt-dlp (Vimeo, Twitch,
TikTok, X, SoundCloud, …), and local media files.

Usage:
    python summarize.py                      # interactive: asks for the input
    python summarize.py <url-or-file>        # one-shot
    python summarize.py <url> --lang english # force the summary language

The summary is written as a .md file next to where you run the command
(or wherever --output-dir points).

Uses your local Claude (the `claude` CLI / Claude Code) for summarization, so it
runs on your Pro/Max subscription - no API key, no per-token billing. You log the
CLI in once; after that this script calls it automatically.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from prompts import SYSTEM_PROMPT, USER_PROMPT
from sources import TranscriptError, resolve

# Languages to try, in order, when fetching subtitles.
DEFAULT_TRANSCRIPT_LANGS = ["en", "fr", "es", "de", "pt", "it"]

# Max seconds to wait for the claude CLI before giving up (avoids hanging forever
# if the CLI blocks on input or the request stalls).
CLAUDE_TIMEOUT = 600


class SummarizerError(Exception):
    """Raised when the Claude CLI is missing, not logged in, or fails."""


# --------------------------------------------------------------------------- #
# Summarization
# --------------------------------------------------------------------------- #
def _version_key(path: Path) -> tuple:
    """Sort key from a version-numbered folder name like '2.1.165'."""
    parts = re.findall(r"\d+", path.parent.name)
    return tuple(int(p) for p in parts) if parts else (0,)


def find_claude() -> str:
    """Locate the `claude` executable.

    Looks on PATH first (standalone CLI install), then falls back to the copy
    bundled inside the Claude desktop app on Windows, picking the newest version.
    """
    exe = shutil.which("claude")
    if exe:
        return exe

    # Prefer the standalone native install (~/.local/bin) - it's a normal,
    # non-packaged binary, so headless use and login are reliable.
    home = Path.home()
    for standalone in (home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude"):
        if standalone.is_file():
            return str(standalone)

    # Last resort: the copy bundled inside the Claude desktop app (versioned
    # folder). Pick the newest version. Note: as a packaged app, its headless
    # login can be unreliable - the standalone install above is preferred.
    bundled: list[Path] = []
    for root_env in ("APPDATA", "LOCALAPPDATA"):
        root = os.environ.get(root_env)
        if not root:
            continue
        base = Path(root)
        bundled += base.glob("Claude/claude-code/*/claude.exe")
        bundled += base.glob("Packages/Claude_*/LocalCache/Roaming/Claude/claude-code/*/claude.exe")

    existing = [p for p in bundled if p.is_file()]
    if existing:
        return str(max(existing, key=_version_key))

    raise SummarizerError(
        "Couldn't find the 'claude' CLI.\n"
        "  Install Claude Code (https://docs.claude.com/claude-code), or make sure\n"
        "  the Claude desktop app is installed, then run this again."
    )


def build_prompt(
    transcript: str, output_language: str | None, context: str | None = None
) -> str:
    """Assemble the full prompt (system + user + transcript) sent to Claude.

    `context` is an optional video-metadata block (title/channel/tags/description)
    that helps Claude fix proper nouns the auto-transcript garbled phonetically.
    """
    if output_language and output_language.lower() != "auto":
        lang_instruction = f" Write the summary in {output_language}."
    else:
        lang_instruction = ""

    if context:
        video_context = (
            "\nReference metadata for this video (use it to correct phonetically "
            "garbled proper nouns - people, places, teams, organizations - in the "
            "transcript below; do NOT summarize this metadata itself):\n"
            f"<video_metadata>\n{context}\n</video_metadata>\n"
        )
    else:
        video_context = ""

    user_prompt = USER_PROMPT.format(
        output_language_instruction=lang_instruction,
        video_context=video_context,
        transcript=transcript,
    )
    return f"{SYSTEM_PROMPT}\n\n{user_prompt}"


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 characters per token).

    Not exact - it's an order-of-magnitude heads-up before sending a large
    transcript to Claude, not a billing figure (the subscription isn't per-token).
    """
    return max(1, len(text) // 4)


def summarize(full_prompt: str, model: str | None) -> str:
    """Summarize by piping the prepared prompt to your local `claude` CLI.

    Runs on your Claude Pro/Max subscription (via Claude Code) - no API key and no
    per-token billing. The CLI must be logged in once (`claude` then `/login`).
    """
    # The transcript can be huge, so feed the whole prompt via stdin rather than
    # as a command-line argument (which has length limits, especially on Windows).
    exe = find_claude()
    cmd = [exe, "-p", "--output-format", "text", "--max-turns", "1"]
    if model:
        cmd += ["--model", model]

    print("Summarizing with your local Claude (using your subscription)...", flush=True)
    # Only capture stdout (the summary). Let claude's stderr stream straight to the
    # terminal so a long or stalled run isn't a silent black box: any progress,
    # rate-limit, or login message shows up live instead of being swallowed.
    try:
        proc = subprocess.run(
            cmd,
            input=full_prompt,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            timeout=CLAUDE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SummarizerError(
            f"claude CLI timed out after {CLAUDE_TIMEOUT}s. The transcript may be too "
            "long, the CLI may be waiting on a usage limit, or it's stuck. See its "
            "output above."
        ) from exc
    except OSError as exc:
        raise SummarizerError(f"Failed to launch the claude CLI: {exc}") from exc

    output = (proc.stdout or "").strip()
    if proc.returncode != 0 or not output:
        raise SummarizerError(
            f"claude CLI failed (exit {proc.returncode}) or returned no output - see "
            "its output above. If it mentions login, log in once:\n"
            f'    & "{exe}"\n'
            "  then /login, choose your subscription account, finish in the browser, /exit."
        )

    print("done.\n")
    return output


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def _safe_stem(text: str, max_len: int = 80) -> str:
    """Filesystem-safe filename stem (keeps case; strips invalid characters)."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip()
    text = re.sub(r"\s+", "-", text)
    return text[:max_len].strip("-.") or "summary"


def write_markdown(
    summary: str,
    title: str | None,
    ref: str,
    slug_base: str,
    output_dir: Path,
    source: str = "subtitles",
) -> Path:
    heading = title or "Video summary"
    source_label = "subtitles" if source == "subtitles" else "Whisper (auto-transcribed)"
    # Render the source as a link for URLs, inline code for local file paths.
    ref_md = f"[{ref}]({ref})" if re.match(r"^https?://", ref) else f"`{ref}`"

    header = (
        f"# {heading}\n\n"
        f"- **Source:** {ref_md}\n"
        f"- **Transcript:** {source_label}\n"
        f"- **Summarized:** {date.today().isoformat()}\n\n"
        f"---\n\n"
    )

    out_path = output_dir / f"{_safe_stem(slug_base)}.md"
    out_path.write_text(header + summary + "\n", encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize a video (YouTube, yt-dlp sites, or a local file) "
        "into a Markdown file."
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="Video URL (YouTube, Vimeo, Twitch, TikTok, …) or a local file path. "
        "If omitted, you'll be prompted for it.",
    )
    parser.add_argument(
        "--lang",
        default="auto",
        help='Output language for the summary (e.g. "english", "french"). '
        'Default "auto" keeps the video\'s own language.',
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        type=Path,
        help="Directory to write the .md file into (default: current directory).",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the token-estimate confirmation and summarize straight away.",
    )
    parser.add_argument(
        "--no-whisper",
        action="store_true",
        help="Disable the Whisper fallback for videos without subtitles.",
    )
    parser.add_argument(
        "--whisper-model",
        default="small",
        help='faster-whisper model size for the fallback: tiny | base | small | '
        "medium | large-v3 (default: small). Bigger = more accurate but slower.",
    )
    parser.add_argument(
        "--claude-model",
        default=None,
        help="Model for the claude CLI (e.g. opus, sonnet, haiku). "
        "Default: whatever your Claude Code is configured to use.",
    )
    return parser.parse_args(argv)


def _confirm(question: str) -> bool:
    """Ask a [Y/n] question (Enter = yes, apt-style). Auto-yes when stdin isn't a
    terminal, so piped/automated runs never hang waiting for input."""
    if not sys.stdin.isatty():
        return True
    try:
        reply = input(f"{question} [Y/n] ").strip().lower()
    except EOFError:
        return True
    return reply in ("", "y", "yes")


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    source_input = args.source or input("Enter a video URL or file path: ").strip()
    if not source_input:
        print("No input provided.", file=sys.stderr)
        return 1

    # Create the output directory up front so an invalid --output-dir fails fast,
    # before spending minutes on transcription and summarization.
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Error: cannot use output directory {args.output_dir}: {exc}", file=sys.stderr)
        return 1

    print("Resolving source and fetching transcript...")
    try:
        result = resolve(
            source_input,
            languages=DEFAULT_TRANSCRIPT_LANGS,
            use_whisper=not args.no_whisper,
            whisper_model=args.whisper_model,
        )
    except TranscriptError as exc:
        print("Error: could not obtain a transcript.", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return 2

    if result.title:
        print(f"Title: {result.title}")
    via = "subtitles" if result.source == "subtitles" else f"Whisper ({args.whisper_model})"
    print(f"Transcript ready via {via} (~{len(result.transcript.split())} words).\n")

    prompt = build_prompt(result.transcript, args.lang, result.context)
    est_tokens = estimate_tokens(prompt)
    if not args.yes and not _confirm(
        f"This summary will send ~{est_tokens:,} input tokens to Claude. Continue?"
    ):
        print("Aborted.")
        return 0

    try:
        summary = summarize(prompt, args.claude_model)
    except SummarizerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3

    out_path = write_markdown(
        summary, result.title, result.ref, result.slug_base, args.output_dir, result.source
    )
    print(f"Summary written to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
