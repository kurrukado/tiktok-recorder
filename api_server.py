import os
import sys
import json
import time
import shutil
import threading
import subprocess
import re
import requests
import concurrent.futures
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, Response, RedirectResponse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
try:
    import imageio_ffmpeg
    IMGIO_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    IMGIO_FFMPEG = None

if sys.platform == "win32":
    win_ffmpeg = os.path.join(BASE_DIR, "ffmpeg.exe")
    FFMPEG_PATH = win_ffmpeg if os.path.exists(win_ffmpeg) else (shutil.which("ffmpeg") or IMGIO_FFMPEG or "ffmpeg")
else:
    FFMPEG_PATH = shutil.which("ffmpeg") or IMGIO_FFMPEG or "/usr/bin/ffmpeg"

import recorder_core
import auto_h264
import gdrive_manager
import notifier
import supabase_sync

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
    if duration and duration > 2:
        seek_time = duration / 2.0
    elif duration and duration > 0.5:
        seek_time = duration / 2.0
    else:
        seek_time = 1.0

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

    # Dự phòng: Cắt frame ngay đầu video nếu vị trí giữa video không thành công
    try:
        cmd_fallback = [
            FFMPEG_PATH, "-y",
            "-ss", "00:00:00.500",
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            output_thumb
        ]
        proc = subprocess.run(cmd_fallback, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        if proc.returncode == 0 and os.path.exists(output_thumb) and os.path.getsize(output_thumb) > 500:
            return output_thumb
    except Exception:
        pass

    return None

# ---------------- LIVE STATUS CACHE ----------------
LIVE_CACHE = {}  # {username: {"is_live": bool, "room_id": str, "is_sub_only": bool, "is_preview": bool, "timestamp": float}}
LIVE_CACHE_TTL = 15.0  # Cache 15 giây để phản hồi API siêu nhanh, không gây nghẽn

def get_user_live_details_cached(user: str) -> dict:
    user = user.strip().replace("@", "").lower()
    now = time.time()
    cached = LIVE_CACHE.get(user)
    if cached and (now - cached.get("timestamp", 0) < LIVE_CACHE_TTL) and "is_sub_only" in cached:
        return cached

    details = {
        "is_live": False,
        "room_id": None,
        "is_sub_only": False,
        "is_preview": False,
        "timestamp": now
    }
    try:
        raw = recorder_core.check_live_details(user)
        details["is_live"] = raw.get("is_live", False)
        details["room_id"] = raw.get("room_id")
        details["is_sub_only"] = raw.get("is_sub_only", False)
        details["is_preview"] = raw.get("is_preview", False)
    except Exception:
        try:
            is_live, room_id = recorder_core.check_user_live(user)
            details["is_live"] = is_live
            details["room_id"] = room_id
        except Exception:
            pass

    LIVE_CACHE[user] = details
    return details

def get_user_live_status_cached(user: str):
    d = get_user_live_details_cached(user)
    return d["is_live"], d["room_id"]

def is_local_recorder_running_for_user(user: str) -> bool:
    """
    Kiểm tra xem có tiến trình ghi hình cục bộ nào (in-memory task hoặc subprocess ffmpeg/python)
    đang thực sự chạy cho streamer này không.
    """
    user_clean = user.strip().replace("@", "").lower()
    with RECORDING_LOCK:
        if user_clean in ACTIVE_RECORDING_TASKS:
            return True

    try:
        import psutil
        for proc in psutil.process_iter(['name', 'cmdline']):
            try:
                p_name = (proc.info.get('name') or '').lower()
                cmdline = proc.info.get('cmdline') or []
                cmd_str = " ".join(cmdline).lower()
                if user_clean in cmd_str:
                    if "ffmpeg" in p_name or "python" in p_name:
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception:
        pass
    return False

def clean_zombie_recordings(live_statuses=None):
    """
    Tự động quét và dọn dẹp các user bị kẹt trạng thái ma (Zombie Recording) trong active_recordings.json
    nếu họ offline trên TikTok và không có PID recorder cục bộ nào đang chạy.
    """
    try:
        drive_details = gdrive_manager.load_active_recordings_from_drive(as_details=True) or []
        if not drive_details:
            return []

        zombies_cleaned = []
        for item in drive_details:
            u = item.get("username") if isinstance(item, dict) else str(item)
            u = u.strip().replace("@", "").lower()
            if not u:
                continue

            # Nếu đang có PID cục bộ chạy thì không phải zombie
            if is_local_recorder_running_for_user(u):
                continue

            # Kiểm tra trạng thái trực tiếp trên TikTok
            is_live = False
            if live_statuses and u in live_statuses:
                val = live_statuses[u]
                is_live = val.get("is_live", False) if isinstance(val, dict) else val[0]
            else:
                is_live, _ = get_user_live_status_cached(u)

            if not is_live:
                print(f"[🧟 Zombie Cleaner] Phát hiện streamer @{u} bị kẹt trạng thái ma (Offline & không có PID). Đang dọn dẹp...")
                gdrive_manager.set_user_recording_status_drive(u, False)
                zombies_cleaned.append(u)

        return zombies_cleaned
    except Exception as e:
        print(f"[!] Lỗi dọn dẹp Zombie Recording: {e}")
        return []

class AddUserRequest(BaseModel):
    username: str

class RecordRequest(BaseModel):
    username: str
    duration_seconds: Optional[int] = None

@app.get("/api/health")
def health_check():
    active = set()
    try:
        drive_act = gdrive_manager.load_active_recordings_from_drive() or []
        active.update(drive_act)
    except Exception:
        pass
    active.update(ACTIVE_RECORDING_TASKS.keys())
    all_active = list(active)
    return {
        "status": "online",
        "time": datetime.now().isoformat(),
        "active_recordings": all_active,
        "active_count": len(all_active)
    }

@app.get("/api/recordings/active")
def get_active_recordings():
    """
    Trả về danh sách chính xác các streamer hiện đang được bot ghi hình thực sự.
    Tự động dọn dẹp các streamer kẹt trạng thái ma (Zombie Recording).
    """
    try:
        clean_zombie_recordings()
    except Exception:
        pass

    recording = set()
    try:
        drive_act = gdrive_manager.load_active_recordings_from_drive() or []
        recording.update(drive_act)
    except Exception:
        pass
    recording.update(ACTIVE_RECORDING_TASKS.keys())

    # Danh sách các user đang phát live trên TikTok
    live_streamers = []
    cfg = load_config()
    users = cfg.get("monitored_users", [])
    try:
        drive_users = gdrive_manager.load_streamers_from_drive()
        if drive_users is not None and isinstance(drive_users, list):
            users = drive_users
    except Exception:
        pass

    for u in users:
        is_live, _ = get_user_live_status_cached(u)
        if is_live:
            live_streamers.append(u)

    all_recording = list(recording)
    return {
        "total_active": len(all_recording),
        "active_streamers": all_recording,
        "live_streamers": live_streamers,
        "total_live": len(live_streamers),
        "status": "recording" if all_recording else "idle"
    }

@app.get("/api/users")
def get_users(check_live: bool = True):
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
    
    # Kiểm tra live đa luồng song song (ThreadPoolExecutor) để tốc độ siêu nhanh
    live_statuses = {}
    if check_live and users:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(users), 8)) as executor:
            future_to_user = {executor.submit(get_user_live_details_cached, u): u for u in users}
            for fut in concurrent.futures.as_completed(future_to_user):
                u = future_to_user[fut]
                try:
                    live_statuses[u] = fut.result()
                except Exception:
                    live_statuses[u] = {"is_live": False, "room_id": None, "is_sub_only": False, "is_preview": False}

    # Tự động quét dọn dẹp Zombie Recording
    try:
        zombies_cleaned = clean_zombie_recordings(live_statuses=live_statuses)
        for z in zombies_cleaned:
            active_users.discard(z)
    except Exception:
        pass

    result = []
    for u in users:
        det = live_statuses.get(u, {})
        is_live = det.get("is_live", False)
        room_id = det.get("room_id")
        is_sub_only = det.get("is_sub_only", False)
        is_preview = det.get("is_preview", False)
        is_recording = (u in active_users)
        if is_recording:
            status_str = "recording"
        elif is_live:
            status_str = "live"
        else:
            status_str = "offline"
        result.append({
            "username": u,
            "is_live": is_live,
            "room_id": room_id,
            "is_recording": is_recording,
            "status": status_str,
            "is_sub_only": is_sub_only,
            "is_preview": is_preview
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
def delete_user(username: str, delete_files: bool = True):
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

    # Dừng tiến trình ghi hình nếu đang hoạt động
    with RECORDING_LOCK:
        task_info = ACTIVE_RECORDING_TASKS.pop(user, None)
        if task_info and isinstance(task_info, dict):
            se = task_info.get("stop_event")
            if se:
                se.set()
    try:
        gdrive_manager.set_user_recording_status_drive(user, False)
    except Exception:
        pass

    # Đợi 3s để FFmpeg và thread worker kịp thời đóng luồng và giải phóng file lock
    time.sleep(3)

    # Xóa thư mục trên Drive cùng toàn bộ dữ liệu bên trong (mặc định luôn xóa sạch)
    gdrive_status = "Đã xóa toàn bộ thư mục và file trên Drive"
    if delete_files:
        try:
            ok, msg = gdrive_manager.delete_streamer_folder_drive(user)
            gdrive_status = msg
        except Exception as e:
            gdrive_status = f"Lỗi xóa folder Drive: {e}"

        local_dir = os.path.join(BASE_DIR, user)
        if os.path.exists(local_dir):
            try:
                shutil.rmtree(local_dir, ignore_errors=True)
            except Exception:
                pass

        # Xóa dữ liệu Supabase (bảng recordings, streamers và thumbnail Storage)
        try:
            supabase_sync.delete_streamer_data_supabase(user)
        except Exception as sb_err:
            print(f"[API] Lỗi xóa dữ liệu Supabase của {user}: {sb_err}")

        # Invalidate và dọn dẹp cache danh sách video trong RAM của API server
        global _RECORDINGS_CACHE
        with _RECORDINGS_CACHE_LOCK:
            _RECORDINGS_CACHE["timestamp"] = 0
            _RECORDINGS_CACHE["data"] = [v for v in _RECORDINGS_CACHE.get("data", []) if (v.get("user") != user and v.get("username") != user)]

    return {
        "message": f"Đã xóa @{user} khỏi danh sách theo dõi cùng toàn bộ folder và dữ liệu trên Drive",
        "username": user,
        "gdrive_status": gdrive_status,
        "users": users
    }

@app.get("/api/stream/{username}")
def get_stream_url(username: str):
    user = username.strip().replace("@", "").lower()
    is_live, room_id = get_user_live_status_cached(user)
    if not is_live or not room_id:
        return {
            "username": user,
            "is_live": False,
            "status": "offline",
            "message": "Streamer hiện tại đang ngoại tuyến (Offline)"
        }
    
    stream_url = recorder_core.get_live_stream_url(room_id, user=user)
    if not stream_url:
        raise HTTPException(status_code=500, detail="Không thể trích xuất link stream")
    
    return {
        "username": user,
        "is_live": True,
        "status": "recording",
        "room_id": room_id,
        "stream_url": stream_url,
        "format": "flv" if ".flv" in stream_url else "hls_m3u8",
        "note": "Link trực tiếp từ máy chủ CDN của TikTok, có thể phát trực tiếp trên web hoặc tải tốc độ cao tối đa băng thông."
    }

def concat_mp4_segments(segment_files, output_file):
    """
    Ghép nối nhiều phân đoạn MP4 cùng chuẩn H.264/AAC thành 1 file duy nhất bằng FFmpeg concat demuxer (-c copy)
    hoàn toàn không re-encode, 0% CPU, tốc độ < 1 giây.
    """
    if not segment_files:
        return None
    valid_files = [f for f in segment_files if f and os.path.exists(f) and os.path.getsize(f) > 50000]
    if not valid_files:
        return None
    if len(valid_files) == 1:
        if valid_files[0] != output_file:
            try:
                shutil.move(valid_files[0], output_file)
            except Exception:
                return valid_files[0]
        return output_file

    list_txt = output_file + ".concat.txt"
    try:
        with open(list_txt, "w", encoding="utf-8") as f:
            for seg in valid_files:
                clean_path = os.path.abspath(seg).replace("\\", "/")
                f.write(f"file '{clean_path}'\n")

        cmd = [
            recorder_core.FFMPEG_PATH,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_txt,
            "-c", "copy",
            "-movflags", "+faststart",
            output_file
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if res.returncode == 0 and os.path.exists(output_file) and os.path.getsize(output_file) > 100000:
            for seg in valid_files:
                if seg != output_file and os.path.exists(seg):
                    try:
                        os.remove(seg)
                    except Exception:
                        pass
            return output_file
        else:
            largest = max(valid_files, key=lambda f: os.path.getsize(f) if os.path.exists(f) else 0)
            return largest
    except Exception as e:
        print(f"[!] Lỗi khi ghép nối phân đoạn video: {e}")
        return valid_files[0]
    finally:
        if os.path.exists(list_txt):
            try:
                os.remove(list_txt)
            except Exception:
                pass

MAX_CHUNK_SECONDS = 3600  # Đúng 1 tiếng (1h = 3600s), tự động tách video và up lên Cloud

def bg_record_worker(user: str, duration: Optional[int] = None, stop_event: Optional[threading.Event] = None):
    # Chỉ chạy local recorder nếu hệ thống có sẵn ffmpeg
    if not shutil.which("ffmpeg") and not (FFMPEG_PATH and os.path.exists(FFMPEG_PATH)):
        return

    part_number = 1
    consecutive_failures = 0
    max_consecutive_failures = 4

    try:
        while True:
            if stop_event and stop_event.is_set():
                print(f"[⏹️] [@{user}] Nhận tín hiệu dừng từ người dùng.")
                break

            live_details = get_user_live_details_cached(user)
            is_live = live_details.get("is_live", False)
            room_id = live_details.get("room_id")
            is_sub_only = live_details.get("is_sub_only", False)

            if not is_live or not room_id:
                print(f"[🏁] [@{user}] Streamer hiện không live. Dừng ghi hình.")
                break

            now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            part_suffix = f"_part{part_number}" if part_number > 1 else ""
            user_dir = os.path.join(BASE_DIR, user)
            os.makedirs(user_dir, exist_ok=True)
            output_file = os.path.join(user_dir, f"{user}_{now_str}{part_suffix}.mp4")

            part_segments = []
            accumulated_seconds = 0.0
            print(f"[🔴] [@{user}] Bắt đầu tích lũy Phần {part_number} (Mục tiêu gom đủ 1 tiếng: {MAX_CHUNK_SECONDS}s)...")

            while accumulated_seconds < (300 if is_sub_only else (MAX_CHUNK_SECONDS - 60)):
                if stop_event and stop_event.is_set():
                    break

                target_duration = (300 - int(accumulated_seconds)) if is_sub_only else (MAX_CHUNK_SECONDS - int(accumulated_seconds))
                if target_duration <= 30:
                    break

                stream_url = None
                for s_att in range(3):
                    stream_url = recorder_core.get_live_stream_url(room_id, user=user)
                    if stream_url:
                        break
                    time.sleep(2.5)

                if not stream_url:
                    st_live, new_rid = recorder_core.check_live_status(user)
                    if st_live and new_rid:
                        room_id = new_rid
                        stream_url = recorder_core.get_live_stream_url(room_id, user=user)

                if not stream_url:
                    print(f"[!] [@{user}] Không lấy được link stream sau các lần thử. Kết thúc tích lũy Phần {part_number}.")
                    consecutive_failures += 1
                    time.sleep(5)
                    break

                seg_name = os.path.join(user_dir, f"{user}_{now_str}_p{part_number}_seg{len(part_segments)+1}.mp4")
                rec_res = recorder_core.record_stream_ffmpeg(
                    stream_url,
                    output_filename=seg_name,
                    target_user=user,
                    duration=target_duration,
                    stop_event=stop_event,
                    auto_sync_gdrive=False,
                    is_sub_only=is_sub_only
                )

                from auto_h264 import validate_playable_video
                is_valid, reason, dur = validate_playable_video(rec_res, min_duration=5.0, min_size_bytes=250000)

                if is_valid:
                    consecutive_failures = 0
                    part_segments.append(rec_res)
                    accumulated_seconds += dur
                    print(f"[✓] [@{user}] Thu đoạn {len(part_segments)} ({dur:.1f}s). Tích lũy Phần {part_number}: {accumulated_seconds:.1f}s / {MAX_CHUNK_SECONDS}s")
                else:
                    consecutive_failures += 1
                    print(f"[!] [API Server] Phân đoạn {len(part_segments)+1} của @{user} không đạt chuẩn ({reason}).")
                    if rec_res and os.path.exists(rec_res):
                        try:
                            os.remove(rec_res)
                        except Exception:
                            pass
                    if consecutive_failures >= max_consecutive_failures:
                        print(f"[!] [@{user}] Quá {max_consecutive_failures} lần lỗi thu luồng liên tiếp. Dừng tích lũy.")
                        break
                    time.sleep(5)

                if is_sub_only:
                    break

                if accumulated_seconds >= MAX_CHUNK_SECONDS - 60:
                    print(f"[⏱️ Đủ 1 tiếng] [@{user}] Phần {part_number} đã tích lũy đủ 1 tiếng ({accumulated_seconds:.1f}s)!")
                    break

                # Kiểm tra streamer còn live không để tiếp tục tích lũy
                time.sleep(3)
                curr_det = recorder_core.check_live_details(user)
                if not curr_det.get("is_live"):
                    time.sleep(4)
                    curr_det = recorder_core.check_live_details(user)

                if curr_det.get("is_live"):
                    if curr_det.get("room_id"):
                        room_id = curr_det.get("room_id")
                    print(f"[⏩] [@{user}] Luồng tạm gián đoạn sau {dur:.1f}s nhưng streamer VẪN ĐANG LIVE. Tự động thu tiếp nối vào Phần {part_number} (còn thiếu {MAX_CHUNK_SECONDS - int(accumulated_seconds)}s)...")
                else:
                    print(f"[🏁] [@{user}] Streamer đã xuống live sau {accumulated_seconds:.1f}s tích lũy.")
                    break

            if not part_segments:
                if consecutive_failures >= max_consecutive_failures:
                    break
                time.sleep(5)
                continue

            # Nếu nhận tín hiệu dừng / xóa streamer, hủy bỏ toàn bộ phân đoạn tạm và kết thúc ngay
            if stop_event and stop_event.is_set():
                print(f"[⏹️] [@{user}] Phát hiện yêu cầu dừng/xóa streamer. Hủy toàn bộ phân đoạn tạm dở.")
                for seg in part_segments:
                    if seg and os.path.exists(seg):
                        try:
                            os.remove(seg)
                        except Exception:
                            pass
                break

            # Ghép tất cả các đoạn thành 1 file MP4 duy nhất
            final_rec_file = output_file
            if len(part_segments) == 1:
                if os.path.exists(output_file) and output_file != part_segments[0]:
                    try:
                        os.remove(output_file)
                    except Exception:
                        pass
                try:
                    shutil.move(part_segments[0], output_file)
                    final_rec_file = output_file
                except Exception:
                    final_rec_file = part_segments[0]
            else:
                print(f"[🧩] [@{user}] Đang ghép nối {len(part_segments)} phân đoạn thành 1 file MP4 duy nhất cho Phần {part_number} ({accumulated_seconds:.1f}s)...")
                final_rec_file = concat_mp4_segments(part_segments, output_file)

            if stop_event and stop_event.is_set():
                if os.path.exists(final_rec_file):
                    try:
                        os.remove(final_rec_file)
                    except Exception:
                        pass
                break

            is_valid, reason, final_dur = validate_playable_video(final_rec_file, min_duration=5.0, min_size_bytes=250000)
            if not is_valid:
                print(f"[!] File Phần {part_number} không đạt chuẩn ({reason}). Bỏ qua.")
                if os.path.exists(final_rec_file):
                    try:
                        os.remove(final_rec_file)
                    except Exception:
                        pass
                continue

            consecutive_failures = 0
            print(f"[✓] [@{user}] Hoàn tất trọn vẹn Phần {part_number} ({final_dur:.1f}s): {os.path.basename(final_rec_file)}")
            # Kiểm tra thời lượng video:
            # Nếu video ngắn dưới 50 phút (< 3000s, chênh lệch 10p so với 60p): Đẩy vào Staging Queue trên Google Drive!
            if final_dur < 3000:
                print(f"[📦] [@{user}] Video Phần {part_number} ngắn hơn 50 phút ({final_dur/60:.1f}p < 50p). Chuyển vào Cloud Staging Queue trên Google Drive...")
                try:
                    import staging_queue
                    token = gdrive_manager.get_access_token()
                    q_res = staging_queue.add_to_staging_queue(user, final_rec_file, final_dur, access_token=token)
                    print(f"[📋] [@{user}] Trạng thái Staging Queue: {q_res.get('status')} - {q_res.get('message', '')}")
                except Exception as sq_err:
                    print(f"[!] [@{user}] Lỗi khi chuyển vào Staging Queue: {sq_err}")
            else:
                # Video đạt chuẩn >= 50 phút: Đồng bộ Supabase Storage & Database và tải lên Drive chính
                thumb_f = extract_middle_thumbnail(final_rec_file)
                try:
                    import supabase_sync
                    sz = os.path.getsize(final_rec_file)
                    supabase_sync.sync_recording_to_supabase(
                        user=user,
                        filename=os.path.basename(final_rec_file),
                        size_bytes=sz,
                        thumb_source=thumb_f,
                        source="api_server"
                    )
                except Exception as sb_err:
                    print(f"[!] Lỗi đồng bộ Supabase từ bg_record_worker: {sb_err}")

                # Upload Google Drive
                token = gdrive_manager.get_access_token()
                if token:
                    root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=token)
                    sub_id = gdrive_manager.find_or_create_folder(user, parent_id=root_id, access_token=token)
                    ok = gdrive_manager.upload_file_to_drive(final_rec_file, sub_id, access_token=token)
                    if ok:
                        try:
                            os.remove(final_rec_file)
                            print(f"[🗑️] [@{user}] Đã xóa video tạm Phần {part_number} sau khi upload Drive thành công.")
                        except Exception:
                            pass
                    if thumb_f and os.path.exists(thumb_f):
                        gdrive_manager.upload_file_to_drive(thumb_f, sub_id, access_token=token)
                        try:
                            os.remove(thumb_f)
                        except Exception:
                            pass

            part_number += 1

            if stop_event and stop_event.is_set():
                break

            # Kiểm tra streamer còn live không để tiếp tục phân đoạn tiếp theo
            time.sleep(3)
            curr_det = recorder_core.check_live_details(user)
            if not curr_det.get("is_live"):
                time.sleep(4)
                curr_det = recorder_core.check_live_details(user)

            if not curr_det.get("is_live"):
                print(f"[🏁] [@{user}] Streamer đã xuống live sau {part_number - 1} phần.")
                break
            else:
                print(f"[⏩] [@{user}] Streamer VẪN ĐANG LIVE! Tự động ghi hình nối tiếp Phần {part_number} (1 tiếng tiếp theo)...")

    except Exception as e:
        print(f"[!] Lỗi ghi hình worker: {e}")
    finally:
        with RECORDING_LOCK:
            ACTIVE_RECORDING_TASKS.pop(user, None)
        try:
            gdrive_manager.set_user_recording_status_drive(user, False)
        except Exception:
            pass

@app.post("/api/record/start")
@app.post("/api/record")
def start_record(req: RecordRequest, bg_tasks: BackgroundTasks):
    user = req.username.strip().replace("@", "").lower()
    
    # Đảm bảo streamer có trong danh sách theo dõi
    cfg = load_config()
    users = cfg.get("monitored_users", [])
    try:
        drive_users = gdrive_manager.load_streamers_from_drive()
        if drive_users is not None and isinstance(drive_users, list):
            users = drive_users
    except Exception:
        pass
    if user not in users:
        users.append(user)
        cfg["monitored_users"] = users
        save_config(cfg)
        try:
            gdrive_manager.save_streamers_to_drive(users)
        except Exception:
            pass

    # Tạo folder trên Google Drive
    try:
        gdrive_manager.create_streamer_folder_drive(user)
    except Exception:
        pass

    # Kiểm tra xem streamer có đang phát trực tiếp không (kèm chi tiết VIP Sub-Only)
    live_details = get_user_live_details_cached(user)
    is_live = live_details.get("is_live", False)
    room_id = live_details.get("room_id")
    is_sub_only = live_details.get("is_sub_only", False)
    is_preview = live_details.get("is_preview", False)

    has_ffmpeg = bool(shutil.which("ffmpeg") or (FFMPEG_PATH and os.path.exists(FFMPEG_PATH)))

    if is_live:
        if has_ffmpeg:
            stop_evt = threading.Event()
            with RECORDING_LOCK:
                ACTIVE_RECORDING_TASKS[user] = {"start_time": time.time(), "stop_event": stop_evt}
            try:
                gdrive_manager.set_user_recording_status_drive(user, True)
            except Exception:
                pass
            bg_tasks.add_task(bg_record_worker, user, req.duration_seconds, stop_evt)
            is_recording = True
        else:
            is_recording = False

        msg_prefix = f"@{user} đang phát trực tiếp"
        if is_sub_only:
            msg_prefix += " (🔒 VIP Sub-Only)"
        return {
            "message": f"{msg_prefix}! Hệ thống đang ghi hình phiên live.",
            "status": "recording" if is_recording else "live",
            "is_live": True,
            "is_recording": is_recording,
            "username": user,
            "room_id": room_id,
            "is_sub_only": is_sub_only,
            "is_preview": is_preview
        }
    else:
        with RECORDING_LOCK:
            ACTIVE_RECORDING_TASKS.pop(user, None)
        try:
            gdrive_manager.set_user_recording_status_drive(user, False)
        except Exception:
            pass
        return {
            "message": f"@{user} hiện đang ngoại tuyến (Offline). Bot 24/7 đã lưu vào danh sách và sẽ tự động ghi hình ngay khi họ live!",
            "status": "offline",
            "is_live": False,
            "is_recording": False,
            "username": user
        }

@app.post("/api/record/stop")
def stop_record(req: AddUserRequest):
    user = req.username.strip().replace("@", "").lower()
    with RECORDING_LOCK:
        task_info = ACTIVE_RECORDING_TASKS.pop(user, None)
        if task_info and isinstance(task_info, dict):
            se = task_info.get("stop_event")
            if se:
                se.set()
    try:
        gdrive_manager.set_user_recording_status_drive(user, False)
    except Exception:
        pass
    return {
        "message": f"Đã dừng ghi hình @{user}",
        "status": "offline",
        "is_recording": False,
        "username": user
    }

_RECORDINGS_CACHE = {"timestamp": 0, "data": []}
_RECORDINGS_CACHE_LOCK = threading.Lock()

def list_recordings_from_drive(access_token=None, force_refresh=False):
    """
    Quét danh sách toàn bộ video và thumbnail đã lưu trên Google Drive bằng ThreadPool song song.
    """
    global _RECORDINGS_CACHE
    now = time.time()
    if not force_refresh and (now - _RECORDINGS_CACHE["timestamp"] < 10) and _RECORDINGS_CACHE["data"]:
        return _RECORDINGS_CACHE["data"]

    recordings = []
    try:
        if not access_token:
            access_token = gdrive_manager.get_access_token()
        if not access_token:
            return recordings
            
        root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=access_token)
        headers = {"Authorization": f"Bearer {access_token}"}
        
        q_folders = f"'{root_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res_f = requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_folders, "fields": "files(id,name)"}, timeout=10)
        folders = res_f.json().get("files", [])

        def _fetch_folder_files(fold):
            uname = fold["name"]
            fid = fold["id"]
            q_files = f"'{fid}' in parents and trashed = false"
            res_files = requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_files, "fields": "files(id,name,mimeType,size,createdTime)"}, timeout=10)
            files = res_files.json().get("files", [])
            thumbs = {f["name"]: f["id"] for f in files if f["name"].endswith(".jpg")}
            folder_recs = []
            for f in files:
                fname = f["name"]
                if fname.endswith(".mp4"):
                    base_name = fname.replace(".mp4", "")
                    thumb_name = base_name + ".jpg"
                    thumb_id = thumbs.get(thumb_name)
                    
                    recorded_at = None
                    date_match = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})", fname)
                    if date_match:
                        d_str, t_str = date_match.groups()
                        recorded_at = f"{d_str} {t_str.replace('-', ':')}"
                    else:
                        recorded_at = f.get("createdTime", "")
                        
                    sz_bytes = int(f.get("size", 0))
                    # Loại bỏ triệt để các file rác / lỗi 0:00s dưới 250 KB
                    if sz_bytes < 250 * 1024:
                        continue
                    cdn_url = f"https://drive.usercontent.google.com/download?id={f['id']}&export=download&authuser=0"
                    folder_recs.append({
                        "filename": fname,
                        "user": uname,
                        "size_bytes": sz_bytes,
                        "size_mb": round(sz_bytes / (1024 * 1024), 2),
                        "recorded_at": recorded_at,
                        "created_at": f.get("createdTime"),
                        "thumbnail_url": f"/api/thumbnail/{uname}/{fname}",
                        "download_url": f"/api/download/{uname}/{fname}",
                        "cdn_download_url": cdn_url,
                        "drive_file_id": f["id"],
                        "drive_thumb_id": thumb_id,
                        "stream_url": f"/api/stream-video-id/{f['id']}",
                        "source": "google_drive"
                    })
            return folder_recs

        if folders:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(folders), 8)) as ex:
                results = ex.map(_fetch_folder_files, folders)
                for r in results:
                    recordings.extend(r)

        with _RECORDINGS_CACHE_LOCK:
            _RECORDINGS_CACHE["timestamp"] = now
            _RECORDINGS_CACHE["data"] = recordings
    except Exception as e:
        print(f"[!] Lỗi lấy danh sách video từ Google Drive: {e}")
    return recordings

