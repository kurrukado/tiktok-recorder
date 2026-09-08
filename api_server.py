import os
import sys
import json
import time
import shutil
import threading
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

import recorder_core
import auto_h264
import gdrive_manager
import notifier

app = FastAPI(
    title='TikTok Live Recorder & Cloud Sync API',
    description='API kết nối web: thêm streamer, kiểm tra live, ghi hình chuẩn H.264 và tải video tốc độ cao.',
    version='2.0.0'
)

# Enable CORS for any external web integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

ACTIVE_RECORDING_TASKS = {}
RECORDING_LOCK = threading.Lock()

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        'monitored_users': ['islizanx', 'itsme_kate0110', 'urielhui38'],
        'check_interval_seconds': 20
    }

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

class AddUserRequest(BaseModel):
    username: str

class RecordRequest(BaseModel):
    username: str
    duration_seconds: Optional[int] = None

@app.get('/api/health')
def health_check():
    return {
        'status': 'online',
        'time': datetime.now().isoformat(),
        'active_recordings': list(ACTIVE_RECORDING_TASKS.keys())
    }

@app.get('/api/users')
def get_users():
    cfg = load_config()
    users = cfg.get('monitored_users', ['islizanx', 'itsme_kate0110', 'urielhui38'])
    
    result = []
    for u in users:
        is_live, room_id = recorder_core.check_user_live(u)
        is_recording = (u in ACTIVE_RECORDING_TASKS)
        result.append({
            'username': u,
            'is_live': is_live,
            'room_id': room_id,
            'is_recording': is_recording
        })
    return {'users': result}

@app.post('/api/users')
def add_user(req: AddUserRequest):
    user = req.username.strip().replace('@', '').lower()
    if not user:
        raise HTTPException(status_code=400, detail='Tên tài khoản không hợp lệ')
    
    cfg = load_config()
    users = cfg.get('monitored_users', [])
    if user in users:
        return {'message': f'@{user} đã có trong danh sách theo dõi', 'users': users}
    
    users.append(user)
    cfg['monitored_users'] = users
    save_config(cfg)
    return {'message': f'Đã thêm @{user} vào danh sách theo dõi', 'users': users}

@app.delete('/api/users/{username}')
def delete_user(username: str):
    user = username.strip().replace('@', '').lower()
    cfg = load_config()
    users = cfg.get('monitored_users', [])
    if user not in users:
        raise HTTPException(status_code=404, detail=f'@{user} không có trong danh sách')
    
    users.remove(user)
    cfg['monitored_users'] = users
    save_config(cfg)
    return {'message': f'Đã xóa @{user} khỏi danh sách theo dõi', 'users': users}

@app.get('/api/stream/{username}')
def get_stream_url(username: str):
    user = username.strip().replace('@', '').lower()
    is_live, room_id = recorder_core.check_user_live(user)
    if not is_live or not room_id:
        return {
            'username': user,
            'is_live': False,
            'message': 'Streamer hiện tại đang ngoại tuyến (Offline)'
        }
    
    stream_url = recorder_core.get_live_stream_url(room_id, user=user)
    if not stream_url:
        raise HTTPException(status_code=500, detail='Không thể trích xuất link stream')
    
    return {
        'username': user,
        'is_live': True,
        'room_id': room_id,
        'stream_url': stream_url,
        'format': 'flv' if '.flv' in stream_url else 'hls_m3u8',
        'note': 'Link trực tiếp từ máy chủ CDN của TikTok, có thể phát trực tiếp trên web hoặc tải tốc độ cao tối đa băng thông.'
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
                # Auto sync Google Drive
                token = gdrive_manager.get_access_token()
                if token:
                    root_id = gdrive_manager.find_or_create_folder('tiktok-record', access_token=token)
                    sub_id = gdrive_manager.find_or_create_folder(user, parent_id=root_id, access_token=token)
                    gdrive_manager.upload_file_to_drive(output_file, sub_id, access_token=token)
    finally:
        with RECORDING_LOCK:
            ACTIVE_RECORDING_TASKS.pop(user, None)

@app.post('/api/record/start')
def start_record(req: RecordRequest, bg_tasks: BackgroundTasks):
    user = req.username.strip().replace('@', '').lower()
    with RECORDING_LOCK:
        if user in ACTIVE_RECORDING_TASKS:
            return {'message': f'@{user} đang được ghi hình rồi', 'status': 'already_recording'}
        ACTIVE_RECORDING_TASKS[user] = {'start_time': time.time()}

    bg_tasks.add_task(bg_record_worker, user, req.duration_seconds)
    return {
        'message': f'Đã bắt đầu tiến trình ghi hình @{user}',
        'status': 'recording_started',
        'username': user
    }

@app.get('/api/recordings')
def list_recordings():
    users = ['islizanx', 'itsme_kate0110', 'urielhui38']
    cfg = load_config()
    for u in cfg.get('monitored_users', []):
        if u not in users:
            users.append(u)

    files_list = []
    for u in users:
        u_dir = os.path.join(BASE_DIR, u)
        if os.path.exists(u_dir):
            for f in os.listdir(u_dir):
                if f.endswith('.mp4') and not f.endswith('.tmp.mp4'):
                    fp = os.path.join(u_dir, f)
                    st = os.stat(fp)
                    files_list.append({
                        'filename': f,
                        'user': u,
                        'size_bytes': st.st_size,
                        'size_mb': round(st.st_size / (1024 * 1024), 2),
                        'created_at': datetime.fromtimestamp(st.st_mtime).isoformat(),
                        'download_url': f'/api/download/{u}/{f}'
                    })
    files_list.sort(key=lambda x: x['created_at'], reverse=True)
    return {'total_files': len(files_list), 'recordings': files_list}

@app.get('/api/download/{user}/{filename}')
def download_video(user: str, filename: str):
    user = user.strip().replace('@', '').lower()
    fp = os.path.join(BASE_DIR, user, filename)
    if not os.path.exists(fp):
        raise HTTPException(status_code=404, detail='File video không tồn tại')
    
    # High-speed streaming response with accept-ranges
    return FileResponse(
        path=fp,
        media_type='video/mp4',
        filename=filename
    )

@app.post('/api/gdrive/sync')
def trigger_gdrive_sync(bg_tasks: BackgroundTasks):
    bg_tasks.add_task(gdrive_manager.sync_all_to_gdrive)
    return {'message': 'Đã kích hoạt tiến trình đồng bộ toàn bộ video lên Google Drive ngầm'}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8000)
