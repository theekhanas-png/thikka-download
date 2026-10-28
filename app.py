"""
Thikka Download — single-file build.

TikTok & YouTube downloader: FastAPI + yt-dlp, with the whole frontend
(HTML/CSS/JS) embedded so the app is exactly two files:

    app.py            <- this file
    requirements.txt

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, quote

import imageio_ffmpeg
import yt_dlp
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(os.environ.get("THIKKA_WORKDIR", "/tmp/thikka"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

ALLOWED_HOSTS = (
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
    "tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
)

THUMB_HOSTS = (
    "ytimg.com", "i.ytimg.com", "img.youtube.com", "ggpht.com",
    "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com",
    "ibyteimg.com", "byteimg.com", "ttwstatic.com", "tiktok.com",
)

app = FastAPI(title="Thikka Download", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------------------- helpers

def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def clean_url(raw: str) -> str:
    url = (raw or "").strip()
    if not url:
        raise ValueError("Paste a TikTok or YouTube link first.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    host = host_of(url)
    if not host or not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise ValueError("Only YouTube and TikTok links are supported right now.")
    return url


def platform_of(url: str) -> str:
    h = host_of(url)
    return "youtube" if ("youtu" in h) else "tiktok"


def human_size(n) -> str:
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_dur(sec) -> str:
    try:
        sec = int(sec or 0)
    except Exception:
        return ""
    if sec <= 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def num(n) -> str:
    try:
        n = int(n or 0)
    except Exception:
        return ""
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def safe_name(text: str, fallback: str = "thikka-video") -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", text or "").strip()
    text = re.sub(r"\s{2,}", " ", text)
    return (text[:80].strip() or fallback)


YDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "noprogress": True,
    "socket_timeout": 25,
    "retries": 3,
    "fragment_retries": 5,
    "ffmpeg_location": FFMPEG,
    "http_headers": {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
}

# Optional: cookies for datacenter IPs that YouTube flags with a bot check.
# Mount a Netscape-format cookies.txt and set THIKKA_COOKIES_FILE to its path.
_COOKIES = os.environ.get("THIKKA_COOKIES_FILE", "").strip()
if _COOKIES and Path(_COOKIES).is_file():
    YDL_BASE["cookiefile"] = _COOKIES


# ----------------------------------------------------------------------------- formats

def tier_name(short_side: int) -> str:
    if short_side >= 2000:
        return "4K"
    if short_side >= 1400:
        return "2K"
    if short_side >= 1000:
        return "Full HD"
    if short_side >= 700:
        return "HD"
    if short_side >= 300:
        return "SD"
    return "Low"


WATERMARK_IDS = {"download", "watermark", "watermarked", "wm", "download_addr"}
TIKTOK_API_HOSTS = (
    "api22-normal-c-useast2a.tiktokv.com",
    "api16-normal-c-useast1a.tiktokv.com",
)


def is_watermarked(f: dict) -> bool:
    """True when a stream is the watermarked re-upload (TikTok 'download' addr)."""
    note = (f.get("format_note") or "").lower()
    desc = (f.get("format") or "").lower()
    fid = (f.get("format_id") or "").lower()
    url = (f.get("url") or "").lower()
    if "watermark" in note or "watermark" in desc:
        return True
    if fid in WATERMARK_IDS or fid.startswith("download") or "watermark" in fid:
        return True
    if "watermark=1" in url or "watermark_yes" in url or "/watermarked/" in url:
        return True
    return False


def clean_video_formats(info: dict) -> list[dict]:
    """Video streams that carry no platform watermark."""
    out = []
    for f in info.get("formats") or []:
        if f.get("vcodec") in (None, "none"):
            continue
        if (f.get("height") or 0) < 100:
            continue
        if is_watermarked(f):
            continue
        out.append(f)
    return out


def _extract_raw(url: str, extra_opts: dict | None = None) -> dict:
    opts = dict(YDL_BASE)
    opts["skip_download"] = True
    if extra_opts:
        opts.update(extra_opts)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]
    return info


def extract_clean_info(url: str) -> tuple[dict, bool]:
    """Extract metadata and guarantee the best available watermark-free stream.

    YouTube never ships a burned-in platform watermark; TikTok sometimes exposes
    the watermarked re-upload only. When that happens we re-query TikTok's
    mobile API, which returns the clean `play_addr` stream.
    """
    info = _extract_raw(url)
    clean = clean_video_formats(info)
    plat = platform_of(url)

    if plat == "tiktok" and not clean:
        for host in TIKTOK_API_HOSTS:
            try:
                alt = _extract_raw(url, {"extractor_args": {"tiktok": {"api_hostname": [host]}}})
                alt_clean = clean_video_formats(alt)
                if alt_clean:
                    info, clean = alt, alt_clean
                    break
            except Exception:  # noqa: BLE001
                continue

    watermark_free = bool(clean) or plat == "youtube"
    return info, watermark_free


def build_qualities(info: dict) -> list[dict]:
    """Turn yt-dlp formats into a tidy list the UI can render as chips.

    Resolution is bucketed by the SHORT side so vertical TikTok clips
    (1080x1920) are labelled 1080p instead of 1920p.
    """
    formats = info.get("formats") or []
    buckets: dict[int, dict] = {}
    audio_size = 0

    for f in formats:
        if f.get("vcodec") in (None, "none"):
            if f.get("acodec") not in (None, "none"):
                audio_size = max(audio_size, f.get("filesize") or f.get("filesize_approx") or 0)
            continue
        if is_watermarked(f):
            continue
        w = f.get("width") or 0
        h = f.get("height") or 0
        if not h:
            continue
        portrait = h > w > 0
        key = min(w, h) if portrait else h
        size = f.get("filesize") or f.get("filesize_approx") or 0
        cur = buckets.get(key)
        # keep the biggest (usually highest bitrate) stream for each resolution
        if cur is None or size > cur["size"]:
            buckets[key] = {
                "size": size,
                "fps": f.get("fps") or 0,
                "ext": f.get("ext") or "mp4",
                "w": w, "h": h, "portrait": portrait,
            }

    out: list[dict] = []
    for key in sorted(buckets, reverse=True)[:6]:
        meta = buckets[key]
        tier = tier_name(key)
        notes = [tier]
        if meta["fps"] >= 50:
            notes.append(f"{int(meta['fps'])}fps")
        if meta["portrait"]:
            notes.append(f"{meta['w']}×{meta['h']}")
        est = (meta["size"] + audio_size) if meta["size"] else 0
        out.append({
            "id": f"v{key}",
            "type": "video",
            "label": f"{key}p",
            "sub": " · ".join(notes),
            "size": human_size(est),
            "bytes": est,
            "fps": meta["fps"],
        })

    # Fallback for platforms that only expose a single muxed stream
    if not out:
        h = info.get("height") or 0
        w = info.get("width") or 0
        key = min(w, h) if (w and h and h > w) else h
        out.append({
            "id": "vbest", "type": "video",
            "label": f"{key}p" if key else "Best",
            "sub": tier_name(key) if key else "Best available",
            "size": "", "bytes": 0, "fps": 0,
        })

    out[0]["sub"] = (out[0]["sub"] + " · Best").strip(" ·")
    out[0]["best"] = True

    out.append({"id": "amp3", "type": "audio", "label": "MP3", "sub": "Audio · 192kbps", "size": "", "bytes": 0, "fps": 0})
    out.append({"id": "am4a", "type": "audio", "label": "M4A", "sub": "Audio · original", "size": "", "bytes": 0, "fps": 0})
    return out


def short_side(f: dict) -> int:
    """Resolution bucket of a stream: min(w, h) for vertical clips, else height."""
    w = f.get("width") or 0
    h = f.get("height") or 0
    if not h:
        return 0
    return min(w, h) if (w and h > w) else h


def choose_format_string(info: dict, quality: str) -> str:
    """Build an exact yt-dlp selector.

    Portrait clips (TikTok, YouTube Shorts) have height 1920 for a 1080p
    vertical video, so ${height<=1080} would wrongly skip it. We bucket by the
    short side and then pin the concrete format ids instead.
    """
    q = (quality or "vbest").lower()
    formats = info.get("formats") or []

    videos = []
    audios = []
    for f in formats:
        fid = f.get("format_id")
        if not fid:
            continue
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        if vcodec in (None, "none"):
            if acodec not in (None, "none"):
                audios.append(f)
            continue
        if (f.get("height") or 0) < 100:
            continue
        if is_watermarked(f):
            continue
        videos.append(f)

    if not videos:
        # No clean stream at all — fall back to the best available stream.
        return "bv*+ba/b"

    target = None
    if q.startswith("v") and q[1:].isdigit():
        target = int(q[1:])

    pool = videos
    if target:
        pool = [f for f in videos if short_side(f) <= target] or videos
        best_key = max(short_side(f) for f in pool)
        pool = [f for f in pool if short_side(f) == best_key]

    def score(f):
        return ((f.get("filesize") or f.get("filesize_approx") or 0),
                f.get("fps") or 0, f.get("tbr") or 0)

    video = max(pool, key=score)

    if video.get("acodec") not in (None, "none"):
        return video["format_id"]

    if audios:
        audio = max(audios, key=lambda f: (f.get("abr") or 0,
                                           f.get("filesize") or f.get("filesize_approx") or 0))
        return f"{video['format_id']}+{audio['format_id']}"

    return f"{video['format_id']}+bestaudio/{video['format_id']}"


def ydl_options_for(quality: str) -> dict:
    """Container / post-processing options for a quality choice."""
    opts = dict(YDL_BASE)
    if (quality or "best").startswith("v"):
        opts["format"] = "best"  # replaced later by choose_format_string()
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    else:
        opts["format"] = "bestaudio/best"
        if quality == "amp3":
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
    return opts


# ----------------------------------------------------------------------------- jobs

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 60 * 45  # seconds


def new_job(url: str, quality: str, title: str, ext_hint: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "url": url,
            "quality": quality,
            "title": title,
            "status": "queued",
            "progress": 0.0,
            "stage": "queued",
            "speed": None,
            "eta": None,
            "error": None,
            "file": None,
            "filename": None,
            "bytes": 0,
            "watermark_free": True,
            "created": time.time(),
        }
    return job_id


def run_job(job_id: str) -> None:
    job = JOBS.get(job_id)
    if not job:
        return
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job["status"] = "downloading"

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            job["stage"] = "downloading"
            job["progress"] = round(min(99.0, done / total * 100), 1) if total else 0.0
            job["speed"] = d.get("speed")
            job["eta"] = d.get("eta")
        elif d.get("status") == "finished":
            job["progress"] = 99.0
            job["stage"] = "processing"

    try:
        # Phase 1 — probe once so we know the title and whether a clean
        # (watermark-free) stream exists for this link.
        job["stage"] = "probing"
        info, watermark_free = extract_clean_info(job["url"])
        job["watermark_free"] = watermark_free
        if not job["title"] or job["title"] == "thikka-video":
            job["title"] = safe_name(info.get("title") or "thikka-video")

        opts = ydl_options_for(job["quality"])
        opts["outtmpl"] = str(job_dir / "media.%(ext)s")
        opts["progress_hooks"] = [hook]

        if job["quality"].startswith("v"):
            # Pin the exact streams from the SAME format table we probed, so the
            # ids are always valid (TikTok regenerates ids on every request).
            opts["format"] = choose_format_string(info, job["quality"])

        # Phase 2 — download using the probed result (no second extraction)
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.process_ie_result(info, download=True)

        files = [p for p in job_dir.iterdir() if p.is_file() and p.stat().st_size > 0]
        media = [p for p in files if p.suffix.lower() not in (".part", ".ytdl")]
        if not media:
            raise RuntimeError("Download finished but no file was produced.")
        media.sort(key=lambda p: -p.stat().st_size)
        chosen = media[0]

        ext = chosen.suffix.lstrip(".")
        job["file"] = str(chosen)
        job["bytes"] = chosen.stat().st_size
        job["filename"] = f"{safe_name(job['title'])}.{ext}"
        job["progress"] = 100.0
        job["speed"] = None
        job["eta"] = None
        job["stage"] = "done"
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        msg = re.sub(r"\x1b\[[0-9;]*m", "", msg).replace("ERROR:", "").strip()
        if len(msg) > 260:
            msg = msg[:260] + "…"
        if "Sign in to confirm" in msg or "bot" in msg.lower():
            msg = "YouTube blocked this request (bot check). Try another link or wait a moment."
        job["status"] = "error"
        job["stage"] = "error"
        job["error"] = msg or "Something went wrong."
        shutil.rmtree(job_dir, ignore_errors=True)


def sweeper() -> None:
    while True:
        time.sleep(120)
        now = time.time()
        with JOBS_LOCK:
            dead = [k for k, v in JOBS.items() if now - v["created"] > JOB_TTL]
            for k in dead:
                JOBS.pop(k, None)
        for k in dead:
            shutil.rmtree(WORK_DIR / k, ignore_errors=True)


threading.Thread(target=sweeper, daemon=True).start()


# ----------------------------------------------------------------------------- api

class InfoReq(BaseModel):
    url: str


class DownloadReq(BaseModel):
    url: str
    quality: str = "vbest"
    title: str = ""


@app.post("/api/info")
def api_info(req: InfoReq):
    try:
        url = clean_url(req.url)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    plat = platform_of(url)
    try:
        info, watermark_free = extract_clean_info(url)
    except Exception as exc:  # noqa: BLE001
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).replace("ERROR:", "").strip()
        if len(msg) > 240:
            msg = msg[:240] + "…"
        return JSONResponse({"error": msg or "Could not read that link."}, status_code=422)

    thumb = info.get("thumbnail") or ""
    if not thumb:
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumb = thumbs[-1].get("url") or ""
    proxied = f"/api/thumb?u={quote(thumb, safe='')}" if thumb else ""

    return {
        "url": url,
        "platform": plat,
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("uploader_id") or info.get("channel") or "",
        "duration": human_dur(info.get("duration")),
        "views": num(info.get("view_count")),
        "likes": num(info.get("like_count")),
        "thumbnail": proxied,
        "watermark_free": watermark_free,
        "qualities": build_qualities(info),
    }


@app.post("/api/download")
def api_download(req: DownloadReq):
    try:
        url = clean_url(req.url)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    title = safe_name(req.title or "thikka-video")
    job_id = new_job(url, req.quality, title, "mp4")
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "Job expired or not found."}, status_code=404)
    return {
        "status": job["status"],
        "stage": job["stage"],
        "progress": job["progress"],
        "speed": human_size(job["speed"]) + "/s" if job["speed"] else "",
        "eta": human_dur(job["eta"]),
        "error": job["error"],
        "filename": job["filename"],
        "size": human_size(job["bytes"]),
        "watermark_free": job.get("watermark_free", True),
        "ready": job["status"] == "done",
    }


@app.get("/api/file/{job_id}")
def api_file(job_id: str):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done" or not job["file"]:
        return JSONResponse({"error": "File not ready."}, status_code=404)
    path = Path(job["file"])
    if not path.exists():
        return JSONResponse({"error": "File expired. Please download again."}, status_code=410)
    return FileResponse(
        path,
        filename=job["filename"],
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/thumb")
def api_thumb(u: str = ""):
    """Proxy thumbnails so they work behind sandboxed previews / ad blockers."""
    if not u:
        return JSONResponse({"error": "missing url"}, status_code=400)
    host = host_of(u)
    if not any(host == h or host.endswith("." + h) for h in THUMB_HOSTS):
        return JSONResponse({"error": "host not allowed"}, status_code=403)
    try:
        import urllib.request
        r = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": "https://www.tiktok.com/"})
        with urllib.request.urlopen(r, timeout=15) as resp:
            data = resp.read(4_000_000)
            ctype = resp.headers.get("Content-Type", "image/jpeg")
        return Response(content=data, media_type=ctype, headers={"Cache-Control": "public, max-age=3600"})
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "thumb fetch failed"}, status_code=502)


@app.get("/api/health")
def health():
    return {"ok": True, "service": "thikka-download", "ytdlp": yt_dlp.version.__version__}


# ----------------------------------------------------------------------------- embedded frontend

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Thikka Download — TikTok &amp; YouTube Video Downloader</title>
<meta name="description" content="Thikka Download — save TikTok and YouTube videos in full quality. Up to 4K, no watermark, no sign-up." />
<meta name="theme-color" content="#05050a" />
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%23ff2e7e'/%3E%3Cstop offset='.55' stop-color='%238b5cf6'/%3E%3Cstop offset='1' stop-color='%2322d3ee'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='18' fill='url(%23g)'/%3E%3Cpath d='M32 15v21m0 0l-8.5-8.5M32 36l8.5-8.5M18 45h28' fill='none' stroke='white' stroke-width='5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E" />
<link rel="stylesheet" href="/styles.css" />
</head>
<body>

<!-- ───────────────── backdrop ───────────────── -->
<div class="bg" aria-hidden="true">
  <div class="bg-glow bg-glow-1"></div>
  <div class="bg-glow bg-glow-2"></div>
  <div class="bg-glow bg-glow-3"></div>
  <div class="bg-grid"></div>
  <div class="bg-noise"></div>
</div>

<!-- ───────────────── nav ───────────────── -->
<header class="nav" id="nav">
  <div class="nav-inner">
    <a class="brand" href="#top" aria-label="Thikka Download home">
      <span class="brand-mark">
        <svg viewBox="0 0 64 64" width="22" height="22" aria-hidden="true">
          <path d="M32 15v21m0 0l-8.5-8.5M32 36l8.5-8.5M18 45h28" fill="none" stroke="currentColor"
                stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </span>
      <span class="brand-text">Thikka<span class="brand-sub">Download</span></span>
    </a>

    <nav class="nav-links" aria-label="Sections">
      <a href="#features">Features</a>
      <a href="#how">How it works</a>
      <a href="#faq">FAQ</a>
    </nav>

    <div class="nav-right">
      <span class="chip-live"><span class="dot"></span> All systems normal</span>
      <a class="nav-cta" href="#top">Start download</a>
    </div>
  </div>
</header>

<main id="top">
  <!-- ───────────────── hero ───────────────── -->
  <section class="hero">
    <div class="eyebrow">
      <span class="eyebrow-dot"></span>
      TikTok &amp; YouTube · up to 4K · watermark removed
    </div>

    <h1>Save any video.<br /><span class="grad-text">Full quality.</span></h1>

    <p class="lede">
      Pull videos straight from the source — original resolution, original audio, no logos burned in.
      <span class="lede-si">TikTok සහ YouTube වීඩියෝ 4K තෙක් නොමිලේ save කරගන්න.</span>
    </p>

    <!-- search -->
    <div class="search">
      <form id="form" autocomplete="off">
        <div class="field">
          <span class="field-icon" id="inputIcon">
            <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7L12 5"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7L12 19"/>
            </svg>
          </span>
          <input id="url" type="text" inputmode="url" spellcheck="false"
                 placeholder="Paste a TikTok or YouTube link…" aria-label="Video URL" />
          <kbd class="field-kbd">⌘V</kbd>
          <button type="button" class="paste" id="pasteBtn" title="Paste from clipboard">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <rect x="8" y="3" width="8" height="4" rx="1.5"/><path d="M16 5h2a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2"/>
            </svg>
            Paste
          </button>
        </div>
        <button type="submit" class="cta" id="submitBtn">
          <span class="cta-label">Get video</span>
          <svg class="cta-arrow" viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M5 12h13M13 6l6 6-6 6"/>
          </svg>
          <span class="spinner" aria-hidden="true"></span>
        </button>
      </form>

      <div class="search-foot">
        <span class="search-foot-label">Supports</span>
        <span class="fmt">youtube.com/watch</span>
        <span class="fmt">youtu.be</span>
        <span class="fmt">/shorts</span>
        <span class="fmt">tiktok.com/@user/video</span>
      </div>

      <p class="error" id="error" role="alert"></p>
    </div>

    <!-- trust stats -->
    <ul class="stats">
      <li><strong>4K</strong><span>max resolution</span></li>
      <li><strong>0</strong><span>watermarks</span></li>
      <li><strong>MP3</strong><span>audio extract</span></li>
      <li><strong>Free</strong><span>no sign-up</span></li>
    </ul>
  </section>

  <!-- ───────────────── result ───────────────── -->
  <section class="result" id="result" hidden aria-live="polite">
    <div class="panel">
      <!-- preview skeleton / content -->
      <div class="media">
        <div class="thumb">
          <img id="thumb" alt="" loading="lazy" />
          <div class="thumb-shade"></div>
          <span class="plat-badge" id="platBadge"></span>
          <span class="dur" id="dur" hidden></span>
        </div>
        <h3 id="title">—</h3>
        <div class="meta" id="meta"></div>
      </div>

      <div class="picker">
        <div class="picker-head">
          <div class="tabs" role="tablist" aria-label="Download type">
            <button class="tab active" data-tab="video" role="tab" aria-selected="true">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10 9.5l5 2.5-5 2.5z" fill="currentColor" stroke="none"/></svg>
              Video
            </button>
            <button class="tab" data-tab="audio" role="tab" aria-selected="false">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 17V6l10-2v11"/><circle cx="6.5" cy="17.5" r="2.5"/><circle cx="16.5" cy="15.5" r="2.5"/></svg>
              Audio
            </button>
          </div>
          <div class="head-right">
            <span class="wm-pill" id="wmPill" title="Streams are pulled straight from the source — no platform logo is burned in.">
              <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12.5l5 5L20 6.5"/></svg>
              No watermark
            </span>
            <span class="picker-badge" id="sizeTag"></span>
          </div>
        </div>

        <div class="chips" id="chips"></div>

        <div class="summary">
          <div class="summary-info">
            <span class="summary-format" id="summaryFormat">MP4 · 1080p</span>
            <span class="summary-size" id="summarySize">—</span>
          </div>
          <button class="download-btn" id="downloadBtn">
            <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M12 4v11m0 0l-4.5-4.5M12 15l4.5-4.5M5 19.5h14"/>
            </svg>
            <span id="downloadBtnLabel">Download</span>
          </button>
        </div>

        <!-- progress -->
        <div class="progress" id="progressBox" hidden>
          <div class="progress-top">
            <span id="progressStage">Starting…</span>
            <span id="progressPct" class="mono">0%</span>
          </div>
          <div class="bar"><div class="bar-fill" id="barFill"></div></div>
          <div class="progress-sub" id="progressSub">Preparing your file…</div>
        </div>

        <!-- done -->
        <div class="done" id="doneBox" hidden>
          <span class="done-icon">
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12.5l5 5L20 6.5"/></svg>
          </span>
          <div class="done-text">
            <strong id="doneName">Ready</strong>
            <span id="doneSize"></span>
          </div>
          <a class="save-btn" id="saveBtn" href="#" download>Save file</a>
        </div>
      </div>
    </div>

    <div class="result-foot">
      <p class="tip" id="tipLine">
        <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.6v.6"/></svg>
        <span id="tipText">Watermark-free stream selected · files auto-delete after 45 minutes.</span>
      </p>
      <button class="reset" id="resetBtn">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12a7 7 0 1 1-2.1-5"/><path d="M19 4.5V9h-4.5"/></svg>
        New link
      </button>
    </div>
  </section>

  <!-- ───────────────── features ───────────────── -->
  <section class="features" id="features">
    <div class="section-head">
      <span class="kicker">Why Thikka</span>
      <h2>Built to keep the quality intact</h2>
    </div>

    <div class="feature-grid">
      <article class="feature">
        <span class="f-icon grad-1">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l2.4 5.5 5.9.6-4.5 4 1.3 5.9L12 15.9 6.9 19l1.3-5.9-4.5-4 5.9-.6z"/></svg>
        </span>
        <h3>Source-level quality</h3>
        <p>Every resolution the platform exposes — up to 2160p. Video and audio streams are muxed locally, never re-encoded.</p>
      </article>

      <article class="feature">
        <span class="f-icon grad-2">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 4l16 16"/><path d="M12 4v11m0 0l-4.5-4.5M12 15l4.5-4.5M5 19.5h14"/></svg>
        </span>
        <h3>Watermark removed</h3>
        <p>Thikka skips the platform's watermarked re-upload and pulls the clean source stream — TikTok and YouTube alike. No logos, no cropping.</p>
      </article>

      <article class="feature">
        <span class="f-icon grad-3">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 17V6l10-2v11"/><circle cx="6.5" cy="17.5" r="2.5"/><circle cx="16.5" cy="15.5" r="2.5"/></svg>
        </span>
        <h3>Audio in one click</h3>
        <p>Extract the soundtrack as MP3 192 kbps or keep the original M4A — ideal for edits, reels and podcasts.</p>
      </article>

      <article class="feature">
        <span class="f-icon grad-4">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l7 3v6c0 4.4-3 7.5-7 9-4-1.5-7-4.6-7-9V6z"/><path d="M9 12l2 2 4-4"/></svg>
        </span>
        <h3>Private by default</h3>
        <p>No account, no tracking pixels, no history kept. Links are processed server-side and files expire automatically.</p>
      </article>
    </div>
  </section>

  <!-- ───────────────── how it works ───────────────── -->
  <section class="how" id="how">
    <div class="section-head">
      <span class="kicker">Three steps</span>
      <h2>From link to file in seconds</h2>
    </div>

    <ol class="steps">
      <li>
        <span class="step-n">01</span>
        <h3>Copy the link</h3>
        <p>Tap <em>Share → Copy link</em> in TikTok or YouTube. Short links and Shorts work too.</p>
      </li>
      <li>
        <span class="step-n">02</span>
        <h3>Paste it above</h3>
        <p>Thikka detects the platform and lists every available resolution with an estimated file size.</p>
      </li>
      <li>
        <span class="step-n">03</span>
        <h3>Pick &amp; save</h3>
        <p>Choose a quality, watch the live progress, then hit <em>Save file</em>. No pop-ups, no redirects.</p>
      </li>
    </ol>
  </section>

  <!-- ───────────────── faq ───────────────── -->
  <section class="faq" id="faq">
    <div class="section-head">
      <span class="kicker">FAQ</span>
      <h2>Questions, answered</h2>
    </div>

    <div class="faq-list">
      <details open>
        <summary>Is it really free?<span class="plus" aria-hidden="true"></span></summary>
        <p>Yes. There are no accounts, no download limits and no ads to click through. The service runs as a small self-hosted app.</p>
      </details>
      <details>
        <summary>What is the highest quality I can get?<span class="plus" aria-hidden="true"></span></summary>
        <p>Whatever the source offers — YouTube commonly serves up to 2160p (4K), TikTok up to 1080p. Video and audio are merged into a single MP4 without re-encoding.</p>
      </details>
      <details>
        <summary>Do downloads have a watermark?<span class="plus" aria-hidden="true"></span></summary>
        <p>No. Thikka filters out every watermarked variant and downloads the clean source stream instead — this covers TikTok's watermark re-upload and YouTube's Shorts/overlay versions. Thikka never stamps its own logo either. Only logos burned into the original footage by the creator can't be removed.</p>
      </details>
      <details>
        <summary>Where do my files go?<span class="plus" aria-hidden="true"></span></summary>
        <p>They are written to a temporary server folder, served to you once, and deleted after 45 minutes. Nothing is archived.</p>
      </details>
      <details>
        <summary>Can I download private or age-restricted videos?<span class="plus" aria-hidden="true"></span></summary>
        <p>No. Thikka only handles publicly available links. Private, members-only and age-gated content stays that way.</p>
      </details>
    </div>
  </section>

  <!-- ───────────────── cta strip ───────────────── -->
  <section class="cta-strip">
    <div class="cta-strip-inner">
      <div>
        <h2>Ready when you are.</h2>
        <p>Paste a link and grab it in full quality — nothing to install.</p>
      </div>
      <a class="cta-strip-btn" href="#top">Get a video</a>
    </div>
  </section>
</main>

<!-- ───────────────── footer ───────────────── -->
<footer>
  <div class="foot-top">
    <div class="foot-brand">
      <a class="brand" href="#top">
        <span class="brand-mark">
          <svg viewBox="0 0 64 64" width="18" height="18" aria-hidden="true"><path d="M32 15v21m0 0l-8.5-8.5M32 36l8.5-8.5M18 45h28" fill="none" stroke="currentColor" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </span>
        <span class="brand-text">Thikka<span class="brand-sub">Download</span></span>
      </a>
      <p>Save TikTok and YouTube videos at the quality they were uploaded in.</p>
    </div>

    <nav class="foot-cols" aria-label="Footer">
      <div>
        <h4>Product</h4>
        <a href="#top">Downloader</a>
        <a href="#features">Features</a>
        <a href="#how">How it works</a>
      </div>
      <div>
        <h4>Platforms</h4>
        <span>YouTube</span>
        <span>TikTok</span>
        <span class="muted">Instagram — soon</span>
      </div>
      <div>
        <h4>Support</h4>
        <a href="#faq">FAQ</a>
        <span>Status: operational</span>
      </div>
    </nav>
  </div>

  <div class="foot-bottom">
    <span>© <span id="year">2026</span> Thikka Download. Built for personal use.</span>
    <span class="disclaimer">Download only content you own or have permission to use. Respect creators.</span>
  </div>
</footer>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script src="/app.js"></script>
</body>
</html>
"""

