import os
import sys
import json
import time
import random
import argparse
import re
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

import recorder_core
import auto_h264
import gdrive_manager
import notifier

def log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        log(f"[!] Không thể ghi config.json: {e}")

_LAST_DRIVE_CHECK = 0
_CACHED_DRIVE_USERS = None

def load_monitored_users():
    global _LAST_DRIVE_CHECK, _CACHED_DRIVE_USERS

    now = time.time()
    # Kiểm tra danh sách streamer mới từ Google Drive sau mỗi 15 giây
    if now - _LAST_DRIVE_CHECK > 15:
        _LAST_DRIVE_CHECK = now
        try:
            drive_users = gdrive_manager.load_streamers_from_drive()
            if drive_users is not None and isinstance(drive_users, list):
                _CACHED_DRIVE_USERS = [u.strip().replace("@", "") for u in drive_users if u.strip()]
        except Exception:
            pass

    if _CACHED_DRIVE_USERS is not None:
        return _CACHED_DRIVE_USERS

    cfg = load_config()
    users = cfg.get("monitored_users")
    if users is not None and isinstance(users, list):
        return [u.strip().replace("@", "") for u in users if u.strip()]
    return []

def discover_new_streamers(current_user):
    """
    Tự động phát hiện đối thủ PK, khách mời co-host hoặc streamer liên quan từ trang Live.
    """
    try:
        from curl_cffi import requests
        cookies = recorder_core.load_cookies()
        s = requests.Session(impersonate="chrome136")
        if cookies:
            s.cookies.update(cookies)
        resp = s.get(f"https://www.tiktok.com/@{current_user}/live", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }, timeout=8)
        found = set(re.findall(r'"uniqueId":"([a-zA-Z0-9_\.]+)"', resp.text))
        found.discard(current_user.lower())
        for sys_id in ["tiktok", "live", "admin", "help", "privacy"]:
            found.discard(sys_id)
        return list(found)
    except Exception:
        return []

MAX_CONCURRENT_RECORDERS = 10  # Tối đa 10 streamer ghi hình cùng lúc
MAX_CHUNK_SECONDS = 7000       # 1 tiếng 56 phút (< 2 tiếng), tự động cắt và ghi tiếp
ACTIVE_RECORDERS = {}          # {user: {"thread": Thread, "start_time": float}}
RECORDERS_LOCK = threading.Lock()

