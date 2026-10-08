import os
import sys
import pathlib
import json
import time
import shutil
import threading
import subprocess
import re
import requests
import concurrent.futures
import gc
from datetime import datetime
from typing import Optional
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, RedirectResponse
from config_lock import config_transaction


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
import supabase_sync
from contextlib import asynccontextmanager

ACTIVE_RECORDING_TASKS = {}
RECORDING_LOCK = threading.Lock()
LIVE_CACHE_LOCK = threading.Lock()
_RECORDINGS_CACHE = {"timestamp": 0, "data": []}
_RECORDINGS_CACHE_LOCK = threading.Lock()

def shutdown_all_recording_tasks():
    """
    Dừng an toàn tất cả các tiến trình ghi hình khi server tắt hoặc nhận tín hiệu SIGTERM/SIGINT.
    Gửi tín hiệu stop_event để FFmpeg chốt moov atom chuẩn MP4, tránh hỏng video.
    """
    with RECORDING_LOCK:
        active_list = list(ACTIVE_RECORDING_TASKS.items())
        for u, info in active_list:
            if isinstance(info, dict):
                se = info.get("stop_event")
                if se:
                    se.set()
    # Chờ tối đa 6 giây để worker và FFmpeg chốt moov atom an toàn
    for u, info in active_list:
        t = info.get("thread") if isinstance(info, dict) else None
        if t and hasattr(t, "join") and t.is_alive():
            t.join(timeout=6)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if get_api_pin():
        print("[🔐] Đã BẬT xác thực PIN cho mọi endpoint POST/PUT/PATCH/DELETE.")
    else:
        print('[⚠️] Chưa bật PIN cho API: mọi POST/DELETE đều công khai nếu expose ra Internet. '
              'Đặt biến môi trường RECORD_PIN (hoặc khóa "api_pin" trong config.json) để bật xác thực.')
    yield
    shutdown_all_recording_tasks()

app = FastAPI(
    title="TikTok Live Recorder & Cloud Sync API",
    description="API kết nối web: thêm streamer, kiểm tra live, ghi hình chuẩn H.264, cắt ảnh xem trước (thumbnail) giữa video và tải video tốc độ cao.",
    version="2.1.0",
    lifespan=lifespan
)

# Enable CORS for any external web integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------- #
# Xác thực PIN cho các endpoint thay đổi dữ liệu (POST/PUT/PATCH/DELETE)
# - Kích hoạt khi khai báo biến môi trường RECORD_PIN HOẶC khóa "api_pin" trong config.json.
# - Chưa khai báo -> giữ nguyên hành vi cũ (không auth) để không làm đứt tích hợp cũ,
#   đồng thời in cảnh báo khi khởi động.
# - Client gửi PIN qua header `x-record-pin` hoặc `Authorization: Bearer <pin>`
#   (web-truyen proxy đã gửi `x-record-pin` sẵn).
# --------------------------------------------------------------------------- #
_PIN_CACHE = {"value": None, "checked_at": 0.0}
_PIN_CACHE_TTL = 5.0

def get_api_pin() -> str:
    env_pin = (os.environ.get("RECORD_PIN", "") or "").strip()
    if env_pin:
        return env_pin
    now = time.time()
    if now - _PIN_CACHE["checked_at"] > _PIN_CACHE_TTL:
        pin = ""
        try:
            cfg = load_config()
            pin = str(cfg.get("api_pin", "") or "").strip()
        except Exception:
            pin = ""
        _PIN_CACHE["value"] = pin
        _PIN_CACHE["checked_at"] = now
    return _PIN_CACHE["value"] or ""

def _extract_pin(request: Request) -> str:
    pin = (request.headers.get("x-record-pin") or "").strip()
    if pin:
        return pin
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""

@app.middleware("http")
async def enforce_pin_on_mutations(request: Request, call_next):
    # Preflight CORS phải đi thẳng vào CORSMiddleware (nằm phía trong).
    if request.method == "OPTIONS" or request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return await call_next(request)

    required = get_api_pin()
    if required and _extract_pin(request) != required:
        # Middleware này chạy NGOÀI CORSMiddleware -> tự gắn header CORS,
        # nếu không trình duyệt sẽ báo lỗi CORS thay vì 401.
        headers = {}
        origin = request.headers.get("origin")
        if origin:
            headers = {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Headers": request.headers.get("access-control-request-headers", "*"),
                "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            }
        return JSONResponse(
            status_code=401,
            content={"detail": "PIN không hợp lệ hoặc thiếu header x-record-pin", "status": "unauthorized"},
            headers=headers,
        )
    return await call_next(request)

# load -> mutate -> save trên config.json phải nguyên tử: 2 request chồng lấp
# (thêm 1 streamer / xóa 1 streamer / start_record tự thêm user) sẽ làm mất 1 trong 2 thay đổi.
# config_transaction() cũng khóa liên tiến trình với cloud_daemon / tiktok_recorder.

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
    tmp_path = CONFIG_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        os.replace(tmp_path, CONFIG_FILE)
    except Exception as e:
        print(f"[!] Lỗi ghi config: {e}")
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except Exception: pass

_DURATION_CACHE = {}
_DURATION_CACHE_LOCK = threading.Lock()

def get_video_duration(filepath):
    """Lấy thời lượng video tính bằng giây, có cache theo mtime để không spawn subprocess lặp lại."""
    try:
        mtime = os.path.getmtime(filepath)
        with _DURATION_CACHE_LOCK:
            cached = _DURATION_CACHE.get(filepath)
            if cached and cached[0] == mtime:
                return cached[1]
    except Exception:
        mtime = None

    cmd = [FFMPEG_PATH, "-i", filepath]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=6)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", p.stderr)
        if m:
            hours = int(m.group(1))
            mins = int(m.group(2))
            secs = float(m.group(3))
            dur = round(hours * 3600 + mins * 60 + secs, 2)
            if mtime is not None:
                with _DURATION_CACHE_LOCK:
                    if len(_DURATION_CACHE) > 100:
                        _DURATION_CACHE.pop(next(iter(_DURATION_CACHE)), None)
                    _DURATION_CACHE[filepath] = (mtime, dur)
            return dur
    except Exception:
        pass
    return None

# ponytail: reuse auto_h264.extract_middle_thumbnail
extract_middle_thumbnail = auto_h264.extract_middle_thumbnail


# ---------------- LIVE STATUS CACHE ----------------
LIVE_CACHE = {}  # {username: {"is_live": bool, "room_id": str, "is_sub_only": bool, "is_preview": bool, "timestamp": float}}
LIVE_CACHE_TTL = 60.0  # Cache 60 giây — frontend poll mỗi 25s sẽ dùng cache, giảm tải RAM và network
LIVE_CACHE_OFFLINE_TTL = 15.0  # Cache 15 giây khi streamer offline để phát hiện live mới tức thì

def get_user_live_details_cached(user: str, force_refresh: bool = False) -> dict:
    user = user.strip().replace("@", "").lower()
    now = time.time()
    with LIVE_CACHE_LOCK:
        cached = LIVE_CACHE.get(user)
        if not force_refresh and cached and "is_sub_only" in cached:
            ttl = LIVE_CACHE_TTL if cached.get("is_live") else LIVE_CACHE_OFFLINE_TTL
            if (now - cached.get("timestamp", 0) < ttl):
                return cached.copy()

    details = {
        "is_live": False,
        "room_id": None,
        "is_sub_only": False,
        "is_preview": False,
        "avatar_thumb": None,
        "nickname": None,
        "timestamp": now
    }
    probe_ok = False
    try:
        raw = recorder_core.check_live_details(user)
        details["is_live"] = raw.get("is_live", False)
        details["room_id"] = raw.get("room_id")
        details["is_sub_only"] = raw.get("is_sub_only", False)
        details["is_preview"] = raw.get("is_preview", False)
        details["avatar_thumb"] = raw.get("avatar_thumb")
        details["nickname"] = raw.get("nickname")
        probe_ok = True
    except Exception as probe_err:
        # Chỉ ghi cache khi probe THẬT SỰ trả lời được. Một lỗi mạng tạm thời
        # mà vẫn lưu is_live=False vào cache 60s sẽ làm /api/record từ chối khởi động ghi hình.
        try:
            is_live, room_id = recorder_core.check_user_live(user)
            details["is_live"] = is_live
            details["room_id"] = room_id
            probe_ok = True
        except Exception as fb_err:
            print(f"[!] [LiveCache] Không kiểm tra được trạng thái live của @{user}: {probe_err} / {fb_err}")

    if probe_ok:
        with LIVE_CACHE_LOCK:
            LIVE_CACHE[user] = details.copy()
    return details.copy()

def get_user_live_status_cached(user: str):
    d = get_user_live_details_cached(user)
    return d["is_live"], d["room_id"]

def is_local_recorder_running_for_user(user: str) -> bool:
    """
    Kiểm tra xem có tiến trình ghi hình cục bộ nào (in-memory task)
    đang thực sự chạy cho streamer này không.
    """
    user_clean = user.strip().replace("@", "").lower()
    with RECORDING_LOCK:
        task = ACTIVE_RECORDING_TASKS.get(user_clean)
        if task:
            if isinstance(task, dict):
                t = task.get("thread")
                if t and hasattr(t, "is_alive") and not t.is_alive():
                    return False
            return True
    return False

