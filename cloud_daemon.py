import os
import sys
import json
import time
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

import recorder_core
import auto_h264
import gdrive_manager
import notifier

def log(msg):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{now}] {msg}', flush=True)

def load_monitored_users():
    default_users = ['islizanx', 'itsme_kate0110', 'urielhui38']
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                users = cfg.get('monitored_users')
                if users and isinstance(users, list):
                    return [u.strip().replace('@', '') for u in users if u.strip()]
        except Exception:
            pass
    return default_users

def run_daemon(max_minutes=300, interval=20):
    start_time = time.time()
    max_seconds = max_minutes * 60

    log('=' * 65)
    log('   TIKTOK 24/7 CLOUD AUTO RECORDER (GITHUB ACTIONS)')
    log('=' * 65)
    log(f'[*] Thời gian chạy phiên này: {max_minutes} phút')
    log(f'[*] Chu kỳ quét: mỗi {interval} giây')

    # Test Google Drive connection
    try:
        token = gdrive_manager.get_access_token()
        if token:
            root_id = gdrive_manager.find_or_create_folder('tiktok-record', access_token=token)
            log(f'[✓] Kết nối Google Drive thành công! Thư mục gốc tiktok-record ID: {root_id}')
        else:
            log('[!] CẢNH BÁO: Chưa lấy được token Google Drive. Vui lòng kiểm tra config.json.')
    except Exception as e:
        log(f'[!] Lỗi kiểm tra Google Drive: {e}')

    while True:
        elapsed = time.time() - start_time
        if elapsed >= max_seconds:
            log(f'[*] Đã hoàn thành phiên chạy ({max_minutes} phút). Chuẩn bị chuyển giao lượt chạy tiếp theo...')
            break

        users = load_monitored_users()
        for user in users:
            try:
                is_live, room_id = recorder_core.check_user_live(user)
                if is_live and room_id:
                    log(f'🔴 PHÁT HIỆN LIVESTREAM: @{user} đang trực tiếp (Room ID: {room_id})')
                    notifier.send_telegram(f'🔴 <b>STREAMER ĐANG LIVE!</b>\n👤 <code>@{user}</code> bắt đầu phát livestream.\nĐang tự động ghi hình HD H.264...')

                    stream_url = recorder_core.get_live_stream_url(room_id, user=user)
                    if stream_url:
                        output_file = recorder_core.record_stream_ffmpeg(stream_url, target_user=user)
                        if output_file and os.path.exists(output_file):
                            log(f'[✓] Ghi hình hoàn tất: {os.path.basename(output_file)}')

                            # Tự động tải lên Google Drive
                            try:
                                log(f'[*] Đang tải video lên Google Drive (tiktok-record/{user}/)...')
                                tok = gdrive_manager.get_access_token()
                                if tok:
                                    r_id = gdrive_manager.find_or_create_folder('tiktok-record', access_token=tok)
                                    s_id = gdrive_manager.find_or_create_folder(user, parent_id=r_id, access_token=tok)
                                    ok = gdrive_manager.upload_file_to_drive(output_file, s_id, access_token=tok)
                                    if ok:
                                        log(f'[✓] Đã lưu thành công lên Google Drive: {os.path.basename(output_file)}')
                                        try:
                                            os.remove(output_file)
                                            log(f'[*] Đã xóa file tạm trên máy chủ mây.')
                                        except Exception:
                                            pass
                            except Exception as up_err:
                                log(f'[!] Lỗi khi tải lên Google Drive: {up_err}')
                    else:
                        log(f'[!] Không lấy được URL stream của @{user}')
            except Exception as e:
                log(f'[!] Lỗi kiểm tra @{user}: {e}')

            time.sleep(3)

        time.sleep(interval)

    log('[✓] Phiên làm việc kết thúc thành công.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='TikTok Cloud Daemon')
    parser.add_argument('--duration-minutes', type=int, default=300, help='Thời gian chạy (phút)')
    parser.add_argument('--interval', type=int, default=20, help='Khoảng cách giữa các lần kiểm tra (giây)')
    args = parser.parse_args()
    run_daemon(max_minutes=args.duration_minutes, interval=args.interval)
