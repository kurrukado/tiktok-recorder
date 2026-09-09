import os
import sys
import json
import time
import re
import shutil
import subprocess
from datetime import datetime
from curl_cffi import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG_PATH = os.path.join(BASE_DIR, "ffmpeg.exe") if os.path.exists(os.path.join(BASE_DIR, "ffmpeg.exe")) else (shutil.which("ffmpeg") or "ffmpeg")
COOKIES_FILE = os.path.join(BASE_DIR, "cookies.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "target_user": "islizanx",
    "check_interval_seconds": 20,
    "quality": "best",
    "output_dir": ".",
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "gdrive_enabled": False,
    "gdrive_remote": "gdrive",
    "gdrive_delete_local": False
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

def load_cookies():
    if os.path.exists(COOKIES_FILE):
        try:
            with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # If user entered sessionid or sessionid_ss
                cookies = {}
                for k, v in data.items():
                    if v:
                        cookies[k] = str(v).strip()
                return cookies
        except Exception:
            pass
    return {}

def save_cookies(cookies_dict):
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(cookies_dict, f, indent=4, ensure_ascii=False)
    # Also sync to _tiktok_recorder/src/cookies.json if it exists
    alt_path = os.path.join(BASE_DIR, "_tiktok_recorder", "src", "cookies.json")
    if os.path.exists(os.path.dirname(alt_path)):
        try:
            with open(alt_path, "w", encoding="utf-8") as f:
                json.dump(cookies_dict, f, indent=4, ensure_ascii=False)
        except Exception:
            pass


def check_live_status(user):
    """
    Returns (is_live: bool, room_id: str or None, title: str)
    """
    from TikTokLive import TikTokLiveClient
    import asyncio

    async def _check():
        client = TikTokLiveClient(unique_id=user)
        try:
            is_live = await client.is_live()
            room_id = None
            if is_live:
                try:
                    room_id = await client.web.fetch_room_id_from_api(unique_id=user)
                except Exception:
                    try:
                        room_id = await client.web.fetch_room_id_from_html(unique_id=user)
                    except Exception:
                        pass
            return is_live, str(room_id) if room_id else None
        except Exception as e:
            return False, None

    try:
        return asyncio.run(_check())
    except Exception as e:
        return False, None

check_user_live = check_live_status

def get_live_stream_url(room_id, user=None, cookies=None):
    try:
        urls = get_stream_urls(room_id, user, cookies=cookies)
        return urls[0] if urls else None
    except Exception:
        return None

def get_stream_urls(room_id, user, cookies=None):
    """
    Extract candidate stream URLs (FLV or HLS / m3u8).
    Supports 18+ restricted streams using authenticated sessionid cookies.
    """
    if cookies is None:
        cookies = load_cookies()

    session = requests.Session(impersonate="chrome136")
    if cookies:
        session.cookies.update(cookies)

    # First attempt: Direct scrape of live page HTML with session cookies (bypasses 18+ restriction)
    if user:
        try:
            live_page_url = f"https://www.tiktok.com/@{user}/live"
            page_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.127 Safari/537.36",
                "Referer": "https://www.tiktok.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            page_res = session.get(live_page_url, headers=page_headers, timeout=15)
            content = page_res.text.replace('\\"', '"').replace('\\u0026', '&').replace('&amp;', '&').replace('\\/', '/')
            
            flv_matches = re.findall(r'https?://[^\s"\'<>]+\.flv\?[^\s"\'<>]+', content)
            if flv_matches:
                clean_flv = [u.replace('&amp;', '&') for u in flv_matches]
                hd_matches = [u for u in clean_flv if "_hd" in u or "_or4" in u]
                return hd_matches if hd_matches else clean_flv

            hls_matches = re.findall(r'https?://[^\s"\'<>]+\.m3u8\?[^\s"\'<>]*', content)
            if hls_matches:
                clean_hls = [u.replace('&amp;', '&') for u in hls_matches]
                hd_matches = [u for u in clean_hls if "_hd" in u or "_or4" in u]
                return hd_matches if hd_matches else clean_hls
        except Exception:
            pass

    # Second attempt: Webcast room/info API
    url = f"https://webcast.tiktok.com/webcast/room/info/?aid=1988&room_id={room_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.127 Safari/537.36",
        "Referer": "https://www.tiktok.com/",
        "Accept": "*/*",
        "Origin": "https://www.tiktok.com",
    }
    session.headers.update(headers)
    res = session.get(url)
    try:
        data = res.json()
    except Exception:
        data = {}

    status_code = data.get("status_code", -1)
    if status_code == 4003110:
        return "AGE_RESTRICTED"

    room_data = data.get("data") or {}
    stream_url_obj = room_data.get("stream_url") or {}
    candidates = []

    sdk_data_str = (
        stream_url_obj.get("live_core_sdk_data", {})
        .get("pull_data", {})
        .get("stream_data")
    )
    if sdk_data_str:
        try:
            sdk_json = json.loads(sdk_data_str).get("data", {})
            for key, entry in sdk_json.items():
                stream_main = entry.get("main", {})
                flv = stream_main.get("flv")
                hls = stream_main.get("hls") or stream_main.get("m3u8")
                if flv and flv not in candidates:
                    candidates.append(flv)
                if hls and hls not in candidates:
                    candidates.append(hls)
        except Exception:
            pass

    # Fallback to direct URLs
    flv_pull = stream_url_obj.get("flv_pull_url") or {}
    if isinstance(flv_pull, dict):
        for k in ("FULL_HD1", "HD1", "SD2", "SD1"):
            u = flv_pull.get(k)
            if u and u not in candidates:
                candidates.append(u)

    hls_pull = stream_url_obj.get("hls_pull_url")
    if hls_pull and hls_pull not in candidates:
        candidates.append(hls_pull)

    return candidates