def streamer_recording_worker(user, initial_room_id, auto_discover=True):
    """
    Luồng ghi hình độc lập cho từng streamer:
    - Ghi từng đoạn ngắn dưới 2 tiếng (mặc định 1h56m).
    - Hết đoạn: tự xuất file MP4 progressive +faststart, cắt thumbnail 50% thời lượng, upload Google Drive và xóa file tạm.
    - Nếu streamer vẫn đang live: tự động nối tiếp ghi Phần tiếp theo (part 2, part 3...) mà không ngắt quãng bot.
    - Cập nhật trạng thái đang quay lên Google Drive theo thời gian thực để API hiển thị.
    """
    log(f"🎬 [Luồng mới] Bắt đầu phiên ghi hình cho @{user} (Hỗ trợ tối đa {MAX_CONCURRENT_RECORDERS} streamer cùng lúc)...")
    
    # Cập nhật trạng thái đang quay và tạo ngay thư mục trên Google Drive
    try:
        gdrive_manager.set_user_recording_status_drive(user, True)
        gdrive_manager.create_streamer_folder_drive(user)
    except Exception:
        pass

    part_number = 1
    current_room_id = initial_room_id

    try:
        while True:
            # Tự động tìm kiếm đối thủ PK / Co-hosts nếu bật
            if auto_discover:
                try:
                    new_found = discover_new_streamers(user)
                    if new_found:
                        cfg = load_config()
                        current_list = cfg.get("monitored_users", [])
                        added_any = False
                        for nf in new_found:
                            if nf not in current_list:
                                current_list.append(nf)
                                added_any = True
                                log(f"✨ [Auto-Discover] Tự động phát hiện streamer mới từ phiên live: @{nf}")
                        if added_any:
                            cfg["monitored_users"] = current_list
                            save_config(cfg)
                            try:
                                gdrive_manager.save_streamers_to_drive(current_list)
                            except Exception:
                                pass
                except Exception:
                    pass

            stream_url = recorder_core.get_live_stream_url(current_room_id, user=user)
            if not stream_url:
                log(f"[!] Không lấy được URL stream của @{user}, kết thúc luồng.")
                break

            now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            part_suffix = f"_part{part_number}" if part_number > 1 else ""
            user_dir = os.path.join(BASE_DIR, user)
            os.makedirs(user_dir, exist_ok=True)
            output_file = os.path.join(user_dir, f"{user}_{now_str}{part_suffix}.mp4")

            log(f"🔴 [@{user}] Đang ghi hình Phần {part_number} (Tối đa < 2 tiếng: {MAX_CHUNK_SECONDS}s)...")

            # Ghi hình với giới hạn duration = MAX_CHUNK_SECONDS (7000s ~ 1h56m)
            rec_result = recorder_core.record_stream_ffmpeg(
                stream_url,
                output_filename=output_file,
                target_user=user,
                duration=MAX_CHUNK_SECONDS
            )

            if rec_result and os.path.exists(rec_result) and os.path.getsize(rec_result) > 1024:
                log(f"[✓] [@{user}] Hoàn tất Phần {part_number}: {os.path.basename(rec_result)}")

                # 1. Trích xuất thumbnail từ 50% thời lượng của đoạn này
                thumb_file = None
                try:
                    from api_server import extract_middle_thumbnail
                    thumb_file = extract_middle_thumbnail(rec_result)
                    if thumb_file and os.path.exists(thumb_file):
                        log(f"[✓] [@{user}] Đã tạo thumbnail Phần {part_number}: {os.path.basename(thumb_file)}")
                except Exception as th_err:
                    log(f"[!] [@{user}] Lỗi tạo thumbnail: {th_err}")

                # 2. Tự động đồng bộ ngay vào Supabase Storage (ảnh thumbnail) & Database
                rec_file_name = os.path.basename(rec_result)
                rec_file_size = os.path.getsize(rec_result) if os.path.exists(rec_result) else 0
                try:
                    import supabase_sync
                    log(f"⚡ [@{user}] Tự động đồng bộ thumbnail & metadata Phần {part_number} lên Supabase...")
                    supabase_sync.sync_recording_to_supabase(
                        user=user,
                        filename=rec_file_name,
                        size_bytes=rec_file_size,
                        thumb_source=thumb_file,
                        source="cloud_daemon"
                    )
                except Exception as sb_err:
                    log(f"[!] [@{user}] Lỗi đồng bộ Supabase: {sb_err}")

                # 3. Tải video & thumbnail lên Google Drive
                try:
                    log(f"[*] [@{user}] Đang tải Phần {part_number} lên Google Drive...")
                    tok = gdrive_manager.get_access_token()
                    if tok:
                        r_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=tok)
                        s_id = gdrive_manager.find_or_create_folder(user, parent_id=r_id, access_token=tok)
                        ok = gdrive_manager.upload_file_to_drive(rec_result, s_id, access_token=tok)
                        if ok:
                            log(f"[✓] [@{user}] Đã lưu video Phần {part_number} lên Google Drive!")
                            try:
                                os.remove(rec_result)
                                log(f"🗑️ [@{user}] Đã xóa video tạm Phần {part_number} để giải phóng ổ cứng.")
                            except Exception:
                                pass

                        if thumb_file and os.path.exists(thumb_file):
                            gdrive_manager.upload_file_to_drive(thumb_file, s_id, access_token=tok)
                            try:
                                os.remove(thumb_file)
                            except Exception:
                                pass
                except Exception as up_err:
                    log(f"[!] [@{user}] Lỗi khi tải lên Google Drive: {up_err}")

            # 3. Kiểm tra xem streamer còn live hay không để ghi tiếp Phần tiếp theo
            log(f"🔍 [@{user}] Kiểm tra xem streamer còn live để ghi tiếp Phần {part_number + 1}...")
            time.sleep(3)
            is_live, new_room_id = recorder_core.check_user_live(user)
            if is_live and new_room_id:
                part_number += 1
                current_room_id = new_room_id
                log(f"⏩ [@{user}] Streamer VẪN ĐANG LIVE! Tiếp tục ghi hình nối tiếp Phần {part_number} ngay lập tức...")
                continue
            else:
                log(f"🏁 [@{user}] Phiên livestream đã kết thúc hoàn toàn sau {part_number} phần.")
                break

    except Exception as err:
        log(f"[!] Lỗi trong luồng ghi hình của @{user}: {err}")
    finally:
        with RECORDERS_LOCK:
            ACTIVE_RECORDERS.pop(user, None)
        try:
            gdrive_manager.set_user_recording_status_drive(user, False)
        except Exception:
            pass
        log(f"⏹️ [@{user}] Đã đóng luồng ghi hình.")

