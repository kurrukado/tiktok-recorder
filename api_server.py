import os
import sys
import json
import time
import shutil
import threading
import subprocess
import re
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
FFMPEG_PATH = os.path.join(BASE_DIR, "ffmpeg.exe") if os.path.exists(os.path.join(BASE_DIR, "ffmpeg.exe")) else (shutil.which("ffmpeg") or "ffmpeg")

import recorder_core
import auto_h264
import gdrive_manager
import notifier

app = FastAPI(
    title="TikTok Live Recorder & Cloud Sync API",
    description="API kết nối web: thêm streamer, kiểm tra live, ghi hình chuẩn H.264, cắt ảnh xem trước (thumbnail) giữa video và tải video tốc độ cao.",
    version="2.1.0"
)

# Enable CORS for any external web integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ACTIVE_RECORDING_TASKS = {}
RECORDING_LOCK = threading.Lock()

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "monitored_users": ["islizanx", "itsme_kate0110", "urielhui38", "fmmaidcoffee"],
        "check_interval_seconds": 20
    }

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

def get_video_duration(filepath):
    """Lấy thời lượng video tính bằng giây."""
    cmd = [FFMPEG_PATH, "-i", filepath]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=6)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", p.stderr)
        if m:
            hours = int(m.group(1))
            mins = int(m.group(2))
            secs = float(m.group(3))
            return round(hours * 3600 + mins * 60 + secs, 2)
    except Exception:
        pass
    return None

