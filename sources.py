"""Source resolution: turn any input into a transcript.

Supported inputs:
  - YouTube URLs        → fast subtitle path (youtube-transcript-api), else Whisper
  - Other site URLs     → yt-dlp (Vimeo, Twitch, TikTok, X, SoundCloud, ~1800 sites):
                          downloads subtitles if available, else audio → Whisper
  - Local files         → Whisper directly (no download); video or audio

`resolve()` returns a TranscriptResult. The heavy deps (yt-dlp, faster-whisper)
are imported lazily, so the YouTube-subtitle path works without them installed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Silence a cosmetic HuggingFace cache warning when faster-whisper downloads a
# model on Windows without Developer Mode (symlinks unavailable; harmless).
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


class TranscriptError(Exception):
    """Base class for transcript-acquisition failures."""


class NoSubtitlesError(TranscriptError):
    """No usable subtitles (so we should try Whisper)."""


@dataclass
class TranscriptResult:
    transcript: str           # the flattened transcript text
    title: str | None         # human title (for the heading)
    ref: str                  # URL or file path (shown in the output header)
    source: str               # "subtitles" or "whisper"
    slug_base: str            # basis for the output filename (kept unique-ish)
    context: str | None = None  # metadata block (title/channel/tags/desc) for the prompt


def _format_context(
    *,
    title: str | None = None,
    channel: str | None = None,
    tags: list | None = None,
    description: str | None = None,
) -> str | None:
    """Build a compact metadata block to help the summarizer fix garbled proper
    nouns. Returns None when there's nothing useful. Description and tags are
    capped so this stays small next to the (much larger) transcript."""
    lines: list[str] = []
    if title:
        lines.append(f"Title: {title}")
    if channel:
        lines.append(f"Channel/uploader: {channel}")
    if tags:
        joined = ", ".join(str(t) for t in tags[:30] if t)
        if joined:
            lines.append(f"Tags: {joined}")
    if description:
        desc = re.sub(r"\s+", " ", description).strip()
        if len(desc) > 800:
            desc = desc[:800].rstrip() + "..."
        if desc:
            lines.append(f"Description: {desc}")
    return "\n".join(lines) if lines else None


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #
def resolve(
    source_input: str,
    *,
    languages: list[str],
    use_whisper: bool = True,
    whisper_model: str = "small",
) -> TranscriptResult:
    """Detect the input type and return a TranscriptResult."""
    s = source_input.strip().strip('"').strip("'")

    # 1) Existing local file → transcribe directly.
    local = Path(s).expanduser()
    if local.is_file():
        return _resolve_local_file(local, use_whisper, whisper_model)

    # 2) URL → YouTube fast path, else generic yt-dlp.
    if re.match(r"^https?://", s, re.IGNORECASE):
        if youtube_video_id(s):
            return _resolve_youtube(s, languages, use_whisper, whisper_model)
        return _resolve_generic_url(s, languages, use_whisper, whisper_model)

    raise TranscriptError(
        f"Not a recognizable video URL or an existing file: {s!r}\n"
        "  Pass a video URL (YouTube, Vimeo, Twitch, TikTok, …) or a local file path."
    )


# --------------------------------------------------------------------------- #
# YouTube source (fast subtitle path)
# --------------------------------------------------------------------------- #
def youtube_video_id(url: str) -> str | None:
    """Return the 11-char YouTube video ID, or None if not a YouTube URL."""
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url

    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()

    if host == "youtu.be" or host.endswith(".youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
        return candidate or None

    if host.endswith("youtube.com") or host == "youtube.com":
        query = urllib.parse.parse_qs(parsed.query)
        if "v" in query:
            return query["v"][0]
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live", "v"}:
            return parts[1]
    return None


def _fetch_youtube_oembed(video_id: str) -> dict:
    """Video metadata via YouTube's public oEmbed endpoint (no API key).

    Returns a dict (notably `title` and `author_name`), or {} on failure.
    """
    oembed = (
        "https://www.youtube.com/oembed?url="
        + urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
        + "&format=json"
    )
    try:
        with urllib.request.urlopen(oembed, timeout=10) as resp:
            return json.load(resp)
    except Exception:
        return {}


def _fetch_youtube_subtitles(video_id: str, languages: list[str]) -> str:
    """Fetch YouTube subtitles via youtube-transcript-api. Raises NoSubtitlesError."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    try:
        if hasattr(api, "fetch"):  # youtube-transcript-api >= 1.0
            try:
                fetched = api.fetch(video_id, languages=languages)
            except Exception:
                fetched = next(iter(api.list(video_id))).fetch()
            text = " ".join(snippet.text for snippet in fetched)
        else:  # legacy classmethod API
            try:
                chunks = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
            except Exception:
                chunks = next(iter(YouTubeTranscriptApi.list_transcripts(video_id))).fetch()
            text = " ".join(c["text"] for c in chunks)
    except Exception as exc:
        raise NoSubtitlesError(f"{type(exc).__name__}: {exc}") from exc

    text = text.strip()
    if not text:
        raise NoSubtitlesError("subtitles were empty")
    return text


