import os
import sys
import json
import time
import random
import argparse
import re
import threading
import subprocess
import shutil
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

import recorder_core
import auto_h264
import gdrive_manager
import notifier

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
        log(f"[!] Lỗi khi ghép nối phân đoạn video: {e}")
        return valid_files[0]
    finally:
        if os.path.exists(list_txt):
            try:
                os.remove(list_txt)
            except Exception:
                pass

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
                cleaned = [u.strip().replace("@", "") for u in drive_users if u.strip()]
                if _CACHED_DRIVE_USERS != cleaned:
                    log(f"[*] Cập nhật danh sách từ Google Drive ({len(cleaned)} streamers): {cleaned}")
                _CACHED_DRIVE_USERS = cleaned
        except Exception as e:
            log(f"[!] Lỗi đọc danh sách streamer từ Drive: {e}")

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
MAX_CHUNK_SECONDS = 3600       # Đúng 1 tiếng (1h = 3600s), tự động tách video và up lên Cloud
ACTIVE_RECORDERS = {}          # {user: {"thread": Thread, "start_time": float}}
RECORDERS_LOCK = threading.Lock()

def streamer_recording_worker(user, initial_room_id, auto_discover=True, stop_event=None):
    """
    Luồng ghi hình độc lập cho từng streamer:
    - Ghi từng đoạn tối đa 1 tiếng (3600s).
    - Hết đoạn 1 tiếng: tự xuất file MP4 H.264 +faststart, cắt thumbnail 50% thời lượng, upload Google Drive và Supabase, xóa file tạm.
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
    max_vip_attempts = 5
    guest_session = None
    consecutive_failures = 0
    max_consecutive_failures = 4

    # Kiểm tra xem buổi live có phải VIP Sub-only không qua check_live_details
    live_details = recorder_core.check_live_details(user)
    is_sub_only = live_details.get("is_sub_only", False)
    if is_sub_only:
        log(f"🔒 [@{user}] Phát hiện phòng live VIP Sub-Only / Paid Event! Kích hoạt chế độ VIP Sub-Only Quick Watchdog & Session Rotation.")

    try:
        while True:
            if stop_event and stop_event.is_set():
                log(f"⏹️ [@{user}] Dừng luồng theo yêu cầu của hệ thống.")
                break

            # Tự động tìm kiếm đối thủ PK / Co-hosts nếu bật
            if auto_discover:
                try:
                    new_found = discover_new_streamers(user)
                    if new_found:
                        current_list = list(load_monitored_users())
                        added_any = False
                        for nf in new_found:
                            if nf not in current_list:
                                current_list.append(nf)
                                added_any = True
                                log(f"✨ [Auto-Discover] Tự động phát hiện streamer mới từ phiên live: @{nf}")
                        if added_any:
                            _CACHED_DRIVE_USERS = current_list
                            cfg = load_config()
                            cfg["monitored_users"] = current_list
                            save_config(cfg)
                            try:
                                gdrive_manager.save_streamers_to_drive(current_list)
                            except Exception:
                                pass
                except Exception:
                    pass

            now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            part_suffix = f"_part{part_number}" if part_number > 1 else ""
            user_dir = os.path.join(BASE_DIR, user)
            os.makedirs(user_dir, exist_ok=True)
            output_file = os.path.join(user_dir, f"{user}_{now_str}{part_suffix}.mp4")

            part_segments = []
            accumulated_seconds = 0.0
            log(f"🔴 [@{user}] Bắt đầu tích lũy Phần {part_number}{' (VIP Sub-Only Preview)' if is_sub_only else f' (Mục tiêu gom đủ 1 tiếng: {MAX_CHUNK_SECONDS}s)'}...")

            # Vòng lặp thu thập các phân đoạn cho đến khi đủ 1 tiếng hoặc streamer tắt live
            while accumulated_seconds < (300 if is_sub_only else (MAX_CHUNK_SECONDS - 60)):
                if stop_event and stop_event.is_set():
                    break

                target_duration = (300 - int(accumulated_seconds)) if is_sub_only else (MAX_CHUNK_SECONDS - int(accumulated_seconds))
                if target_duration <= 30:
                    break

                stream_url = None
                for s_attempt in range(3):
                    if is_sub_only:
                        log(f"🔄 [@{user}] Tạo phiên khách vô danh mới (Guest Session) để lấy link preview Sub-Only Phần {part_number}...")
                        guest_session = recorder_core.generate_guest_session()
                        stream_url = recorder_core.get_live_stream_url(current_room_id, user=user, session=guest_session)
                    else:
                        stream_url = recorder_core.get_live_stream_url(current_room_id, user=user)
                    
                    if stream_url:
                        break
                    time.sleep(2.5)

                if not stream_url:
                    # Thử refresh lại room_id từ live status
                    st_live, new_rid = recorder_core.check_live_status(user)
                    if st_live and new_rid:
                        current_room_id = new_rid
                        stream_url = recorder_core.get_live_stream_url(current_room_id, user=user)

                if not stream_url:
                    log(f"[!] Không lấy được URL stream của @{user}. Dừng tích lũy Phần {part_number}.")
                    consecutive_failures += 1
                    time.sleep(5)
                    break

                seg_name = os.path.join(user_dir, f"{user}_{now_str}_p{part_number}_seg{len(part_segments)+1}.mp4")
                rec_result = recorder_core.record_stream_ffmpeg(
                    stream_url,
                    output_filename=seg_name,
                    target_user=user,
                    duration=target_duration,
                    stop_event=stop_event,
                    auto_sync_gdrive=False,
                    is_sub_only=is_sub_only
                )

                from auto_h264 import validate_playable_video
                is_valid = False
                v_reason = "Không có kết quả thu"
                v_dur = 0.0
                if rec_result and os.path.exists(rec_result):
                    is_valid, v_reason, v_dur = validate_playable_video(rec_result, min_duration=5.0, min_size_bytes=250000)

                if is_valid:
                    consecutive_failures = 0
                    part_segments.append(rec_result)
                    accumulated_seconds += v_dur
                    log(f"[✓] [@{user}] Thu đoạn {len(part_segments)} ({v_dur:.1f}s). Tích lũy Phần {part_number}: {accumulated_seconds:.1f}s / {MAX_CHUNK_SECONDS}s")
                else:
                    consecutive_failures += 1
                    log(f"[!] [@{user}] Phân đoạn {len(part_segments)+1} không đạt chuẩn ({v_reason}).")
                    if rec_result and os.path.exists(rec_result):
                        try:
                            os.remove(rec_result)
                        except Exception:
                            pass
                    if consecutive_failures >= max_consecutive_failures:
                        log(f"⏸️ [@{user}] Gặp {consecutive_failures} lỗi thu luồng liên tiếp. Dừng tích lũy Phần {part_number}.")
                        break
                    time.sleep(5)

                if is_sub_only:
                    break

                if accumulated_seconds >= MAX_CHUNK_SECONDS - 60:
                    log(f"[⏱️ Đủ 1 tiếng] [@{user}] Phần {part_number} đã tích lũy đủ 1 tiếng ({accumulated_seconds:.1f}s)!")
                    break

                # Kiểm tra streamer còn live không để tiếp tục tích lũy vào Phần hiện tại
                time.sleep(3)
                curr_det = recorder_core.check_live_details(user)
                if not curr_det.get("is_live"):
                    time.sleep(4)
                    curr_det = recorder_core.check_live_details(user)

                if curr_det.get("is_live"):
                    if curr_det.get("room_id"):
                        current_room_id = curr_det.get("room_id")
                    if curr_det.get("is_sub_only"):
                        is_sub_only = True
                    log(f"⏩ [@{user}] Luồng tạm gián đoạn sau {v_dur:.1f}s nhưng streamer VẪN ĐANG LIVE. Tự động thu tiếp nối vào Phần {part_number} (còn thiếu {MAX_CHUNK_SECONDS - int(accumulated_seconds)}s)...")
                else:
                    log(f"🏁 [@{user}] Streamer đã xuống live sau {accumulated_seconds:.1f}s tích lũy.")
                    break

            if not part_segments:
                if consecutive_failures >= max_consecutive_failures:
                    break
                time.sleep(5)
                continue

            if stop_event and stop_event.is_set():
                log(f"⏹️ [@{user}] Phát hiện yêu cầu dừng tiến trình. Hủy phần đang dở.")
                for seg in part_segments:
                    if seg and os.path.exists(seg):
                        try:
                            os.remove(seg)
                        except Exception:
                            pass
                break

            # Ghép tất cả các đoạn của Phần này lại thành 1 file MP4 duy nhất
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
                log(f"🧩 [@{user}] Đang ghép nối {len(part_segments)} phân đoạn thành 1 file MP4 duy nhất cho Phần {part_number} ({accumulated_seconds:.1f}s)...")
                final_rec_file = concat_mp4_segments(part_segments, output_file)

            # Đảm bảo file hợp lệ trước khi đẩy lên Cloud
            is_valid, v_reason, final_dur = validate_playable_video(final_rec_file, min_duration=5.0, min_size_bytes=250000)
            if not is_valid:
                log(f"[!] [@{user}] File Phần {part_number} không đạt chuẩn ({v_reason}). Bỏ qua.")
                if os.path.exists(final_rec_file):
                    try:
                        os.remove(final_rec_file)
                    except Exception:
                        pass
                continue

            consecutive_failures = 0
            log(f"[✓] [@{user}] Hoàn tất trọn vẹn Phần {part_number} ({final_dur:.1f}s): {os.path.basename(final_rec_file)}")

            # 1. Trích xuất thumbnail từ 50% thời lượng của đoạn này
            thumb_file = None
            try:
                from api_server import extract_middle_thumbnail
                thumb_file = extract_middle_thumbnail(final_rec_file)
                if thumb_file and os.path.exists(thumb_file):
                    log(f"[✓] [@{user}] Đã tạo thumbnail Phần {part_number}: {os.path.basename(thumb_file)}")
            except Exception as th_err:
                log(f"[!] [@{user}] Lỗi tạo thumbnail: {th_err}")

            # 2. Tự động đồng bộ ngay vào Supabase Storage (ảnh thumbnail) & Database
            rec_file_name = os.path.basename(final_rec_file)
            rec_file_size = os.path.getsize(final_rec_file) if os.path.exists(final_rec_file) else 0
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
                log(f"[*] [@{user}] Đang tải Phần {part_number} ({final_dur:.1f}s) lên Google Drive...")
                tok = gdrive_manager.get_access_token()
                if tok:
                    r_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=tok)
                    s_id = gdrive_manager.find_or_create_folder(user, parent_id=r_id, access_token=tok)
                    ok = gdrive_manager.upload_file_to_drive(final_rec_file, s_id, access_token=tok)
                    if ok:
                        log(f"[✓] [@{user}] Đã lưu video Phần {part_number} lên Google Drive!")
                        try:
                            os.remove(final_rec_file)
                            log(f"🗑️ [@{user}] Đã xóa video tạm Phần {part_number} để giải phóng ổ cứng.")
                        except Exception:
                            pass
                    else:
                        log(f"[!] [@{user}] Không thể upload video lên Drive sau các lần thử. Giữ lại file local.")

                    if thumb_file and os.path.exists(thumb_file):
                        gdrive_manager.upload_file_to_drive(thumb_file, s_id, access_token=tok)
                        try:
                            os.remove(thumb_file)
                        except Exception:
                            pass
            except Exception as up_err:
                log(f"[!] [@{user}] Lỗi khi tải lên Google Drive: {up_err}")

            part_number += 1

            if stop_event and stop_event.is_set():
                log(f"⏹️ [@{user}] Nhận lệnh dừng phiên. Không ghi tiếp phần mới.")
                break

            # 4. Kiểm tra xem streamer còn live hay không để ghi tiếp Phần tiếp theo
            if is_sub_only:
                if part_number >= max_vip_attempts:
                    log(f"🛑 [@{user}] Đã đạt giới hạn tối đa {max_vip_attempts} lần xoay Guest Session preview Sub-Only. Kết thúc luồng.")
                    break

                log(f"🔍 [@{user}] Kiểm tra xem streamer còn live VIP Sub-Only để xoay Guest Session cho Phần {part_number}...")
                time.sleep(2)
                curr_det = recorder_core.check_live_details(user)
                if curr_det.get("is_live"):
                    if curr_det.get("room_id"):
                        current_room_id = curr_det.get("room_id")
                    log(f"⏩ [@{user}] Streamer VẪN ĐANG LIVE VIP Sub-Only! Tiếp tục xoay Guest Session ghi tiếp Phần {part_number}...")
                    continue
                else:
                    log(f"🏁 [@{user}] Streamer đã xuống live sau {part_number - 1} phần preview.")
                    break
            else:
                log(f"🔍 [@{user}] Kiểm tra xem streamer còn live để ghi tiếp Phần {part_number} (1 tiếng tiếp theo)...")
                time.sleep(3)
                curr_det = recorder_core.check_live_details(user)
                if not curr_det.get("is_live"):
                    time.sleep(4)
                    curr_det = recorder_core.check_live_details(user)

                is_live = curr_det.get("is_live", False)
                new_room_id = curr_det.get("room_id")
                if is_live and new_room_id:
                    current_room_id = new_room_id
                    if curr_det.get("is_sub_only"):
                        is_sub_only = True
                        log(f"🔒 [@{user}] Streamer đã chuyển sang chế độ VIP Sub-Only! Kích hoạt Quick Watchdog và Guest Rotation...")
                    log(f"⏩ [@{user}] Streamer VẪN ĐANG LIVE! Tiếp tục ghi hình nối tiếp Phần {part_number} ngay lập tức...")
                    continue
                else:
                    log(f"🏁 [@{user}] Phiên livestream đã kết thúc hoàn toàn sau {part_number - 1} phần.")
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
    log(f"[*] Giới hạn mỗi video live: < 1 tiếng ({MAX_CHUNK_SECONDS}s/đoạn, tự động ghi tiếp)")
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
                    log("[!] Chạm ngưỡng an toàn 5.3 giờ của GitHub. Bắt đầu kết thúc an toàn phiên để chốt video và upload...")
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

        with RECORDERS_LOCK:
            active_set = set(ACTIVE_RECORDERS.keys())
            slots_available = MAX_CONCURRENT_RECORDERS - len(active_set)

        users_to_check = [u for u in users if u not in active_set]

        if users_to_check and slots_available > 0:
            import concurrent.futures

            def _check(u):
                try:
                    return u, recorder_core.check_user_live(u)
                except Exception:
                    return u, (False, None)

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(users_to_check), 6)) as executor:
                check_results = list(executor.map(_check, users_to_check))

            for user, (is_live, room_id) in check_results:
                with RECORDERS_LOCK:
                    if user in ACTIVE_RECORDERS or len(ACTIVE_RECORDERS) >= MAX_CONCURRENT_RECORDERS:
                        continue

                if is_live and room_id:
                    log(f"🔴 PHÁT HIỆN LIVESTREAM: @{user} đang trực tiếp (Room ID: {room_id})")
                    notifier.send_telegram(f"🔴 <b>STREAMER ĐANG LIVE!</b>\n👤 <code>@{user}</code> bắt đầu phát livestream.\nĐang tự động ghi hình đa luồng HD H.264 (< 2 tiếng/phần)...")

                    # Khởi chạy luồng ghi hình riêng biệt (không chặn luồng quét)
                    stop_ev = threading.Event()
                    t = threading.Thread(
                        target=streamer_recording_worker,
                        args=(user, room_id, auto_discover, stop_ev),
                        daemon=False
                    )
                    with RECORDERS_LOCK:
                        ACTIVE_RECORDERS[user] = {
                            "thread": t,
                            "start_time": time.time(),
                            "stop_event": stop_ev
                        }
                    t.start()
                    time.sleep(0.5)

        # Smart Jitter Delay để tránh bị TikTok chặn tần suất
        jitter = random.uniform(-2.0, 3.0)
        time.sleep(max(10, interval + jitter))

    # Chờ tất cả luồng ghi hình hoàn tất đóng gói và upload lên Google Drive
    with RECORDERS_LOCK:
        remaining_recorders = list(ACTIVE_RECORDERS.items())
    if remaining_recorders:
        log(f"⏳ Đang chờ {len(remaining_recorders)} luồng ghi hình hoàn tất đóng gói và upload lên Google Drive...")
        for u, info in remaining_recorders:
            if "stop_event" in info:
                info["stop_event"].set()
        for u, info in remaining_recorders:
            info["thread"].join(timeout=180)
            log(f"  [✓] Luồng @{u} đã hoàn tất.")

    log("[✓] Phiên làm việc kết thúc thành công.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TikTok Cloud Daemon")
    parser.add_argument("--duration-minutes", type=int, default=210, help="Thời gian chạy phiên (phút)")
    parser.add_argument("--interval", type=int, default=25, help="Khoảng cách kiểm tra (giây)")
    parser.add_argument("--no-discover", action="store_true", help="Tắt tính năng tự động khám phá streamer mới")
    args = parser.parse_args()
    run_daemon(max_minutes=args.duration_minutes, interval=args.interval, auto_discover=not args.no_discover)
