import os
import sys
import json
import time
import random
import argparse
import re
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

def load_monitored_users():
    default_users = ["islizanx", "itsme_kate0110", "urielhui38"]
    cfg = load_config()
    users = cfg.get("monitored_users")
    if users and isinstance(users, list):
        return [u.strip().replace("@", "") for u in users if u.strip()]
    return default_users

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

def run_daemon(max_minutes=210, interval=25, auto_discover=True):
    start_time = time.time()
    max_seconds = max_minutes * 60
    hard_limit_seconds = 320 * 60  # Giới hạn tối đa 5 tiếng 20 phút (tránh chạm mốc 6h của GitHub)

    log("=" * 65)
    log("   TIKTOK 24/7 CLOUD AUTO RECORDER (GITHUB ACTIONS OPTIMIZED)")
    log("=" * 65)
    log(f"[*] Thời gian định kỳ phiên: {max_minutes} phút")
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

        # Kiểm tra điều kiện luân chuyển phiên mượt mà (Graceful Rotation)
        if elapsed >= max_seconds:
            log(f"[*] Đã đạt mốc chuyển giao ({int(elapsed // 60)} phút). Đang kiểm tra trước khi kết thúc phiên...")
            log("[✓] Không có livestream nào đang dở. Luân chuyển sang phiên mới ngay lập tức!")
            break

        if elapsed >= hard_limit_seconds:
            log("[!] Chạm ngưỡng an toàn 5.3 giờ của GitHub. Bắt buộc kết thúc phiên để bảo vệ video.")
            break

        users = load_monitored_users()
        session_count += 1
        if session_count % 15 == 1:
            log(f"[*] Đang theo dõi {len(users)} streamers: {users}")

        for user in users:
            try:
                is_live, room_id = recorder_core.check_user_live(user)
                if is_live and room_id:
                    log(f"🔴 PHÁT HIỆN LIVESTREAM: @{user} đang trực tiếp (Room ID: {room_id})")
                    notifier.send_telegram(f"🔴 <b>STREAMER ĐANG LIVE!</b>\n👤 <code>@{user}</code> bắt đầu phát livestream.\nĐang tự động ghi hình HD H.264...")

                    # Tự động tìm kiếm đối thủ PK / Co-hosts nếu tính năng bật
                    if auto_discover:
                        new_found = discover_new_streamers(user)
                        if new_found:
                            cfg = load_config()
                            current_list = cfg.get("monitored_users", users)
                            added_any = False
                            for nf in new_found:
                                if nf not in current_list:
                                    current_list.append(nf)
                                    added_any = True
                                    log(f"✨ [Auto-Discover] Tự động phát hiện streamer mới từ phiên live: @{nf}")
                            if added_any:
                                cfg["monitored_users"] = current_list
                                save_config(cfg)

                    stream_url = recorder_core.get_live_stream_url(room_id, user=user)
                    if stream_url:
                        output_file = recorder_core.record_stream_ffmpeg(stream_url, target_user=user)
                        if output_file and os.path.exists(output_file):
                            log(f"[✓] Ghi hình hoàn tất: {os.path.basename(output_file)}")

                            # Tự động cắt ảnh xem trước (thumbnail) từ giữa video
                            thumb_file = None
                            try:
                                from api_server import extract_middle_thumbnail
                                thumb_file = extract_middle_thumbnail(output_file)
                                if thumb_file and os.path.exists(thumb_file):
                                    log(f"[✓] Đã tạo ảnh xem trước (thumbnail): {os.path.basename(thumb_file)}")
                            except Exception as th_err:
                                log(f"[!] Không thể tạo thumbnail: {th_err}")

                            # Tự động tải lên Google Drive
                            try:
                                log(f"[*] Đang tải video & ảnh xem trước lên Google Drive (tiktok-record/{user}/)...")
                                tok = gdrive_manager.get_access_token()
                                if tok:
                                    r_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=tok)
                                    s_id = gdrive_manager.find_or_create_folder(user, parent_id=r_id, access_token=tok)
                                    ok = gdrive_manager.upload_file_to_drive(output_file, s_id, access_token=tok)
                                    if ok:
                                        log(f"[✓] Đã lưu video thành công lên Google Drive: {os.path.basename(output_file)}")
                                        try:
                                            os.remove(output_file)
                                            log(f"🗑️ Đã xóa file video tạm để giải phóng ổ cứng.")
                                        except Exception:
                                            pass

                                    # Tải tiếp thumbnail lên Google Drive
                                    if thumb_file and os.path.exists(thumb_file):
                                        gdrive_manager.upload_file_to_drive(thumb_file, s_id, access_token=tok)
                                        try:
                                            os.remove(thumb_file)
                                        except Exception:
                                            pass
                            except Exception as up_err:
                                log(f"[!] Lỗi khi tải lên Google Drive: {up_err}")
                    else:
                        log(f"[!] Không lấy được URL stream của @{user}")

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