def clean_zombie_recordings(live_statuses=None):
    """
    Tự động quét và dọn dẹp các user bị kẹt trạng thái ma (Zombie Recording) trong Drive & Supabase
    nếu họ offline trên TikTok và không có PID recorder cục bộ nào đang chạy.
    """
    try:
        drive_details = gdrive_manager.load_active_recordings_from_drive(as_details=True) or []
        supa_active = []
        try:
            supa_active = supabase_sync.get_active_recordings_from_supabase() or []
        except Exception:
            pass

        now_ts = int(time.time())
        candidates = {}

        # Nguồn 1: Google Drive active_recordings.json
        for item in drive_details:
            u = item.get("username") if isinstance(item, dict) else str(item)
            u = u.strip().replace("@", "").lower()
            if u:
                candidates[u] = item.get("updated_at", 0) if isinstance(item, dict) else 0

        # Nguồn 2: Supabase tiktok_streamers (is_recording = true)
        for su in supa_active:
            u = su.strip().replace("@", "").lower()
            if u and u not in candidates:
                candidates[u] = 0

        if not candidates:
            return []

        zombies_cleaned = []
        for u, updated_at in candidates.items():
            # Nếu đang có tiến trình ghi hình cục bộ trên máy này -> không phải zombie
            if is_local_recorder_running_for_user(u):
                continue

            # Heartbeat check: Nếu vừa cập nhật trong 5 phút -> Cloud Runner đang chạy
            if updated_at and (now_ts - updated_at) < 300:
                continue

            # Kiểm tra trạng thái thực tế trên TikTok
            is_live = False
            if live_statuses and u in live_statuses:
                val = live_statuses[u]
                is_live = val.get("is_live", False) if isinstance(val, dict) else (val[0] if isinstance(val, (tuple, list)) else bool(val))
            else:
                is_live, _ = get_user_live_status_cached(u)

            if not is_live:
                print(f"[🧟 Zombie Cleaner] Phát hiện streamer @{u} bị kẹt trạng thái ma (Offline trên TikTok & không recorder cục bộ). Đang dọn dẹp...")
                try:
                    threading.Thread(target=gdrive_manager.set_user_recording_status_drive, args=(u, False), daemon=True).start()
                except Exception:
                    pass
                try:
                    threading.Thread(target=supabase_sync.update_streamer_status_supabase, args=(u, False, False, None, None, None, "offline"), daemon=True).start()
                except Exception:
                    pass
                zombies_cleaned.append(u)

        return zombies_cleaned
    except Exception as e:
        print(f"[!] Lỗi dọn dẹp Zombie Recording: {e}")
        return []

def extract_tiktok_username(raw: str) -> str:
    """Trích xuất và chuẩn hóa TikTok username từ link hoặc chuỗi nhập liệu bất kỳ."""
    if not raw:
        return ""
    s = str(raw).strip()
    # 1. Tìm pattern /@username
    m = re.search(r"/@([a-zA-Z0-9_.-]+)", s)
    if m:
        return m.group(1).lower()
    # 2. Nếu là URL tiktok.com/username
    if "tiktok.com" in s:
        s = s.split("?")[0].split("#")[0].rstrip("/")
        parts = s.split("/")
        if parts:
            cand = parts[-1].lstrip("@")
            if cand and not cand.endswith(".com"):
                return re.sub(r"[^a-z0-9_.-]", "", cand.lower())
    # 3. Chuỗi thường
    s = s.strip().lstrip("@").strip().lower()
    return re.sub(r"[^a-z0-9_.-]", "", s)

class AddUserRequest(BaseModel):
    username: str

class RecordRequest(BaseModel):
    username: str
    duration_seconds: Optional[int] = None

@app.get("/api/health")
@app.get("/api/status")
def health_check():
    active = set()
    try:
        drive_act = gdrive_manager.load_active_recordings_from_drive() or []
        active.update(drive_act)
    except Exception:
        pass
    try:
        supa_act = supabase_sync.get_active_recordings_from_supabase() or []
        active.update(supa_act)
    except Exception:
        pass
    with RECORDING_LOCK:
        active_keys = list(ACTIVE_RECORDING_TASKS.keys())
    active.update(active_keys)
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
    try:
        supa_act = supabase_sync.get_active_recordings_from_supabase() or []
        recording.update(supa_act)
    except Exception:
        pass
    with RECORDING_LOCK:
        active_keys = list(ACTIVE_RECORDING_TASKS.keys())
    recording.update(active_keys)

    # Danh sách các user đang phát live trên TikTok (dùng cache tức thời 0ms)
    live_streamers = []
    cfg = load_config()
    users = cfg.get("monitored_users", [])
    try:
        drive_users = gdrive_manager.load_streamers_from_drive()
        if drive_users is not None and isinstance(drive_users, list):
            users = drive_users
    except Exception:
        pass

    with LIVE_CACHE_LOCK:
        live_streamers = [u for u in users if LIVE_CACHE.get(u, {}).get("is_live")]

    all_recording = list(recording)
    return {
        "total_active": len(all_recording),
        "active_streamers": all_recording,
        "live_streamers": live_streamers,
        "total_live": len(live_streamers),
        "status": "recording" if all_recording else "idle"
    }

