import os
import re
import json
import glob
import uuid
import logging
import subprocess
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ------------------------------------------------------------------
# Logging setup - this is the important part for debugging.
# Railway captures stdout/stderr automatically and shows it in the
# deploy logs, so everything logged here will show up there.
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("downloader")

app = FastAPI(title="Social Media Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# Storage locations
# ------------------------------------------------------------------
BASE_DIR = os.path.join(os.getcwd(), "downloads")
os.makedirs(BASE_DIR, exist_ok=True)
TASKS_FILE = os.path.join(BASE_DIR, "tasks.json")

logger.info(f"BASE_DIR resolved to: {BASE_DIR}")
logger.info(f"TASKS_FILE resolved to: {TASKS_FILE}")


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
    """Read-modify-write the tasks file. Simple, no locking, fine for low volume."""
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


@app.get("/debug/list-files")
def debug_list_files():
    """Temporary debug endpoint: shows exactly what's on disk right now."""
    try:
        files = os.listdir(BASE_DIR)
    except Exception as e:
        files = [f"ERROR listing dir: {e}"]
    return {
        "base_dir": BASE_DIR,
        "files": files,
        "tasks": load_tasks(),
    }


@app.post("/download")
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())[:12]
    file_path = os.path.join(BASE_DIR, f"{task_id}.mp4")

    logger.info(f"[{task_id}] New download requested. url={request.url}")
    logger.info(f"[{task_id}] Target file_path={file_path}")

    save_task(task_id, {
        "status": "started",
        "progress": 0,
        "url": request.url,
        "file_path": file_path,
    })

    background_tasks.add_task(download_task, request.url, task_id, file_path)
    return {"task_id": task_id, "status": "started", "message": "Download started"}


def parse_progress_line(line: str):
    """
    yt-dlp with --newline prints lines like:
    [download]  12.3% of 10.00MiB at 1.20MiB/s ETA 00:08
    Returns an int percentage, or None if the line doesn't match.
    """
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
        "--newline",                 # <-- required so we get one progress line at a time
        "--merge-output-format", "mp4",
        "-f", "best[ext=mp4]/bestvideo+bestaudio/best",
        "-o", file_path,
        "--max-filesize", "500M",
        url,
    ]
    logger.info(f"[{task_id}] Running command: {' '.join(cmd)}")

    last_progress = 0

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into stdout so we see everything in order
            text=True,
            bufsize=1,
        )

        # Stream output line by line, log everything, update progress as we go
        for line in process.stdout:
            line = line.rstrip()
            if not line:
                continue
            logger.info(f"[{task_id}] yt-dlp: {line}")

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
            logger.error(f"[{task_id}] yt-dlp failed with exit code {returncode}")
            save_task(task_id, {
                "status": "failed",
                "progress": last_progress,
                "url": url,
                "file_path": file_path,
                "error": f"yt-dlp exited with code {returncode}, see logs for details",
            })
            return

        # yt-dlp sometimes writes a different extension than requested
        # (e.g. if merging fails or the source format differs) - handle that.
        actual_file = file_path if os.path.exists(file_path) else None
        if not actual_file:
            matches = glob.glob(os.path.join(BASE_DIR, f"{task_id}.*"))
            # Exclude the tasks.json itself if glob is too broad (it isn't, but be safe)
            matches = [m for m in matches if not m.endswith(".json")]
            if matches:
                actual_file = matches[0]
                logger.warning(
                    f"[{task_id}] Expected file at {file_path} not found, "
                    f"but found alternate file: {actual_file}"
                )

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
            logger.error(
                f"[{task_id}] yt-dlp exited 0 but no file found on disk. "
                f"Dir listing: {os.listdir(BASE_DIR)}"
            )
            save_task(task_id, {
                "status": "failed",
                "progress": last_progress,
                "url": url,
                "file_path": file_path,
                "error": "yt-dlp exited 0 but no output file was found",
            })

    except subprocess.TimeoutExpired:
        logger.error(f"[{task_id}] Download timed out after 300s")
        save_task(task_id, {
            "status": "failed",
            "progress": last_progress,
            "url": url,
            "file_path": file_path,
            "error": "timeout",
        })
    except FileNotFoundError as e:
        # This fires if yt-dlp binary itself isn't installed / not on PATH
        logger.error(f"[{task_id}] yt-dlp binary not found: {e}")
        save_task(task_id, {
            "status": "failed",
            "progress": last_progress,
            "url": url,
            "file_path": file_path,
            "error": f"yt-dlp not found on server: {e}",
        })
    except Exception as e:
        logger.exception(f"[{task_id}] Unexpected exception in download_task")
        save_task(task_id, {
            "status": "failed",
            "progress": last_progress,
            "url": url,
            "file_path": file_path,
            "error": str(e),
        })


@app.get("/status/{task_id}")
def get_status(task_id: str):
    tasks = load_tasks()
    task = tasks.get(task_id)
    if not task:
        logger.warning(f"[{task_id}] /status called but task not found")
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/download-file/{task_id}")
def serve_file(task_id: str):
    tasks = load_tasks()
    task = tasks.get(task_id)

    if not task:
        logger.warning(f"[{task_id}] /download-file called but task not found in tasks.json. "
                        f"Known task ids: {list(tasks.keys())}")
        raise HTTPException(status_code=404, detail="Task not found")

    logger.info(f"[{task_id}] /download-file called. Current task status: {task}")

    if task.get("status") != "completed":
        raise HTTPException(
            status_code=404,
            detail=f"File not ready. Current status: {task.get('status')}, progress: {task.get('progress')}",
        )

    file_path = task.get("file_path")
    if file_path and os.path.exists(file_path):
        logger.info(f"[{task_id}] Serving file: {file_path}")
        return FileResponse(file_path, media_type="video/mp4", filename="video.mp4")

    # Fallback: maybe extension differs from what's recorded
    matches = glob.glob(os.path.join(BASE_DIR, f"{task_id}.*"))
    matches = [m for m in matches if not m.endswith(".json")]
    if matches:
        logger.warning(f"[{task_id}] Recorded file_path missing, serving fallback: {matches[0]}")
        return FileResponse(matches[0], media_type="video/mp4", filename="video.mp4")

    logger.error(f"[{task_id}] status=completed but file missing on disk. "
                 f"Dir listing: {os.listdir(BASE_DIR)}")
    raise HTTPException(status_code=404, detail="File not found on disk (expired or lost on redeploy)")