def run_daemon(max_minutes=210, interval=25, auto_discover=True):
    start_time = time.time()
    max_seconds = max_minutes * 60
    hard_limit_seconds = 320 * 60  # Giới hạn tối đa 5 tiếng 20 phút (tránh chạm mốc 6h của GitHub)

    log("=" * 65)
    log("   TIKTOK 24/7 CLOUD AUTO RECORDER (MULTI-THREADED & CHUNKING)")
    log("=" * 65)
    log(f"[*] Thời gian định kỳ phiên: {max_minutes} phút")
    log(f"[*] Ghi hình đồng thời tối đa: {MAX_CONCURRENT_RECORDERS} streamers song song")
    log(f"[*] Giới hạn mỗi video live: < 2 tiếng ({MAX_CHUNK_SECONDS}s/đoạn, tự động ghi tiếp)")
    log(f"[*] Chu kỳ quét thông minh: ~{interval}s (kèm độ trễ ngẫu nhiên)")
    log(f"[*] Tự động khám phá streamer (PK / Co-host): {'BẬT' if auto_discover else 'TẮT'}")

    # Kiểm tra kết nối Google Drive
    try:
        token = gdrive_manager.get_access_token()
        if token:
            root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=token)
            log(f"[✓] Kết nối Google Drive thành công! (ID: {root_id})")
        else:
            log("[!] Chưa có token Google Drive, video sẽ chỉ lưu tạm trên mây.")
    except Exception as e:
        log(f"[!] Lỗi kiểm tra Google Drive: {e}")

    session_count = 0

    while True:
        elapsed = time.time() - start_time

        # Dọn dẹp các luồng đã hoàn tất
        with RECORDERS_LOCK:
            dead_users = [u for u, info in ACTIVE_RECORDERS.items() if not info["thread"].is_alive()]
            for u in dead_users:
                ACTIVE_RECORDERS.pop(u, None)

        # Kiểm tra điều kiện luân chuyển phiên mượt mà (Graceful Rotation)
        if elapsed >= max_seconds:
            with RECORDERS_LOCK:
                active_count = len(ACTIVE_RECORDERS)
            if active_count > 0:
                log(f"[*] Đã qua {int(elapsed // 60)} phút, hiện có {active_count} streamer đang quay dở. Tiếp tục chờ hoàn tất...")
                if elapsed >= hard_limit_seconds:
                    log("[!] Chạm ngưỡng an toàn 5.3 giờ của GitHub. Bắt buộc kết thúc phiên để bảo vệ video.")
                    break
            else:
                log(f"[*] Đã đạt mốc chuyển giao ({int(elapsed // 60)} phút). Không có livestream nào đang dở. Luân chuyển sang phiên mới ngay lập tức!")
                break

        users = load_monitored_users()
        session_count += 1
        with RECORDERS_LOCK:
            active_now = list(ACTIVE_RECORDERS.keys())
        if session_count % 15 == 1:
            log(f"[*] Đang theo dõi {len(users)} streamers. Đang ghi hình song song ({len(active_now)}/{MAX_CONCURRENT_RECORDERS}): {active_now}")

        for user in users:
            # Nếu streamer này đang được ghi hình -> Bỏ qua
            with RECORDERS_LOCK:
                if user in ACTIVE_RECORDERS:
                    continue
                if len(ACTIVE_RECORDERS) >= MAX_CONCURRENT_RECORDERS:
                    log(f"[!] Đã đạt giới hạn tối đa {MAX_CONCURRENT_RECORDERS} streamer cùng lúc. Chờ luồng trống...")
                    break

            try:
                is_live, room_id = recorder_core.check_user_live(user)
                if is_live and room_id:
                    log(f"🔴 PHÁT HIỆN LIVESTREAM: @{user} đang trực tiếp (Room ID: {room_id})")
                    notifier.send_telegram(f"🔴 <b>STREAMER ĐANG LIVE!</b>\n👤 <code>@{user}</code> bắt đầu phát livestream.\nĐang tự động ghi hình đa luồng HD H.264 (< 2 tiếng/phần)...")

                    # Khởi chạy luồng ghi hình riêng biệt (không chặn luồng quét)
                    t = threading.Thread(
                        target=streamer_recording_worker,
                        args=(user, room_id, auto_discover),
                        daemon=True
                    )
                    with RECORDERS_LOCK:
                        ACTIVE_RECORDERS[user] = {"thread": t, "start_time": time.time()}
                    t.start()
                    time.sleep(1)

            except Exception as e:
                log(f"[!] Lỗi kiểm tra @{user}: {e}")

            time.sleep(2)

        # Smart Jitter Delay để tránh bị TikTok chặn tần suất
        jitter = random.uniform(-3.0, 4.0)
        time.sleep(max(15, interval + jitter))

    log("[✓] Phiên làm việc kết thúc thành công.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TikTok Cloud Daemon")
    parser.add_argument("--duration-minutes", type=int, default=210, help="Thời gian chạy phiên (phút)")
    parser.add_argument("--interval", type=int, default=25, help="Khoảng cách kiểm tra (giây)")
    parser.add_argument("--no-discover", action="store_true", help="Tắt tính năng tự động khám phá streamer mới")
    args = parser.parse_args()
    run_daemon(max_minutes=args.duration_minutes, interval=args.interval, auto_discover=not args.no_discover)