@app.get("/api/users")
def get_users(check_live: bool = True, fresh: bool = False):
    try:
        users_list = []

        # 1. Nguồn dữ liệu số 1: Supabase Database (đồng bộ tức thì từ Web)
        supa_users = []
        supa_streamers_map = {}
        try:
            supa_users = supabase_sync.fetch_streamers_from_supabase()
            if supa_users:
                users_list.extend(supa_users)
        except Exception as e:
            print(f"[!] Lỗi nạp streamers từ Supabase: {e}")

        try:
            supa_details = supabase_sync.fetch_streamers_details_from_supabase()
            if supa_details:
                for sd in supa_details:
                    u = sd.get("username")
                    if u:
                        supa_streamers_map[u] = sd
                        if u not in users_list:
                            users_list.append(u)
        except Exception:
            pass

        # 2. Nguồn dữ liệu số 2: Google Drive
        drive_users = []
        d_users = None
        try:
            d_users = gdrive_manager.load_streamers_from_drive()
            if d_users is not None and isinstance(d_users, list):
                drive_users = [u.strip().replace("@", "").lower() for u in d_users if u.strip()]
                users_list.extend(drive_users)
        except Exception as e:
            print(f"[!] Lỗi nạp streamers từ Drive: {e}")

        # 3. Nguồn dữ liệu số 3: config.json local (chỉ dự phòng khởi động khi cả Supabase và Drive đều rỗng/offline)
        if not users_list:
            cfg = load_config()
            cfg_users = cfg.get("monitored_users", [])
            if cfg_users:
                users_list.extend(cfg_users)

        # Khử trùng lặp và giữ thứ tự chuẩn
        users = list(dict.fromkeys([u.strip().replace("@", "").lower() for u in users_list if u.strip()]))

        # Tự động đồng bộ lên Drive nếu Supabase có streamer mới (chạy nền để không block GET API)
        if d_users is not None and supa_users and (set(supa_users) - set(drive_users)):
            def _bg_drive_folder_sync(all_u, new_u):
                try:
                    gdrive_manager.save_streamers_to_drive(all_u)
                    for nu in new_u:
                        gdrive_manager.create_streamer_folder_drive(nu)
                except Exception:
                    pass
            threading.Thread(target=_bg_drive_folder_sync, args=(users, set(supa_users) - set(drive_users)), daemon=True).start()

        # Tự động đồng bộ lên Supabase nếu Drive có streamer mới (chạy nền để không block GET API)
        if d_users is not None and (set(drive_users) - set(supa_users)):
            def _bg_supa_sync(missing_u):
                for mu in missing_u:
                    try:
                        supabase_sync.add_streamer_to_supabase(mu)
                    except Exception:
                        pass
            threading.Thread(target=_bg_supa_sync, args=(set(drive_users) - set(supa_users),), daemon=True).start()

        active_users = set()
        try:
            drive_act = gdrive_manager.load_active_recordings_from_drive()
            if drive_act:
                active_users.update(drive_act)
        except Exception:
            pass
        # Bổ sung active recordings từ Supabase (bảo đảm nhận diện ngay cả khi máy chủ không có Google Drive token, vd Render)
        try:
            for u, sd in supa_streamers_map.items():
                if sd.get("is_recording"):
                    active_users.add(u)
        except Exception:
            pass
        with RECORDING_LOCK:
            active_keys = list(ACTIVE_RECORDING_TASKS.keys())
        active_users.update(active_keys)
        
        # Probe live status: Khi fresh=True, probe toàn bộ streamer.
        # Khi fresh=False, probe các streamer chưa có recorder cục bộ
        local_rec_users = set()
        with RECORDING_LOCK:
            local_rec_users.update(ACTIVE_RECORDING_TASKS.keys())
        users_to_probe = list(users) if fresh else [u for u in users if u not in local_rec_users]
        live_statuses = {}
        if check_live and users_to_probe:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(users_to_probe), 8)) as executor:
                future_to_user = {executor.submit(get_user_live_details_cached, u, fresh): u for u in users_to_probe}
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

        # Tự động đồng bộ trạng thái live mới phát hiện lên Supabase chạy nền
        if live_statuses:
            def _bg_sync_supa_live(live_map):
                for usr, d_info in live_map.items():
                    if d_info.get("is_live"):
                        supabase_sync.update_streamer_status_supabase(
                            usr,
                            is_live=True,
                            room_id=d_info.get("room_id"),
                            nickname=d_info.get("nickname"),
                            avatar_thumb=d_info.get("avatar_thumb"),
                            status="live"
                        )
            threading.Thread(target=_bg_sync_supa_live, args=(live_statuses,), daemon=True).start()

        result = []
        for u in users:
            is_recording = (u in active_users)
            det = live_statuses.get(u) or LIVE_CACHE.get(u, {})
            sd = supa_streamers_map.get(u, {})

            # Streamer offline trên TikTok và không có recorder cục bộ -> dứt khoát offline
            if not is_local_recorder_running_for_user(u) and det and not det.get("is_live", False):
                is_recording = False
                is_live = False
            else:
                is_live = det.get("is_live", False) or is_recording or sd.get("is_live", False)

            room_id = det.get("room_id") or sd.get("room_id")
            avatar_thumb = det.get("avatar_thumb") or sd.get("avatar_thumb")
            nickname = det.get("nickname") or sd.get("nickname")
            is_sub_only = det.get("is_sub_only", False) or sd.get("is_sub_only", False)
            is_preview = det.get("is_preview", False) or sd.get("is_preview", False)

            if is_recording:
                status_str = "recording"
                is_live = True
            elif is_live:
                status_str = "live"
            else:
                status_str = "offline"

            result.append({
                "username": u,
                "nickname": nickname,
                "avatar_thumb": avatar_thumb,
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
    finally:
        # Thu hồi RAM sau khi xử lý endpoint nặng (curl_cffi C-level memory)
        gc.collect()

@app.get("/api/memory")
def get_memory_usage():
    """Endpoint giám sát RAM thời gian thực trên Render — giúp phát hiện rò rỉ bộ nhớ sớm."""
    try:
        import psutil
        proc = psutil.Process()
        mem = proc.memory_info()
        rss_mb = mem.rss / (1024 * 1024)
        vms_mb = mem.vms / (1024 * 1024)
    except Exception:
        try:
            import resource
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = rss_kb / 1024
            vms_mb = 0
        except Exception:
            rss_mb = 0
            vms_mb = 0
    if rss_mb > 300:
        # Chủ động dọn dẹp các cache khi RAM chạm ngưỡng 300MB để không bao giờ chạm tới 512MB
        try:
            with LIVE_CACHE_LOCK:
                LIVE_CACHE.clear()
            with _RECORDINGS_CACHE_LOCK:
                _RECORDINGS_CACHE["data"] = []
                _RECORDINGS_CACHE["timestamp"] = 0
            with _DURATION_CACHE_LOCK:
                _DURATION_CACHE.clear()
            gc.collect()
        except Exception:
            pass

    with RECORDING_LOCK:
        active_count = len(ACTIVE_RECORDING_TASKS)

    return {
        "rss_mb": round(rss_mb, 2),
        "vms_mb": round(vms_mb, 2),
        "render_limit_mb": 512,
        "cache_ttl_seconds": LIVE_CACHE_TTL,
        "live_cache_entries": len(LIVE_CACHE),
        "active_recordings": active_count,
        "gc_counts": gc.get_count(),
        "warning": "HIGH" if rss_mb > 350 else ("MEDIUM" if rss_mb > 200 else "LOW")
    }

@app.post("/api/users")
def add_user(req: AddUserRequest, bg_tasks: BackgroundTasks):
    user = extract_tiktok_username(req.username)
    if not user or user in (".", ".."):
        raise HTTPException(status_code=400, detail="Tên tài khoản không hợp lệ")

    users_list = []
    # 1. Nguồn Supabase
    try:
        supa_users = supabase_sync.fetch_streamers_from_supabase()
        if supa_users:
            users_list.extend(supa_users)
    except Exception:
        pass

    # 2. Nguồn Google Drive
    try:
        drive_users = gdrive_manager.load_streamers_from_drive()
        if drive_users is not None and isinstance(drive_users, list):
            users_list.extend([u.strip().replace("@", "").lower() for u in drive_users if u.strip()])
    except Exception:
        pass

    # 3. Nguồn Local config
    cfg = load_config()
    cfg_users = cfg.get("monitored_users", [])
    if cfg_users:
        users_list.extend(cfg_users)

    # Khử trùng lặp
    users = list(dict.fromkeys([u.strip().replace("@", "").lower() for u in users_list if u.strip()]))
    already_in = (user in users)
    if not already_in:
        users.insert(0, user)

    # Cập nhật config local
    try:
        with config_transaction():
            c = load_config()
            c["monitored_users"] = users
            save_config(c)
    except Exception:
        pass

    # Đồng bộ tức thì lên Supabase Database
    try:
        supabase_sync.add_streamer_to_supabase(user)
    except Exception:
        pass

    # Tác vụ nặng (lưu Drive, tạo thư mục Drive, check live và kích hoạt quay) chạy nền để phản hồi HTTP ngay tức thì (~50ms)
    def _bg_post_add_tasks(usr, all_u):
        try:
            gdrive_manager.save_streamers_to_drive(all_u)
        except Exception:
            pass
        try:
            gdrive_manager.create_streamer_folder_drive(usr)
        except Exception:
            pass
        try:
            with LIVE_CACHE_LOCK:
                LIVE_CACHE.pop(usr, None)
            live_details = get_user_live_details_cached(usr, fresh=True)
            if live_details.get("is_live"):
                has_ffmpeg = bool(shutil.which("ffmpeg") or (FFMPEG_PATH and os.path.exists(FFMPEG_PATH)))
                if has_ffmpeg:
                    with RECORDING_LOCK:
                        if usr not in ACTIVE_RECORDING_TASKS:
                            stop_evt = threading.Event()
                            ACTIVE_RECORDING_TASKS[usr] = {"start_time": time.time(), "stop_event": stop_evt}
                            try:
                                gdrive_manager.set_user_recording_status_drive(usr, True)
                            except Exception:
                                pass
                            threading.Thread(target=bg_record_worker, args=(usr, None, stop_evt), daemon=True).start()
        except Exception:
            pass

    if bg_tasks:
        bg_tasks.add_task(_bg_post_add_tasks, user, users)
    else:
        threading.Thread(target=_bg_post_add_tasks, args=(user, users), daemon=True).start()

    msg = f"@{user} đã có trong danh sách theo dõi" if already_in else f"Đã thêm @{user} vào danh sách theo dõi"

    return {
        "ok": True,
        "status": "success",
        "message": msg,
        "username": user,
        "is_recording": False,
        "gdrive_status": "Đang chuẩn bị thư mục Google Drive trong nền",
        "gdrive_folder_id": None,
        "users": users
    }

@app.delete("/api/users/{username}")
def delete_user(username: str, delete_files: bool = True, bg_tasks: BackgroundTasks = None):
    user = extract_tiktok_username(username)
    if not user or user in (".", ".."):
        raise HTTPException(status_code=400, detail="Tên streamer không hợp lệ")

    # 1. Cập nhật config.json cục bộ ngay lập tức
    with config_transaction():
        cfg = load_config()
        cfg_users = cfg.get("monitored_users", [])
        if user in cfg_users:
            cfg["monitored_users"] = [u for u in cfg_users if u != user]
            save_config(cfg)
        users = cfg.get("monitored_users", [])

    # 2. Ngắt luồng quay cục bộ ngay lập tức nếu đang quay
    with RECORDING_LOCK:
        task_info = ACTIVE_RECORDING_TASKS.pop(user, None)
        if task_info and isinstance(task_info, dict):
            se = task_info.get("stop_event")
            if se:
                se.user_deleted = True
                se.set()

    # 3. Xóa dữ liệu Supabase ngay lập tức (streamers & recordings)
    try:
        supabase_sync.delete_streamer_data_supabase(user)
    except Exception as sb_err:
        print(f"[API] Lỗi xóa dữ liệu Supabase của {user}: {sb_err}")

    # 4. Invalidate RAM caches ngay lập tức
    with LIVE_CACHE_LOCK:
        LIVE_CACHE.pop(user, None)
    with _RECORDINGS_CACHE_LOCK:
        _RECORDINGS_CACHE["timestamp"] = 0
        _RECORDINGS_CACHE["data"] = [v for v in _RECORDINGS_CACHE.get("data", []) if (v.get("user") != user and v.get("username") != user)]

    # 5. Các tác vụ nặng (xóa Drive, cập nhật streamers.json Drive, xóa files) chạy ngầm
    def _bg_delete_streamer_cloud(usr, del_files, t_info):
        try:
            if t_info and isinstance(t_info, dict):
                t = t_info.get("thread")
                if t and hasattr(t, 'join') and t.is_alive():
                    t.join(timeout=6)
        except Exception:
            pass

        try:
            gdrive_manager.set_user_recording_status_drive(usr, False)
        except Exception:
            pass

        try:
            drive_users = gdrive_manager.load_streamers_from_drive()
            if drive_users is not None and isinstance(drive_users, list):
                clean_drive = [u.strip().replace("@", "").lower() for u in drive_users if u.strip() and u.strip().replace("@", "").lower() != usr]
                gdrive_manager.save_streamers_to_drive(clean_drive)
        except Exception as d_err:
            print(f"[API] Lỗi cập nhật streamers.json trên Drive: {d_err}")

        if del_files:
            try:
                gdrive_manager.delete_streamer_folder_drive(usr)
            except Exception as e:
                print(f"[API] Lỗi xóa folder Drive của {usr}: {e}")

            try:
                local_dir = os.path.join(BASE_DIR, usr)
                resolved_dir = os.path.abspath(local_dir)
                base_resolved = os.path.abspath(BASE_DIR)
                if resolved_dir != base_resolved and resolved_dir.startswith(base_resolved) and os.path.exists(resolved_dir):
                    shutil.rmtree(resolved_dir, ignore_errors=True)
            except Exception:
                pass

    if bg_tasks:
        bg_tasks.add_task(_bg_delete_streamer_cloud, user, delete_files, task_info)
    else:
        threading.Thread(target=_bg_delete_streamer_cloud, args=(user, delete_files, task_info), daemon=True).start()

    return {
        "ok": True,
        "status": "success",
        "message": f"Đã xóa @{user} khỏi danh sách theo dõi thành công",
        "username": user,
        "users": users
    }

@app.delete("/api/recordings")
async def delete_recordings_batch(request: Request, bg_tasks: BackgroundTasks = None):
    try:
        req = await request.json()
    except Exception:
        req = {}

    filenames = req.get("filenames") or []
    drive_file_ids = req.get("drive_file_ids") or []
    user = (req.get("user") or "").strip().replace("@", "").lower()

    # 1. Xóa Supabase database records ngay lập tức
    if filenames:
        try:
            headers = supabase_sync.get_supabase_headers()
            quoted_names = ",".join([f'"{fn}"' for fn in filenames])
            del_url = f"{supabase_sync.SUPABASE_URL}/rest/v1/tiktok_recordings?filename=in.({quoted_names})"
            requests.delete(del_url, headers=headers, timeout=5)
        except Exception as e:
            print(f"[API] Lỗi xóa bản ghi Supabase: {e}")

    # 2. Invalidate cache RAM ngay lập tức
    with _RECORDINGS_CACHE_LOCK:
        _RECORDINGS_CACHE["timestamp"] = 0
        if filenames:
            fn_set = set(filenames)
            _RECORDINGS_CACHE["data"] = [v for v in _RECORDINGS_CACHE.get("data", []) if v.get("filename") not in fn_set]

    # 3. Tác vụ xóa file Drive & local chạy ngầm đa luồng cực nhanh
    def _bg_delete_files(fids, usr, fnames):
        if fids:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(fids), 8)) as executor:
                    list(executor.map(gdrive_manager.delete_file_drive, fids))
            except Exception as e:
                print(f"[API] Lỗi xóa file Drive song song: {e}")

        if usr and fnames:
            u_dir = os.path.join(".", usr)
            if os.path.exists(u_dir):
                for fn in fnames:
                    safe_fn = os.path.basename(fn)
                    lp = os.path.join(u_dir, safe_fn)
                    if os.path.exists(lp):
                        try:
                            os.remove(lp)
                        except Exception:
                            pass

    if bg_tasks:
        bg_tasks.add_task(_bg_delete_files, drive_file_ids, user, filenames)
    else:
        threading.Thread(target=_bg_delete_files, args=(drive_file_ids, user, filenames), daemon=True).start()

    return {
        "ok": True,
        "status": "success",
        "message": f"Đã xóa {len(filenames)} video thành công!",
        "deleted_count": len(filenames),
        "deleted_drive_count": len(drive_file_ids)
    }