def record_stream_ffmpeg(stream_url, output_filename=None, target_user="islizanx", duration=None):
    """
    Records a live stream URL to an MP4 file using ffmpeg.exe without re-encoding,
    then automatically ensures it is in standard H.264 (AVC) format.
    """
    if not os.path.exists(FFMPEG_PATH):
        raise FileNotFoundError(f"Không tìm thấy ffmpeg tại {FFMPEG_PATH}")

    now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    if not output_filename:
        user_dir = os.path.join(BASE_DIR, target_user)
        os.makedirs(user_dir, exist_ok=True)
        output_filename = os.path.join(user_dir, f"{target_user}_{now_str}.mp4")
    else:
        os.makedirs(os.path.dirname(os.path.abspath(output_filename)), exist_ok=True)

    print(f"\n[+] Đang chuẩn bị file ghi hình: {os.path.basename(output_filename)}")
    print(f"[+] Thư mục đích: {os.path.dirname(os.path.abspath(output_filename))}")
    print(f"[+] Bắt đầu thu tín hiệu livestream từ máy chủ TikTok...")
    print(f"[*] Nhấn [Ctrl + C] bất kỳ lúc nào để dừng và lưu video.\n")

    cmd = [
        FFMPEG_PATH,
        "-y",
        "-rw_timeout", "15000000",
        "-headers", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36\r\n"
            "Referer: https://www.tiktok.com/\r\n"
        ),
        "-i", stream_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
    ]
    if duration:
        cmd.extend(["-t", str(duration)])
    cmd.append(output_filename)

    start_time = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        )
        
        last_size = 0
        last_growth_time = time.time()
        last_live_check_time = time.time()

        while proc.poll() is None:
            time.sleep(1)
            now = time.time()
            elapsed = int(now - start_time)
            mins, secs = divmod(elapsed, 60)
            hours, mins = divmod(mins, 60)
            
            size_bytes = 0
            if os.path.exists(output_filename):
                size_bytes = os.path.getsize(output_filename)
            size_mb = size_bytes / (1024 * 1024)
            
            # Theo dõi tăng trưởng dung lượng file
            if size_bytes > last_size:
                last_size = size_bytes
                last_growth_time = now

            stagnant_seconds = int(now - last_growth_time)

            # Nếu trong 12s liên tục không có thêm dữ liệu mới -> Kiểm tra xem streamer đã tắt live chưa
            if size_bytes > 1024 and stagnant_seconds >= 12:
                if (now - last_live_check_time) >= 8:
                    last_live_check_time = now
                    if target_user:
                        try:
                            is_still_live, _ = check_live_status(target_user)
                            if not is_still_live:
                                print(f"\n\n[✓] [@{target_user}] Streamer ĐÃ XUỐNG LIVE (Không còn tín hiệu sau {stagnant_seconds}s).")
                                print(f"[*] [@{target_user}] Đang chốt file video MP4 và lưu trữ ngay lập tức...")
                                if proc.stdin:
                                    try:
                                        proc.stdin.write(b"q\n")
                                        proc.stdin.flush()
                                    except Exception:
                                        pass
                                try:
                                    proc.wait(timeout=4)
                                except Exception:
                                    proc.terminate()
                                break
                        except Exception:
                            pass

            # Nếu 30 giây liên tiếp không nhận được bất kỳ byte nào -> Buộc kết thúc để đóng gói video
            if size_bytes > 1024 and stagnant_seconds >= 30:
                print(f"\n\n[!] [@{target_user}] Tín hiệu live ngắt quãng {stagnant_seconds}s. Tự động chốt video...")
                if proc.stdin:
                    try:
                        proc.stdin.write(b"q\n")
                        proc.stdin.flush()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=4)
                except Exception:
                    proc.terminate()
                break

            sys.stdout.write(f"\r🔴 Đang ghi hình: [{hours:02d}:{mins:02d}:{secs:02d}] - Dung lượng: {size_mb:.2f} MB")
            sys.stdout.flush()

        proc.wait()
    except KeyboardInterrupt:
        print("\n\n[!] Nhận lệnh dừng từ người dùng. Đang đóng gói file video...")
        try:
            if proc.stdin:
                proc.stdin.write(b"q\n")
                proc.stdin.flush()
            proc.wait(timeout=4)
        except Exception:
            proc.terminate()

    if os.path.exists(output_filename) and os.path.getsize(output_filename) > 1024:
        final_size = os.path.getsize(output_filename) / (1024 * 1024)
        print(f"\n[✓] Ghi hình thành công! File đã lưu tại:")
        print(f"    --> {output_filename} ({final_size:.2f} MB)\n")
        
        # Tự động kiểm tra và chuyển sang H.264 nếu cần thiết
        try:
            from auto_h264 import ensure_h264
            ensure_h264(output_filename)
        except Exception as e:
            print(f"[!] Lỗi khi tự động kiểm tra định dạng H.264: {e}")

        # Tự động gửi thông báo Telegram & đồng bộ Google Drive
        try:
            from notifier import send_telegram, sync_to_gdrive
            final_mb = os.path.getsize(output_filename) / (1024 * 1024) if os.path.exists(output_filename) else 0
            fname = os.path.basename(output_filename)
            send_telegram(
                f"✅ <b>Đã lưu thành công phiên Live!</b>\n"
                f"👤 Streamer: <code>@{target_user}</code>\n"
                f"📁 File: <code>{fname}</code>\n"
                f"💾 Dung lượng: <b>{final_mb:.2f} MB</b> (Chuẩn H.264)"
            )
            sync_to_gdrive(output_filename, target_user)
        except Exception as e:
            print(f"[!] Lỗi khi gửi thông báo/đồng bộ đám mây: {e}")

        return output_filename
    else:
        print("\n[!] Stream ngắt hoặc không nhận được dữ liệu hợp lệ.")
        if os.path.exists(output_filename) and os.path.getsize(output_filename) <= 1024:
            try:
                os.remove(output_filename)
            except Exception:
                pass
        return None