@app.get("/api/recordings")
def list_recordings():
    """
    Trả về danh sách toàn bộ video đã ghi hình (từ cả Google Drive và ổ đĩa máy tính).
    Bao gồm: Ngày giờ bắt đầu quay (recorded_at), dung lượng, link ảnh thumbnail ở giữa video (50%).
    """
    merged_files = {}

    # 1. Lấy từ Google Drive (nơi Bot 24/7 tải video lên)
    drive_recordings = list_recordings_from_drive()
    for item in drive_recordings:
        merged_files[item["filename"]] = item

    # 2. Lấy từ ổ đĩa máy tính (nếu có)
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

    for u in users_set:
        u_dir = os.path.join(BASE_DIR, u)
        if os.path.exists(u_dir):
            for f in os.listdir(u_dir):
                if f.endswith(".mp4") and not f.endswith(".tmp.mp4"):
                    fp = os.path.join(u_dir, f)
                    st = os.stat(fp)
                    # Loại bỏ các file rác / lỗi 0:00s dưới 250 KB
                    if st.st_size < 250 * 1024:
                        continue
                    dur = get_video_duration(fp)
                    dur_fmt = f"{int(dur//60):02d}:{int(dur%60):02d}" if dur else "00:00"
                    
                    # Parse recorded_at từ tên file
                    recorded_at = None
                    date_match = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})", f)
                    if date_match:
                        d_str, t_str = date_match.groups()
                        recorded_at = f"{d_str} {t_str.replace('-', ':')}"
                    else:
                        recorded_at = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

                    merged_files[f] = {
                        "filename": f,
                        "user": u,
                        "size_bytes": st.st_size,
                        "size_mb": round(st.st_size / (1024 * 1024), 2),
                        "duration_seconds": dur,
                        "duration_formatted": dur_fmt,
                        "recorded_at": recorded_at,
                        "created_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
                        "thumbnail_url": f"/api/thumbnail/{u}/{f}",
                        "download_url": f"/api/download/{u}/{f}",
                        "stream_url": f"/api/stream-video/{u}/{f}",
                        "source": "local"
                    }

    files_list = list(merged_files.values())
    files_list.sort(key=lambda x: x.get("recorded_at") or x.get("created_at") or "", reverse=True)
    return {"total_files": len(files_list), "recordings": files_list}

