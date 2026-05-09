import json
import os
import re
import subprocess
import sys
import tempfile
import glob
import hashlib
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_from_directory, abort

app = Flask(__name__)

YT_DLP_CMD = [sys.executable, "-m", "yt_dlp"]
COOKIES_FILE = Path("/opt/youtube-transcripts/cookies.txt")


def _yt_dlp_base() -> list:
    cmd = YT_DLP_CMD[:]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    return cmd

# Transcripts are saved here so you can browse them later or download as files.
TRANSCRIPTS_DIR = Path(__file__).parent / "transcripts"
TRANSCRIPTS_DIR.mkdir(exist_ok=True)


def extract_video_id(url: str) -> str | None:
    """Pull the 11-char video ID out of any YouTube URL format."""
    patterns = [
        r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def clean_vtt(content: str) -> str:
    """Strip VTT/SRT timestamps and metadata, collapse duplicate lines."""
    # Remove WEBVTT header and NOTE blocks
    content = re.sub(r"WEBVTT.*?\n", "", content)
    content = re.sub(r"NOTE\n.*?\n", "", content)

    # Remove timestamp lines like 00:00:01.000 --> 00:00:04.000 or HH:MM:SS,mmm --> ...
    content = re.sub(r"\d{1,2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[.,]\d{3}.*\n", "", content)

    # Remove sequence numbers (SRT format)
    content = re.sub(r"^\d+\n", "", content, flags=re.MULTILINE)

    # Remove HTML tags common in auto-captions (<c>, <i>, etc.)
    content = re.sub(r"<[^>]+>", "", content)

    # Remove VTT cue identifiers (lines that are just alphanumeric IDs before a timestamp)
    content = re.sub(r"^[a-f0-9-]+\n", "", content, flags=re.MULTILINE)

    # Split into lines, strip whitespace, remove blank lines
    lines = [line.strip() for line in content.splitlines()]
    lines = [line for line in lines if line]

    # Collapse consecutive duplicate lines (auto-captions repeat a lot)
    deduped = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)

    # Join into paragraphs: add a newline when there's a clear sentence break
    text = " ".join(deduped)

    # Re-break into readable paragraphs at sentence boundaries (~80 words each)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    paragraphs = []
    current = []
    word_count = 0
    for sentence in sentences:
        words = len(sentence.split())
        current.append(sentence)
        word_count += words
        if word_count >= 80:
            paragraphs.append(" ".join(current))
            current = []
            word_count = 0
    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


def get_metadata(url: str) -> dict:
    """Fetch video metadata via yt-dlp --dump-json. Returns {} on failure."""
    try:
        result = subprocess.run(
            _yt_dlp_base() + ["--skip-download", "--no-playlist", "--dump-json", url],
            capture_output=True, text=True, timeout=30, check=False,
        )
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