@app.get("/api/stream/{username}")
def get_stream_url(username: str):
    user = username.strip().replace("@", "").lower()
    if not user: raise HTTPException(status_code=400, detail="Tên tài khoản không hợp lệ")
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
    
    with RECORDING_LOCK:
        is_recording = (user in ACTIVE_RECORDING_TASKS)

    return {
        "username": user,
        "is_live": True,
        "status": "recording" if is_recording else "live",
        "is_recording": is_recording,
        "room_id": room_id,
        "stream_url": stream_url,
        "format": "flv" if ".flv" in stream_url else "hls_m3u8",
        "note": "Link trực tiếp từ máy chủ CDN của TikTok, có thể phát trực tiếp trên web hoặc tải tốc độ cao tối đa băng thông."
    }

@app.get("/api/version")
def get_version():
    return {"version": "2.2.0", "build": "native-safari-tls-sync", "time": datetime.now().isoformat()}

@app.get("/api/test-live/{username}")
def test_live_diagnostic(username: str):
    try:
        user = username.strip().replace("@", "").lower()
        if not user: raise HTTPException(status_code=400, detail="Tên tài khoản không hợp lệ")
        import traceback
        out = {"user": user, "api_version": "2.2.0"}
        try:
            raw_det = recorder_core.check_live_details(user)
            out["check_live_details"] = raw_det
        except Exception as e:
            out["check_live_details_error"] = traceback.format_exc()

        try:
            s_live, s_rid = recorder_core.check_live_status(user)
            out["check_live_status"] = {"is_live": s_live, "room_id": s_rid}
        except Exception as e:
            out["check_live_status_error"] = traceback.format_exc()

        # Thử nghiệm trực tiếp TikTok Native API
        r_nat = None
        try:
            from curl_cffi import requests as c_req
            api_url = "https://www.tiktok.com/api-live/user/room"
            api_params = {
                "aid": 1988,
                "sourceType": 54,
                "staleTime": 600000,
                "uniqueId": user.lower(),
            }
            api_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                "Referer": f"https://www.tiktok.com/@{user}/live",
                "Accept": "*/*",
            }
            r_nat = c_req.get(api_url, params=api_params, headers=api_headers, cookies={"tt-target-idc": "useast1a"}, impersonate="chrome136", timeout=7)
            out["native_api"] = {
                "status_code": r_nat.status_code,
                "text_len": len(r_nat.text),
                "is_json": False
            }
            if r_nat.status_code == 200:
                try:
                    j = r_nat.json()
                    if isinstance(j, dict):
                        out["native_api"]["is_json"] = True
                        d = j.get("data")
                        d = d if isinstance(d, dict) else {}
                        lr = d.get("liveRoom")
                        lr = lr if isinstance(lr, dict) else {}
                        u = d.get("user")
                        u = u if isinstance(u, dict) else {}
                        out["native_api"]["status"] = lr.get("status")
                        out["native_api"]["roomId"] = u.get("roomId") or lr.get("roomId")
                except Exception as j_err:
                    out["native_api"]["json_error"] = str(j_err)
                    out["native_api"]["text_preview"] = r_nat.text[:200]
            else:
                out["native_api"]["text_preview"] = r_nat.text[:200]
        except Exception as e:
            out["native_api_error"] = traceback.format_exc()
        finally:
            if r_nat is not None:
                try:
                    r_nat.close()
                except Exception:
                    pass

        sess = None
        r = None
        try:
            from curl_cffi import requests as c_req
            sess = c_req.Session(impersonate="chrome136")
            r = sess.get(f"https://www.tiktok.com/@{user}/live", headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            out["direct_scrape"] = {
                "status_code": r.status_code,
                "text_len": len(r.text),
                "has_SIGI": '<script id="SIGI_STATE"' in r.text,
                "has_roomId": bool(re.search(r'"roomId"[:"]+(\d{15,25})', r.text)),
                "status_match": re.findall(r'"status":\s*(\d+)', r.text)[:5]
            }
        except Exception as e:
            out["direct_scrape_error"] = traceback.format_exc()
        finally:
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass
            if sess:
                try:
                    sess.close()
                except Exception:
                    pass

        return out
    finally:
        gc.collect()

# ponytail: centralized in recorder_core
concat_mp4_segments = recorder_core.concat_mp4_segments

MAX_CHUNK_SECONDS = 3600  # Đúng 1 tiếng (1h = 3600s), tự động tách video và up lên Cloud

def bg_record_worker(user: str, duration: Optional[int] = None, stop_event: Optional[threading.Event] = None):
    part_number = 1
    consecutive_failures = 0
    max_consecutive_failures = 8
    max_vip_attempts = 5
    # Một lần check live lỗi/False KHÔNG được kết thúc phiên: cần N lần liên tiếp.
    consecutive_offline_checks = 0
    worker_started_at = time.time()
    last_heartbeat_at = 0.0

    my_task_entry = None
    rec_lock_cm = None
    try:
        from config_lock import streamer_recording_lock
        rec_lock_cm = streamer_recording_lock(user)
        got_lock = rec_lock_cm.__enter__()
        if not got_lock:
            rec_lock_cm.__exit__(None, None, None)
            rec_lock_cm = None
            print(f"[⚠️] [@{user}] Streamer đang được ghi hình bởi tiến trình khác trên máy chủ. Bỏ qua.")
            return

        with RECORDING_LOCK:
            if user in ACTIVE_RECORDING_TASKS and isinstance(ACTIVE_RECORDING_TASKS[user], dict):
                ACTIVE_RECORDING_TASKS[user]["thread"] = threading.current_thread()
                my_task_entry = ACTIVE_RECORDING_TASKS[user]

        # Chỉ chạy local recorder nếu hệ thống có sẵn ffmpeg
        if not shutil.which("ffmpeg") and not (FFMPEG_PATH and os.path.exists(FFMPEG_PATH)):
            return

        while True:
            if stop_event and stop_event.is_set():
                print(f"[⏹️] [@{user}] Nhận tín hiệu dừng từ người dùng.")
                break

            # Gia hạn heartbeat ghi hình: start_record chỉ ghi 1 lần, sau 180s TTL hết hạn
            # thì Cloud Runner (GitHub Actions) tưởng user trống và ghi hình TRÙNG LẶP.
            if time.time() - last_heartbeat_at >= 60:
                last_heartbeat_at = time.time()
                try:
                    gdrive_manager.set_user_recording_status_drive(user, True)
                except Exception:
                    pass

            # duration_seconds từ API: phải thực thi, nếu không client yêu cầu 5 phút
            # sẽ ghi cho tới khi streamer tắt live.
            if duration and (time.time() - worker_started_at) >= duration:
                print(f"[⏱️] [@{user}] Đã đạt thời lượng yêu cầu ({duration}s). Kết thúc phiên ghi hình.")
                break

            live_details = get_user_live_details_cached(user)
            is_live = live_details.get("is_live", False)
            room_id = live_details.get("room_id")
            is_sub_only = live_details.get("is_sub_only", False)

            if not is_live or not room_id:
                consecutive_offline_checks += 1
                if consecutive_offline_checks >= 3:
                    print(f"[🏁] [@{user}] Xác nhận {consecutive_offline_checks} lần liên tiếp streamer không live. Dừng ghi hình.")
                    break
                print(f"[⏳] [@{user}] Chưa thấy live (lần {consecutive_offline_checks}/3) — có thể lỗi mạng tạm thời. Chờ 10s rồi kiểm tra lại...")
                time.sleep(10)
                continue
            consecutive_offline_checks = 0

            now_str = recorder_core.get_now_str("%Y-%m-%d_%H-%M-%S")
            part_suffix = f"_part{part_number}" if part_number > 1 else ""
            user_dir = os.path.join(BASE_DIR, user)
            os.makedirs(user_dir, exist_ok=True)
            output_file = os.path.join(user_dir, f"{user}_{now_str}{part_suffix}.mp4")

            part_segments = []
            accumulated_seconds = 0.0
            offline_confirmed = False
            print(f"[🔴] [@{user}] Bắt đầu tích lũy Phần {part_number} (Tối đa 1 tiếng: {MAX_CHUNK_SECONDS}s)...")

            while accumulated_seconds < (300 if is_sub_only else MAX_CHUNK_SECONDS):
                if stop_event and stop_event.is_set():
                    break

                target_duration = (300 - int(accumulated_seconds)) if is_sub_only else (MAX_CHUNK_SECONDS - int(accumulated_seconds))
                if duration:
                    # Không bao giờ ghi vượt quá thời lượng client yêu cầu
                    remaining_req = duration - int(time.time() - worker_started_at)
                    target_duration = min(target_duration, remaining_req)
                if target_duration <= 30:
                    break

                stream_candidates = []
                for s_att in range(3):
                    if is_sub_only:
                        guest_session = None
                        try:
                            guest_session = recorder_core.generate_guest_session()
                            stream_candidates = recorder_core.get_stream_candidates(room_id, user=user, session=guest_session)
                        except Exception as gs_err:
                            print(f"[!] [API Server] Lỗi xoay Guest Session cho @{user}: {gs_err}")
                            stream_candidates = []
                        finally:
                            if guest_session:
                                try:
                                    guest_session.close()
                                except Exception:
                                    pass
                    else:
                        stream_candidates = recorder_core.get_stream_candidates(room_id, user=user)
                    if stream_candidates:
                        break
                    time.sleep(2.5)

                if not stream_candidates:
                    st_live, new_rid = recorder_core.check_live_status(user)
                    if st_live and new_rid:
                        room_id = new_rid
                        stream_candidates = recorder_core.get_stream_candidates(room_id, user=user)

                if not stream_candidates:
                    print(f"[!] [@{user}] Không lấy được link stream sau các lần thử. Kết thúc tích lũy Phần {part_number}.")
                    consecutive_failures += 1
                    time.sleep(5)
                    break

                seg_name = os.path.join(user_dir, f"{user}_{now_str}_p{part_number}_seg{len(part_segments)+1}.mp4")
                rec_res = None
                from auto_h264 import validate_playable_video
                is_valid = False
                reason = "Không có kết quả thu"
                dur = 0.0

                # Tự động fallback qua các luồng (HLS -> FLV HD...) để tránh lỗi HTTP 403 Forbidden
                for cand_idx, stream_url in enumerate(stream_candidates[:4]):
                    try:
                        rec_res = recorder_core.record_stream_ffmpeg(
                            stream_url,
                            output_filename=seg_name,
                            target_user=user,
                            duration=target_duration,
                            stop_event=stop_event,
                            auto_sync_gdrive=False,
                            is_sub_only=is_sub_only
                        )
                    except Exception as rec_err:
                        print(f"[!] [@{user}] Lỗi khi ghi hình đoạn mới (luồng #{cand_idx+1}): {rec_err}")
                        rec_res = None

                    if rec_res and os.path.exists(rec_res):
                        is_valid, reason, dur = validate_playable_video(rec_res, min_duration=5.0, min_size_bytes=250000)
                        if is_valid:
                            break
                        else:
                            try:
                                os.remove(rec_res)
                            except Exception:
                                pass
                            if len(stream_candidates) > cand_idx + 1:
                                print(f"[⚠️] [@{user}] Luồng #{cand_idx+1} không đạt chuẩn ({reason}). Fallback sang luồng dự phòng #{cand_idx+2}...")

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

                if accumulated_seconds >= MAX_CHUNK_SECONDS - 5:
                    print(f"[⏱️ Đạt tối đa 1:00:00] [@{user}] Phần {part_number} đã tích lũy đủ 1 tiếng ({accumulated_seconds:.1f}s)! Chuẩn bị chốt và bắt đầu phần mới...")
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
                    # Streamer ngắt live khi chưa đủ 1:00:00 -> Chờ đúng 5 phút (300s) xác nhận offline chắc chắn
                    cfg_offline_wait = load_config().get("offline_confirm_seconds", 300)
                    print(f"[⏳] [@{user}] Tín hiệu live tạm ngắt sau {accumulated_seconds:.1f}s (< 1:00:00). Bắt đầu chờ {cfg_offline_wait//60} phút ({cfg_offline_wait}s) xác nhận offline...")
                    streamer_reconnected = False
                    offline_checks = max(1, int(cfg_offline_wait / 15))
                    for _ in range(offline_checks):
                        if stop_event and stop_event.is_set():
                            break
                        try:
                            gdrive_manager.set_user_recording_status_drive(user, True)
                        except Exception:
                            pass
                        time.sleep(15)
                        recheck = recorder_core.check_live_details(user)
                        if recheck.get("is_live"):
                            streamer_reconnected = True
                            if recheck.get("room_id"):
                                room_id = recheck.get("room_id")
                            print(f"[🔴] [@{user}] Streamer ĐÃ LIVE TRỞ LẠI! Tiếp tục thu tiếp nối vào Phần {part_number} (còn thiếu {MAX_CHUNK_SECONDS - int(accumulated_seconds)}s)...")
                            break

                    if streamer_reconnected:
                        continue
                    else:
                        offline_confirmed = True
                        print(f"[🏁] [@{user}] Đã xác nhận streamer offline đủ {cfg_offline_wait//60} phút ({accumulated_seconds:.1f}s tích lũy). Xuất bản video lên Drive & Supabase ngay...")
                        break

            if not part_segments:
                if offline_confirmed:
                    print(f"[🏁] [@{user}] Streamer đã xác nhận offline hoàn toàn. Kết thúc luồng ghi hình.")
                    break
                if consecutive_failures >= max_consecutive_failures:
                    break
                if is_sub_only and part_number >= max_vip_attempts:
                    print(f"🛑 [@{user}] Đã đạt giới hạn tối đa {max_vip_attempts} lần xoay Guest Session preview Sub-Only. Kết thúc luồng.")
                    break
                time.sleep(5)
                continue

            # Nhận tín hiệu dừng: CHỈ hủy phân đoạn khi streamer BỊ XÓA (user_deleted).
            # Dừng bình thường (stop API / shutdown server / SIGTERM) phải CHỐT và TẢI LÊN
            # phần đã ghi — nếu không mỗi lần stop/restart sẽ mất tới ~1 tiếng footage.
            if stop_event and stop_event.is_set():
                if getattr(stop_event, "user_deleted", False):
                    print(f"[🗑️] [@{user}] Streamer bị xóa. Hủy toàn bộ phân đoạn tạm dở.")
                    for seg in part_segments:
                        if seg and os.path.exists(seg):
                            try:
                                os.remove(seg)
                            except Exception:
                                pass
                    break
                print(f"[⏹️] [@{user}] Nhận yêu cầu dừng. Chốt {len(part_segments)} phân đoạn hiện có để lưu trữ an toàn...")

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
                final_rec_file = recorder_core.concat_mp4_segments(part_segments, output_file)
                if not final_rec_file or not os.path.exists(final_rec_file):
                    # concat trả về None khi FFmpeg lỗi: thử lại 1 lần trước khi bỏ cuộc
                    print(f"[!] [@{user}] Ghép nối lần 1 thất bại. Thử lại...")
                    final_rec_file = recorder_core.concat_mp4_segments(part_segments, output_file)
                if not final_rec_file or not os.path.exists(final_rec_file):
                    # KHÔNG dùng 1 phân đoạn thay cho file ghép: sẽ xuất bản sai và xóa mất dữ liệu.
                    print(f"[!] [@{user}] Không ghép được Phần {part_number}. GIỮ nguyên {len(part_segments)} phân đoạn trên đĩa để xử lý ở lần sau.")
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        break
                    time.sleep(5)
                    continue

            # Đảm bảo video chuẩn H.264 và upscale lên 1080p (giữ nguyên fps gốc)
            try:
                from auto_h264 import ensure_h264, upscale_to_1080p_if_needed
                final_rec_file = ensure_h264(final_rec_file)
                final_rec_file = upscale_to_1080p_if_needed(final_rec_file)
            except Exception as up_err:
                print(f"[!] [@{user}] Lỗi chuẩn hóa H.264 / upscale 1080p: {up_err}")

            # Streamer bị xóa giữa chừng -> hủy kết quả đã ghép. Dừng bình thường thì
            # GIỮ file để tiếp tục validate + upload ở phía dưới.
            if stop_event and stop_event.is_set() and getattr(stop_event, "user_deleted", False):
                if final_rec_file and os.path.exists(final_rec_file):
                    try:
                        os.remove(final_rec_file)
                    except Exception:
                        pass
                break

            is_valid, reason, final_dur = validate_playable_video(final_rec_file, min_duration=5.0, min_size_bytes=250000)
            if not is_valid:
                print(f"[!] File Phần {part_number} không đạt chuẩn ({reason}). Bỏ qua.")
                if final_rec_file and os.path.exists(final_rec_file):
                    try:
                        os.remove(final_rec_file)
                    except Exception:
                        pass
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    print(f"⏸️ [@{user}] Gặp {consecutive_failures} lỗi tạo video liên tiếp. Dừng luồng ghi hình.")
                    break
                continue

            consecutive_failures = 0
            is_final_part = offline_confirmed or bool(stop_event and stop_event.is_set())
            part_desc = "phần ghi sau cùng" if is_final_part else f"Phần {part_number}"
            print(f"[✓] [@{user}] Hoàn tất trọn vẹn {part_desc} ({final_dur:.1f}s): {os.path.basename(final_rec_file)}")

            # Upload trực tiếp lên Drive và đồng bộ Supabase cho MỌI segment
            # (segment đủ 1h upload ngay, segment cuối upload sau khi xác nhận offline 5 phút)
            # File có thể biến mất giữa chừng (worker khác dọn, antivirus...) -> không để
            # lỗi thumbnail/getsize làm sập TOÀN BỘ worker và mất phần vừa ghi.
            thumb_f = None
            try:
                thumb_f = extract_middle_thumbnail(final_rec_file)
            except Exception as th_err:
                print(f"[!] [@{user}] Lỗi tạo thumbnail Phần {part_number}: {th_err}")
            try:
                sz = os.path.getsize(final_rec_file) if (final_rec_file and os.path.exists(final_rec_file)) else 0
            except OSError:
                sz = 0
            rec_basename = os.path.basename(final_rec_file)

            # 1. Upload Google Drive trước để lấy drive_file_id
            drive_file_id = None
            drive_thumb_id = None
            try:
                token = gdrive_manager.get_access_token()
                if token:
                    root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=token)
                    sub_id = gdrive_manager.find_or_create_folder(user, parent_id=root_id, access_token=token)
                    ok = gdrive_manager.upload_file_to_drive(final_rec_file, sub_id, access_token=token)
                    if ok:
                        drive_file_id = ok if isinstance(ok, str) else None
                        try:
                            os.remove(final_rec_file)
                            print(f"[🗑️] [@{user}] Đã xóa video tạm Phần {part_number} sau khi upload Drive thành công.")
                        except Exception:
                            pass
                    if thumb_f and os.path.exists(thumb_f):
                        t_ok = gdrive_manager.upload_file_to_drive(thumb_f, sub_id, access_token=token)
                        if t_ok and isinstance(t_ok, str):
                            drive_thumb_id = t_ok
            except Exception as gdrive_err:
                # KHÔNG để lỗi Drive làm sập toàn bộ worker: vẫn đồng bộ Supabase với file local còn lại
                print(f"[!] [@{user}] Lỗi tải lên Google Drive ở Phần {part_number}: {gdrive_err}")

            # 2. Đồng bộ Supabase Storage & Database kèm drive_file_id
            try:
                import supabase_sync
                if not drive_file_id:
                    print(f"[⚠️] [@{user}] Upload lên Drive thất bại -> KHÔNG tạo bản ghi Supabase cho {rec_basename} (tránh link chết). File local được GIỮ lại.")
                else:
                    supabase_sync.sync_recording_to_supabase(
                        user=user,
                        filename=rec_basename,
                        size_bytes=sz,
                        thumb_source=thumb_f,
                        drive_file_id=drive_file_id,
                        drive_thumb_id=drive_thumb_id,
                        source="api_server"
                    )
            except Exception as sb_err:
                print(f"[!] Lỗi đồng bộ Supabase từ bg_record_worker: {sb_err}")
            finally:
                if thumb_f and os.path.exists(thumb_f):
                    try:
                        os.remove(thumb_f)
                    except Exception:
                        pass

            # Dọn phân đoạn gốc: nội dung đã nằm trong final_rec_file (đã ghép/đã bàn giao).
            # Không xóa ở đây thì mỗi phần sẽ còn 2 bản sao -> đầy ổ cứng và
            # /api/recordings liệt kê trùng lặp cùng footage.
            for seg in part_segments:
                if not seg or not os.path.exists(seg):
                    continue
                if final_rec_file and os.path.abspath(seg) == os.path.abspath(final_rec_file):
                    continue
                try:
                    os.remove(seg)
                except Exception:
                    pass

            part_number += 1

            if is_sub_only and part_number > max_vip_attempts:
                print(f"🛑 [@{user}] Đã đạt giới hạn tối đa {max_vip_attempts} lần xoay Guest Session preview Sub-Only. Kết thúc luồng.")
                break

            if stop_event and stop_event.is_set():
                break


            # Kiểm tra streamer còn live không để tiếp tục phân đoạn tiếp theo
            time.sleep(3)
            curr_det = recorder_core.check_live_details(user)
            if not curr_det.get("is_live"):
                # Khoảng cách 12s (trước đây 4s) để phân biệt lỗi mạng tạm thời với thật sự xuống live
                time.sleep(12)
                curr_det = recorder_core.check_live_details(user)

            if offline_confirmed:
                print(f"[🏁] [@{user}] Buổi live đã hoàn tất trọn vẹn và đã gửi lên Drive & Supabase sau 5 phút offline.")
                break

            if not curr_det.get("is_live"):
                consecutive_offline_checks += 1
                if consecutive_offline_checks < 3:
                    print(f"[⏳] [@{user}] Chưa xác nhận streamer xuống live (lần {consecutive_offline_checks}/3). Kiểm tra lại...")
                    continue
                cfg_offline_wait = load_config().get("offline_confirm_seconds", 300)
                print(f"[⏳] [@{user}] Xác nhận {consecutive_offline_checks} lần streamer ngắt live. Chờ xác nhận offline {cfg_offline_wait//60} phút ({cfg_offline_wait}s)...")
                offline_checks = max(1, int(cfg_offline_wait / 15))
                streamer_back = False
                for _ in range(offline_checks):
                    if stop_event and stop_event.is_set():
                        break
                    try:
                        gdrive_manager.set_user_recording_status_drive(user, True)
                    except Exception:
                        pass
                    time.sleep(15)
                    recheck = recorder_core.check_live_details(user)
                    if recheck.get("is_live"):
                        streamer_back = True
                        print(f"[🔴] [@{user}] Streamer ĐÃ LIVE TRỞ LẠI! Tự động ghi hình nối tiếp Phần {part_number} (1:00:00 tiếp theo)...")
                        break
                if streamer_back:
                    consecutive_offline_checks = 0
                    continue

                print(f"[🏁] [@{user}] ĐÃ XÁC NHẬN OFFLINE ĐỦ {cfg_offline_wait//60} PHÚT. Buổi live đã kết thúc hoàn toàn.")
                break
            else:
                consecutive_offline_checks = 0
                print(f"[⏩] [@{user}] Streamer VẪN ĐANG LIVE! Tự động ghi hình nối tiếp Phần {part_number} (1:00:00 tiếp theo)...")

    except Exception as e:
        print(f"[!] Lỗi ghi hình worker: {e}")
    finally:
        still_our_registration = False
        with RECORDING_LOCK:
            cur = ACTIVE_RECORDING_TASKS.get(user)
            if my_task_entry is not None and cur is my_task_entry:
                ACTIVE_RECORDING_TASKS.pop(user, None)
                still_our_registration = True
            elif cur is None:
                # Đã bị gỡ (ví dụ delete_user) -> không còn gì để dọn
                pass
            else:
                # Một phiên ghi hình MỚI đã được đăng ký cho cùng user trong lúc mình finalize
                # (delete -> add -> start) -> KHÔNG được gỡ đăng ký của phiên mới.
                print(f"⚠️ [@{user}] Đã có phiên ghi hình mới được đăng ký; phiên cũ không đụng vào registry.")

        # Chỉ tắt heartbeat khi mình vẫn là phiên đang đăng ký: nếu phiên mới đã đăng ký,
        # việc ghi False sẽ làm Cloud Runner tưởng streamer trống -> ghi hình TRÙNG LẶP.
        if still_our_registration:
            try:
                gdrive_manager.set_user_recording_status_drive(user, False)
            except Exception:
                pass

        # Upload trực tiếp lên Drive, không sử dụng Staging Queue

        if rec_lock_cm:
            try:
                rec_lock_cm.__exit__(None, None, None)
            except Exception:
                pass

        gc.collect()