@app.post("/api/recordings/sync-supabase")
def sync_supabase_endpoint(bg_tasks: BackgroundTasks):
    """
    Kích hoạt đồng bộ ngầm toàn bộ video và ảnh thumbnail lên Supabase Storage và Database.
    """
    def worker():
        try:
            import supabase_sync
            recs = list_recordings_from_drive()
            host_base = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("BASE_API_URL", "https://tiktok-api-i0o8.onrender.com")
            for r in recs:
                u = r["user"]
                fn = r["filename"]
                th_url = f"{host_base.rstrip('/')}/api/thumbnail/{u}/{fn}"
                supabase_sync.sync_recording_to_supabase(
                    user=u,
                    filename=fn,
                    size_bytes=r.get("size_bytes", 0),
                    recorded_at=r.get("recorded_at"),
                    created_at=r.get("created_at"),
                    thumb_source=th_url,
                    drive_file_id=r.get("drive_file_id"),
                    drive_thumb_id=r.get("drive_thumb_id"),
                    cdn_download_url=r.get("cdn_download_url"),
                    source="google_drive"
                )
        except Exception as e:
            print(f"[!] Lỗi sync supabase endpoint worker: {e}")

    bg_tasks.add_task(worker)
    return {"status": "started", "message": "Đang đồng bộ toàn bộ video và thumbnail lên Supabase trong nền"}

