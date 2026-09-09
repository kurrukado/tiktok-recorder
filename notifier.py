import os
import sys
import json
import subprocess
import shutil
import urllib.request
import urllib.parse

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
    enabled = cfg.get("telegram_enabled", False)

    if not token or not chat_id:
        return False
    if not enabled and not cfg.get("telegram_bot_token"):
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "TikTokRecorderBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True
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
    if cfg.get("gdrive_refresh_token"):
        try:
            import gdrive_manager
            token = gdrive_manager.get_access_token()
            if token:
                root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=token)
                sub_id = gdrive_manager.find_or_create_folder(target_user, parent_id=root_id, access_token=token)
                ok = gdrive_manager.upload_file_to_drive(local_path, sub_id, access_token=token)
                if ok:
                    send_telegram(f"☁️ <b>Đã đồng bộ Google Drive:</b>\nVideo của <code>@{target_user}</code> đã được tải lên: <code>tiktok-record/{target_user}/</code>")
                    return True
        except Exception as e:
            print(f"[!] Lỗi khi upload qua gdrive_manager: {e}")

    # Option 2: Fallback to rclone
    rclone_bin = shutil.which("rclone")
    if not rclone_bin:
        print("[!] Không tìm thấy công cụ rclone trên hệ thống.")
        return None

    remote_name = cfg.get("gdrive_remote", "gdrive").rstrip(":")
    remote_dest = f"{remote_name}:tiktok-record/{target_user}/"
    delete_local = cfg.get("gdrive_delete_local", False)

    action = "move" if delete_local else "copy"
    print(f"[*] Đang tải video lên Google Drive ({remote_dest})...")
    
    cmd = [rclone_bin, action, local_path, remote_dest, "--progress"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0:
            print(f"[✓] Đã đồng bộ lên Google Drive thành công: {remote_dest}")
            send_telegram(f"☁️ <b>Đã đồng bộ Google Drive:</b>\nVideo của <code>@{target_user}</code> đã được tải lên: <code>tiktok-record/{target_user}/</code>")
            return True
        else:
            print(f"[!] Rclone lỗi: {proc.stderr[:200]}")
    except Exception as e:
        print(f"[!] Lỗi khi chạy rclone: {e}")

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