def _resolve_youtube(url, languages, use_whisper, whisper_model) -> TranscriptResult:
    vid = youtube_video_id(url)
    canonical = f"https://www.youtube.com/watch?v={vid}"
    meta = _fetch_youtube_oembed(vid)
    title = meta.get("title")
    context = _format_context(title=title, channel=meta.get("author_name"))
    slug = f"{_slug(title)}-{vid}" if title else vid

    try:
        text = _fetch_youtube_subtitles(vid, languages)
        return TranscriptResult(text, title, canonical, "subtitles", slug, context)
    except NoSubtitlesError as exc:
        if not use_whisper:
            raise TranscriptError(
                f"No subtitles ({exc}) and Whisper is disabled (--no-whisper)."
            ) from exc
        print(f"  No subtitles found ({exc}). Falling back to Whisper...")
        text = _transcribe_url(canonical, whisper_model)
        return TranscriptResult(text, title, canonical, "whisper", slug, context)


# --------------------------------------------------------------------------- #
# Generic URL source (yt-dlp: ~1800 sites)
# --------------------------------------------------------------------------- #
def _ytdlp():
    try:
        import yt_dlp
    except ImportError as exc:
        raise TranscriptError(
            "yt-dlp is not installed. Run: pip install -r requirements.txt"
        ) from exc
    return yt_dlp


def _ytdlp_info(url: str) -> dict:
    yt_dlp = _ytdlp()
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as exc:
        raise TranscriptError(f"yt-dlp could not read that URL: {exc}") from exc


def _pick_sub_lang(prefs, manual: dict, auto: dict):
    """Choose (lang_key, is_auto), preferring manual subs and our language order."""
    for table, is_auto in ((manual, False), (auto, True)):
        if not table:
            continue
        for want in prefs:
            if want in table:
                return want, is_auto
            hits = [k for k in table if k.split("-")[0] == want]
            if hits:
                return hits[0], is_auto
    # No preferred language matched: take whatever exists, manual first.
    if manual:
        return next(iter(manual)), False
    if auto:
        return next(iter(auto)), True
    return None, False


def _download_and_parse_subs(url: str, lang: str, is_auto: bool) -> str:
    yt_dlp = _ytdlp()
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "writesubtitles": not is_auto,
            "writeautomaticsub": is_auto,
            "subtitleslangs": [lang],
            "subtitlesformat": "vtt/srt/best",
            "outtmpl": str(Path(tmp) / "%(id)s.%(ext)s"),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = list(Path(tmp).glob("*.vtt")) + list(Path(tmp).glob("*.srt"))
        if not files:
            return ""
        return _parse_subtitle_file(files[0])


def _parse_subtitle_file(path: Path) -> str:
    """Flatten a .vtt/.srt subtitle file to plain text, stripping timing/markup
    and collapsing the consecutive duplicates common in auto-captions."""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:")):
            continue
        if "-->" in line:                 # timestamp line
            continue
        if re.fullmatch(r"\d+", line):    # SRT cue index
            continue
        line = re.sub(r"<[^>]+>", "", line)          # inline <c>/<00:00:..> tags
        line = re.sub(r"\s+", " ", line).strip()      # normalize whitespace
        if not line:
            continue
        if out and out[-1] == line:       # auto-caption rollup dedupe
            continue
        out.append(line)
    return " ".join(out).strip()