@app.get("/api/thumbnail/{user}/{filename}")
def get_thumbnail(user: str, filename: str):
    """
    Trả về ảnh xem trước được cắt từ chính giữa video (50% thời lượng).
    Hỗ trợ đọc từ cả ổ đĩa local và Google Drive.
    """
    user = user.strip().replace("@", "").lower()
    clean_name = filename.replace(".jpg", "").replace(".mp4", "")
    
    video_path = os.path.join(BASE_DIR, user, clean_name + ".mp4")
    thumb_path = os.path.join(BASE_DIR, user, clean_name + ".jpg")

    # 1. Nếu thumbnail đã có sẵn dưới máy local
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
        return FileResponse(path=thumb_path, media_type="image/jpeg", filename=clean_name + ".jpg")

    # 2. Nếu chưa có nhưng video local tồn tại -> Cắt thumbnail ngay lập tức từ giữa video
    if os.path.exists(video_path):
        out = extract_middle_thumbnail(video_path, thumb_path)
        if out and os.path.exists(out):
            return FileResponse(path=out, media_type="image/jpeg", filename=clean_name + ".jpg")

    # 3. Tìm thumbnail hoặc video trên Google Drive
    try:
        tok = gdrive_manager.get_access_token()
        if tok:
            root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=tok)
            user_fid = gdrive_manager.find_or_create_folder(user, parent_id=root_id, access_token=tok)
            headers = {"Authorization": f"Bearer {tok}"}
            
            # Tìm ảnh .jpg trước
            q_thumb = f"name = '{clean_name}.jpg' and '{user_fid}' in parents and trashed = false"
            res_th = requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_thumb, "fields": "files(id)"}, timeout=10)
            th_files = res_th.json().get("files", [])
            if th_files:
                img_id = th_files[0]["id"]
                img_res = requests.get(f"https://www.googleapis.com/drive/v3/files/{img_id}?alt=media", headers=headers, timeout=15)
                if img_res.status_code == 200:
                    return Response(content=img_res.content, media_type="image/jpeg")

            # Nếu chưa có ảnh .jpg riêng, tìm file .mp4 để lấy thumbnail tích hợp sẵn của Google Drive
            q_vid = f"name = '{clean_name}.mp4' and '{user_fid}' in parents and trashed = false"
            res_vid = requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_vid, "fields": "files(id,thumbnailLink)"}, timeout=10)
            vid_files = res_vid.json().get("files", [])
            if vid_files:
                vid_id = vid_files[0]["id"]
                thumb_link = vid_files[0].get("thumbnailLink")
                if thumb_link:
                    return RedirectResponse(url=thumb_link, status_code=302)
                return RedirectResponse(url=f"https://drive.google.com/thumbnail?id={vid_id}&sz=w800", status_code=302)
    except Exception as e:
        print(f"[!] Lỗi tải thumbnail từ Google Drive: {e}")

    raise HTTPException(status_code=404, detail="Không tìm thấy video hoặc không thể tạo ảnh xem trước")