STYLES_CSS = r"""/* ============================================================
   Thikka Download — design system v2
   ============================================================ */
:root {
  /* surfaces */
  --bg: #05050a;
  --bg-2: #08080f;
  --surface: rgba(255, 255, 255, 0.032);
  --surface-2: rgba(255, 255, 255, 0.055);
  --surface-3: rgba(255, 255, 255, 0.085);
  --line: rgba(255, 255, 255, 0.075);
  --line-2: rgba(255, 255, 255, 0.12);
  --line-3: rgba(255, 255, 255, 0.18);

  /* ink */
  --ink: #f4f5fa;
  --ink-2: #b9bdcd;
  --ink-3: #868ba1;
  --ink-4: #5d6278;

  /* brand */
  --pink: #ff2e7e;
  --violet: #8b5cf6;
  --cyan: #22d3ee;
  --green: #34e6a1;
  --grad: linear-gradient(112deg, #ff2e7e 0%, #a855f7 50%, #22d3ee 100%);
  --grad-quiet: linear-gradient(112deg, rgba(255,46,126,.14), rgba(168,85,247,.14), rgba(34,211,238,.14));

  /* radii */
  --r-xl: 26px;
  --r-lg: 20px;
  --r-md: 14px;
  --r-sm: 10px;

  /* shadows */
  --sh-sm: 0 2px 8px rgba(0, 0, 0, 0.35);
  --sh-md: 0 18px 40px -22px rgba(0, 0, 0, 0.9);
  --sh-lg: 0 40px 90px -40px rgba(0, 0, 0, 0.95), 0 0 0 1px rgba(255, 255, 255, 0.04) inset;
  --sh-glow: 0 18px 44px -18px rgba(168, 85, 247, 0.75);

  --font: "Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
    "Helvetica Neue", "Noto Sans Sinhala", Arial, sans-serif;
  --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;

  --maxw: 1120px;
  --ease: cubic-bezier(0.22, 0.8, 0.24, 1);
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; scroll-padding-top: 88px; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: var(--font);
  font-size: 15px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  overflow-x: hidden;
  letter-spacing: -0.006em;
}

::selection { background: rgba(168, 85, 247, 0.38); color: #fff; }

:focus-visible {
  outline: 2px solid rgba(168, 85, 247, 0.95);
  outline-offset: 2px;
  border-radius: 6px;
}

::-webkit-scrollbar { width: 11px; height: 11px; }
::-webkit-scrollbar-track { background: #07070d; }
::-webkit-scrollbar-thumb {
  background: #23232f;
  border: 3px solid #07070d;
  border-radius: 999px;
}
::-webkit-scrollbar-thumb:hover { background: #34344a; }

/* ───────────────── backdrop ───────────────── */
.bg {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  overflow: hidden;
  background:
    radial-gradient(1100px 700px at 50% -22%, #150a2c 0%, transparent 62%),
    radial-gradient(780px 520px at 88% 4%, #05222b 0%, transparent 58%),
    linear-gradient(180deg, #07070e 0%, #05050a 55%, #04040a 100%);
}

.bg-glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(110px);
  will-change: transform;
}
.bg-glow-1 {
  width: 700px; height: 700px; top: -340px; left: -180px;
  background: radial-gradient(circle, #ff2e7e 0%, transparent 62%);
  opacity: 0.3;
  animation: drift-a 26s ease-in-out infinite;
}
.bg-glow-2 {
  width: 620px; height: 620px; top: -260px; right: -200px;
  background: radial-gradient(circle, #22d3ee 0%, transparent 62%);
  opacity: 0.17;
  animation: drift-b 32s ease-in-out infinite;
}
.bg-glow-3 {
  width: 820px; height: 820px; bottom: -520px; left: 34%;
  background: radial-gradient(circle, #7c3aed 0%, transparent 64%);
  opacity: 0.22;
  animation: drift-c 38s ease-in-out infinite;
}
@keyframes drift-a {
  0%, 100% { transform: translate3d(0, 0, 0) scale(1); }
  50% { transform: translate3d(70px, 50px, 0) scale(1.07); }
}
@keyframes drift-b {
  0%, 100% { transform: translate3d(0, 0, 0) scale(1); }
  50% { transform: translate3d(-60px, 80px, 0) scale(1.1); }
}
@keyframes drift-c {
  0%, 100% { transform: translate3d(0, 0, 0) scale(1); }
  50% { transform: translate3d(-80px, -50px, 0) scale(1.05); }
}

.bg-grid {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(to right, rgba(255, 255, 255, 0.028) 1px, transparent 1px),
    linear-gradient(to bottom, rgba(255, 255, 255, 0.028) 1px, transparent 1px);
  background-size: 64px 64px;
  mask-image: radial-gradient(ellipse 78% 55% at 50% 0%, #000 8%, transparent 72%);
  -webkit-mask-image: radial-gradient(ellipse 78% 55% at 50% 0%, #000 8%, transparent 72%);
}

.bg-noise {
  position: absolute;
  inset: 0;
  opacity: 0.035;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='3'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)'/%3E%3C/svg%3E");
  mix-blend-mode: overlay;
}

main, .nav, footer { position: relative; z-index: 1; }

/* ───────────────── shared ───────────────── */
.wrap, .nav-inner, main > section, .foot-top, .foot-bottom {
  width: 100%;
  max-width: var(--maxw);
  margin-inline: auto;
  padding-inline: 24px;
}

.mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.muted { color: var(--ink-4); }

.grad-text {
  background: var(--grad);
  background-size: 220% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  animation: shimmer 9s linear infinite;
}
@keyframes shimmer {
  0% { background-position: 0% 50%; }
  100% { background-position: 220% 50%; }
}

.section-head { margin-bottom: 30px; }
.kicker {
  display: inline-block;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-4);
  margin-bottom: 10px;
}
.section-head h2 {
  margin: 0;
  font-size: clamp(22px, 3.1vw, 32px);
  line-height: 1.18;
  letter-spacing: -0.028em;
  font-weight: 700;
}

/* ───────────────── nav ───────────────── */
.nav {
  position: sticky;
  top: 0;
  z-index: 40;
  background: rgba(5, 5, 10, 0.62);
  border-bottom: 1px solid transparent;
  backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
  transition: border-color 0.3s, background 0.3s;
}
.nav.scrolled {
  border-bottom-color: var(--line);
  background: rgba(5, 5, 10, 0.84);
}

.nav-inner {
  display: flex;
  align-items: center;
  gap: 18px;
  height: 68px;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  text-decoration: none;
  color: var(--ink);
  flex: none;
}
.brand-mark {
  display: grid;
  place-items: center;
  width: 34px; height: 34px;
  border-radius: 10px;
  background: var(--grad);
  color: #fff;
  box-shadow: 0 8px 20px -8px rgba(168, 85, 247, 0.9), inset 0 1px 0 rgba(255, 255, 255, 0.4);
  flex: none;
}
.brand-text {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  font-size: 16.5px;
  font-weight: 800;
  letter-spacing: -0.03em;
}
.brand-sub {
  font-weight: 600;
  color: var(--ink-3);
  letter-spacing: -0.02em;
}

.nav-links {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-inline: auto;
}
.nav-links a {
  padding: 8px 13px;
  border-radius: 9px;
  font-size: 13.5px;
  font-weight: 550;
  color: var(--ink-3);
  text-decoration: none;
  transition: color 0.18s, background 0.18s;
}
.nav-links a:hover { color: var(--ink); background: var(--surface-2); }

.nav-right { display: flex; align-items: center; gap: 10px; flex: none; }

.chip-live {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: var(--surface);
  font-size: 11.5px;
  font-weight: 600;
  color: var(--ink-2);
  white-space: nowrap;
}
.dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 0 0 rgba(52, 230, 161, 0.6);
  animation: ping 2.4s infinite;
}
@keyframes ping {
  0% { box-shadow: 0 0 0 0 rgba(52, 230, 161, 0.55); }
  70% { box-shadow: 0 0 0 7px rgba(52, 230, 161, 0); }
  100% { box-shadow: 0 0 0 0 rgba(52, 230, 161, 0); }
}

.nav-cta {
  padding: 9px 17px;
  border-radius: 10px;
  border: 1px solid var(--line-2);
  background: var(--surface-2);
  color: var(--ink);
  font-size: 13px;
  font-weight: 650;
  text-decoration: none;
  transition: 0.2s;
  white-space: nowrap;
}
.nav-cta:hover { background: var(--surface-3); border-color: var(--line-3); }

/* ───────────────── hero ───────────────── */
.hero {
  text-align: center;
  padding-top: 68px;
  padding-bottom: 26px;
}

.eyebrow {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  padding: 6px 14px 6px 11px;
  border-radius: 999px;
  border: 1px solid var(--line-2);
  background: linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.02));
  font-size: 12px;
  font-weight: 600;
  color: var(--ink-2);
  letter-spacing: -0.005em;
  animation: rise 0.6s var(--ease) both;
}
.eyebrow-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--grad);
  box-shadow: 0 0 10px rgba(168, 85, 247, 0.9);
}

.hero h1 {
  margin: 24px auto 0;
  font-size: clamp(38px, 7.2vw, 76px);
  line-height: 0.99;
  letter-spacing: -0.045em;
  font-weight: 800;
  animation: rise 0.7s var(--ease) 0.05s both;
}

.lede {
  max-width: 620px;
  margin: 22px auto 0;
  color: var(--ink-2);
  font-size: 15.5px;
  line-height: 1.62;
  animation: rise 0.7s var(--ease) 0.1s both;
}
.lede-si {
  display: block;
  margin-top: 5px;
  font-size: 13.5px;
  color: var(--ink-4);
}

@keyframes rise {
  from { opacity: 0; transform: translateY(14px); }
  to { opacity: 1; transform: none; }
}

/* search */
.search {
  max-width: 760px;
  margin: 34px auto 0;
  padding: 14px;
  border-radius: var(--r-xl);
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.062), rgba(255, 255, 255, 0.022));
  border: 1px solid var(--line-2);
  box-shadow: var(--sh-lg);
  backdrop-filter: blur(26px) saturate(150%);
  -webkit-backdrop-filter: blur(26px) saturate(150%);
  position: relative;
  animation: rise 0.8s var(--ease) 0.16s both;
}
.search::before {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: inherit;
  padding: 1px;
  background: linear-gradient(135deg, rgba(255, 46, 126, 0.55), transparent 32%, transparent 68%, rgba(34, 211, 238, 0.45));
  -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
  -webkit-mask-composite: xor;
  mask-composite: exclude;
  pointer-events: none;
}

#form { display: flex; gap: 9px; }

.field {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 9px;
  height: 54px;
  padding: 0 8px 0 15px;
  border-radius: var(--r-md);
  background: rgba(5, 5, 12, 0.78);
  border: 1px solid var(--line);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
  transition: border-color 0.2s, box-shadow 0.2s, background 0.2s;
  min-width: 0;
}
.field:focus-within {
  border-color: rgba(168, 85, 247, 0.6);
  box-shadow: 0 0 0 4px rgba(168, 85, 247, 0.13), inset 0 1px 0 rgba(255, 255, 255, 0.04);
  background: rgba(6, 6, 14, 0.92);
}

.field-icon { display: grid; place-items: center; color: var(--ink-4); flex: none; transition: color 0.2s; }
.field-icon.yt { color: #ff4d6d; }
.field-icon.tt { color: #67e8f9; }

#url {
  flex: 1;
  min-width: 0;
  height: 100%;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--ink);
  font-family: inherit;
  font-size: 14.5px;
}
#url::placeholder { color: var(--ink-4); }

.field-kbd {
  font-family: var(--mono);
  font-size: 10.5px;
  font-weight: 600;
  color: var(--ink-4);
  padding: 3px 6px;
  border-radius: 5px;
  border: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.035);
  flex: none;
}
.field:focus-within .field-kbd { opacity: 0.4; }

.paste {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 38px;
  padding: 0 12px;
  border-radius: 9px;
  border: 1px solid var(--line-2);
  background: var(--surface-2);
  color: var(--ink-2);
  font-family: inherit;
  font-size: 12px;
  font-weight: 650;
  cursor: pointer;
  transition: 0.18s;
  flex: none;
}
.paste:hover { color: #fff; background: var(--surface-3); border-color: var(--line-3); }
.paste:active { transform: scale(0.97); }

.cta {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: 54px;
  padding: 0 24px;
  border: 0;
  border-radius: var(--r-md);
  background: var(--grad);
  background-size: 190% 100%;
  color: #fff;
  font-family: inherit;
  font-size: 14.5px;
  font-weight: 700;
  letter-spacing: -0.012em;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  box-shadow: 0 14px 34px -14px rgba(236, 72, 153, 0.9), inset 0 1px 0 rgba(255, 255, 255, 0.35);
  transition: background-position 0.55s var(--ease), box-shadow 0.3s, transform 0.14s;
}
.cta::after {
  content: "";
  position: absolute;
  top: 0; left: -60%;
  width: 45%; height: 100%;
  background: linear-gradient(100deg, transparent, rgba(255, 255, 255, 0.35), transparent);
  transform: skewX(-18deg);
  transition: left 0.6s var(--ease);
}
.cta:hover { background-position: 100% 50%; box-shadow: 0 18px 44px -14px rgba(168, 85, 247, 0.95); }
.cta:hover::after { left: 130%; }
.cta:active { transform: scale(0.99); }
.cta[disabled] { cursor: progress; }
.cta-arrow { transition: transform 0.22s var(--ease); }
.cta:hover .cta-arrow { transform: translateX(3px); }

.spinner {
  display: none;
  width: 16px; height: 16px;
  border-radius: 50%;
  border: 2px solid rgba(255, 255, 255, 0.32);
  border-top-color: #fff;
  animation: spin 0.65s linear infinite;
}
.cta.loading .spinner { display: block; }
.cta.loading .cta-arrow { display: none; }
@keyframes spin { to { transform: rotate(360deg); } }

.search-foot {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 12px;
  padding: 0 4px;
}
.search-foot-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--ink-4);
  margin-right: 2px;
}
.fmt {
  font-family: var(--mono);
  font-size: 10.5px;
  padding: 3px 7px;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.03);
  color: var(--ink-2);
}

.error {
  margin: 12px 2px 0;
  padding: 10px 13px;
  border-radius: var(--r-sm);
  font-size: 13px;
  color: #ffa3b5;
  background: rgba(255, 46, 126, 0.08);
  border: 1px solid rgba(255, 46, 126, 0.26);
  display: none;
  text-align: left;
}
.error.show { display: block; animation: rise 0.28s var(--ease) both; }

/* stats */
.stats {
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 0;
  margin: 30px auto 0;
  padding: 0;
  animation: rise 0.8s var(--ease) 0.24s both;
}
.stats li {
  display: flex;
  align-items: baseline;
  gap: 7px;
  padding: 0 26px;
  border-right: 1px solid var(--line);
}
.stats li:last-child { border-right: 0; }
.stats strong {
  font-size: 15px;
  font-weight: 750;
  letter-spacing: -0.02em;
  color: var(--ink);
}
.stats span { font-size: 12px; color: var(--ink-4); }

/* ───────────────── result ───────────────── */
.result {
  margin-top: 34px;
  animation: rise 0.5s var(--ease) both;
}
.result[hidden] { display: none; }

.panel {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 24px;
  padding: 20px;
  border-radius: var(--r-xl);
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.058), rgba(255, 255, 255, 0.02));
  border: 1px solid var(--line-2);
  box-shadow: var(--sh-lg);
  backdrop-filter: blur(26px) saturate(150%);
  -webkit-backdrop-filter: blur(26px) saturate(150%);
}

.media { min-width: 0; }

.thumb {
  position: relative;
  aspect-ratio: 16 / 9;
  border-radius: var(--r-lg);
  overflow: hidden;
  background: linear-gradient(180deg, #101020, #0a0a14);
  border: 1px solid var(--line);
  box-shadow: var(--sh-md);
}
.thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.thumb-shade {
  position: absolute;
  inset: 0;
  background: linear-gradient(180deg, transparent 52%, rgba(4, 4, 10, 0.82));
}
.plat-badge,
.dur {
  position: absolute;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 9px;
  border-radius: 7px;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: #fff;
  background: rgba(6, 6, 14, 0.76);
  border: 1px solid rgba(255, 255, 255, 0.14);
  backdrop-filter: blur(8px);
  text-transform: capitalize;
}
.plat-badge { top: 9px; left: 9px; }
.plat-badge::before {
  content: "";
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--grad);
}
.dur { bottom: 9px; right: 9px; font-family: var(--mono); text-transform: none; }
.dur[hidden] { display: none; }

.media h3 {
  margin: 14px 0 9px;
  font-size: 14px;
  font-weight: 650;
  line-height: 1.45;
  letter-spacing: -0.012em;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.meta { display: flex; flex-wrap: wrap; gap: 5px; }
.meta span {
  font-size: 11px;
  font-weight: 550;
  color: var(--ink-3);
  padding: 3px 8px;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.03);
  white-space: nowrap;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* picker */
.picker { display: flex; flex-direction: column; min-width: 0; }

.picker-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.tabs {
  display: inline-flex;
  gap: 3px;
  padding: 3px;
  border-radius: 11px;
  background: rgba(5, 5, 12, 0.6);
  border: 1px solid var(--line);
}
.tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--ink-3);
  font-family: inherit;
  font-size: 12.5px;
  font-weight: 650;
  cursor: pointer;
  transition: 0.2s;
}
.tab:hover { color: var(--ink-2); }
.tab.active {
  color: #fff;
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.13), rgba(255, 255, 255, 0.06));
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.12) inset, var(--sh-sm);
}
.tab.active svg { color: #c4a8ff; }

.head-right { display: flex; align-items: center; gap: 7px; min-width: 0; }

.wm-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 9px;
  border-radius: 6px;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: #7ff0c0;
  background: rgba(52, 230, 161, 0.1);
  border: 1px solid rgba(52, 230, 161, 0.28);
  white-space: nowrap;
}
.wm-pill svg { flex: none; }
.wm-pill.warn {
  color: #ffd27a;
  background: rgba(255, 176, 32, 0.1);
  border-color: rgba(255, 176, 32, 0.3);
}

.picker-badge {
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: #d8c9ff;
  padding: 4px 9px;
  border-radius: 6px;
  background: rgba(168, 85, 247, 0.12);
  border: 1px solid rgba(168, 85, 247, 0.3);
  white-space: nowrap;
}
.picker-badge:empty { display: none; }

.chips {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(112px, 1fr));
  gap: 8px;
  margin-bottom: 16px;
  min-height: 66px;
}

.chip {
  position: relative;
  text-align: left;
  padding: 10px 12px;
  border-radius: var(--r-md);
  background: rgba(255, 255, 255, 0.032);
  border: 1px solid var(--line);
  color: var(--ink);
  font-family: inherit;
  cursor: pointer;
  transition: 0.18s var(--ease);
  overflow: hidden;
}
.chip:hover {
  background: rgba(255, 255, 255, 0.07);
  border-color: var(--line-2);
  transform: translateY(-1px);
}
.chip strong {
  display: block;
  font-size: 13.5px;
  font-weight: 700;
  letter-spacing: -0.015em;
  line-height: 1.2;
}
.chip small {
  display: block;
  margin-top: 3px;
  font-size: 10.5px;
  font-weight: 550;
  color: var(--ink-4);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chip.active {
  background: var(--grad-quiet);
  border-color: rgba(168, 85, 247, 0.65);
  box-shadow: 0 0 0 3px rgba(168, 85, 247, 0.11), 0 12px 28px -18px rgba(168, 85, 247, 0.95);
}
.chip.active small { color: #d9ccff; }
.chip.active::after {
  content: "";
  position: absolute;
  top: 10px; right: 10px;
  width: 5px; height: 5px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 8px rgba(52, 230, 161, 0.9);
}

/* summary bar */
.summary {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: auto;
  padding: 12px 12px 12px 16px;
  border-radius: var(--r-md);
  background: rgba(5, 5, 12, 0.55);
  border: 1px solid var(--line);
}
.summary-info { display: flex; flex-direction: column; min-width: 0; flex: 1; }
.summary-format {
  font-size: 12.5px;
  font-weight: 700;
  letter-spacing: -0.01em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.summary-size { font-size: 11px; color: var(--ink-4); }

.download-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  height: 42px;
  padding: 0 20px;
  border: 0;
  border-radius: 10px;
  background: var(--grad);
  background-size: 190% 100%;
  color: #fff;
  font-family: inherit;
  font-size: 13.5px;
  font-weight: 700;
  cursor: pointer;
  white-space: nowrap;
  transition: 0.25s;
  box-shadow: 0 12px 30px -14px rgba(236, 72, 153, 0.9), inset 0 1px 0 rgba(255, 255, 255, 0.3);
  flex: none;
}
.download-btn:hover { background-position: 100% 50%; box-shadow: 0 16px 38px -14px rgba(168, 85, 247, 0.95); }
.download-btn:active { transform: scale(0.99); }
.download-btn[disabled] { opacity: 0.55; cursor: not-allowed; }

/* progress */
.progress {
  margin-top: 12px;
  padding: 13px 15px;
  border-radius: var(--r-md);
  background: rgba(5, 5, 12, 0.62);
  border: 1px solid var(--line);
}
.progress[hidden] { display: none; }
.progress-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ink-2);
  margin-bottom: 9px;
}
#progressPct { color: #fff; font-size: 12px; }
.bar {
  height: 6px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.07);
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  width: 0%;
  border-radius: 999px;
  background: var(--grad);
  background-size: 200% 100%;
  animation: shimmer 2.6s linear infinite;
  transition: width 0.4s var(--ease);
  box-shadow: 0 0 14px rgba(168, 85, 247, 0.75);
}
.progress-sub {
  margin-top: 8px;
  font-size: 11px;
  color: var(--ink-4);
  font-variant-numeric: tabular-nums;
}

/* done */
.done {
  display: flex;
  align-items: center;
  gap: 11px;
  margin-top: 12px;
  padding: 11px 12px;
  border-radius: var(--r-md);
  background: rgba(52, 230, 161, 0.055);
  border: 1px solid rgba(52, 230, 161, 0.28);
  animation: rise 0.35s var(--ease) both;
}
.done[hidden] { display: none; }
.done-icon {
  display: grid;
  place-items: center;
  width: 27px; height: 27px;
  border-radius: 50%;
  color: var(--green);
  background: rgba(52, 230, 161, 0.14);
  border: 1px solid rgba(52, 230, 161, 0.3);
  flex: none;
}
.done-text { flex: 1; min-width: 0; }
.done-text strong {
  display: block;
  font-size: 12px;
  font-weight: 650;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.done-text span { font-size: 10.5px; color: var(--ink-4); }
.save-btn {
  flex: none;
  padding: 8px 15px;
  border-radius: 9px;
  background: #fff;
  color: #06060c;
  font-size: 12px;
  font-weight: 800;
  text-decoration: none;
  transition: 0.18s;
  box-shadow: 0 8px 20px -10px rgba(255, 255, 255, 0.5);
}
.save-btn:hover { background: #eceaf5; transform: translateY(-1px); }

.result-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  margin-top: 14px;
  padding: 0 4px;
}
.tip {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-size: 11.5px;
  color: var(--ink-4);
}
.tip svg { flex: none; opacity: 0.7; }
.tip.warn { color: #ffcf8a; }

.reset {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 13px;
  border-radius: 9px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink-2);
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: 0.18s;
  flex: none;
}
.reset:hover { color: #fff; background: var(--surface-2); border-color: var(--line-2); }

/* ───────────────── features ───────────────── */
.features, .how, .faq { margin-top: 92px; }

.feature-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}

.feature {
  padding: 22px;
  border-radius: var(--r-lg);
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.045), rgba(255, 255, 255, 0.014));
  border: 1px solid var(--line);
  transition: 0.28s var(--ease);
  position: relative;
  overflow: hidden;
}
.feature::after {
  content: "";
  position: absolute;
  inset: 0;
  background: radial-gradient(320px 140px at 12% 0%, rgba(168, 85, 247, 0.14), transparent 70%);
  opacity: 0;
  transition: opacity 0.3s;
  pointer-events: none;
}
.feature:hover {
  transform: translateY(-2px);
  border-color: var(--line-2);
  box-shadow: 0 30px 60px -40px rgba(139, 92, 246, 0.9);
}
.feature:hover::after { opacity: 1; }

.f-icon {
  display: grid;
  place-items: center;
  width: 34px; height: 34px;
  border-radius: 10px;
  color: #fff;
  margin-bottom: 14px;
  position: relative;
  z-index: 1;
}
.grad-1 { background: linear-gradient(140deg, #ff2e7e, #a855f7); box-shadow: 0 10px 22px -12px rgba(255, 46, 126, 0.95); }
.grad-2 { background: linear-gradient(140deg, #a855f7, #22d3ee); box-shadow: 0 10px 22px -12px rgba(34, 211, 238, 0.85); }
.grad-3 { background: linear-gradient(140deg, #6366f1, #22d3ee); box-shadow: 0 10px 22px -12px rgba(99, 102, 241, 0.95); }
.grad-4 { background: linear-gradient(140deg, #14b8a6, #a3e635); box-shadow: 0 10px 22px -12px rgba(20, 184, 166, 0.9); }

.feature h3 {
  position: relative;
  z-index: 1;
  margin: 0 0 6px;
  font-size: 14.5px;
  font-weight: 700;
  letter-spacing: -0.018em;
}
.feature p {
  position: relative;
  z-index: 1;
  margin: 0;
  font-size: 13px;
  line-height: 1.62;
  color: var(--ink-3);
}

/* ───────────────── how it works ───────────────── */
.steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  counter-reset: step;
}
.steps li {
  position: relative;
  padding: 22px 22px 20px;
  border-radius: var(--r-lg);
  background: rgba(255, 255, 255, 0.026);
  border: 1px solid var(--line);
}
.steps li::before {
  content: "";
  position: absolute;
  top: 0; left: 22px;
  width: 34px; height: 2px;
  border-radius: 2px;
  background: var(--grad);
  opacity: 0.9;
}
.step-n {
  display: block;
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
  color: var(--ink-4);
  letter-spacing: 0.06em;
  margin-bottom: 12px;
}
.steps h3 {
  margin: 0 0 6px;
  font-size: 14.5px;
  font-weight: 700;
  letter-spacing: -0.018em;
}
.steps p {
  margin: 0;
  font-size: 12.8px;
  line-height: 1.62;
  color: var(--ink-3);
}
.steps em {
  font-style: normal;
  color: var(--ink-2);
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 12px;
}

/* ───────────────── faq ───────────────── */
.faq-list {
  border-top: 1px solid var(--line);
}
.faq-list details {
  border-bottom: 1px solid var(--line);
}
.faq-list summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 4px;
  font-size: 14.5px;
  font-weight: 600;
  letter-spacing: -0.012em;
  color: var(--ink);
  cursor: pointer;
  list-style: none;
  transition: color 0.2s;
}
.faq-list summary::-webkit-details-marker { display: none; }
.faq-list summary:hover { color: #fff; }
.faq-list .plus {
  position: relative;
  width: 16px; height: 16px;
  flex: none;
  border-radius: 50%;
  border: 1px solid var(--line-2);
  transition: 0.25s var(--ease);
}
.faq-list .plus::before,
.faq-list .plus::after {
  content: "";
  position: absolute;
  top: 50%; left: 50%;
  width: 7px; height: 1.5px;
  border-radius: 2px;
  background: var(--ink-3);
  transform: translate(-50%, -50%);
  transition: 0.25s var(--ease);
}
.faq-list .plus::after { transform: translate(-50%, -50%) rotate(90deg); }
.faq-list details[open] .plus { border-color: rgba(168, 85, 247, 0.6); background: rgba(168, 85, 247, 0.14); }
.faq-list details[open] .plus::before,
.faq-list details[open] .plus::after { background: #d9ccff; }
.faq-list details[open] .plus::after { transform: translate(-50%, -50%) rotate(0deg); opacity: 0; }
.faq-list p {
  margin: 0;
  padding: 0 4px 18px;
  font-size: 13.5px;
  line-height: 1.68;
  color: var(--ink-3);
  max-width: 68ch;
  animation: rise 0.3s var(--ease) both;
}

/* ───────────────── cta strip ───────────────── */
.cta-strip { margin-top: 88px; }
.cta-strip-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 30px 32px;
  border-radius: var(--r-xl);
  background:
    radial-gradient(600px 200px at 8% 0%, rgba(255, 46, 126, 0.16), transparent 70%),
    radial-gradient(600px 200px at 95% 100%, rgba(34, 211, 238, 0.14), transparent 70%),
    linear-gradient(180deg, rgba(255, 255, 255, 0.055), rgba(255, 255, 255, 0.018));
  border: 1px solid var(--line-2);
  box-shadow: var(--sh-lg);
  position: relative;
  overflow: hidden;
}
.cta-strip h2 {
  margin: 0 0 5px;
  font-size: clamp(20px, 2.6vw, 26px);
  font-weight: 750;
  letter-spacing: -0.03em;
}
.cta-strip p { margin: 0; font-size: 13.5px; color: var(--ink-3); }
.cta-strip-btn {
  flex: none;
  padding: 13px 24px;
  border-radius: 12px;
  background: var(--grad);
  background-size: 190% 100%;
  color: #fff;
  font-size: 14px;
  font-weight: 700;
  text-decoration: none;
  transition: 0.3s;
  box-shadow: 0 16px 38px -16px rgba(236, 72, 153, 0.95), inset 0 1px 0 rgba(255, 255, 255, 0.3);
}
.cta-strip-btn:hover { background-position: 100% 50%; box-shadow: 0 20px 46px -16px rgba(168, 85, 247, 0.95); }

/* ───────────────── footer ───────────────── */
footer {
  margin-top: 92px;
  border-top: 1px solid var(--line);
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.018), transparent);
}
.foot-top {
  display: grid;
  grid-template-columns: 1.4fr 2fr;
  gap: 40px;
  padding-top: 44px;
  padding-bottom: 34px;
}
.foot-brand p {
  margin: 14px 0 0;
  font-size: 12.8px;
  line-height: 1.65;
  color: var(--ink-4);
  max-width: 30ch;
}
.foot-cols {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}
.foot-cols h4 {
  margin: 0 0 12px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  color: var(--ink-4);
}
.foot-cols a,
.foot-cols span {
  display: block;
  font-size: 12.8px;
  color: var(--ink-3);
  text-decoration: none;
  margin-bottom: 8px;
  transition: color 0.18s;
}
.foot-cols a:hover { color: var(--ink); }

.foot-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  padding-top: 18px;
  padding-bottom: 26px;
  border-top: 1px solid var(--line);
  font-size: 11.5px;
  color: var(--ink-4);
}
.foot-bottom .disclaimer { max-width: 52ch; }

/* ───────────────── toast ───────────────── */
.toast {
  position: fixed;
  left: 50%;
  bottom: 28px;
  z-index: 60;
  transform: translate(-50%, 16px);
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 11px 18px;
  border-radius: 999px;
  background: rgba(12, 12, 20, 0.94);
  border: 1px solid var(--line-2);
  color: var(--ink);
  font-size: 12.5px;
  font-weight: 600;
  box-shadow: 0 24px 60px -20px rgba(0, 0, 0, 0.95), inset 0 1px 0 rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(16px);
  opacity: 0;
  pointer-events: none;
  transition: 0.32s var(--ease);
  max-width: min(92vw, 440px);
  text-align: center;
}
.toast.show { opacity: 1; transform: translate(-50%, 0); }
.toast::before {
  content: "";
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--green);
  flex: none;
}
.toast.err::before { background: #ff5b7f; }

/* ───────────────── skeleton ───────────────── */
.skeleton {
  position: relative;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.045);
  border-radius: var(--r-md);
}
.skeleton::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.09), transparent);
  transform: translateX(-100%);
  animation: sweep 1.4s infinite;
}
@keyframes sweep { to { transform: translateX(100%); } }

/* ───────────────── responsive ───────────────── */
@media (max-width: 980px) {
  .nav-links { display: none; }
  .nav-right { margin-left: auto; }
  .panel { grid-template-columns: 1fr; gap: 20px; }
  .media {
    display: grid;
    grid-template-columns: 172px 1fr;
    gap: 4px 16px;
    align-items: start;
  }
  .thumb { grid-row: span 2; aspect-ratio: 1 / 1; }
  .media h3 { margin: 2px 0 8px; }
  .media .meta { grid-column: 2; }
  .feature-grid { grid-template-columns: 1fr; }
  .foot-top { grid-template-columns: 1fr; gap: 30px; }
}

@media (max-width: 780px) {
  .hero { padding-top: 44px; }
  .stats li { padding: 0 16px; }
  .steps { grid-template-columns: 1fr; }
  .cta-strip-inner { flex-direction: column; align-items: flex-start; gap: 18px; }
  .cta-strip-btn { width: 100%; text-align: center; }
  .faq-list summary { font-size: 14px; }
}

@media (max-width: 620px) {
  .nav-inner { height: 60px; gap: 10px; }
  .chip-live { display: none; }
  .brand-text { font-size: 15px; }
  .brand-mark { width: 30px; height: 30px; border-radius: 9px; }
  .nav-cta { padding: 8px 13px; font-size: 12px; }

  main > section, .nav-inner, .foot-top, .foot-bottom { padding-inline: 16px; }

  .hero { padding-top: 34px; }
  .hero h1 { letter-spacing: -0.04em; }
  .lede { font-size: 14px; }
  .lede-si { font-size: 12.5px; }

  .search { padding: 11px; border-radius: 20px; }
  #form { flex-direction: column; }
  .field { height: 52px; padding-left: 13px; }
  .field-kbd { display: none; }
  .cta { width: 100%; height: 50px; }
  .search-foot { gap: 5px; }
  .fmt:nth-of-type(n + 3) { display: none; }

  .stats { gap: 10px 0; margin-top: 24px; }
  .stats li { padding: 0 12px; flex-direction: column; align-items: flex-start; gap: 0; }
  .stats li:first-child { padding-left: 0; }
  .stats strong { font-size: 14px; }

  .panel { padding: 13px; border-radius: 20px; }
  .media { grid-template-columns: 108px 1fr; gap: 4px 12px; }
  .media h3 { font-size: 13px; }
  .plat-badge { font-size: 9.5px; padding: 3px 7px; }
  .dur { font-size: 9.5px; padding: 3px 7px; }

  .picker-head { flex-wrap: wrap; gap: 8px; }
  .head-right { width: 100%; justify-content: space-between; }
  .wm-pill, .picker-badge { font-size: 10px; }
  .chips { grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 7px; }
  .chip { padding: 9px 10px; }
  .chip strong { font-size: 12.5px; }
  .chip small { font-size: 10px; }

  .summary { flex-wrap: wrap; padding: 11px; gap: 10px; }
  .summary-info { flex-basis: 100%; }
  .download-btn { width: 100%; }

  .result-foot { flex-direction: column; align-items: stretch; gap: 10px; }
  .reset { justify-content: center; }

  .features, .how, .faq, .cta-strip { margin-top: 62px; }
  .feature, .steps li { border-radius: 18px; }
  .cta-strip-inner { padding: 24px 20px; border-radius: 20px; }
  .foot-cols { grid-template-columns: 1fr 1fr; gap: 18px; }
  .foot-bottom { font-size: 11px; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important;
    scroll-behavior: auto !important;
  }
}
"""

