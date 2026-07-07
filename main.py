from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import subprocess
import uuid
import os
import time
from fastapi.middleware.cors import CORSMiddleware
from threading import Lock

app = FastAPI(title="Social Media Video Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

tasks = {}  # Store task status
lock = Lock()

class DownloadRequest(BaseModel):
    url: str

@app.get("/")
def home():
    return {"message": "Social Media Downloader API is running!"}

@app.post("/download")
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())[:12]
    
    with lock:
        tasks[task_id] = {"status": "started", "progress": 0, "url": request.url}
    
    background_tasks.add_task(download_video, request.url, task_id)
    
    return {
        "task_id": task_id,
        "status": "started",
        "message": "Download started"
    }

@app.get("/status/{task_id}")
def get_status(task_id: str):
    with lock:
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

def download_video(url: str, task_id: str):
    try:
        file_path = f"/tmp/{task_id}.mp4"
        
        command = [
            "yt-dlp",
            "-f", "bestvideo+bestaudio/best",
            "--max-filesize", "200M",
            "--progress-template", "%(progress) %(info.downloaded_bytes)s %(info.total_bytes)s",
            "-o", file_path,
            url
        ]
        
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        with lock:
            tasks[task_id]["status"] = "downloading"
        
        # Simulate progress (yt-dlp progress is complex, this is basic version)
        for _ in range(100):
            time.sleep(2)
            with lock:
                if task_id in tasks:
                    tasks[task_id]["progress"] = min(100, tasks[task_id]["progress"] + 5)
        
        # After completion
        if os.path.exists(file_path):
            file_size = os.path.getsize(file_path)
            download_url = f"https://web-production-12a1b.up.railway.app/download-file/{task_id}"
            
            with lock:
                tasks[task_id] = {
                    "status": "completed",
                    "progress": 100,
                    "download_url": download_url,
                    "file_path": file_path,
                    "size": file_size
                }
        else:
            with lock:
                tasks[task_id]["status"] = "failed"
                
    except Exception as e:
        with lock:
            tasks[task_id]["status"] = "failed"
            tasks[task_id]["error"] = str(e)

# Add this new endpoint to serve the file
@app.get("/download-file/{task_id}")
def download_file(task_id: str):
    with lock:
        task = tasks.get(task_id)
        if not task or task["status"] != "completed":
            raise HTTPException(status_code=404, detail="File not ready or expired")
        
        file_path = task.get("file_path")
        if os.path.exists(file_path):
            # Auto delete after serving
            response = FileResponse(file_path, media_type="video/mp4", filename=f"video_{task_id}.mp4")
            background_tasks = BackgroundTasks()
            background_tasks.add_task(os.remove, file_path)
            return response
        else:
            raise HTTPException(status_code=404, detail="File not found")