@app.post("/api/record/start")
@app.post("/api/record")
def start_record(req: RecordRequest, bg_tasks: BackgroundTasks):
    user = req.username.strip().replace("@", "").lower()
    if not user: raise HTTPException(status_code=400, detail="Tên tài khoản không hợp lệ")
    
    # Đảm bảo streamer có trong danh sách theo dõi
    with config_transaction():
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
        stop_evt = None
        is_recording = False
        with RECORDING_LOCK:
            if user in ACTIVE_RECORDING_TASKS:
                return {"message": f"@{user} đang ghi hình rồi", "is_recording": True, "status": "already_recording"}

        # ponytail: Chống trùng lặp ghi hình giữa Cloud Runner (GitHub Actions) và Render
        try:
            drive_details = gdrive_manager.load_active_recordings_from_drive(as_details=True) or []
            now_ts = int(time.time())
            for it in drive_details:
                u_name = it.get("username") if isinstance(it, dict) else str(it)
                if u_name and u_name.strip().replace("@", "").lower() == user:
                    up_at = it.get("updated_at", 0) if isinstance(it, dict) else 0
                    if now_ts - up_at < 180:  # ponytail: TTL chống trùng với cloud_daemon (2+ heartbeat cycle 60s); zombie cleaner (api_server.py:316) cho phép tới 600s rồi mới tự dọn
                        return {
                            "message": f"@{user} đang được Cloud Runner ghi hình (heartbeat {now_ts - up_at}s trước).",
                            "is_recording": True,
                            "status": "already_recording",
                            "source": "cloud_runner"
                        }
        except Exception:
            pass

        with RECORDING_LOCK:
            if user in ACTIVE_RECORDING_TASKS:
                return {"message": f"@{user} đang ghi hình rồi", "is_recording": True, "status": "already_recording"}
            if has_ffmpeg:
                stop_evt = threading.Event()
                ACTIVE_RECORDING_TASKS[user] = {"start_time": time.time(), "stop_event": stop_evt}
                is_recording = True

        if is_recording and stop_evt:
            try:
                gdrive_manager.set_user_recording_status_drive(user, True)
            except Exception:
                pass
            bg_tasks.add_task(bg_record_worker, user, req.duration_seconds, stop_evt)

        if not has_ffmpeg:
            # KHÔNG được trả HTTP 200 "Hệ thống đang ghi hình" khi không có worker nào chạy:
            # client sẽ tưởng đã ghi và không bao giờ thử lại.
            return JSONResponse(status_code=503, content={
                "message": f"@{user} đang live NHƯNG hệ thống không tìm thấy ffmpeg. Không thể ghi hình.",
                "status": "no_recorder",
                "is_live": True,
                "is_recording": False,
                "username": user,
                "room_id": room_id
            })


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
        # KHÔNG được dừng recording đang chạy chỉ vì một lần check live thất bại/cached:
        # transient network error sẽ giết ffmpeg giữa chừng -> video bị gián đoạn.
        with RECORDING_LOCK:
            task = ACTIVE_RECORDING_TASKS.get(user)
        if task and isinstance(task, dict) and is_local_recorder_running_for_user(user):
            return {
                "message": f"@{user} đang được ghi hình. Trạng thái live trả về ngoại tuyến (có thể do cache/lỗi mạng tạm thời) nên bot GIỮ NGUYÊN phiên ghi hiện tại.",
                "status": "recording",
                "is_live": False,
                "is_recording": True,
                "username": user
            }
        with RECORDING_LOCK:
            task = ACTIVE_RECORDING_TASKS.get(user)
            if task and isinstance(task, dict) and task.get("stop_event"):
                task["stop_event"].set()
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
    if not user: raise HTTPException(status_code=400, detail="Tên tài khoản không hợp lệ")
    task_info = None
    with RECORDING_LOCK:
        task_info = ACTIVE_RECORDING_TASKS.get(user)
        if task_info and isinstance(task_info, dict):
            se = task_info.get("stop_event")
            if se:
                se.set()
    if task_info and isinstance(task_info, dict):
        t = task_info.get("thread")
        if t and hasattr(t, "join") and t.is_alive():
            t.join(timeout=3)
        with RECORDING_LOCK:
            if user in ACTIVE_RECORDING_TASKS:
                t_check = ACTIVE_RECORDING_TASKS[user].get("thread") if isinstance(ACTIVE_RECORDING_TASKS[user], dict) else None
                if not (t_check and hasattr(t_check, "is_alive") and t_check.is_alive()):
                    ACTIVE_RECORDING_TASKS.pop(user, None)
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

