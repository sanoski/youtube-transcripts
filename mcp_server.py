"""
YouTube Transcript MCP Server

Exposes a single tool — get_youtube_transcript(url) — that Claude Code
calls directly. No copy-pasting, no browser, no middleman.

Setup:
  pip install mcp yt-dlp
  Then register this file in Claude Code settings (see README.md).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import glob
from pathlib import Path
from datetime import datetime

# Use the same Python interpreter that's running this script so we don't
# depend on yt-dlp being in PATH as a standalone command (it often isn't on Windows).
YT_DLP_CMD = [sys.executable, "-m", "yt_dlp"]

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("youtube-transcripts")

# Optional: save transcripts to disk so you can reference them later.
# Set to None to disable saving.
SAVE_DIR = Path(__file__).parent / "transcripts"
SAVE_DIR.mkdir(exist_ok=True)


def _extract_video_id(url: str) -> str | None:
    match = re.search(r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})", url)
    return match.group(1) if match else None


def _clean_vtt(content: str) -> str:
    """Strip VTT/SRT timestamps and collapse duplicate lines from auto-captions."""
    # Remove WEBVTT header
    content = re.sub(r"WEBVTT.*?\n", "", content)
    # Remove timestamp lines
    content = re.sub(
        r"\d{1,2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[.,]\d{3}.*\n",
        "", content,
    )
    # Remove SRT sequence numbers
    content = re.sub(r"^\d+\n", "", content, flags=re.MULTILINE)
    # Remove HTML tags (<c>, <i>, etc.)
    content = re.sub(r"<[^>]+>", "", content)
    # Remove VTT cue identifiers
    content = re.sub(r"^[a-f0-9-]+\n", "", content, flags=re.MULTILINE)

    lines = [l.strip() for l in content.splitlines() if l.strip()]

    # Collapse consecutive duplicate lines (auto-captions repeat heavily)
    deduped: list[str] = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)

    # Join into flowing paragraphs (~80 words each)
    text = " ".join(deduped)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    paragraphs, current, count = [], [], 0
    for s in sentences:
        current.append(s)
        count += len(s.split())
        if count >= 80:
            paragraphs.append(" ".join(current))
            current, count = [], 0
    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


def _get_metadata(url: str) -> dict:
    """Fetch video metadata via yt-dlp --dump-json. Returns {} on failure."""
    cmd = YT_DLP_CMD + ["--skip-download", "--no-playlist", "--dump-json", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip().splitlines()[0])
            upload_date = data.get("upload_date", "")
            if len(upload_date) == 8:
                upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
            duration_s = data.get("duration", 0) or 0
            duration = f"{int(duration_s)//3600}:{(int(duration_s)%3600)//60:02d}:{int(duration_s)%60:02d}"
            views = data.get("view_count")
            likes = data.get("like_count")
            return {
                "title": data.get("title", ""),
                "uploader": data.get("uploader") or data.get("channel", ""),
                "channel_url": data.get("channel_url") or data.get("uploader_url", ""),
                "upload_date": upload_date,
                "duration": duration,
                "view_count": f"{views:,}" if views is not None else "",
                "like_count": f"{likes:,}" if likes is not None else "",
            }
    except Exception:
        pass
    return {}


def _run_yt_dlp(url: str, tmpdir: str, auto: bool) -> list[str]:
    cmd = YT_DLP_CMD + [
        "--skip-download",
        "--no-playlist",
        "--sub-lang", "en",
        "--convert-subs", "vtt",
        "-o", os.path.join(tmpdir, "sub"),
        "--write-sub",
    ]
    if auto:
        cmd.append("--write-auto-sub")
    cmd.append(url)
    subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    return glob.glob(os.path.join(tmpdir, "*.vtt"))


@mcp.tool()
def get_youtube_transcript(url: str) -> str:
    """
    Extract the transcript of a YouTube video and return it as clean text.

    Prefers manually-authored captions; falls back to auto-generated ones.
    Works on any public video that has English captions.

    Args:
        url: A YouTube video URL in any standard format
             (watch?v=, youtu.be/, shorts/, embed/).
    """
    if not re.search(r"(youtube\.com|youtu\.be)", url):
        return "Error: That doesn't look like a YouTube URL."

    video_id = _extract_video_id(url)
    if not video_id:
        return "Error: Could not parse a video ID from that URL."

    meta = _get_metadata(url)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Try manual captions first, then auto-generated
        vtt_files = _run_yt_dlp(url, tmpdir, auto=False)
        caption_type = "manual"

        if not vtt_files:
            vtt_files = _run_yt_dlp(url, tmpdir, auto=True)
            caption_type = "auto-generated"

        if not vtt_files:
            return (
                "No English captions found (manual or auto-generated). "
                "The video may not have subtitles, may be private, or may be region-locked."
            )

        with open(vtt_files[0], "r", encoding="utf-8") as f:
            raw = f.read()

    transcript = _clean_vtt(raw)
    if not transcript.strip():
        return "Captions were found but appear to be empty after cleaning."

    word_count = len(transcript.split())

    meta_lines = [
        f"Title:       {meta.get('title', video_id)}",
        f"Channel:     {meta.get('uploader', '')}",
        f"Channel URL: {meta.get('channel_url', '')}",
        f"Published:   {meta.get('upload_date', '')}",
        f"Duration:    {meta.get('duration', '')}",
        f"Views:       {meta.get('view_count', '')}",
        f"Likes:       {meta.get('like_count', '')}",
        f"URL:         {url}",
        f"Captions:    {caption_type}",
    ]
    meta_block = "\n".join(line for line in meta_lines if line.split(":", 1)[1].strip())

    # Save to disk so you can reference it later
    if SAVE_DIR:
        def _safe(s: str) -> str:
            s = re.sub(r'[<>:"/\\|?*]', "", s).strip()
            return re.sub(r"\s+", "_", s)
        date_part = meta.get("upload_date", "").replace("-", "")[:8] or datetime.now().strftime("%Y%m%d")
        channel_part = _safe(meta.get("uploader", ""))[:40]
        title_part = _safe(meta.get("title", video_id))[:60]
        parts = [p for p in [date_part, channel_part, title_part] if p]
        filename = "-".join(parts) + ".txt"
        filepath = SAVE_DIR / filename
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(meta_block + "\n")
            f.write(f"Extracted:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("-" * 60 + "\n\n")
            f.write(transcript)

    header = f"[Transcript — {caption_type} captions — {word_count:,} words]\n\n{meta_block}\n\n" + "-" * 60 + "\n\n"
    return header + transcript


@mcp.tool()
def list_saved_transcripts() -> str:
    """
    List previously extracted transcripts saved to disk.
    Returns filenames, video IDs, and timestamps.
    """
    if not SAVE_DIR or not SAVE_DIR.exists():
        return "No transcripts directory found."

    files = sorted(SAVE_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return "No saved transcripts yet."

    lines = []
    for f in files[:20]:
        stat = f.stat()
        modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        size_kb = round(stat.st_size / 1024, 1)
        url = ""
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("URL: "):
                        url = line[5:].strip()
                        break
        except Exception:
            pass
        lines.append(f"{modified}  {size_kb:6.1f} KB  {url or f.name}")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
