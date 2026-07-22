import os
import re
import json
import glob
import time
import uuid
import base64
import shutil
import logging
import secrets
import subprocess
from urllib.parse import urlparse

import yt_dlp
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
# We check for it on startup and log loudly, because a missing
# ffmpeg binary otherwise fails silently deep inside yt-dlp.
# ------------------------------------------------------------------
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
if not FFMPEG_AVAILABLE:
    logger.error(
        "ffmpeg was NOT found on this system. Video+audio merging will fail "
        "and downloads will silently be video-only or audio-only. "
        "Add ffmpeg to the deployment image (e.g. nixpacks.toml "
        "[phases.setup] aptPkgs = [\"ffmpeg\"], or apt-get install ffmpeg "
        "in your Dockerfile)."
    )
else:
    logger.info("ffmpeg found, merging is available")

# ------------------------------------------------------------------
# Cookie setup
#
# Don't commit cookie .txt files to your repo. Instead, base64-encode
# them and store as Railway env vars, e.g.:
#
#   base64 -w0 www.youtube.com_cookies.txt   -> paste into YOUTUBE_COOKIES_B64
#   base64 -w0 www.instagram.com_cookies.txt -> paste into INSTAGRAM_COOKIES_B64
#
# On startup we decode those env vars back into real files inside the
# container (which lives only as long as the deploy - nothing persists
# to git or a public image layer).
# ------------------------------------------------------------------
COOKIE_ENV_MAP = {
    "youtube": "YOUTUBE_COOKIES_B64",
    "instagram": "INSTAGRAM_COOKIES_B64",
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
    else:
        return None
    return path if os.path.exists(path) else None


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


def is_allowed_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
    except Exception:
        return False

    try:
        for ie in _YDL_FOR_CHECK._ies.values():
            if ie.suitable(url) and ie.ie_key() not in ("Generic",):
                return True
    except Exception as e:
        logger.error(f"Extractor check failed for {url}: {e}")
        return False

    return False


# ------------------------------------------------------------------
# Task persistence (simple JSON file, fine for low volume / single worker)
# ------------------------------------------------------------------
def load_tasks() -> dict:
    if not os.path.exists(TASKS_FILE):
        return {}
    try:
        with open(TASKS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load tasks.json: {e}")
        return {}


def save_task(task_id: str, data: dict):
    tasks = load_tasks()
    tasks[task_id] = data
    try:
        with open(TASKS_FILE, "w") as f:
            json.dump(tasks, f)
        logger.info(f"[{task_id}] status saved -> {data.get('status')} "
                    f"progress={data.get('progress')}")
    except Exception as e:
        logger.error(f"[{task_id}] Failed to save tasks.json: {e}")


class DownloadRequest(BaseModel):
    url: str


@app.get("/")
def home():
    return {"message": "API Working"}


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

    # NEW: surface ffmpeg status directly, since a missing ffmpeg is the
    # most common cause of "video downloaded but has no audio".
    result["ffmpeg_available"] = FFMPEG_AVAILABLE
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=15)
        result["ffmpeg_version"] = (r.stdout.strip() or r.stderr.strip()).splitlines()[0]
    except Exception as e:
        result["ffmpeg_version"] = None
        result["ffmpeg_error"] = str(e)

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
             "ffmpeg_available": FFMPEG_AVAILABLE, "tasks": load_tasks()}


@app.post("/download", dependencies=[Depends(require_api_key)])
@limiter.limit("5/minute")
async def start_download(request: Request, body: DownloadRequest, background_tasks: BackgroundTasks):
    if not is_allowed_url(body.url):
        raise HTTPException(status_code=400, detail="URL host is not supported")

    cleanup_old_files()

    task_id = str(uuid.uuid4())[:12]
    file_path = os.path.join(BASE_DIR, f"{task_id}.mp4")

    logger.info(f"[{task_id}] New download requested. url={body.url}")

    save_task(task_id, {
        "status": "started",
        "progress": 0,
        "url": body.url,
        "file_path": file_path,
    })

    background_tasks.add_task(download_task, body.url, task_id, file_path)
    return {"task_id": task_id, "status": "started", "message": "Download started"}


def parse_progress_line(line: str):
    match = re.search(r"\[download\]\s+(\d{1,3}\.\d)%", line)
    if match:
        try:
            return int(float(match.group(1)))
        except ValueError:
            return None
    return None


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