@app.get("/api/download/{user}/{filename}")
def download_video(user: str, filename: str):
    """
    Tải video về máy qua CDN tốc độ cao của Google Edge (Hỗ trợ IDM đa luồng, tua video Range 206).
    """
    user = user.strip().replace("@", "").lower()
    fp = os.path.join(BASE_DIR, user, filename)
    if os.path.exists(fp):
        return FileResponse(path=fp, media_type="video/mp4", filename=filename)
    
    # Tìm kiếm trên Google Drive và chuyển hướng trực tiếp đến CDN tải tốc độ cao
    try:
        tok = gdrive_manager.get_access_token()
        if tok:
            root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=tok)
            user_fid = gdrive_manager.find_or_create_folder(user, parent_id=root_id, access_token=tok)
            headers = {"Authorization": f"Bearer {tok}"}
            q_vid = f"name = '{filename}' and '{user_fid}' in parents and trashed = false"
            res_vid = requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_vid, "fields": "files(id)"}, timeout=10)
            vid_files = res_vid.json().get("files", [])
            if vid_files:
                file_id = vid_files[0]["id"]
                # Cấp quyền đọc công khai để CDN tải mượt mà không cần xác thực
                gdrive_manager.make_file_public(file_id, access_token=tok)
                cdn_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0"
                return RedirectResponse(url=cdn_url, status_code=302)
    except Exception as e:
        print(f"[!] Lỗi tìm video trên Google Drive: {e}")

    raise HTTPException(status_code=404, detail="File video không tồn tại")

