import os
import re
import json
import glob
import time
import uuid
import stat
import base64
import shutil
import logging
import secrets
import zipfile
import subprocess
import urllib.request
import random
import threading
import asyncio
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()

import yt_dlp
import imageio_ffmpeg
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Header, Depends
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("downloader")

# ------------------------------------------------------------------
# Rate limiting (per-IP). Adjust limits to taste.
# ------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Social Media Downloader API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Lock this down to your real frontend domain(s) in production.
# "*" means literally anyone on the internet can call this API from a browser.
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# API key auth
# Set API_KEY as a Railway environment variable. Anyone calling the
# API must send it back in the "X-API-Key" header.
# ------------------------------------------------------------------
API_KEY = os.environ.get("API_KEY")  # if unset, auth is skipped (dev mode)


def require_api_key(x_api_key: str = Header(default=None)):
    if API_KEY is None:
        return  # no key configured -> auth disabled, useful for local dev
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ------------------------------------------------------------------
# Storage locations
# ------------------------------------------------------------------
BASE_DIR = os.path.join(os.getcwd(), "downloads")
COOKIES_DIR = os.path.join(os.getcwd(), "cookies")
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(COOKIES_DIR, exist_ok=True)
TASKS_FILE = os.path.join(BASE_DIR, "tasks.json")

logger.info(f"BASE_DIR resolved to: {BASE_DIR}")
logger.info(f"COOKIES_DIR resolved to: {COOKIES_DIR}")

# ------------------------------------------------------------------
# FFmpeg presence check.
#
# yt-dlp needs ffmpeg to MUX separately-downloaded video and audio
# DASH streams into one file (Instagram/YouTube/etc always serve
# these as separate streams). Without it, yt-dlp downloads both
# streams as loose fragment files (e.g. "<id>.fdash-....m4a" and
# "<id>.fdash-....v.mp4") and never combines them - which is exactly
# what produced the video-only files reported in the bug report.
#
# Rather than relying on a system-level ffmpeg install (apt-get /
# nixpacks.toml / Dockerfile), we use `imageio-ffmpeg`, which ships a
# self-contained static ffmpeg binary and downloads it automatically
# at `pip install` time - no OS package manager access needed, which
# is convenient on platforms like Railway where you may not control
# the build image. `imageio_ffmpeg.get_ffmpeg_exe()` returns the path
# to that binary so we can hand it to yt-dlp explicitly via
# --ffmpeg-location (it won't be on PATH, so yt-dlp can't find it on
# its own).
#
# Make sure "imageio-ffmpeg" is in requirements.txt.
# ------------------------------------------------------------------
try:
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    FFMPEG_AVAILABLE = bool(FFMPEG_PATH and os.path.exists(FFMPEG_PATH))
except Exception as e:
    FFMPEG_PATH = None
    FFMPEG_AVAILABLE = False
    logger.error(f"imageio_ffmpeg failed to provide an ffmpeg binary: {e}")

if not FFMPEG_AVAILABLE:
    logger.error(
        "ffmpeg was NOT found (imageio-ffmpeg did not provide a working "
        "binary). Video+audio merging will fail and downloads will be "
        "video-only or audio-only. Confirm 'imageio-ffmpeg' is in "
        "requirements.txt and that it installed successfully in the build "
        "logs."
    )
else:
    logger.info(f"ffmpeg found via imageio-ffmpeg at: {FFMPEG_PATH}")

# ------------------------------------------------------------------
# Deno bootstrap.
#
# yt-dlp's current JS-challenge solver (EJS, used for YouTube's
# "n"/nsig challenge) is built primarily around Deno. Rather than
# requiring a nixpacks.toml / Dockerfile change to install it at the
# OS level, we just download the Deno binary directly here at
# startup - the same idea as imageio-ffmpeg above, just done by hand
# since there's no pip package that bundles Deno.
#
# This downloads a ~30-40MB zip from Deno's GitHub releases once per
# container start (the filesystem is ephemeral on Railway, so it
# can't be cached across deploys/restarts - that's fine, it only
# takes a few seconds) and adds it to PATH for this process, which
# subprocess.Popen() calls below inherit automatically.
# ------------------------------------------------------------------
DENO_DIR = os.path.join(os.getcwd(), "bin")
DENO_PATH = os.path.join(DENO_DIR, "deno")
DENO_RELEASE_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"