def download_task(url: str, task_id: str, file_path: str):
    logger.info(f"[{task_id}] download_task() started")
    save_task(task_id, {
        "status": "downloading",
        "progress": 0,
        "url": url,
        "file_path": file_path,
    })

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "--newline",
        "--merge-output-format", "mp4",
        # Prefer H.264 video + AAC audio (plays on virtually every Android
        # device/player), falling back to any video+audio combo, then any
        # single combined stream. The previous unconstrained "bv*+ba/b"
        # was happily matching VP9+Opus, which is what produced files
        # that either had no audio track after a failed merge, or played
        # with codecs some Android players choke on even when merged.
        "-f", "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*+ba/b",
        "-S", "ext:mp4:m4a",
        "-o", file_path,
        "--max-filesize", "500M",
        # The Docker image installs Node.js specifically so this has a
        # runtime to use for YouTube's JS signature/n-parameter challenge.
        # Without a working JS runtime, YouTube formats get silently
        # dropped and every download fails with "format not available".
        "--js-runtimes", "node",
        # Belt-and-suspenders: also allow fetching the EJS solver scripts
        # straight from GitHub if the bundled ones (from yt-dlp[default])
        # are ever missing or out of date.
        "--remote-components", "ejs:github",
    ]

    # YouTube specifically: try several internal "player clients". YouTube
    # has been rolling out a PO Token requirement for some clients' stream
    # URLs - when yt-dlp can't produce one, that client's formats get
    # silently dropped, which is what causes "Requested format is not
    # available" even though the video itself is fine. Trying multiple
    # clients means we only need ONE of them to still work unauthenticated.
    if "youtube.com" in url or "youtu.be" in url:
        cmd += ["--extractor-args", "youtube:player_client=web,tv"]

    cookie_path = cookie_file_for_url(url)
    if cookie_path:
        cmd += ["--cookies", cookie_path]
        logger.info(f"[{task_id}] Using cookies file: {cookie_path}")
    else:
        logger.info(f"[{task_id}] No cookie file for this URL, downloading unauthenticated")

    cmd.append(url)

    # Don't log the full command if it contains a cookies path with an
    # account attached - fine to log the path, never log cookie contents.
    logger.info(f"[{task_id}] Running yt-dlp (cookies={'yes' if cookie_path else 'no'}, "
                f"ffmpeg_available={FFMPEG_AVAILABLE})")

    last_progress = 0
    output_tail = []  # keep the last ~20 lines so failures are self-explanatory
    TAIL_MAX = 20

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
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
                save_task(task_id, {
                    "status": "downloading",
                    "progress": pct,
                    "url": url,
                    "file_path": file_path,
                })

        returncode = process.wait(timeout=300)
        logger.info(f"[{task_id}] yt-dlp exited with code {returncode}")

        if returncode != 0:
            # Prefer the last real ERROR line yt-dlp printed, since that's
            # almost always the actionable one (bot-check, private video,
            # geo-block, etc). Fall back to the raw tail if none matched.
            error_lines = [l for l in output_tail if "ERROR" in l]
            detail = "\n".join(error_lines[-3:]) if error_lines else "\n".join(output_tail[-6:])
            detail = detail[:1500]  # keep the status payload sane

            save_task(task_id, {
                "status": "failed",
                "progress": last_progress,
                "url": url,
                "file_path": file_path,
                "error": detail or f"yt-dlp exited with code {returncode}, no output captured",
            })
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
            size = os.path.getsize(file_path)
            logger.info(f"[{task_id}] File confirmed on disk: {file_path} ({size} bytes)")
            save_task(task_id, {
                "status": "completed",
                "progress": 100,
                "url": url,
                "file_path": file_path,
                "download_url": f"/download-file/{task_id}",
            })
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
                save_task(task_id, {
                    "status": "failed",
                    "progress": last_progress,
                    "url": url,
                    "file_path": file_path,
                    "error": error_msg,
                })
                cleanup_task_fragments(task_id)
            else:
                save_task(task_id, {
                    "status": "failed",
                    "progress": last_progress,
                    "url": url,
                    "file_path": file_path,
                    "error": "yt-dlp exited 0 but no output file was found",
                })

    except subprocess.TimeoutExpired:
        save_task(task_id, {
            "status": "failed", "progress": last_progress, "url": url,
            "file_path": file_path, "error": "timeout",
        })
        cleanup_task_fragments(task_id)
    except FileNotFoundError as e:
        save_task(task_id, {
            "status": "failed", "progress": last_progress, "url": url,
            "file_path": file_path, "error": f"yt-dlp not found on server: {e}",
        })
    except Exception as e:
        logger.exception(f"[{task_id}] Unexpected exception in download_task")
        save_task(task_id, {
            "status": "failed", "progress": last_progress, "url": url,
            "file_path": file_path, "error": str(e),
        })
        cleanup_task_fragments(task_id)


@app.get("/status/{task_id}", dependencies=[Depends(require_api_key)])
def get_status(task_id: str):
    tasks = load_tasks()
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


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
        return FileResponse(file_path, media_type="video/mp4", filename="video.mp4")

    raise HTTPException(status_code=404, detail="File not found on disk (expired or lost on redeploy)")


# ------------------------------------------------------------------
# Basic disk hygiene: delete finished/failed files older than
# MAX_FILE_AGE_HOURS so a public-facing box doesn't fill its disk.
# Runs opportunistically on each new /download call.
# ------------------------------------------------------------------
MAX_FILE_AGE_HOURS = float(os.environ.get("MAX_FILE_AGE_HOURS", "2"))


def cleanup_old_files():
    cutoff = time.time() - (MAX_FILE_AGE_HOURS * 3600)
    for path in glob.glob(os.path.join(BASE_DIR, "*")):
        if path == TASKS_FILE:
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                logger.info(f"Cleaned up old file: {path}")
        except Exception as e:
            logger.warning(f"Cleanup failed for {path}: {e}")