@app.get("/api/cdn/{user}/{filename}")
def get_cdn_url(user: str, filename: str, redirect: bool = False):
    """
    Cung cấp link CDN tải trực tiếp tốc độ cao tối đa (Gigabit Google Edge CDN).
    - Mặc định trả về JSON chứa link CDN và thông số kỹ thuật.
    - Nếu thêm ?redirect=true: Tự động chuyển hướng tải ngay lập tức.
    """
    user = user.strip().replace("@", "").lower()
    try:
        tok = gdrive_manager.get_access_token()
        if tok:
            root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=tok)
            user_fid = gdrive_manager.find_or_create_folder(user, parent_id=root_id, access_token=tok)
            headers = {"Authorization": f"Bearer {tok}"}
            q_vid = f"name = '{filename}' and '{user_fid}' in parents and trashed = false"
            res_vid = requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_vid, "fields": "files(id,size,createdTime)"}, timeout=10)
            vid_files = res_vid.json().get("files", [])
            if vid_files:
                file_id = vid_files[0]["id"]
                sz_bytes = int(vid_files[0].get("size", 0))
                gdrive_manager.make_file_public(file_id, access_token=tok)
                cdn_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0"
                
                if redirect:
                    return RedirectResponse(url=cdn_url, status_code=302)
                    
                return {
                    "filename": filename,
                    "user": user,
                    "cdn_download_url": cdn_url,
                    "size_mb": round(sz_bytes / (1024 * 1024), 2),
                    "size_bytes": sz_bytes,
                    "cdn_provider": "Google Cloud Global Edge CDN",
                    "supports_idm_multithread": True,
                    "supports_range_seeking": True,
                    "note": "Link CDN trực tiếp, không giới hạn băng thông, hỗ trợ tải đa luồng và tua video tức thì."
                }
    except Exception as e:
        print(f"[!] Lỗi CDN trên Google Drive: {e}")