def fetch_transcript(url: str) -> dict:
    """
    Use yt-dlp to download subtitles into a temp dir, then clean and return the text.
    Prefers manual subtitles over auto-generated ones.
    """
    video_id = extract_video_id(url)
    if not video_id:
        return {"error": "Could not parse a YouTube video ID from that URL."}

    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = os.path.join(tmpdir, "sub")

        # Try manual subtitles first, fall back to auto-generated
        for attempt in ("manual", "auto"):
            cmd = _yt_dlp_base() + [
                "--skip-download",
                "--no-playlist",
                "--sub-lang", "en",
                "--convert-subs", "vtt",
                "-o", base_path,
            ]

            if attempt == "manual":
                cmd.append("--write-sub")
            else:
                cmd.extend(["--write-sub", "--write-auto-sub"])

            cmd.append(url)

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                return {"error": "yt-dlp timed out after 60 seconds. The video may be too long or unreachable."}
            except FileNotFoundError:
                return {"error": "yt-dlp is not installed or not in PATH. Run: pip install yt-dlp"}

            # yt-dlp names the file like sub.en.vtt or sub.en-US.vtt
            vtt_files = glob.glob(os.path.join(tmpdir, "*.vtt"))

            if vtt_files:
                with open(vtt_files[0], "r", encoding="utf-8") as f:
                    raw = f.read()
                caption_type = "manual" if attempt == "manual" else "auto-generated"
                transcript_text = clean_vtt(raw)

                if not transcript_text.strip():
                    return {"error": "Transcript was found but appears to be empty after cleaning."}

                meta = get_metadata(url)
                title = meta.get("title") or extract_title_from_output(result.stdout) or video_id

                # Save to disk
                def _safe(s: str) -> str:
                    s = re.sub(r'[<>:"/\\|?*]', "", s).strip()
                    return re.sub(r"\s+", "_", s)
                date_part = meta.get("upload_date", "").replace("-", "")[:8] or datetime.now().strftime("%Y%m%d")
                channel_part = _safe(meta.get("uploader", ""))[:40]
                title_part = _safe(title)[:60]
                parts = [p for p in [date_part, channel_part, title_part] if p]
                filename = "-".join(parts) + ".txt"
                filepath = TRANSCRIPTS_DIR / filename

                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(f"Title:       {title}\n")
                    f.write(f"Channel:     {meta.get('uploader', '')}\n")
                    f.write(f"Channel URL: {meta.get('channel_url', '')}\n")
                    f.write(f"Published:   {meta.get('upload_date', '')}\n")
                    f.write(f"Duration:    {meta.get('duration', '')}\n")
                    f.write(f"Views:       {meta.get('view_count', '')}\n")
                    f.write(f"Likes:       {meta.get('like_count', '')}\n")
                    f.write(f"URL:         {url}\n")
                    f.write(f"Captions:    {caption_type}\n")
                    f.write(f"Extracted:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write("-" * 60 + "\n\n")
                    f.write(transcript_text)

                return {
                    "transcript": transcript_text,
                    "title": title,
                    "uploader": meta.get("uploader", ""),
                    "channel_url": meta.get("channel_url", ""),
                    "upload_date": meta.get("upload_date", ""),
                    "duration": meta.get("duration", ""),
                    "view_count": meta.get("view_count", ""),
                    "like_count": meta.get("like_count", ""),
                    "caption_type": caption_type,
                    "filename": filename,
                    "video_id": video_id,
                }

        # Neither manual nor auto subtitles found
        stderr = result.stderr if result else ""
        if "This video is unavailable" in stderr:
            return {"error": "Video is unavailable (private, deleted, or region-locked)."}
        if "Sign in" in stderr or "members-only" in stderr.lower():
            return {"error": "This video requires sign-in or is members-only."}
        return {"error": "No English subtitles found (manual or auto-generated). The video may not have captions."}


def extract_title_from_output(stdout: str) -> str | None:
    """Parse the video title out of yt-dlp's stdout."""
    match = re.search(r"\[info\] ([^:]+): Downloading", stdout)
    if match:
        return match.group(1).strip()
    # Fallback: yt-dlp sometimes prints the title differently
    match = re.search(r"Destination: .+?_(.+?)\.(en|en-US)\.vtt", stdout)
    if match:
        return match.group(1).replace("_", " ").strip()
    return None


def get_recent_transcripts(limit: int = 10) -> list[dict]:
    """Read the transcripts directory and return metadata for recent files."""
    files = sorted(TRANSCRIPTS_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for f in files[:limit]:
        stat = f.stat()
        size_kb = round(stat.st_size / 1024, 1)
        # Read just the header lines for title/url
        title, url = f.name, ""
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("Title: "):
                        title = line[7:].strip()
                    elif line.startswith("URL: "):
                        url = line[5:].strip()
                    elif line.startswith("---"):
                        break
        except Exception:
            pass
        result.append({
            "filename": f.name,
            "title": title,
            "url": url,
            "size_kb": size_kb,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return result


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/extract", methods=["POST"])
def api_extract():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "No URL provided."}), 400

    # Basic sanity check — must look like a YouTube URL
    if not re.search(r"(youtube\.com|youtu\.be)", url):
        return jsonify({"error": "That doesn't look like a YouTube URL."}), 400

    result = fetch_transcript(url)

    if "error" in result:
        return jsonify(result), 422

    return jsonify(result)


@app.route("/api/recent")
def api_recent():
    return jsonify(get_recent_transcripts())


@app.route("/transcripts/<path:filename>")
def download_transcript(filename):
    # Prevent path traversal
    safe = Path(filename).name
    if safe != filename:
        abort(400)
    return send_from_directory(TRANSCRIPTS_DIR, safe, as_attachment=True)


if __name__ == "__main__":
    # Bind to all interfaces so it's reachable via Tailscale IP.
    # Port 5000 is the Flask default; nginx will proxy to it.
    app.run(host="0.0.0.0", port=5000, debug=False)