def extract_middle_thumbnail(video_path, output_thumb=None):
    """
    Trích xuất khung hình chất lượng cao tại chính giữa video (50% thời lượng).
    """
    if not os.path.exists(video_path):
        return None

    if not output_thumb:
        base, _ = os.path.splitext(video_path)
        output_thumb = base + ".jpg"

    if os.path.exists(output_thumb) and os.path.getsize(output_thumb) > 1024:
        return output_thumb

    duration = get_video_duration(video_path)
    seek_time = (duration / 2.0) if duration and duration > 2 else 5.0

    mins, secs = divmod(seek_time, 60)
    hours, mins = divmod(mins, 60)
    ts_str = f"{int(hours):02d}:{int(mins):02d}:{secs:06.3f}"

    cmd = [
        FFMPEG_PATH, "-y",
        "-ss", ts_str,
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        output_thumb
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
        if proc.returncode == 0 and os.path.exists(output_thumb) and os.path.getsize(output_thumb) > 500:
            return output_thumb
    except Exception as e:
        print(f"[!] Lỗi khi cắt thumbnail: {e}")

    return None

class AddUserRequest(BaseModel):
    username: str

class RecordRequest(BaseModel):
    username: str
    duration_seconds: Optional[int] = None

@app.get("/api/health")
def health_check():
    active = []
    try:
        active = gdrive_manager.load_active_recordings_from_drive() or []
    except Exception:
        pass
    all_active = list(set(active + list(ACTIVE_RECORDING_TASKS.keys())))
    return {
        "status": "online",
        "time": datetime.now().isoformat(),
        "active_recordings": all_active,
        "active_count": len(all_active)
    }

@app.get("/api/recordings/active")
def get_active_recordings():
    """
    Trả về danh sách chính xác các streamer hiện đang được bot 24/7 ghi hình.
    """
    active = []
    try:
        active = gdrive_manager.load_active_recordings_from_drive() or []
    except Exception:
        pass
    all_active = list(set(active + list(ACTIVE_RECORDING_TASKS.keys())))
    return {
        "total_active": len(all_active),
        "active_streamers": all_active
    }

@app.get("/api/users")
def get_users(check_live: bool = False):
    users = None
    try:
        drive_users = gdrive_manager.load_streamers_from_drive()
        if drive_users is not None and isinstance(drive_users, list):
            users = drive_users
    except Exception:
        pass

    if users is None:
        cfg = load_config()
        users = cfg.get("monitored_users", [])

    active_users = set()
    try:
        drive_act = gdrive_manager.load_active_recordings_from_drive()
        if drive_act:
            active_users.update(drive_act)
    except Exception:
        pass
    active_users.update(ACTIVE_RECORDING_TASKS.keys())
    
    result = []
    for u in users:
        is_live, room_id = False, None
        if check_live:
            try:
                is_live, room_id = recorder_core.check_user_live(u)
            except Exception:
                pass
        # Nếu streamer đang live thì luôn được tính là đang ghi hình tự động
        is_recording = (u in active_users) or is_live
        if is_live:
            active_users.add(u)
        result.append({
            "username": u,
            "is_live": is_live,
            "room_id": room_id,
            "is_recording": is_recording
        })
    return {
        "users": result,
        "streamers": users,
        "total": len(users),
        "currently_recording": list(active_users)
    }

@app.post("/api/users")
def add_user(req: AddUserRequest):
    user = req.username.strip().replace("@", "").lower()
    if not user:
        raise HTTPException(status_code=400, detail="Tên tài khoản không hợp lệ")
    
    cfg = load_config()
    users = cfg.get("monitored_users", [])
    try:
        drive_users = gdrive_manager.load_streamers_from_drive()
        if drive_users is not None and isinstance(drive_users, list):
            users = drive_users
    except Exception:
        pass

    already_in = (user in users)
    if not already_in:
        users.append(user)
        cfg["monitored_users"] = users
        save_config(cfg)
        try:
            gdrive_manager.save_streamers_to_drive(users)
        except Exception:
            pass

    # TỰ ĐỘNG TẠO THƯ MỤC TRÊN GOOGLE DRIVE
    gdrive_status = "Chưa kết nối Google Drive"
    folder_id = None
    try:
        ok, res_info = gdrive_manager.create_streamer_folder_drive(user)
        if ok:
            folder_id = res_info
            gdrive_status = f"Đã tạo thành công thư mục 'tiktok-record/{user}/' trên Google Drive"
        else:
            gdrive_status = f"Không thể tạo folder Drive: {res_info}"
    except Exception as e:
        gdrive_status = f"Lỗi tạo folder Drive: {e}"

    msg = f"@{user} đã có trong danh sách theo dõi" if already_in else f"Đã thêm @{user} vào danh sách theo dõi"
    return {
        "message": msg,
        "username": user,
        "gdrive_status": gdrive_status,
        "gdrive_folder_id": folder_id,
        "users": users
    }

@app.delete("/api/users/{username}")
def delete_user(username: str):
    user = username.strip().replace("@", "").lower()
    cfg = load_config()
    users = cfg.get("monitored_users", [])
    try:
        drive_users = gdrive_manager.load_streamers_from_drive()
        if drive_users is not None and isinstance(drive_users, list):
            users = drive_users
    except Exception:
        pass

    if user in users:
        users.remove(user)
        cfg["monitored_users"] = users
        save_config(cfg)
        try:
            gdrive_manager.save_streamers_to_drive(users)
        except Exception:
            pass

    # TỰ ĐỘNG XÓA VĨNH VIỄN THƯ MỤC TRÊN GOOGLE DRIVE
    gdrive_status = "Chưa kết nối Google Drive"
    try:
        ok, msg = gdrive_manager.delete_streamer_folder_drive(user)
        gdrive_status = msg
    except Exception as e:
        gdrive_status = f"Lỗi xóa folder Drive: {e}"

    # Xóa cả folder local nếu có
    local_dir = os.path.join(BASE_DIR, user)
    if os.path.exists(local_dir):
        try:
            shutil.rmtree(local_dir, ignore_errors=True)
        except Exception:
            pass

    return {
        "message": f"Đã xóa @{user} khỏi danh sách theo dõi và dọn dẹp Google Drive",
        "username": user,
        "gdrive_status": gdrive_status,
        "users": users
    }

@app.get("/api/stream/{username}")
def get_stream_url(username: str):
    user = username.strip().replace("@", "").lower()
    is_live, room_id = recorder_core.check_user_live(user)
    if not is_live or not room_id:
        return {
            "username": user,
            "is_live": False,
            "message": "Streamer hiện tại đang ngoại tuyến (Offline)"
        }
    
    stream_url = recorder_core.get_live_stream_url(room_id, user=user)
    if not stream_url:
        raise HTTPException(status_code=500, detail="Không thể trích xuất link stream")
    
    return {
        "username": user,
        "is_live": True,
        "room_id": room_id,
        "stream_url": stream_url,
        "format": "flv" if ".flv" in stream_url else "hls_m3u8",
        "note": "Link trực tiếp từ máy chủ CDN của TikTok, có thể phát trực tiếp trên web hoặc tải tốc độ cao tối đa băng thông."
    }

def bg_record_worker(user: str, duration: Optional[int]):
    try:
        is_live, room_id = recorder_core.check_user_live(user)
        if not is_live or not room_id:
            return
        stream_url = recorder_core.get_live_stream_url(room_id, user=user)
        if stream_url:
            output_file = recorder_core.record_stream_ffmpeg(stream_url, target_user=user, duration=duration)
            if output_file and os.path.exists(output_file):
                # Tự động cắt thumbnail
                extract_middle_thumbnail(output_file)
                # Auto sync Google Drive
                token = gdrive_manager.get_access_token()
                if token:
                    root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=token)
                    sub_id = gdrive_manager.find_or_create_folder(user, parent_id=root_id, access_token=token)
                    gdrive_manager.upload_file_to_drive(output_file, sub_id, access_token=token)
    finally:
        with RECORDING_LOCK:
            ACTIVE_RECORDING_TASKS.pop(user, None)