@app.get("/api/stream-video-id/{file_id}")
def stream_video_by_id(file_id: str, request: Request):
    """
    Phát trực tiếp video từ Google Drive với hỗ trợ tua timeline tức thì (HTTP 206 Partial Content và Range Request).
    """
    tok = gdrive_manager.get_access_token()
    if not tok:
        raise HTTPException(status_code=500, detail="Không có quyền truy cập Google Drive")

    req_headers = {"Authorization": f"Bearer {tok}"}
    range_header = request.headers.get("range")
    if range_header:
        req_headers["Range"] = range_header

    try:
        drive_resp = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
            headers=req_headers,
            stream=True,
            timeout=15
        )

        resp_headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": "video/mp4",
            "Cache-Control": "public, max-age=3600",
        }
        if "Content-Range" in drive_resp.headers:
            resp_headers["Content-Range"] = drive_resp.headers["Content-Range"]
        if "Content-Length" in drive_resp.headers:
            resp_headers["Content-Length"] = drive_resp.headers["Content-Length"]
        if "Content-Disposition" in drive_resp.headers:
            resp_headers["Content-Disposition"] = "inline"

        return StreamingResponse(
            drive_resp.iter_content(chunk_size=128 * 1024),
            status_code=drive_resp.status_code,
            headers=resp_headers
        )
    except Exception as e:
        print(f"[!] Lỗi stream video ID {file_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Lỗi khi phát video: {e}")