def list_recordings_from_drive(access_token=None, force_refresh=False):
    """
    Quét danh sách toàn bộ video và thumbnail đã lưu trên Google Drive bằng ThreadPool song song.
    """
    now = time.time()
    with _RECORDINGS_CACHE_LOCK:
        if not force_refresh and (now - _RECORDINGS_CACHE["timestamp"] < 60) and _RECORDINGS_CACHE["data"]:
            return list(_RECORDINGS_CACHE["data"])

    recordings = []
    try:
        if not access_token:
            access_token = gdrive_manager.get_access_token()
        if not access_token:
            return recordings
            
        root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=access_token)
        headers = {"Authorization": f"Bearer {access_token}"}
        
        q_folders = f"'{root_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        with requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_folders, "fields": "files(id,name)"}, timeout=10) as res_f:
            folders = res_f.json().get("files", []) if res_f.status_code == 200 else []
        folders = [f for f in folders if not f.get("name", "").startswith((".", "_"))]

        def _fetch_folder_files(fold):
            uname = fold["name"]
            fid = fold["id"]
            q_files = f"'{fid}' in parents and trashed = false"
            with requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_files, "fields": "files(id,name,mimeType,size,createdTime)"}, timeout=10) as res_files:
                files = res_files.json().get("files", []) if res_files.status_code == 200 else []
            thumbs = {f["name"]: f["id"] for f in files if f["name"].endswith(".jpg")}
            folder_recs = []
            for f in files:
                fname = f["name"]
                if fname.endswith(".mp4"):
                    # Không liệt kê phân đoạn phụ (đã được ghép vào file _full/part hoàn chỉnh)
                    if re.search(r"_seg\d+\.mp4$", fname):
                        continue
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
                    cdn_url = f"https://drive.usercontent.google.com/download?id={f['id']}&export=download&authuser=0&confirm=t"
                    folder_recs.append({
                        "filename": fname,
                        "user": uname,
                        "size_bytes": sz_bytes,
                        "size_mb": round(sz_bytes / (1024 * 1024), 2),
                        "recorded_at": recorded_at,
                        "created_at": f.get("createdTime"),
                        "thumbnail_url": f"/api/thumbnail/{uname}/{fname}?redirect=true",
                        "download_url": f"/api/download/{uname}/{fname}",
                        "cdn_download_url": cdn_url,
                        "drive_file_id": f["id"],
                        "drive_thumb_id": thumb_id,
                        "stream_url": f"/api/stream-video-id/{f['id']}",
                        "source": "google_drive"
                    })
            return folder_recs

        if folders:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(folders), 3)) as ex:
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
                    if f in merged_files:
                        continue
                    fp = os.path.join(u_dir, f)
                    if os.path.isdir(fp):
                        continue
                    # Ẩn phân đoạn phụ (_seg1, _seg2...): chúng là MỘT PHẦN của file đã ghép,
                    # hiển thị ra sẽ làm trùng lặp cùng footage và làm đầy danh sách.
                    if re.search(r"_seg\d+\.mp4$", f):
                        continue
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
                        "thumbnail_url": f"/api/thumbnail/{u}/{f}?redirect=true",
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
            for r in recs:
                u = r["user"]
                fn = r["filename"]
                thumb_id = r.get("drive_thumb_id") or r.get("drive_file_id")
                th_url = f"gdrive:{thumb_id}" if thumb_id else None
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
def get_thumbnail(user: str, filename: str, redirect: bool = False):
    """
    Trả về ảnh xem trước được cắt từ chính giữa video (50% thời lượng).
    Hỗ trợ đọc từ cả ổ đĩa local và Google Drive.
    """
    user = user.strip().replace("@", "").lower()
    user = os.path.basename(user)
    filename = os.path.basename(filename)
    clean_name = filename.replace(".jpg", "").replace(".mp4", "")
    
    video_path = os.path.join(BASE_DIR, user, clean_name + ".mp4")
    thumb_path = os.path.join(BASE_DIR, user, clean_name + ".jpg")

    resolved = os.path.abspath(video_path)
    if not pathlib.Path(resolved).is_relative_to(os.path.abspath(BASE_DIR)): raise HTTPException(status_code=403, detail="Access denied")

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
            root_id = gdrive_manager.find_folder("tiktok-record", access_token=tok)
            user_fid = gdrive_manager.find_folder(user, parent_id=root_id, access_token=tok) if root_id else None
            if not user_fid:
                raise HTTPException(status_code=404, detail="Thumbnail không tồn tại trên Drive")
            headers = {"Authorization": f"Bearer {tok}"}
            
            # Tìm ảnh .jpg trước
            safe_name = clean_name.replace("'", "\\'")
            q_thumb = f"name = '{safe_name}.jpg' and '{user_fid}' in parents and trashed = false"
            res_th = requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_thumb, "fields": "files(id)"}, timeout=10)
            th_files = []
            try:
                if res_th.status_code == 200:
                    th_files = res_th.json().get("files", [])
            finally:
                res_th.close()

            if th_files:
                img_id = th_files[0]["id"]
                if redirect:
                    gdrive_manager.make_file_public(img_id, access_token=tok)
                    return RedirectResponse(url=f"https://drive.google.com/thumbnail?id={img_id}&sz=w800", status_code=302)
                img_res = requests.get(f"https://www.googleapis.com/drive/v3/files/{img_id}?alt=media", headers=headers, timeout=15)
                try:
                    if img_res.status_code == 200:
                        return Response(content=img_res.content, media_type="image/jpeg")
                finally:
                    img_res.close()

            # Nếu chưa có ảnh .jpg riêng, tìm file .mp4 để lấy thumbnail tích hợp sẵn của Google Drive
            q_vid = f"name = '{safe_name}.mp4' and '{user_fid}' in parents and trashed = false"
            res_vid = requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_vid, "fields": "files(id,thumbnailLink)"}, timeout=10)
            vid_files = []
            try:
                if res_vid.status_code == 200:
                    vid_files = res_vid.json().get("files", [])
            finally:
                res_vid.close()
            if vid_files:
                vid_id = vid_files[0]["id"]
                gdrive_manager.make_file_public(vid_id, access_token=tok)
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
    user = os.path.basename(user)
    filename = os.path.basename(filename)
    if not user or user in (".", "..") or filename in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="Đường dẫn không hợp lệ")
    fp = os.path.join(BASE_DIR, user, filename)
    resolved = os.path.abspath(fp)
    if not pathlib.Path(resolved).is_relative_to(os.path.abspath(BASE_DIR)): raise HTTPException(status_code=403, detail="Access denied")
    # exists + không phải thư mục: filename=".." trỏ tới THƯ MỤC -> FileResponse sẽ ném 500
    if os.path.exists(fp) and not os.path.isdir(fp):
        return FileResponse(path=fp, media_type="video/mp4", filename=filename, headers={"Accept-Ranges": "bytes"})
    
    # Tìm kiếm trên Google Drive và chuyển hướng trực tiếp đến CDN tải tốc độ cao
    try:
        tok = gdrive_manager.get_access_token()
        if tok:
            root_id = gdrive_manager.find_folder("tiktok-record", access_token=tok)
            user_fid = gdrive_manager.find_folder(user, parent_id=root_id, access_token=tok) if root_id else None
            if not user_fid:
                raise HTTPException(status_code=404, detail="File video không tồn tại")
            headers = {"Authorization": f"Bearer {tok}"}
            safe_name = filename.replace("'", "\\'")
            q_vid = f"name = '{safe_name}' and '{user_fid}' in parents and trashed = false"
            with requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_vid, "fields": "files(id)"}, timeout=10) as res_vid:
                vid_files = res_vid.json().get("files", []) if res_vid.status_code == 200 else []
            if vid_files:
                file_id = vid_files[0]["id"]
                # Cấp quyền đọc công khai để CDN tải mượt mà không cần xác thực
                gdrive_manager.make_file_public(file_id, access_token=tok)
                cdn_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t"
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
    user = os.path.basename(user.strip().replace("@", "").lower())
    filename = os.path.basename(filename.strip())
    try:
        tok = gdrive_manager.get_access_token()
        if tok:
            root_id = gdrive_manager.find_folder("tiktok-record", access_token=tok)
            user_fid = gdrive_manager.find_folder(user, parent_id=root_id, access_token=tok) if root_id else None
            if not user_fid:
                raise HTTPException(status_code=404, detail=f"Không tìm thấy streamer @{user} trên Google Drive")
            headers = {"Authorization": f"Bearer {tok}"}
            safe_name = filename.replace("'", "\\'")
            q_vid = f"name = '{safe_name}' and '{user_fid}' in parents and trashed = false"
            with requests.get("https://www.googleapis.com/drive/v3/files", headers=headers, params={"q": q_vid, "fields": "files(id,size,createdTime)"}, timeout=10) as res_vid:
                vid_files = res_vid.json().get("files", []) if res_vid.status_code == 200 else []
            if vid_files:
                file_id = vid_files[0]["id"]
                sz_bytes = int(vid_files[0].get("size", 0))
                gdrive_manager.make_file_public(file_id, access_token=tok)
                cdn_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t"
                
                if redirect:
                    return RedirectResponse(url=cdn_url, status_code=302)
                    
                return {
                    "filename": filename,
                    "user": user,
                    "cdn_download_url": cdn_url,
                    "drive_file_id": file_id,
                    "size_mb": round(sz_bytes / (1024 * 1024), 2),
                    "size_bytes": sz_bytes,
                    "cdn_provider": "Google Cloud Global Edge CDN",
                    "supports_idm_multithread": True,
                    "supports_range_seeking": True,
                    "note": "Link CDN trực tiếp, không giới hạn băng thông, hỗ trợ tải đa luồng và tua video tức thì."
                }
    except Exception as e:
        print(f"[!] Lỗi CDN trên Google Drive: {e}")
    
    raise HTTPException(status_code=404, detail="File video không tồn tại trên CDN Drive")

