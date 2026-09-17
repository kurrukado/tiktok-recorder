import os
import sys
import json
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

def get_notifier_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def send_telegram(message):
    """
    Sends a notification message to Telegram via Bot API.
    Does nothing if telegram is disabled or tokens are not set.
    """
    cfg = get_notifier_config()
    token = cfg.get("telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = cfg.get("telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    enabled = cfg.get("telegram_enabled", True) if (token and chat_id) else False

    if not token or not chat_id or not enabled:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        r = requests.post(url, json=payload, headers={"User-Agent": "TikTokRecorderBot/1.0"}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[!] Lỗi khi gửi thông báo Telegram: {e}")
    return False

def sync_to_gdrive(local_path, target_user):
    """
    Syncs or moves the recorded video to Google Drive.
    Prioritizes direct Google Drive REST API (gdrive_manager) if refresh_token is set,
    falls back to rclone if available.
    """
    cfg = get_notifier_config()
    enabled = cfg.get("gdrive_enabled", False) or bool(cfg.get("gdrive_refresh_token")) or bool(os.environ.get("GDRIVE_REFRESH_TOKEN"))
    if not enabled:
        return None

    # Option 1: Direct Google Drive API via gdrive_manager
    refresh_tok = cfg.get("gdrive_refresh_token") or os.environ.get("GDRIVE_REFRESH_TOKEN", "").strip()
    if refresh_tok:
        try:
            import gdrive_manager
            token = gdrive_manager.get_access_token()
            if token:
                root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=token)
                sub_id = gdrive_manager.find_or_create_folder(target_user, parent_id=root_id, access_token=token)
                ok = gdrive_manager.upload_file_to_drive(local_path, sub_id, access_token=token)
                if ok:
                    send_telegram(f"☁️ <b>Đã đồng bộ Google Drive:</b>\nVideo của <code>@{target_user}</code> đã được tải lên: <code>tiktok-record/{target_user}/</code>")
                    drive_file_id = ok if isinstance(ok, str) else None
                    try:
                        import supabase_sync
                        supabase_sync.sync_recording_to_supabase(
                            user=target_user,
                            filename=os.path.basename(local_path),
                            size_bytes=os.path.getsize(local_path) if os.path.exists(local_path) else 0,
                            drive_file_id=drive_file_id,
                            source="notifier"
                        )
                    except Exception as s_err:
                        print(f"[!] Lỗi sync Supabase từ notifier: {s_err}")

                    if cfg.get("gdrive_delete_local", False) and os.path.exists(local_path):
                        try:
                            os.remove(local_path)
                            print(f"[✓] Đã xóa file local theo cấu hình gdrive_delete_local: {local_path}")
                        except Exception:
                            pass
                    return True
        except Exception as e:
            print(f"[!] Lỗi khi upload qua gdrive_manager: {e}")
    return False

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Test Telegram and Google Drive Notifications")
    parser.add_argument("--test-telegram", action="store_true", help="Gửi tin nhắn thử nghiệm tới Telegram")
    args = parser.parse_args()

    if args.test_telegram:
        ok = send_telegram("🤖 <b>TikTok Recorder Bot:</b> Kết nối Telegram thành công!")
        if ok:
            print("[✓] Tin nhắn kiểm tra đã được gửi thành công đến Telegram!")
        else:
            print("[!] Không thể gửi tin nhắn. Hãy kiểm tra lại telegram_bot_token và telegram_chat_id trong config.json.")