def _resolve_generic_url(url, languages, use_whisper, whisper_model) -> TranscriptResult:
    info = _ytdlp_info(url)
    title = info.get("title")
    vid = info.get("id") or "video"
    ref = info.get("webpage_url") or url
    slug = f"{_slug(title)}-{vid}" if title else vid
    context = _format_context(
        title=title,
        channel=info.get("uploader") or info.get("channel"),
        tags=info.get("tags"),
        description=info.get("description"),
    )

    lang, is_auto = _pick_sub_lang(
        languages, info.get("subtitles") or {}, info.get("automatic_captions") or {}
    )
    if lang:
        kind = "auto-captions" if is_auto else "subtitles"
        print(f"  Found {kind} ({lang}). Downloading...")
        try:
            text = _download_and_parse_subs(url, lang, is_auto)
            if text:
                return TranscriptResult(text, title, ref, "subtitles", slug, context)
            print("  Subtitle file was empty; falling back to Whisper.")
        except Exception as exc:
            print(f"  Subtitle download failed ({exc}); falling back to Whisper.")

    if not use_whisper:
        raise TranscriptError(
            "No usable subtitles and Whisper is disabled (--no-whisper)."
        )
    print(f"  Transcribing with Whisper (model: {whisper_model})...")
    text = _transcribe_url(url, whisper_model)
    return TranscriptResult(text, title, ref, "whisper", slug, context)


# --------------------------------------------------------------------------- #
# Local file source
# --------------------------------------------------------------------------- #
def _resolve_local_file(path: Path, use_whisper, whisper_model) -> TranscriptResult:
    if not use_whisper:
        raise TranscriptError(
            "Local files need the Whisper transcriber, but it's disabled (--no-whisper)."
        )
    print(f"  Local file - transcribing with Whisper (model: {whisper_model})...")
    text = _transcribe_file(path, whisper_model)
    return TranscriptResult(text, path.stem, str(path.resolve()), "whisper", _slug(path.stem))


# --------------------------------------------------------------------------- #
# Whisper (shared)
# --------------------------------------------------------------------------- #
def _transcribe(audio_path: str, model_size: str) -> str:
    """Transcribe an audio/video file with faster-whisper.

    Tries the GPU first, but falls back to CPU if CUDA isn't usable (e.g. the
    cuBLAS/cuDNN libraries aren't installed) - a common case on Windows.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptError(
            "faster-whisper is not installed. Run: pip install -r requirements.txt"
        ) from exc

    last_err: Exception | None = None
    # int8_float16 is faster and more accurate on GPU; int8 is the right choice on
    # CPU. Pairing each device with its preferred compute type is a free speedup.
    for device, compute_type in (("auto", "int8_float16"), ("cpu", "int8")):
        try:
            model = WhisperModel(model_size, device=device, compute_type=compute_type)
            return _run_whisper(model, audio_path)
        except Exception as exc:  # noqa: BLE001 - GPU errors surface here or in transcribe()
            last_err = exc
            if device != "cpu":
                print(f"    GPU unavailable ({type(exc).__name__}); using CPU...")
    raise TranscriptError(f"Whisper transcription failed: {last_err}")


def _run_whisper(model, audio_path: str) -> str:
    # vad_filter strips silences: faster, and avoids the hallucinated repetitions
    # Whisper tends to produce over long blanks.
    segments, info = model.transcribe(audio_path, vad_filter=True)

    total = getattr(info, "duration", 0) or 0
    parts: list[str] = []
    next_pct = 10
    for seg in segments:  # lazy generator - transcription runs as we iterate
        parts.append(seg.text.strip())
        if total:
            pct = min(100, int(seg.end / total * 100))
            if pct >= next_pct:
                print(f"    transcribing... {pct}%", flush=True)
                next_pct = (pct // 10 + 1) * 10

    text = " ".join(parts).strip()
    if not text:
        raise TranscriptError("Whisper produced an empty transcript")
    return text


def _transcribe_file(path: Path, model_size: str) -> str:
    """Transcribe a local media file directly (faster-whisper decodes via PyAV)."""
    return _transcribe(str(path), model_size)


def _download_audio(url: str, dest_dir: Path) -> Path:
    yt_dlp = _ytdlp()
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


def _transcribe_url(url: str, model_size: str) -> str:
    """Download the best audio for a URL with yt-dlp, then transcribe it."""
    with tempfile.TemporaryDirectory() as tmp:
        audio = _download_audio(url, Path(tmp))
        return _transcribe(str(audio), model_size)


# --------------------------------------------------------------------------- #
def _slug(text: str | None, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "", flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:max_len].strip("-")