APP_JS = r"""/* ============================================================
   Thikka Download — frontend logic v2
   ============================================================ */
const $ = (id) => document.getElementById(id);

const form = $("form");
const urlInput = $("url");
const submitBtn = $("submitBtn");
const pasteBtn = $("pasteBtn");
const inputIcon = $("inputIcon");
const errorBox = $("error");
const result = $("result");
const toastEl = $("toast");

const LINK_ICON = `<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7L12 5"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7L12 19"/></svg>`;
const YT_ICON = `<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M21.6 7.2a2.8 2.8 0 0 0-2-2C17.9 4.8 12 4.8 12 4.8s-5.9 0-7.6.4a2.8 2.8 0 0 0-2 2A29 29 0 0 0 2 12a29 29 0 0 0 .4 4.8 2.8 2.8 0 0 0 2 2c1.7.4 7.6.4 7.6.4s5.9 0 7.6-.4a2.8 2.8 0 0 0 2-2A29 29 0 0 0 22 12a29 29 0 0 0-.4-4.8ZM9.9 15.3V8.7l5.7 3.3-5.7 3.3Z"/></svg>`;
const TT_ICON = `<svg viewBox="0 0 24 24" width="19" height="19" fill="currentColor"><path d="M16.6 5.8a4.6 4.6 0 0 1-1.1-2.8h-3.1v11.2a2.6 2.6 0 1 1-1.9-2.5V8.5a5.7 5.7 0 1 0 5 5.7V9.6a7.6 7.6 0 0 0 4.2 1.3V7.8a4.6 4.6 0 0 1-3.1-2Z"/></svg>`;

const state = { info: null, quality: null, tab: "video", polling: null, busy: false };

/* ───────────── small utilities ───────────── */
function toast(msg, isError = false, ms = 2800) {
  toastEl.textContent = msg;
  toastEl.classList.toggle("err", !!isError);
  toastEl.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => toastEl.classList.remove("show"), ms);
}

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.classList.add("show");
}
function clearError() {
  errorBox.textContent = "";
  errorBox.classList.remove("show");
}

function detect(u) {
  const s = (u || "").toLowerCase();
  if (/youtu\.?be/.test(s)) return "youtube";
  if (/tiktok\.com/.test(s)) return "tiktok";
  return "";
}

function setIcon(kind) {
  inputIcon.classList.remove("yt", "tt");
  if (kind === "youtube") {
    inputIcon.innerHTML = YT_ICON;
    inputIcon.classList.add("yt");
  } else if (kind === "tiktok") {
    inputIcon.innerHTML = TT_ICON;
    inputIcon.classList.add("tt");
  } else {
    inputIcon.innerHTML = LINK_ICON;
  }
}

function setLoading(on) {
  state.busy = on;
  submitBtn.classList.toggle("loading", on);
  submitBtn.disabled = on;
  submitBtn.querySelector(".cta-label").textContent = on ? "Reading link…" : "Get video";
}

/* nav elevation on scroll */
const nav = $("nav");
const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 8);
window.addEventListener("scroll", onScroll, { passive: true });
onScroll();

/* year */
$("year").textContent = new Date().getFullYear();

/* ───────────── input ───────────── */
urlInput.addEventListener("input", () => {
  setIcon(detect(urlInput.value));
  if (errorBox.classList.contains("show")) clearError();
});

urlInput.addEventListener("paste", () => {
  setTimeout(() => {
    setIcon(detect(urlInput.value));
    const v = urlInput.value.trim();
    if (detect(v) && v.length > 20) form.requestSubmit();
  }, 60);
});

document.addEventListener("keydown", (e) => {
  const meta = e.metaKey || e.ctrlKey;
  if (meta && e.key.toLowerCase() === "v" && document.activeElement !== urlInput) {
    urlInput.focus();
  }
});

pasteBtn.addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (!text || !text.trim()) return toast("Clipboard is empty", true);
    urlInput.value = text.trim();
    setIcon(detect(urlInput.value));
    toast("Link pasted");
    if (detect(urlInput.value)) form.requestSubmit();
  } catch {
    urlInput.focus();
    toast("Allow clipboard access, or paste manually", true);
  }
});

/* ───────────── skeleton ───────────── */
function showSkeleton() {
  result.hidden = false;
  $("thumb").removeAttribute("src");
  $("thumb").style.display = "none";
  $("platBadge").textContent = "Loading";
  $("dur").hidden = true;
  $("title").textContent = "Reading video…";
  $("title").classList.add("skeleton");
  $("meta").innerHTML = `<span class="skeleton" style="width:74px;height:18px"></span>
                         <span class="skeleton" style="width:92px;height:18px"></span>`;
  $("sizeTag").textContent = "";
  $("wmPill").textContent = "Checking…";
  $("chips").innerHTML = Array.from({ length: 6 })
    .map(() => `<span class="skeleton" style="height:58px"></span>`)
    .join("");
  $("summaryFormat").textContent = "—";
  $("summarySize").textContent = "Loading…";
  $("downloadBtn").disabled = true;
  $("progressBox").hidden = true;
  $("doneBox").hidden = true;
}

/* ───────────── fetch info ───────────── */
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.busy) return;
  clearError();

  const url = urlInput.value.trim();
  if (!url) {
    showError("Paste a TikTok or YouTube link first.");
    urlInput.focus();
    return;
  }
  if (!detect(url)) {
    showError("That doesn't look like a YouTube or TikTok link. Try again.");
    return;
  }

  setLoading(true);
  showSkeleton();
  try {
    const res = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "Could not read that link.");
    renderResult(data);
    result.scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (err) {
    result.hidden = true;
    showError(err.message || "Something went wrong. Try another link.");
  } finally {
    setLoading(false);
  }
});

/* ───────────── render ───────────── */
function renderResult(info) {
  state.info = info;
  stopPolling();

  const img = $("thumb");
  img.style.display = "block";
  if (info.thumbnail) {
    img.onerror = () => { img.style.display = "none"; };
    img.src = info.thumbnail;
    img.alt = info.title || "";
  } else {
    img.style.display = "none";
  }

  const dur = $("dur");
  dur.textContent = info.duration || "";
  dur.hidden = !info.duration;

  $("platBadge").textContent = info.platform === "youtube" ? "YouTube" : "TikTok";

  const titleEl = $("title");
  titleEl.classList.remove("skeleton");
  titleEl.textContent = info.title || "Untitled";

  const meta = $("meta");
  meta.innerHTML = "";
  [info.uploader, info.views && `${info.views} views`, info.likes && `${info.likes} likes`]
    .filter(Boolean)
    .forEach((b) => {
      const s = document.createElement("span");
      s.textContent = b;
      meta.appendChild(s);
    });

  const video = (info.qualities || []).filter((q) => q.type === "video");
  const audio = (info.qualities || []).filter((q) => q.type === "audio");

  const wmPill = $("wmPill");
  const free = info.watermark_free !== false;
  wmPill.classList.toggle("warn", !free);
  wmPill.innerHTML = free
    ? `<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12.5l5 5L20 6.5"/></svg> No watermark`
    : `<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 8v5M12 16.5v.5"/><circle cx="12" cy="12" r="9"/></svg> Watermark detected`;
  wmPill.title = free
    ? "Clean source stream selected — no platform logo is burned in."
    : "This link only exposes a watermarked stream. Try another upload of the same video.";

  const tip = $("tipLine");
  tip.classList.toggle("warn", !free);
  $("tipText").textContent = free
    ? "Watermark-free stream selected · files auto-delete after 45 minutes."
    : "This source only offers a watermarked stream — quality is unaffected.";

  state.tab = video.length ? "video" : "audio";
  syncTabs();
  renderChips();

  $("progressBox").hidden = true;
  $("doneBox").hidden = true;
  $("downloadBtn").disabled = false;
}

function syncTabs() {
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.tab === state.tab;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    if (tab.dataset.tab === state.tab) return;
    state.tab = tab.dataset.tab;
    syncTabs();
    renderChips();
  });
});

function currentList() {
  if (!state.info) return [];
  return (state.info.qualities || []).filter((q) => q.type === state.tab);
}

function renderChips() {
  const list = currentList();
  const box = $("chips");
  box.innerHTML = "";

  if (!list.length) {
    box.innerHTML = `<span style="font-size:12px;color:var(--ink-4);grid-column:1/-1">No ${state.tab} options for this link.</span>`;
    $("downloadBtn").disabled = true;
    $("summaryFormat").textContent = "—";
    $("summarySize").textContent = "Not available";
    $("sizeTag").textContent = "";
    return;
  }

  $("downloadBtn").disabled = false;
  list.forEach((q) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.dataset.id = q.id;
    b.title = q.sub || "";
    b.innerHTML = `<strong>${q.label}</strong><small>${q.size || q.sub || ""}</small>`;
    b.addEventListener("click", () => selectQuality(q));
    box.appendChild(b);
  });

  const preferred = list.find((q) => q.best) || list[0];
  selectQuality(preferred);
}

function selectQuality(q) {
  state.quality = q;
  document.querySelectorAll(".chip").forEach((c) => {
    c.classList.toggle("active", c.dataset.id === q.id);
  });

  const isAudio = q.type === "audio";
  $("summaryFormat").textContent = isAudio ? `${q.label} · audio` : `MP4 · ${q.label}`;
  $("summarySize").textContent = q.size ? `≈ ${q.size}` : (q.sub || "Size shown after start");
  $("downloadBtnLabel").textContent = isAudio ? `Download ${q.label}` : "Download";
  $("sizeTag").textContent = q.best ? "Best" : (q.sub ? q.sub.split(" · ")[0] : "");

  $("progressBox").hidden = true;
  $("doneBox").hidden = true;
  $("downloadBtn").disabled = false;
}

/* ───────────── download ───────────── */
$("downloadBtn").addEventListener("click", async () => {
  if (!state.info || !state.quality) return;
  const btn = $("downloadBtn");
  const box = $("progressBox");
  const fill = $("barFill");

  btn.disabled = true;
  $("doneBox").hidden = true;
  box.hidden = false;
  fill.style.width = "0%";
  $("progressPct").textContent = "0%";
  $("progressStage").textContent = "Starting…";
  $("progressSub").textContent = "Preparing your file…";

  try {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: state.info.url,
        quality: state.quality.id,
        title: state.info.title,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || "Could not start the download.");
    pollJob(data.job_id);
  } catch (err) {
    box.hidden = true;
    btn.disabled = false;
    toast(err.message || "Download failed", true);
  }
});

function pollJob(jobId) {
  stopPolling();
  const fill = $("barFill");
  const pct = $("progressPct");
  const stage = $("progressStage");
  const sub = $("progressSub");

  const tick = async () => {
    try {
      const res = await fetch(`/api/job/${jobId}`);
      const j = await res.json();
      if (!res.ok) throw new Error(j.error || "Job lost.");

      if (j.status === "error") {
        stopPolling();
        $("progressBox").hidden = true;
        $("downloadBtn").disabled = false;
        toast(j.error || "Download failed", true, 4200);
        return;
      }

      if (j.status === "done") {
        stopPolling();
        fill.style.width = "100%";
        pct.textContent = "100%";
        stage.textContent = "Ready";
        sub.textContent = "Your file is ready to save.";
        $("downloadBtn").disabled = false;
        $("doneName").textContent = j.filename || "video.mp4";
        $("doneSize").textContent = j.size ? `${j.size} · verified` : "ready";
        const save = $("saveBtn");
        save.href = `/api/file/${jobId}`;
        save.setAttribute("download", j.filename || "thikka-video.mp4");
        $("progressBox").hidden = true;
        $("doneBox").hidden = false;
        toast(
          j.watermark_free === false
            ? "Download complete — source had a watermark"
            : "Download complete — no watermark · tap “Save file”"
        );
        return;
      }

      const p = Math.max(0, Math.min(99.9, j.progress || 0));
      fill.style.width = p + "%";
      pct.textContent = p.toFixed(0) + "%";

      if (j.stage === "processing") {
        stage.textContent = "Processing…";
        sub.textContent = "Merging video + audio with ffmpeg…";
      } else if (j.stage === "probing") {
        stage.textContent = "Preparing…";
        sub.textContent = "Reading available streams…";
      } else if (j.stage === "queued") {
        stage.textContent = "Queued…";
        sub.textContent = "Waiting for a slot…";
      } else {
        stage.textContent = "Downloading…";
        const bits = [];
        if (j.speed) bits.push(j.speed);
        if (j.eta) bits.push(`ETA ${j.eta}`);
        sub.textContent = bits.join(" · ") || "Fetching stream…";
      }
    } catch (err) {
      stopPolling();
      $("progressBox").hidden = true;
      $("downloadBtn").disabled = false;
      toast(err.message || "Connection lost", true);
    }
  };

  tick();
  state.polling = setInterval(tick, 900);
}

function stopPolling() {
  if (state.polling) clearInterval(state.polling);
  state.polling = null;
}

/* ───────────── reset ───────────── */
$("resetBtn").addEventListener("click", () => {
  stopPolling();
  result.hidden = true;
  state.info = null;
  state.quality = null;
  urlInput.value = "";
  setIcon("");
  clearError();
  $("progressBox").hidden = true;
  $("doneBox").hidden = true;
  window.scrollTo({ top: 0, behavior: "smooth" });
  setTimeout(() => urlInput.focus({ preventScroll: true }), 420);
});

/* share-link support: /?url=... */
const qp = new URLSearchParams(location.search).get("url");
if (qp) {
  urlInput.value = qp;
  setIcon(detect(qp));
  form.requestSubmit();
}
setIcon("");
"""

NO_CACHE = {"Cache-Control": "no-store"}


@app.get("/", response_class=HTMLResponse)
def page_index():
    return HTMLResponse(INDEX_HTML, headers=NO_CACHE)


@app.get("/styles.css")
def page_css():
    return Response(STYLES_CSS, media_type="text/css", headers=NO_CACHE)


@app.get("/app.js")
def page_js():
    return Response(APP_JS, media_type="application/javascript", headers=NO_CACHE)


@app.get("/favicon.ico")
def page_favicon():
    return Response(status_code=204)