@app.post("/api/record/start")
def start_record(req: RecordRequest, bg_tasks: BackgroundTasks):
    user = req.username.strip().replace("@", "").lower()
    with RECORDING_LOCK:
        if user in ACTIVE_RECORDING_TASKS:
            return {"message": f"@{user} đang được ghi hình rồi", "status": "already_recording"}
        ACTIVE_RECORDING_TASKS[user] = {"start_time": time.time()}

    bg_tasks.add_task(bg_record_worker, user, req.duration_seconds)
    return {
        "message": f"Đã bắt đầu tiến trình ghi hình @{user}",
        "status": "recording_started",
        "username": user
    }

@app.post("/api/record/stop")
def stop_record(req: AddUserRequest):
    user = req.username.strip().replace("@", "").lower()
    with RECORDING_LOCK:
        if user in ACTIVE_RECORDING_TASKS:
            ACTIVE_RECORDING_TASKS.pop(user, None)
            return {"message": f"Đã dừng theo dõi/ghi hình @{user}", "status": "stopped"}
        return {"message": f"@{user} không có tiến trình ghi hình nào đang hoạt động", "status": "not_recording"}

@app.get("/api/recordings")
def list_recordings():
    users_set = set()
    try:
        drive_users = gdrive_manager.load_streamers_from_drive()
        if drive_users and isinstance(drive_users, list):
            users_set.update(drive_users)
    except Exception:
        pass
    cfg = load_config()
    users_set.update(cfg.get("monitored_users", []))
    for item in os.listdir(BASE_DIR):
        item_path = os.path.join(BASE_DIR, item)
        if os.path.isdir(item_path) and not item.startswith((".", "_")):
            users_set.add(item)
    users = list(users_set)

    files_list = []
    for u in users:
        u_dir = os.path.join(BASE_DIR, u)
        if os.path.exists(u_dir):
            for f in os.listdir(u_dir):
                if f.endswith(".mp4") and not f.endswith(".tmp.mp4"):
                    fp = os.path.join(u_dir, f)
                    st = os.stat(fp)
                    dur = get_video_duration(fp)
                    dur_fmt = f"{int(dur//60):02d}:{int(dur%60):02d}" if dur else "00:00"
                    
                    # Check or generate thumbnail
                    thumb_name = os.path.splitext(f)[0] + ".jpg"
                    thumb_path = os.path.join(u_dir, thumb_name)
                    has_thumb = os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500

                    files_list.append({
                        "filename": f,
                        "user": u,
                        "size_bytes": st.st_size,
                        "size_mb": round(st.st_size / (1024 * 1024), 2),
                        "duration_seconds": dur,
                        "duration_formatted": dur_fmt,
                        "created_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
                        "thumbnail_url": f"/api/thumbnail/{u}/{f}",
                        "download_url": f"/api/download/{u}/{f}"
                    })
    files_list.sort(key=lambda x: x["created_at"], reverse=True)
    return {"total_files": len(files_list), "recordings": files_list}

@app.get("/api/thumbnail/{user}/{filename}")
def get_thumbnail(user: str, filename: str):
    """
    Trả về ảnh xem trước được cắt từ chính giữa video (50% thời lượng).
    Nếu ảnh chưa có sẵn, hệ thống sẽ tự động cắt trong 0.1s và lưu cache.
    """
    user = user.strip().replace("@", "").lower()
    clean_name = filename.replace(".jpg", "").replace(".mp4", "")
    
    video_path = os.path.join(BASE_DIR, user, clean_name + ".mp4")
    thumb_path = os.path.join(BASE_DIR, user, clean_name + ".jpg")

    # Nếu thumbnail đã có sẵn
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
        return FileResponse(path=thumb_path, media_type="image/jpeg", filename=clean_name + ".jpg")

    # Nếu chưa có nhưng video tồn tại -> Cắt thumbnail ngay lập tức từ giữa video
    if os.path.exists(video_path):
        out = extract_middle_thumbnail(video_path, thumb_path)
        if out and os.path.exists(out):
            return FileResponse(path=out, media_type="image/jpeg", filename=clean_name + ".jpg")

    raise HTTPException(status_code=404, detail="Không tìm thấy video hoặc không thể tạo ảnh xem trước")

@app.get("/api/download/{user}/{filename}")
def download_video(user: str, filename: str):
    user = user.strip().replace("@", "").lower()
    fp = os.path.join(BASE_DIR, user, filename)
    if not os.path.exists(fp):
        raise HTTPException(status_code=404, detail="File video không tồn tại")
    
    # High-speed streaming response with accept-ranges
    return FileResponse(
        path=fp,
        media_type="video/mp4",
        filename=filename
    )

@app.post("/api/gdrive/sync")
def trigger_gdrive_sync(bg_tasks: BackgroundTasks):
    bg_tasks.add_task(gdrive_manager.sync_all_to_gdrive)
    return {"message": "Đã kích hoạt tiến trình đồng bộ toàn bộ video lên Google Drive ngầm"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