@app.get("/api/stream-video/{user}/{filename}")
def stream_video(user: str, filename: str, request: Request):
    """
    Phát trực tiếp video theo username và filename.
    Hỗ trợ cả file local và file lưu trên Google Drive với timeline scrub (HTTP 206 Partial Content).
    """
    user = user.strip().replace("@", "").lower()
    local_path = os.path.join(BASE_DIR, user, filename)

    # 1. Nếu file có sẵn dưới local
    if os.path.exists(local_path):
        return FileResponse(
            path=local_path,
            media_type="video/mp4",
            filename=filename,
            headers={"Accept-Ranges": "bytes"}
        )

    # 2. Tìm kiếm file ID trên Google Drive và chuyển sang stream theo ID
    try:
        tok = gdrive_manager.get_access_token()
        if tok:
            root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=tok)
            user_fid = gdrive_manager.find_or_create_folder(user, parent_id=root_id, access_token=tok)
            headers = {"Authorization": f"Bearer {tok}"}
            q_vid = f"name = '{filename}' and '{user_fid}' in parents and trashed = false"
            res_vid = requests.get(
                "https://www.googleapis.com/drive/v3/files",
                headers=headers,
                params={"q": q_vid, "fields": "files(id)"},
                timeout=10
            )
            vid_files = res_vid.json().get("files", [])
            if vid_files:
                return stream_video_by_id(vid_files[0]["id"], request)
    except Exception as e:
        print(f"[!] Lỗi tìm video stream {user}/{filename}: {e}")

    raise HTTPException(status_code=404, detail="Không tìm thấy file video để phát trực tiếp")

@app.post("/api/gdrive/sync")
def trigger_gdrive_sync(bg_tasks: BackgroundTasks):
    bg_tasks.add_task(gdrive_manager.sync_all_to_gdrive)
    return {"message": "Đã kích hoạt tiến trình đồng bộ toàn bộ video lên Google Drive ngầm"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
