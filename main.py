import os
import re
import json
import glob
import time
import uuid
import base64
import logging
import secrets
import subprocess
from urllib.parse import urlparse

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
# Only allow known platforms through. This is both a security
# measure (no arbitrary-URL SSRF-style abuse of your server) and a
# sanity check (yt-dlp will just fail loudly on garbage otherwise).
# ------------------------------------------------------------------
ALLOWED_DOMAINS = (
    "youtube.com", "youtu.be",
    "instagram.com",
    "tiktok.com",
    "twitter.com", "x.com",
    "facebook.com", "fb.watch",
)


def is_allowed_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)


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

    return {"base_dir": BASE_DIR, "files": files, "cookies": cookie_status, "tasks": load_tasks()}


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
        "-f", "best[ext=mp4]/bestvideo+bestaudio/best",
        "-o", file_path,
        "--max-filesize", "500M",
    ]

    cookie_path = cookie_file_for_url(url)
    if cookie_path:
        cmd += ["--cookies", cookie_path]
        logger.info(f"[{task_id}] Using cookies file: {cookie_path}")
    else:
        logger.info(f"[{task_id}] No cookie file for this URL, downloading unauthenticated")

    cmd.append(url)

    # Don't log the full command if it contains a cookies path with an
    # account attached - fine to log the path, never log cookie contents.
    logger.info(f"[{task_id}] Running yt-dlp (cookies={'yes' if cookie_path else 'no'})")

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
            return

        actual_file = file_path if os.path.exists(file_path) else None
        if not actual_file:
            matches = glob.glob(os.path.join(BASE_DIR, f"{task_id}.*"))
            matches = [m for m in matches if not m.endswith(".json")]
            if matches:
                actual_file = matches[0]

        if actual_file and os.path.exists(actual_file):
            size = os.path.getsize(actual_file)
            logger.info(f"[{task_id}] File confirmed on disk: {actual_file} ({size} bytes)")
            save_task(task_id, {
                "status": "completed",
                "progress": 100,
                "url": url,
                "file_path": actual_file,
                "download_url": f"/download-file/{task_id}",
            })
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
    if file_path and os.path.exists(file_path):
        return {"available": True}

    matches = glob.glob(os.path.join(BASE_DIR, f"{task_id}.*"))
    matches = [m for m in matches if not m.endswith(".json")]
    return {"available": bool(matches)}


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

    file_path = task.get("file_path")
    if file_path and os.path.exists(file_path):
        return FileResponse(file_path, media_type="video/mp4", filename="video.mp4")

    matches = glob.glob(os.path.join(BASE_DIR, f"{task_id}.*"))
    matches = [m for m in matches if not m.endswith(".json")]
    if matches:
        return FileResponse(matches[0], media_type="video/mp4", filename="video.mp4")

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