def ensure_deno_available() -> bool:
    # Already on PATH (e.g. someone did install it at the OS level) - nothing to do.
    existing = shutil.which("deno")
    if existing:
        logger.info(f"deno already available on PATH at: {existing}")
        return True

    # Already downloaded by us in this run.
    if os.path.exists(DENO_PATH):
        os.environ["PATH"] = DENO_DIR + os.pathsep + os.environ.get("PATH", "")
        return True

    try:
        os.makedirs(DENO_DIR, exist_ok=True)
        zip_path = os.path.join(DENO_DIR, "deno.zip")
        logger.info(f"Downloading Deno from {DENO_RELEASE_URL} ...")
        urllib.request.urlretrieve(DENO_RELEASE_URL, zip_path)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(DENO_DIR)
        os.remove(zip_path)

        if not os.path.exists(DENO_PATH):
            logger.error(f"Deno zip extracted but no 'deno' binary found in {DENO_DIR}")
            return False

        st = os.stat(DENO_PATH)
        os.chmod(DENO_PATH, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        os.environ["PATH"] = DENO_DIR + os.pathsep + os.environ.get("PATH", "")
        logger.info(f"Deno installed at {DENO_PATH}")
        return True
    except Exception as e:
        logger.error(
            f"Failed to auto-download Deno: {e}. YouTube nsig-challenge "
            "solving will fall back to Node (less reliable) or fail. "
            "Check that this container has outbound internet access to "
            "github.com."
        )
        return False


DENO_AVAILABLE = ensure_deno_available()

# ------------------------------------------------------------------
# Cookie setup
#
# Don't commit cookie .txt files to your repo. Instead, base64-encode
# them and store as Railway env vars, e.g.:
#
#   base64 -w0 www.youtube.com_cookies.txt   -> paste into YOUTUBE_COOKIES_B64
#   base64 -w0 www.instagram.com_cookies.txt -> paste into INSTAGRAM_COOKIES_B64
#   base64 -w0 www.tiktok.com_cookies.txt    -> paste into TIKTOK_COOKIES_B64
#
# On startup we decode those env vars back into real files inside the
# container (which lives only as long as the deploy - nothing persists
# to git or a public image layer).
# ------------------------------------------------------------------
COOKIE_ENV_MAP = {
    "youtube": "YOUTUBE_COOKIES_B64",
    "instagram": "INSTAGRAM_COOKIES_B64",
    "tiktok": "TIKTOK_COOKIES_B64",
}


def materialize_cookie_files():
    for platform, env_name in COOKIE_ENV_MAP.items():
        b64_value = os.environ.get(env_name)
        target_path = os.path.join(COOKIES_DIR, f"{platform}.txt")
        if b64_value:
            try:
                raw = base64.b64decode(b64_value)
                with open(target_path, "wb") as f:
                    f.write(raw)
                os.chmod(target_path, 0o600)  # owner read/write only
                logger.info(f"Wrote cookies for {platform} -> {target_path}")
            except Exception as e:
                logger.error(f"Failed to decode {env_name}: {e}")
        else:
            logger.info(f"No {env_name} set, {platform} downloads will be unauthenticated")


materialize_cookie_files()


def cookie_file_for_url(url: str):
    host = urlparse(url).netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        path = os.path.join(COOKIES_DIR, "youtube.txt")
    elif "instagram.com" in host:
        path = os.path.join(COOKIES_DIR, "instagram.txt")
    elif "tiktok.com" in host:
        path = os.path.join(COOKIES_DIR, "tiktok.txt")
    else:
        return None
    return path if os.path.exists(path) else None


FACEBOOK_SHARE_RE = re.compile(r'facebook\.com/share/', re.I)


def resolve_redirect_url(url: str, timeout: int = 10) -> str:
    """
    Follows HTTP redirects and returns the final URL. Used for share-link
    formats (e.g. facebook.com/share/v/...) that only match yt-dlp's
    Generic extractor before redirecting to a real, extractor-supported
    URL. is_allowed_url() intentionally excludes Generic matches (SSRF
    protection), so we resolve first and validate the real destination.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl()


def normalize_url(url: str) -> str:
    """Resolve share-link redirects to their real destination before
    validation/extraction, so URLs that only look valid via yt-dlp's
    Generic extractor (which is deliberately excluded from
    is_allowed_url for SSRF safety) get a fair check against their
    real target."""
    if FACEBOOK_SHARE_RE.search(url):
        try:
            return resolve_redirect_url(url)
        except Exception as e:
            logger.warning(f"Failed to resolve redirect for {url}: {e}")
    return url


AUTO_DELETE_SECONDS = int(os.environ.get("DOWNLOAD_EXPIRY_SECONDS", os.environ.get("AUTO_DELETE_SECONDS", "1500")))
MAX_DOWNLOAD_SIZE_MB = int(os.environ.get("MAX_DOWNLOAD_SIZE_MB", "500"))
DOWNLOAD_CONCURRENCY = int(os.environ.get("DOWNLOAD_CONCURRENCY", "4"))
METADATA_CACHE_SECONDS = int(os.environ.get("METADATA_CACHE_SECONDS", "300"))
DISK_SAFETY_BUFFER_MB = int(os.environ.get("DISK_SAFETY_BUFFER_MB", "100"))
DISK_SAFETY_BUFFER_BYTES = DISK_SAFETY_BUFFER_MB * 1024 * 1024


# ------------------------------------------------------------------
# Download queue / concurrency control
# ------------------------------------------------------------------
_download_counter = 0
_download_condition = threading.Condition()


def _acquire_download_slot():
    global _download_counter
    with _download_condition:
        while _download_counter >= DOWNLOAD_CONCURRENCY:
            _download_condition.wait()
        _download_counter += 1


def _release_download_slot():
    global _download_counter
    with _download_condition:
        _download_counter -= 1
        _download_condition.notify_all()


# ------------------------------------------------------------------
# Metadata cache
# ------------------------------------------------------------------
_metadata_cache = {}
_metadata_cache_lock = threading.Lock()


def get_cached_metadata(url: str):
    with _metadata_cache_lock:
        entry = _metadata_cache.get(url)
        if entry:
            ts, info, fetch_duration = entry
            if time.time() - ts < METADATA_CACHE_SECONDS:
                return info, fetch_duration
            del _metadata_cache[url]
    return None, None


def set_cached_metadata(url: str, info: dict, fetch_duration: float = 0.0):
    with _metadata_cache_lock:
        _metadata_cache[url] = (time.time(), info, fetch_duration)


# ------------------------------------------------------------------
# Metadata extraction
# ------------------------------------------------------------------
def fetch_metadata(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "no_playlist": True,
    }
    cookie_path = cookie_file_for_url(url)
    if cookie_path:
        ydl_opts["cookiefile"] = cookie_path
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("Could not extract metadata")

    # Instagram Stories (and some other multi-item URLs) return a
    # playlist wrapper with the real format/duration/thumbnail data
    # nested inside entries[0], not at the top level. Unwrap the first
    # entry so downstream code (build_quality_options etc.) sees a
    # normal flat video dict.
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if entries and entries[0]:
            first = dict(entries[0])
            first["_playlist_title"] = info.get("title")
            first["_playlist_id"] = info.get("id")
            info = first

    return info


def estimate_filesize(fmt: dict, duration: float) -> int | None:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return int(size)

    tbr = fmt.get("tbr")
    if tbr and duration and duration > 0:
        return int(tbr * 1000 * duration / 8)

    vbr = fmt.get("vbr") or 0
    abr = fmt.get("abr") or 0
    if (vbr or abr) and duration and duration > 0:
        return int((vbr + abr) * 1000 * duration / 8)

    return None


def build_quality_options(info: dict) -> list[dict]:
    formats = info.get("formats", [])
    duration = info.get("duration") or 0
    video_by_height = {}
    audio_by_bitrate = {}

    for fmt in formats:
        fmt_id = fmt.get("format_id")
        if not fmt_id:
            continue

        vcodec = fmt.get("vcodec", "none")
        acodec = fmt.get("acodec", "none")

        if vcodec != "none":
            height = fmt.get("height")
            if not height:
                continue
            res = f"{height}p"
            size = estimate_filesize(fmt, duration)
            if size is None:
                size = max(500_000, int((fmt.get("tbr") or 500) * 1000 * (duration or 10) / 8))
            if size > MAX_DOWNLOAD_SIZE_MB * 1024 * 1024:
                continue
            if res not in video_by_height or size > video_by_height[res]["filesize"]:
                video_by_height[res] = {
                    "id": res,
                    "label": res,
                    "type": "video",
                    "height": height,
                    "filesize": size,
                    "format_id": fmt_id,
                }
        elif acodec != "none":
            abr = fmt.get("abr") or fmt.get("audio_bitrate")
            if abr is None:
                continue
            abr_int = int(abr)
            std_abr = min([x for x in (320, 192, 128) if x <= abr_int], default=abr_int)
            size = estimate_filesize(fmt, duration)
            if size is None:
                size = max(200_000, int(abr_int * 1000 * (duration or 10) / 8))
            if size > MAX_DOWNLOAD_SIZE_MB * 1024 * 1024:
                continue
            key = f"mp3-{std_abr}"
            if key not in audio_by_bitrate or size > audio_by_bitrate[key]["filesize"]:
                audio_by_bitrate[key] = {
                    "id": key,
                    "label": f"MP3 {std_abr} kbps",
                    "type": "audio",
                    "bitrate": std_abr,
                    "filesize": size,
                    "format_id": fmt_id,
                }

    video_list = sorted(video_by_height.values(), key=lambda x: x.get("height", 0), reverse=True)
    audio_list = sorted(audio_by_bitrate.values(), key=lambda x: x.get("bitrate", 0), reverse=True)

    if not audio_list:
        for std_abr in (320, 192, 128):
            size_est = max(200_000, int(std_abr * 1000 * (duration or 10) / 8))
            audio_list.append({
                "id": f"mp3-{std_abr}",
                "label": f"MP3 {std_abr} kbps",
                "type": "audio",
                "bitrate": std_abr,
                "filesize": size_est,
                "format_id": None,
            })

    all_opts = video_list + audio_list

    if all_opts:
        all_opts[0]["recommended"] = True

    return all_opts


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def build_frontend_qualities(qualities: list[dict]) -> list[dict]:
    result = []
    for q in qualities:
        entry = {
            "id": q["id"],
            "label": q["label"],
            "type": q["type"],
            "estimated_size": format_bytes(q["filesize"]),
        }
        if q.get("recommended"):
            entry["recommended"] = True
        result.append(entry)
    return result


# ------------------------------------------------------------------
# Disk space management
# ------------------------------------------------------------------
def ensure_disk_space(required_bytes: int, media_type: str = "video") -> bool:
    usage = shutil.disk_usage(BASE_DIR)
    free = usage.free

    if media_type == "audio":
        target = required_bytes + DISK_SAFETY_BUFFER_BYTES // 2
    else:
        target = required_bytes * 2 + DISK_SAFETY_BUFFER_BYTES

    if free >= target:
        return True

    logger.warning(f"Low disk space: need {target}, have {free}. Cleaning up old files...")

    for _ in range(5):
        tasks = load_tasks()
        completed_files = []
        for task_id, task in tasks.items():
            if task.get("status") == "completed":
                fp = task.get("file_path")
                if fp and os.path.exists(fp):
                    completed_files.append((task.get("created_at", 0), fp, task_id))

        completed_files.sort(key=lambda x: x[0])

        for created_at, fp, task_id in completed_files:
            try:
                size = os.path.getsize(fp)
                os.remove(fp)
                logger.info(f"Deleted old file to free space: {fp} ({size} bytes)")
                free += size
                if free >= target:
                    return True
            except Exception as e:
                logger.warning(f"Failed to delete {fp}: {e}")

        cleanup_old_files()
        usage = shutil.disk_usage(BASE_DIR)
        free = usage.free
        if free >= target:
            return True

    return free >= target


# ------------------------------------------------------------------
# URL validation.
#
# The point isn't to restrict which platforms are allowed - yt-dlp
# supports 1800+ sites and there's no reason to hand-maintain a list
# that becomes stale immediately. The point is to stop this endpoint
# being usable as an open URL-fetch proxy: without any check, someone
# could pass http://169.254.169.254/... (cloud metadata endpoints),
# internal network addresses, file:// URLs, etc, and yt-dlp's generic
# extractor would happily try to fetch them - a classic SSRF risk on
# a public endpoint.
#
# So instead of a fixed domain list, we ask yt-dlp itself: "do you
# have a real, non-generic extractor for this URL?" This scales
# automatically to everything yt-dlp supports, and still blocks
# anything yt-dlp wouldn't recognize as an actual video/media page.
# ------------------------------------------------------------------
_YDL_FOR_CHECK = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
_allowed_url_cache = {}
_allowed_url_lock = threading.Lock()


def is_allowed_url(url: str) -> bool:
    url = normalize_url(url)
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        netloc = parsed.netloc.lower()
    except Exception:
        return False

    with _allowed_url_lock:
        if netloc in _allowed_url_cache:
            return _allowed_url_cache[netloc]

    allowed = False
    try:
        for ie in _YDL_FOR_CHECK._ies.values():
            if ie.suitable(url) and ie.ie_key() not in ("Generic",):
                allowed = True
                break
    except Exception as e:
        logger.error(f"Extractor check failed for {url}: {e}")
        return False

    with _allowed_url_lock:
        _allowed_url_cache[netloc] = allowed

    return allowed


# ------------------------------------------------------------------
# Task persistence (in-memory cache with periodic / status-based JSON sync)
# ------------------------------------------------------------------
_tasks_cache = {}
_tasks_cache_loaded = False
_tasks_lock = threading.Lock()


def load_tasks() -> dict:
    global _tasks_cache, _tasks_cache_loaded
    with _tasks_lock:
        if not _tasks_cache_loaded:
            if os.path.exists(TASKS_FILE):
                try:
                    with open(TASKS_FILE, "r") as f:
                        _tasks_cache = json.load(f)
                except Exception as e:
                    logger.error(f"Failed to load tasks.json: {e}")
                    _tasks_cache = {}
            _tasks_cache_loaded = True
        return dict(_tasks_cache)


def save_task(task_id: str, data: dict, force_disk: bool = False):
    global _tasks_cache
    with _tasks_lock:
        if not _tasks_cache_loaded:
            load_tasks()

        prev = _tasks_cache.get(task_id, {})
        _tasks_cache[task_id] = dict(data)

        prev_status = prev.get("status")
        curr_status = data.get("status")
        prev_prog = prev.get("progress", 0)
        curr_prog = data.get("progress", 0)

        # Sync to disk on status changes, completed/failed, or >= 10% progress steps to reduce disk I/O
        should_write = (
            force_disk or
            prev_status != curr_status or
            abs(curr_prog - prev_prog) >= 10 or
            curr_prog in (0, 100)
        )

        if should_write:
            try:
                with open(TASKS_FILE, "w") as f:
                    json.dump(_tasks_cache, f)
                logger.info(f"[{task_id}] status saved -> {data.get('status')} "
                            f"progress={data.get('progress')}")
            except Exception as e:
                logger.error(f"[{task_id}] Failed to save tasks.json: {e}")


class DownloadRequest(BaseModel):
    url: str
    media_type: str = "video"
    quality: str = None


class FrontendQuality(BaseModel):
    id: str
    label: str
    type: str
    estimated_size: str
    recommended: bool = False


class MetadataRequest(BaseModel):
    url: str


class MetadataResponse(BaseModel):
    title: str
    thumbnail: str
    duration: int
    qualities: list[FrontendQuality]


@app.get("/")
def home():
    return {"message": "API Working"}


@app.post("/get-metadata", dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute")
async def get_metadata(request: Request, body: MetadataRequest):
    body.url = normalize_url(body.url)
    if not is_allowed_url(body.url):
        raise HTTPException(status_code=400, detail="URL host is not supported")

    start_t = time.time()
    info, fetch_duration = get_cached_metadata(body.url)
    if not info:
        try:
            info = fetch_metadata(body.url)
            fetch_duration = round(time.time() - start_t, 3)
        except Exception as e:
            logger.error(f"Metadata fetch failed for {body.url}: {e}")
            raise HTTPException(status_code=400, detail=f"Failed to fetch metadata: {e}")
        set_cached_metadata(body.url, info, fetch_duration)

    thumbnail = ""
    if info.get("thumbnail"):
        thumbnail = info["thumbnail"]
    elif info.get("thumbnails"):
        thumbnails = info["thumbnails"]
        if thumbnails:
            thumbnail = thumbnails[-1].get("url", "")

    duration = int(info.get("duration") or 0)
    raw_qualities = build_quality_options(info)
    frontend_qualities = build_frontend_qualities(raw_qualities)

    return MetadataResponse(
        title=info.get("title", ""),
        thumbnail=thumbnail,
        duration=duration,
        qualities=[FrontendQuality(**q) for q in frontend_qualities],
    )


@app.get("/debug/yt-dlp-version", dependencies=[Depends(require_api_key)])
def debug_ytdlp_version():
    result = {}
    try:
        r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=15)
        result["yt_dlp_version"] = r.stdout.strip() or r.stderr.strip()
    except Exception as e:
        result["yt_dlp_version"] = None
        result["yt_dlp_error"] = str(e)

    try:
        r = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=15)
        result["node_version"] = r.stdout.strip() or r.stderr.strip()
    except Exception as e:
        result["node_version"] = None
        result["node_error"] = str(e)

    result["deno_auto_download_succeeded"] = DENO_AVAILABLE
    try:
        r = subprocess.run(["deno", "--version"], capture_output=True, text=True, timeout=15)
        result["deno_version"] = (r.stdout.strip() or r.stderr.strip()).splitlines()[0]
    except Exception as e:
        result["deno_version"] = None
        result["deno_error"] = str(e)
        result["deno_note"] = (
            "Deno not found and the automatic download at startup failed "
            "(see server logs for the reason - usually no outbound internet "
            "access to github.com from this container). YouTube's nsig "
            "challenge solving will fall back to Node or fail."
        )

    # NEW: surface ffmpeg status directly, since a missing ffmpeg is the
    # most common cause of "video downloaded but has no audio".
    result["ffmpeg_available"] = FFMPEG_AVAILABLE
    result["ffmpeg_path"] = FFMPEG_PATH
    if FFMPEG_PATH:
        try:
            r = subprocess.run([FFMPEG_PATH, "-version"], capture_output=True, text=True, timeout=15)
            result["ffmpeg_version"] = (r.stdout.strip() or r.stderr.strip()).splitlines()[0]
        except Exception as e:
            result["ffmpeg_version"] = None
            result["ffmpeg_error"] = str(e)
    else:
        result["ffmpeg_version"] = None

    return result


@app.get("/debug/list-files", dependencies=[Depends(require_api_key)])
def debug_list_files():
    try:
        files = os.listdir(BASE_DIR)
    except Exception as e:
        files = [f"ERROR listing dir: {e}"]

    cookie_status = {}
    for platform in COOKIE_ENV_MAP:
        path = os.path.join(COOKIES_DIR, f"{platform}.txt")
        env_name = COOKIE_ENV_MAP[platform]
        if os.path.exists(path):
            stat = os.stat(path)
            cookie_status[platform] = {
                "env_var_set": bool(os.environ.get(env_name)),
                "file_exists": True,
                "size_bytes": stat.st_size,
                "looks_valid": stat.st_size > 50,  # a real Netscape cookie file is never this small
            }
        else:
            cookie_status[platform] = {
                "env_var_set": bool(os.environ.get(env_name)),
                "file_exists": False,
                "size_bytes": 0,
                "looks_valid": False,
            }

    return {"base_dir": BASE_DIR, "files": files, "cookies": cookie_status,
             "ffmpeg_available": FFMPEG_AVAILABLE, "ffmpeg_path": FFMPEG_PATH,
             "tasks": load_tasks()}


@app.post("/download", dependencies=[Depends(require_api_key)])
@limiter.limit("5/minute")
async def start_download(request: Request, body: DownloadRequest, background_tasks: BackgroundTasks):
    body.url = normalize_url(body.url)
    if not is_allowed_url(body.url):
        raise HTTPException(status_code=400, detail="URL host is not supported")

    if body.media_type not in ("video", "audio"):
        raise HTTPException(status_code=400, detail="media_type must be 'video' or 'audio'")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _acquire_download_slot)

    cleanup_old_files()

    task_id = str(uuid.uuid4())[:12]
    ext = ".mp3" if body.media_type == "audio" else ".mp4"
    file_path = os.path.join(BASE_DIR, f"{task_id}{ext}")

    logger.info(f"[{task_id}] New download requested. url={body.url} media_type={body.media_type} quality={body.quality}")

    created_at = time.time()
    auto_delete_seconds = random.randint(1200, 1800)

    start_meta_t = time.time()
    info, meta_duration = get_cached_metadata(body.url)
    if not info:
        try:
            info = fetch_metadata(body.url)
            meta_duration = round(time.time() - start_meta_t, 3)
            set_cached_metadata(body.url, info, meta_duration)
        except Exception as e:
            _release_download_slot()
            logger.warning(f"[{task_id}] Metadata fetch failed: {e}")
            raise HTTPException(status_code=400, detail=f"Failed to fetch metadata: {e}")

    save_task(task_id, {
        "status": "started",
        "progress": 0,
        "url": body.url,
        "file_path": file_path,
        "media_type": body.media_type,
        "quality": body.quality,
        "created_at": created_at,
        "auto_delete_seconds": auto_delete_seconds,
        "metadata_fetch_seconds": meta_duration or 0.0,
        "download_execution_seconds": 0.0,
        "transcode_seconds": 0.0,
        "total_backend_seconds": 0.0,
        "file_size_bytes": 0,
        "file_size_formatted": "0 B",
    })

    fmt = None
    raw_qualities = build_quality_options(info)
    if body.quality:
        for q in raw_qualities:
            if q["id"] == body.quality:
                fmt = q
                break
            if q["type"] == "video" and str(q.get("height", "")) == body.quality:
                fmt = q
                break
            try:
                if q["type"] == "video" and body.quality.endswith("p"):
                    if int(q.get("height", 0)) <= int(body.quality[:-1]):
                        fmt = q
                        break
            except (ValueError, TypeError):
                pass
    else:
        video_opts = [q for q in raw_qualities if q["type"] == "video"]
        audio_opts = [q for q in raw_qualities if q["type"] == "audio"]
        candidates = video_opts if body.media_type == "video" else audio_opts
        if candidates:
            fmt = candidates[0]

    required_bytes = 0
    if fmt:
        required_bytes = fmt.get("filesize") or 0

    if required_bytes <= 0:
        _release_download_slot()
        raise HTTPException(status_code=400, detail="Unable to estimate download size")

    media_type = body.media_type
    if media_type == "audio":
        target = required_bytes + DISK_SAFETY_BUFFER_BYTES // 2
    else:
        target = required_bytes * 2 + DISK_SAFETY_BUFFER_BYTES

    for _ in range(5):
        if ensure_disk_space(required_bytes, media_type):
            break
        logger.warning(f"[{task_id}] Disk space still insufficient after cleanup, retrying...")
        time.sleep(1)
    else:
        _release_download_slot()
        raise HTTPException(
            status_code=507,
            detail="Server storage is temporarily full. Please try again later."
        )

    background_tasks.add_task(download_task, body.url, task_id, file_path, media_type, body.quality)
    return {"task_id": task_id, "status": "started", "message": "Download started"}


def parse_progress_line(line: str):
    match = re.search(r"\[download\]\s+(\d{1,3}\.\d)%", line)
    if match:
        try:
            return int(float(match.group(1)))
        except ValueError:
            return None
    return None


def ensure_h264_playable(file_path: str, task_id: str) -> tuple[str, float]:
    """
    Some sources (notably Instagram Stories/Reels) only expose VP9/AV1
    video with no H.264 alternative. ffmpeg's MP4 muxer will silently
    accept VP9 in an .mp4 container without complaint, but many real
    players (WhatsApp, various Android/iOS apps) reject it outright -
    this is what caused "couldn't process video" on WhatsApp even
    though the file plays fine in VLC/ffprobe. Only re-encode when the
    codec genuinely isn't H.264, so normal YouTube/TikTok/Facebook
    downloads (already H.264 in the vast majority of cases) pay no
    extra cost.
    """
    if not FFMPEG_PATH:
        return file_path, 0.0
    start_t = time.time()
    try:
        probe = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-i", file_path],
            capture_output=True, text=True, timeout=15,
        )
        info = probe.stderr
        if "h264" in info.lower():
            return file_path, 0.0

        logger.info(f"[{task_id}] Non-H.264 video detected, transcoding for compatibility")
        transcoded_path = file_path + ".transcoded.mp4"
        result = subprocess.run(
            [
                FFMPEG_PATH, "-y", "-hide_banner", "-i", file_path,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-c:a", "copy",
                "-threads", "0",
                "-movflags", "+faststart",
                transcoded_path,
            ],
            capture_output=True, text=True, timeout=120,
        )
        transcode_duration = round(time.time() - start_t, 3)
        if result.returncode == 0 and os.path.exists(transcoded_path):
            os.replace(transcoded_path, file_path)
            logger.info(f"[{task_id}] Transcoded to H.264 successfully in {transcode_duration}s")
            return file_path, transcode_duration
        else:
            logger.warning(f"[{task_id}] Transcode failed, serving original file: {result.stderr[-500:]}")
            if os.path.exists(transcoded_path):
                os.remove(transcoded_path)
            return file_path, transcode_duration
    except Exception as e:
        logger.warning(f"[{task_id}] ensure_h264_playable check failed: {e}")
        return file_path, 0.0

    return file_path, 0.0


def cleanup_task_fragments(task_id: str, keep_path: str = None):
    """
    Remove any leftover per-stream fragment files for this task
    (e.g. "<task_id>.fdash-....m4a", "<task_id>.fdash-....v.mp4",
    "<task_id>.f137.mp4", etc). These are left behind whenever a
    merge fails to run (most commonly: ffmpeg missing) and, if not
    cleaned up, are exactly what caused the video-only file to be
    served in the original bug report - the old code would glob for
    "<task_id>.*" and pick one of these arbitrarily.
    """
    for path in glob.glob(os.path.join(BASE_DIR, f"{task_id}.*")):
        if path == TASKS_FILE:
            continue
        if keep_path and os.path.abspath(path) == os.path.abspath(keep_path):
            continue
        try:
            os.remove(path)
            logger.info(f"[{task_id}] Removed leftover fragment: {path}")
        except Exception as e:
            logger.warning(f"[{task_id}] Failed to remove fragment {path}: {e}")


def download_task(url: str, task_id: str, file_path: str, media_type: str = "video", quality: str = None):
    logger.info(f"[{task_id}] download_task() started media_type={media_type} quality={quality}")

    existing = load_tasks().get(task_id, {})
    existing.update({
        "status": "downloading",
        "progress": 0,
        "url": url,
        "file_path": file_path,
        "media_type": media_type,
    })
    save_task(task_id, existing)

    def _persist(**updates):
        current = load_tasks().get(task_id, {})
        current.update(updates)
        save_task(task_id, current)

    def build_cmd(player_client: str = None) -> list:
            c = [
                "yt-dlp",
                "--no-playlist",
                "--newline",
                "-N", "8",
                "--buffer-size", "64k",
                "--http-chunk-size", "10M",
                "-o", file_path,
                "--max-filesize", "500M",
                "--js-runtimes", "deno,node",
                "--remote-components", "ejs:github",
            ]
            if FFMPEG_PATH:
                c += ["--ffmpeg-location", FFMPEG_PATH]
            if player_client:
                c += ["--extractor-args", f"youtube:player_client={player_client}"]
            cookie_path = cookie_file_for_url(url)
            if cookie_path:
                c += ["--cookies", cookie_path]

            if media_type == "audio":
                c += [
                    "--extract-audio",
                    "--audio-format", "mp3",
                ]
                if quality and quality.startswith("mp3-"):
                    c += ["--audio-quality", quality.split("-")[1] + "K"]
                else:
                    c += ["--audio-quality", "0"]
                c += ["-f", "bestaudio/best"]
            else:
                c += [
                    "--merge-output-format", "mp4",
                ]
                if quality and quality.endswith("p"):
                    try:
                        height = int(quality[:-1])
                        c += [
                            "-f", f"bv*[height<={height}][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<={height}][vcodec^=h264]+ba/bv*[height<={height}]+ba/b",
                        ]
                    except ValueError:
                        c += ["-f", "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*[vcodec^=h264]+ba/bv*+ba/b"]
                else:
                    c += ["-f", "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*[vcodec^=h264]+ba/bv*+ba/b"]
                c += ["-S", "ext:mp4:m4a"]

            c.append(url)
            return c

    # ------------------------------------------------------------------
    # YouTube client-strategy list.
    #
    # CONTEXT: YouTube periodically forces "SABR-only" streaming for
    # specific player clients, causing yt-dlp to receive a format list
    # where every entry is unusable ("Requested format is not available"
    # even though the video plays fine in a browser). WHICH client(s) are
    # currently broken shifts every few weeks as YouTube and yt-dlp go
    # back and forth - see https://github.com/yt-dlp/yt-dlp/issues/12482.
    #
    # There is no single client combo that stays correct for more than a
    # few weeks at a time, so instead of picking one, we try several in
    # order and only give up if all of them fail. Whichever one currently
    # works differs by IP/region/YouTube A/B test, which is exactly why a
    # multi-strategy approach is more robust than hardcoding a "best" pick.
    #
    # Override entirely via YOUTUBE_PLAYER_CLIENT env var (comma-separated
    # strategies, each itself comma-separated client names, semicolon
    # between strategies) e.g. "android,web;tv_simply,web_safari" if you
    # find a combo that works better for your traffic and don't want to
    # redeploy code to change it.
    # ------------------------------------------------------------------
    default_strategies = [None, "android,web", "tv_simply,web", "web_safari,tv"]
    env_override = os.environ.get("YOUTUBE_PLAYER_CLIENT")
    if env_override:
        client_strategies = [s if s else None for s in env_override.split(";")]
    else:
        client_strategies = default_strategies

    is_youtube = "youtube.com" in url or "youtu.be" in url
    strategies_to_try = client_strategies if is_youtube else [None]

    cookie_path = cookie_file_for_url(url)
    if cookie_path:
        logger.info(f"[{task_id}] Using cookies file: {cookie_path}")
    else:
        logger.info(f"[{task_id}] No cookie file for this URL, downloading unauthenticated")

    last_progress = 0
    all_attempts_output = []  # every strategy's tail, so a final failure is fully explained
    TAIL_MAX = 20

    def run_attempt(cmd: list):
        """Runs one yt-dlp attempt. Returns (returncode, output_tail)."""
        nonlocal last_progress
        output_tail = []
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in process.stdout:
            line = line.rstrip()
            if not line:
                continue
            logger.info(f"[{task_id}] yt-dlp: {line}")
            output_tail.append(line)
            if len(output_tail) > TAIL_MAX:
                output_tail.pop(0)
            pct = parse_progress_line(line)
            if pct is not None and pct != last_progress:
                last_progress = pct
                _persist(status="downloading", progress=pct, url=url, file_path=file_path)
        returncode = process.wait(timeout=300)
        return returncode, output_tail

    RETRYABLE_MARKERS = (
        "Requested format is not available",
        "Only images are available for download",
        "forcing SABR streaming",
        "PO Token",
    )

    try:
        final_returncode = None
        final_tail = []

        for attempt_num, strategy in enumerate(strategies_to_try, start=1):
            cmd = build_cmd(strategy)
            logger.info(
                f"[{task_id}] Attempt {attempt_num}/{len(strategies_to_try)} "
                f"(player_client={strategy or 'yt-dlp default'})"
            )
            _persist(status="downloading", progress=last_progress, url=url, file_path=file_path)

            returncode, output_tail = run_attempt(cmd)
            final_returncode, final_tail = returncode, output_tail
            all_attempts_output.append(
                f"--- attempt {attempt_num} (player_client={strategy or 'default'}) ---\n"
                + "\n".join(output_tail)
            )

            if returncode == 0 and os.path.exists(file_path):
                break  # success - stop trying further strategies

            joined = "\n".join(output_tail)
            should_retry = any(marker in joined for marker in RETRYABLE_MARKERS)
            if not should_retry:
                logger.info(f"[{task_id}] Failure doesn't look client-related, not retrying other strategies")
                break
            logger.info(f"[{task_id}] Attempt {attempt_num} failed with a retryable format error, trying next client strategy")

        returncode = final_returncode
        logger.info(f"[{task_id}] yt-dlp exited with code {returncode} after {len(all_attempts_output)} attempt(s)")

        if returncode != 0:
            # BUGFIX: this used to only look at lines containing "ERROR",
            # but the actually-useful diagnostic for SABR/PO-token
            # failures is logged by yt-dlp as a WARNING ("YouTube is
            # forcing SABR streaming for this client..."), so it was
            # silently invisible in the saved error before. Now we surface
            # WARNING lines too, plus which client strategies were
            # attempted, so the failure message is fully self-explanatory.
            tail = final_tail
            important_lines = [
                l for l in tail
                if ("ERROR" in l or "WARNING" in l or "SABR" in l or "PO Token" in l or "PO token" in l)
            ]
            detail_lines = important_lines[-5:] if important_lines else tail[-6:]
            detail = "\n".join(detail_lines)[:1200]

            strategies_tried = [s or "yt-dlp default" for s in strategies_to_try[:len(all_attempts_output)]]
            error_msg = (
                f"Tried {len(all_attempts_output)} client strategy(ies): {strategies_tried}. "
                f"Last error: {detail or f'yt-dlp exited with code {returncode}, no output captured'}"
            )

            _persist(status="failed", progress=last_progress, url=url, file_path=file_path, error=error_msg)
            cleanup_task_fragments(task_id)
            return

        # ------------------------------------------------------------
        # IMPORTANT: only trust the EXACT expected output path here.
        #
        # Previously, if the exact file was missing, the code fell back
        # to `glob.glob(f"{task_id}.*")` and just grabbed the first
        # match - which, when the video+audio merge fails (e.g. ffmpeg
        # missing), returns one of the *unmerged single-stream fragment
        # files* (like "<task_id>.fdash-....v.mp4", video only). That
        # file would then get reported as "completed" and served to
        # users, which is exactly the no-audio bug that was reported.
        #
        # Now: if the exact merged file isn't there, we treat this as a
        # genuine failure and surface a clear, actionable error instead
        # of silently serving a broken file.
        # ------------------------------------------------------------
        if os.path.exists(file_path):
            transcode_duration = 0.0
            if media_type == "video":
                file_path, transcode_duration = ensure_h264_playable(file_path, task_id)
            size_bytes = os.path.getsize(file_path)
            size_formatted = format_bytes(size_bytes)
            completed_at = time.time()
            created_at = existing.get("created_at", time.time())

            download_exec_seconds = round(completed_at - download_start_time - transcode_duration, 3)
            total_backend_seconds = round(completed_at - created_at, 3)

            logger.info(f"[{task_id}] File confirmed on disk: {file_path} ({size_bytes} bytes). "
                        f"Download time: {download_exec_seconds}s, Transcode time: {transcode_duration}s, "
                        f"Total time: {total_backend_seconds}s")

            _persist(
                status="completed",
                progress=100,
                url=url,
                file_path=file_path,
                download_url=f"/download-file/{task_id}",
                completed_at=completed_at,
                download_execution_seconds=download_exec_seconds,
                transcode_seconds=transcode_duration,
                total_backend_seconds=total_backend_seconds,
                file_size_bytes=size_bytes,
                file_size_formatted=size_formatted,
            )
            cleanup_task_fragments(task_id, keep_path=file_path)
        else:
            leftover = glob.glob(os.path.join(BASE_DIR, f"{task_id}.*"))
            leftover = [m for m in leftover if not m.endswith(".json")]

            if leftover:
                # yt-dlp exited 0 but never produced the merged file -
                # almost always means ffmpeg is missing or failed, and
                # separate video/audio streams were left on disk instead.
                error_msg = (
                    "Video and audio downloaded as separate streams but were "
                    "never merged into one file (this usually means ffmpeg is "
                    "missing or failed on the server). "
                    f"ffmpeg_available={FFMPEG_AVAILABLE}. "
                    f"Leftover files: {[os.path.basename(m) for m in leftover]}"
                )
                logger.error(f"[{task_id}] {error_msg}")
                _persist(status="failed", progress=last_progress, url=url, file_path=file_path, error=error_msg)
                cleanup_task_fragments(task_id)
            else:
                _persist(status="failed", progress=last_progress, url=url, file_path=file_path, error="yt-dlp exited 0 but no output file was found")

    except subprocess.TimeoutExpired:
        _persist(status="failed", progress=last_progress, url=url, file_path=file_path, error="timeout")
        cleanup_task_fragments(task_id)
    except FileNotFoundError as e:
        _persist(status="failed", progress=last_progress, url=url, file_path=file_path, error=f"yt-dlp not found on server: {e}")
    except Exception as e:
        logger.exception(f"[{task_id}] Unexpected exception in download_task")
        _persist(status="failed", progress=last_progress, url=url, file_path=file_path, error=str(e))
        cleanup_task_fragments(task_id)
    finally:
        _release_download_slot()


@app.get("/status/{task_id}", dependencies=[Depends(require_api_key)])
def get_status(task_id: str):
    tasks = load_tasks()
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/task-metrics/{task_id}", dependencies=[Depends(require_api_key)])
def get_task_metrics(task_id: str):
    """
    Detailed timing and file size metrics for a download task:
    - metadata_fetch_seconds: Time taken to fetch URL info and variations
    - download_execution_seconds: Time taken to download stream fragments
    - transcode_seconds: Time taken for H.264 re-encoding (if run)
    - total_backend_seconds: Total time elapsed from task request to completion
    - file size in bytes and formatted (e.g. '22.9 MB')
    """
    tasks = load_tasks()
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    file_path = task.get("file_path")
    file_exists = bool(file_path and os.path.exists(file_path))
    file_size_bytes = task.get("file_size_bytes", 0)
    if file_exists and file_size_bytes == 0:
        try:
            file_size_bytes = os.path.getsize(file_path)
        except Exception:
            pass

    size_formatted = task.get("file_size_formatted") or format_bytes(file_size_bytes)

    return {
        "task_id": task_id,
        "status": task.get("status"),
        "progress": task.get("progress", 0),
        "url": task.get("url"),
        "media_type": task.get("media_type"),
        "quality": task.get("quality"),
        "created_at": task.get("created_at"),
        "completed_at": task.get("completed_at"),
        "timing": {
            "metadata_fetch_seconds": task.get("metadata_fetch_seconds", 0.0),
            "download_execution_seconds": task.get("download_execution_seconds", 0.0),
            "transcode_seconds": task.get("transcode_seconds", 0.0),
            "total_backend_seconds": task.get("total_backend_seconds", 0.0),
        },
        "file_info": {
            "file_path": file_path,
            "size_bytes": file_size_bytes,
            "size_formatted": size_formatted,
            "available": file_exists,
        },
        "error": task.get("error"),
    }


@app.get("/file-status/{task_id}", dependencies=[Depends(require_api_key)])
def file_status(task_id: str):
    """
    Cheap existence check - lets a client know whether a completed
    download is still retrievable before showing a "Download" button,
    without pulling the whole file just to find out.
    """
    tasks = load_tasks()
    task = tasks.get(task_id)
    if not task or task.get("status") != "completed":
        return {"available": False}

    file_path = task.get("file_path")
    return {"available": bool(file_path and os.path.exists(file_path))}


@app.get("/download-file/{task_id}", dependencies=[Depends(require_api_key)])
@limiter.limit("20/minute")
def serve_file(request: Request, task_id: str):
    tasks = load_tasks()
    task = tasks.get(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.get("status") != "completed":
        raise HTTPException(
            status_code=404,
            detail=f"File not ready. Current status: {task.get('status')}, progress: {task.get('progress')}",
        )

    # Only ever serve the exact recorded merged file - never fall back to
    # an arbitrary glob match, which is what let unmerged/video-only
    # fragments get served to users before.
    file_path = task.get("file_path")
    if file_path and os.path.exists(file_path):
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".mp3":
            return FileResponse(file_path, media_type="audio/mpeg", filename="audio.mp3")
        return FileResponse(file_path, media_type="video/mp4", filename="video.mp4")

    raise HTTPException(status_code=404, detail="File not found on disk (expired or lost on redeploy)")


# ------------------------------------------------------------------
# Basic disk hygiene: delete finished/failed files older than
# MAX_FILE_AGE_HOURS so a public-facing box doesn't fill its disk.
# Runs opportunistically on each new /download call.
# ------------------------------------------------------------------
MAX_FILE_AGE_HOURS = float(os.environ.get("MAX_FILE_AGE_HOURS", "2"))


def cleanup_old_files():
    now = time.time()
    tasks = load_tasks()

    for task_id, task in tasks.items():
        status = task.get("status")
        created_at = task.get("created_at", now)
        auto_delete_seconds = task.get("auto_delete_seconds", AUTO_DELETE_SECONDS)
        file_path = task.get("file_path")

        if status == "completed" and file_path and os.path.exists(file_path):
            if now - created_at > auto_delete_seconds:
                try:
                    size = os.path.getsize(file_path)
                    os.remove(file_path)
                    logger.info(f"Auto-deleted completed file after timeout: {file_path} ({size} bytes)")
                    task["status"] = "expired"
                    save_task(task_id, task)
                except Exception as e:
                    logger.warning(f"Auto-delete failed for {file_path}: {e}")
        elif status not in ("completed", "expired") and file_path and os.path.exists(file_path):
            try:
                if os.path.getmtime(file_path) < now - (MAX_FILE_AGE_HOURS * 3600):
                    os.remove(file_path)
                    logger.info(f"Cleaned up old non-completed file: {file_path}")
            except Exception as e:
                logger.warning(f"Cleanup failed for {file_path}: {e}")

    for path in glob.glob(os.path.join(BASE_DIR, "*")):
        if path == TASKS_FILE:
            continue
        try:
            if os.path.getmtime(path) < now - (MAX_FILE_AGE_HOURS * 3600):
                os.remove(path)
                logger.info(f"Cleaned up old orphaned file: {path}")
        except Exception as e:
            logger.warning(f"Cleanup failed for orphaned {path}: {e}")