@app.get("/api/stream-video-id/{file_id}")
def stream_video_by_id(file_id: str, request: Request, redirect: bool = True):
    """
    Zero Render Bandwidth (R4): Chuyển hướng trực tiếp 302 sang Google CDN / Drive Preview.
    Triệt tiêu 100% băng thông trung chuyển (0 bytes) và ngăn ngừa lỗi tràn RAM (> 512MB) trên Render.
    """
    tok = gdrive_manager.get_access_token()
    if not tok:
        raise HTTPException(status_code=500, detail="Không có quyền truy cập Google Drive")

    # CHỈ ép quyền public cho file thuộc tiktok-record/. Nếu thiếu bước này thì endpoint
    # không auth này có thể set quyền `anyone` cho bất kỳ file Drive nào token nhìn thấy.
    if not gdrive_manager.is_recorder_owned_file(file_id, access_token=tok):
        print(f"[🛡️] Từ chối stream file_id không thuộc tiktok-record/: {file_id}")
        raise HTTPException(status_code=403, detail="file_id không thuộc thư mục ghi hình")

    try:
        gdrive_manager.make_file_public(file_id, access_token=tok)
        cdn_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0&confirm=t"
        return RedirectResponse(url=cdn_url, status_code=302)
    except Exception as e:
        print(f"[!] Chuyển hướng CDN không thành công cho file {file_id}: {e}")
        preview_url = f"https://drive.google.com/file/d/{file_id}/preview"
        return RedirectResponse(url=preview_url, status_code=302)

