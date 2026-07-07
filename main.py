from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import subprocess
import uuid
import os
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Social Media Video Downloader API")

# Allow frontend to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DownloadRequest(BaseModel):
    url: str

@app.get("/")
def home():
    return {"message": "Social Media Downloader API is running!"}

@app.post("/download")
async def download(request: DownloadRequest, background_tasks: BackgroundTasks):
    if not request.url:
        raise HTTPException(status_code=400, detail="URL is required")
    
    task_id = str(uuid.uuid4())[:8]
    
    background_tasks.add_task(run_download, request.url, task_id)
    
    return {
        "task_id": task_id,
        "status": "started",
        "message": "Download started in background"
    }

def run_download(url: str, task_id: str):
    try:
        output_path = f"/tmp/{task_id}.%(ext)s"
        command = [
            "yt-dlp",
            "--no-warnings",
            "-f", "bestvideo+bestaudio/best",
            "--max-filesize", "150M",
            "-o", output_path,
            url
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        print(f"Download completed for {task_id}")
    except Exception as e:
        print(f"Error downloading {url}: {e}")