@app.get("/api/stream-video/{user}/{filename}")
def stream_video(user: str, filename: str, request: Request, redirect: bool = True):
    """
    Phát trực tiếp video theo username và filename.
    Hỗ trợ cả file local và file lưu trên Google Drive.
    Với Google Drive: Zero Render Bandwidth 302 Redirect sang CDN/Preview.
    """
    user = user.strip().replace("@", "").lower()
    user = os.path.basename(user)
    filename = os.path.basename(filename)
    if not user or user in (".", "..") or filename in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="Đường dẫn không hợp lệ")
    local_path = os.path.join(BASE_DIR, user, filename)

    resolved = os.path.abspath(local_path)
    if not pathlib.Path(resolved).is_relative_to(os.path.abspath(BASE_DIR)): raise HTTPException(status_code=403, detail="Access denied")

    # 1. Nếu file có sẵn dưới local (bỏ qua nếu là thư mục -> tránh FileResponse ném 500)
    if os.path.exists(local_path) and not os.path.isdir(local_path):
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
            root_id = gdrive_manager.find_folder("tiktok-record", access_token=tok)
            user_fid = gdrive_manager.find_folder(user, parent_id=root_id, access_token=tok) if root_id else None
            if not user_fid:
                raise HTTPException(status_code=404, detail=f"Không tìm thấy video @{user}/{filename}")
            headers = {"Authorization": f"Bearer {tok}"}
            safe_name = filename.replace("'", "\\'")
            q_vid = f"name = '{safe_name}' and '{user_fid}' in parents and trashed = false"
            with requests.get(
                "https://www.googleapis.com/drive/v3/files",
                headers=headers,
                params={"q": q_vid, "fields": "files(id)"},
                timeout=10
            ) as res_vid:
                if res_vid.status_code == 200:
                    vid_files = res_vid.json().get("files", [])
                    if vid_files:
                        return stream_video_by_id(vid_files[0]["id"], request, redirect=redirect)
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
