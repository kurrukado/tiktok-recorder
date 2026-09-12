import os
import sys
import json
import time
import re
import shutil
import subprocess
import random
from datetime import datetime
from typing import Optional, Tuple, Dict, Any
from curl_cffi import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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


def generate_guest_session(proxy: Optional[str] = None) -> requests.Session:
    """
    Tạo phiên khách vô danh mới với browser fingerprint ngẫu nhiên để lấy cookie khách sạch sẽ,
    vượt qua cơ chế giới hạn IP/phiên của TikTok khi xem luồng preview Sub-Only.
    """
    impersonates = ["chrome136", "chrome131", "chrome124", "safari17_0", "edge101"]
    chosen_browser = random.choice(impersonates)
    session_kwargs = {"impersonate": chosen_browser}
    if proxy:
        session_kwargs["proxies"] = {"http": proxy, "https": proxy}
    session = requests.Session(**session_kwargs)
    try:
        chrome_vers = ["126.0.6478.127", "128.0.6613.85", "131.0.6778.86", "133.0.6943.53", "136.0.7024.12"]
        ver = random.choice(chrome_vers)

        tt_chain_token = "".join(random.choices("0123456789abcdef", k=32))
        session.cookies.set("tt_chain_token", tt_chain_token, domain=".tiktok.com")
        session.cookies.set("odin_tt", "".join(random.choices("0123456789abcdef", k=64)), domain=".tiktok.com")
        session.cookies.set("msToken", "".join(random.choices("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_", k=128)), domain=".tiktok.com")

        session.headers.update({
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{ver} Safari/537.36",
            "Accept-Language": random.choice(["vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9", "ja-JP,ja;q=0.8"]),
            "Referer": "https://www.tiktok.com/",
            "Accept": "*/*",
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        })
        return session
    except Exception:
        try:
            session.close()
        except Exception:
            pass
        raise

def check_live_details(user: str, cookies: Optional[dict] = None, proxy: Optional[str] = None) -> dict:
    """
    Trích xuất thông tin chi tiết phiên live:
    - is_live: bool
    - room_id: Optional[str]
    - is_sub_only: bool (Phòng live dành riêng cho hội viên VIP có trả phí)
    - is_preview: bool (Đang xem luồng thử nghiệm preview)
    - paid_type: Optional[int]
    - preview_duration: Optional[int]
    Trích xuất từ SIGI_STATE (liveSubOnly, paidEvent) và Webcast API (sub_only, live_sub_only, paid_event).
    """
    user = user.strip().replace("@", "").lower()
    details = {
        "is_live": False,
        "room_id": None,
        "is_sub_only": False,
        "is_preview": False,
        "paid_type": None,
        "preview_duration": None
    }
    if not user:
        return details

    # 0. Phương thức Ưu Tiên Số 1: TikTok Native Live API với TLS impersonation
    try:
        from curl_cffi import requests as c_req
        api_url = f"https://www.tiktok.com/api-live/user/room/?aid=1988&app_language=en&app_name=tiktok_web&device_platform=web_pc&uniqueId={user}&sourceType=54"
        for imp in ["safari15_5", "chrome136"]:
            try:
                api_res = c_req.get(api_url, impersonate=imp, timeout=6)
                if api_res.status_code == 200:
                    api_json = api_res.json()
                    if isinstance(api_json, dict):
                        data = api_json.get("data")
                        data = data if isinstance(data, dict) else {}
                        live_room = data.get("liveRoom")
                        live_room = live_room if isinstance(live_room, dict) else {}
                        user_data = data.get("user")
                        user_data = user_data if isinstance(user_data, dict) else {}
                        status = live_room.get("status")
                        room_id = user_data.get("roomId") or live_room.get("roomId")

                        if live_room.get("liveSubOnly") or live_room.get("subOnly"):
                            details["is_sub_only"] = True
                            details["is_preview"] = True

                        if status == 2 and room_id:
                            details["is_live"] = True
                            details["room_id"] = str(room_id)
                            return details
                        elif status == 4:
                            details["is_live"] = False
                            details["room_id"] = None
                            return details
            except Exception:
                continue
    except Exception:
        pass

    session = None
    try:
        if cookies is None:
            cookies = load_cookies()
        session_kwargs = {"impersonate": "chrome136"}
        if proxy:
            session_kwargs["proxies"] = {"http": proxy, "https": proxy}
        session = requests.Session(**session_kwargs)
        if cookies:
            session.cookies.update(cookies)

        url = f"https://www.tiktok.com/@{user}/live"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Referer": "https://www.tiktok.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        res = session.get(url, headers=headers, timeout=7)
        if res.status_code == 200:
            res_text = res.text
            # Guard: Giới hạn xử lý tối đa 4MB để tránh regex backtracking / phình RAM khi gặp response lỗi
            if len(res_text) <= 4 * 1024 * 1024:
                # 1. Phân tích thẻ SIGI_STATE
                m = re.search(r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', res_text, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(1))
                        if isinstance(data, dict):
                            live_user_info = data.get("LiveRoom", {})
                            live_user_info = live_user_info.get("liveRoomUserInfo", {}) if isinstance(live_user_info, dict) else {}
                            room_info = live_user_info.get("liveRoom", {}) if isinstance(live_user_info, dict) else {}
                            user_info = live_user_info.get("user", {}) if isinstance(live_user_info, dict) else {}
                            status = room_info.get("status") if isinstance(room_info, dict) else None
                            room_id = (room_info.get("roomId") or user_info.get("roomId")) if (isinstance(room_info, dict) and isinstance(user_info, dict)) else None

                            # Kiểm tra các cờ VIP Sub-Only / Paid Event
                            live_sub_only = room_info.get("liveSubOnly", False) if isinstance(room_info, dict) else False
                            sub_only = room_info.get("subOnly", False) if isinstance(room_info, dict) else False
                            paid_evt = room_info.get("paidEvent") if isinstance(room_info, dict) else {}
                            p_type = paid_evt.get("paidType") if isinstance(paid_evt, dict) else None
                            p_dur = (room_info.get("previewDuration") or room_info.get("preview_duration")) if isinstance(room_info, dict) else None

                            if live_sub_only or sub_only or (isinstance(paid_evt, dict) and paid_evt.get("paidType", 0) > 0) or (paid_evt is True):
                                details["is_sub_only"] = True
                                details["is_preview"] = True
                            if p_type is not None:
                                details["paid_type"] = p_type
                            if p_dur is not None:
                                details["preview_duration"] = int(p_dur)

                            # status == 2 nghĩa là ĐANG LIVE, status == 4 nghĩa là ĐÃ XUỐNG LIVE
                            if status == 2:
                                if not room_id:
                                    r_match = re.search(r'"roomId"[:\"]+(\d{15,25})', res_text)
                                    if r_match:
                                        room_id = r_match.group(1)
                                if room_id:
                                    details["is_live"] = True
                                    details["room_id"] = str(room_id)
                            elif status == 4:
                                details["is_live"] = False
                                details["room_id"] = None
                                return details
                    except Exception:
                        pass

                # Regex kiểm tra nhanh toàn bộ HTML
                if re.search(r'"liveSubOnly"\s*:\s*(true|1)', res_text, re.IGNORECASE) or \
                   re.search(r'"subOnly"\s*:\s*(true|1)', res_text, re.IGNORECASE) or \
                   re.search(r'"is_sub_only"\s*:\s*(true|1)', res_text, re.IGNORECASE):
                    details["is_sub_only"] = True
                    details["is_preview"] = True

                p_match = re.search(r'"paidEvent"\s*:\s*\{[^}]*"paidType"\s*:\s*([1-9]\d*)', res_text)
                if p_match:
                    details["is_sub_only"] = True
                    details["is_preview"] = True
                    try:
                        details["paid_type"] = int(p_match.group(1))
                    except Exception:
                        pass

                # Kiểm tra text đặc trưng nếu streamer đã tắt live
                m_stat = re.search(r'"uniqueId":"' + re.escape(user) + r'"[^\}]*?"status":\s*(\d+)', res_text)
                if m_stat and int(m_stat.group(1)) == 4:
                    details["is_live"] = False
                    details["room_id"] = None
                    return details

                if not details["is_live"]:
                    r_match = re.search(r'"roomId"[:\"]+(\d{15,25})', res_text)
                    if r_match and ('"status":2' in res_text or 'liveRoomUserInfo' in res_text):
                        details["is_live"] = True
                        details["room_id"] = r_match.group(1)

        # 2. Bổ sung trích xuất qua Webcast API nếu đã phát hiện live
        if details["is_live"] and details["room_id"]:
            try:
                w_url = f"https://webcast.tiktok.com/webcast/room/info/?aid=1988&room_id={details['room_id']}"
                w_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    "Referer": "https://www.tiktok.com/",
                    "Accept": "*/*",
                    "Origin": "https://www.tiktok.com",
                }
                w_res = session.get(w_url, headers=w_headers, timeout=4)
                if w_res.status_code == 200:
                    try:
                        w_json = w_res.json()
                    except Exception:
                        w_json = {}
                    if isinstance(w_json, dict):
                        r_data = w_json.get("data")
                        r_data = r_data if isinstance(r_data, dict) else {}
                        sub_flag = r_data.get("sub_only") or r_data.get("live_sub_only")
                        paid_data = r_data.get("paid_event")
                        paid_data = paid_data if isinstance(paid_data, dict) else {}
                        p_type = paid_data.get("paid_type") if isinstance(paid_data, dict) else None

                        if sub_flag or (isinstance(paid_data, dict) and paid_data.get("paid_type", 0) > 0):
                            details["is_sub_only"] = True
                            details["is_preview"] = True
                        if p_type is not None:
                            details["paid_type"] = p_type
            except Exception:
                pass

    except Exception:
        pass
    finally:
        if session:
            try:
                session.close()
            except Exception:
                pass

    # 3. Dự phòng qua TikTokLiveClient — ĐÃ LOẠI BỎ HOÀN TOÀN
    # TikTokLiveClient luôn thất bại trên IP datacenter (Render/GitHub Actions) do TikTok chặn httpx,
    # đồng thời import nặng (httpx, pydantic, asyncio.run) gây phình RAM 30-50MB mỗi lần gọi.
    # Native Live API (Step 0) + HTML scrape (Step 1) đã đủ chính xác 100%.

    return details

def check_live_status(user: str) -> Tuple[bool, Optional[str]]:
    """
    Kiểm tra trạng thái live và lấy Room ID của streamer chuẩn xác 100% bằng curl_cffi Chrome 136.
    Trích xuất từ thẻ script SIGI_STATE của chính streamer:
    - status == 2: Streamer ĐANG LIVE.
    - status == 4: Streamer ĐÃ XUỐNG LIVE (Offline). Tránh nhận nhầm các phòng live gợi ý khác!
    Returns (is_live: bool, room_id: str or None)
    """
    det = check_live_details(user)
    return det["is_live"], det["room_id"]

check_user_live = check_live_status

def get_live_stream_url(room_id, user=None, cookies=None, session=None, proxy=None):
    try:
        urls = get_stream_urls(room_id, user, cookies=cookies, session=session, proxy=proxy)
        if isinstance(urls, list) and urls:
            return urls[0]
        return None
    except Exception:
        return None

def classify_stream_urls(raw_urls: list) -> list:
    """
    Sắp xếp các link stream theo thứ tự ưu tiên độ phân giải nghiêm ngặt:
    Tier 1 (1080p Full HD): link có _or4, full_hd1, _uhd, 1080p, 1080, fhd hoặc luồng origin gốc không bị nén hạ cấp.
    Tier 2 (720p HD): _hd, hd1, 720p, 720.
    Tier 3 (540p/SD): _sd, sd1, sd2, 540p, 480p.
    Tier 4 (360p/LD): _ld, 360p.
    Loại bỏ hoàn toàn luồng chỉ có tiếng (only_audio=1, stream_suffix=ao).
    """
    if not raw_urls:
        return []
    clean = [u.replace('&amp;', '&') for u in raw_urls if "only_audio=1" not in u and "stream_suffix=ao" not in u]
    p1080_explicit, p1080_origin, p720, psd, pld, other = [], [], [], [], [], []
    for u in clean:
        u_lower = u.lower()
        if any(s in u_lower for s in ("_or4", "full_hd1", "_uhd", "1080p", "1080", "fhd", "_fhd")):
            p1080_explicit.append(u)
        elif re.search(r'stream-\d+\.(flv|m3u8)', u_lower) or any(s in u_lower for s in ("origin", "_origin")):
            p1080_origin.append(u)
        elif any(s in u_lower for s in ("_hd", "hd1", "720p", "720")):
            p720.append(u)
        elif any(s in u_lower for s in ("_sd", "sd1", "sd2", "540p", "480p")):
            psd.append(u)
        elif any(s in u_lower for s in ("_ld", "360p")):
            pld.append(u)
        else:
            other.append(u)
    return p1080_explicit + p1080_origin + p720 + psd + pld + other

def parse_sdk_stream_data(sdk_data_str: str) -> list:
    """
    Phân tích chuỗi JSON stream_data của TikTok Live SDK và ưu tiên cố định 1080p Full HD:
    1. 1080p H.264 (Copy luồng nguyên bản 0% CPU, mượt mà chuẩn tương thích)
    2. 1080p Codec khác (HEVC/ByteVC1)
    3. 720p H.264
    4. 720p Codec khác
    5. SD (540p/480p)
    6. LD (360p)
    Không bao giờ để 720p hoặc 360p vượt lên trên 1080p!
    """
    candidates = []
    if not sdk_data_str:
        return candidates
    try:
        sdk_json = json.loads(sdk_data_str).get("data", {})
        if not isinstance(sdk_json, dict):
            return candidates

        tier_1080_h264, tier_1080_other = [], []
        tier_720_h264, tier_720_other = [], []
        tier_sd_h264, tier_sd_other = [], []
        tier_ld_h264, tier_ld_other = [], []
        tier_other = []

        for key, entry in sdk_json.items():
            if key == "ao":
                continue
            stream_main = entry.get("main", {}) if isinstance(entry, dict) else {}
            flv = stream_main.get("flv")
            hls = stream_main.get("hls") or stream_main.get("m3u8")
            sdk_p = stream_main.get("sdk_params", "")
            p_str = str(sdk_p).lower()

            if "only_audio=1" in p_str or "stream_suffix\":\"ao\"" in p_str:
                continue

            is_h264 = "h264" in p_str or "avc" in p_str

            # Phân loại độ phân giải chính xác
            is_1080 = any(s in p_str for s in ("1080", "or4", "uhd", "fhd", "origin")) or key in ("origin", "uhd")
            is_720 = "720" in p_str or key == "hd" or "stream_suffix\":\"hd\"" in p_str
            is_sd = any(s in p_str for s in ("540", "480", "sd")) or key == "sd"
            is_ld = "360" in p_str or key == "ld" or "stream_suffix\":\"ld\"" in p_str

            if is_1080 and not ("640x1280" in p_str or "720" in p_str or is_sd or is_ld):
                t_flv, t_hls = (tier_1080_h264, tier_1080_h264) if is_h264 else (tier_1080_other, tier_1080_other)
            elif is_720:
                t_flv, t_hls = (tier_720_h264, tier_720_h264) if is_h264 else (tier_720_other, tier_720_other)
            elif is_sd:
                t_flv, t_hls = (tier_sd_h264, tier_sd_h264) if is_h264 else (tier_sd_other, tier_sd_other)
            elif is_ld:
                t_flv, t_hls = (tier_ld_h264, tier_ld_h264) if is_h264 else (tier_ld_other, tier_ld_other)
            else:
                t_flv, t_hls = tier_other, tier_other

            # Luôn ưu tiên FLV trước HLS để tránh adaptive switching giật độ phân giải
            if flv and flv not in t_flv and flv not in candidates:
                t_flv.append(flv)
            if hls and hls not in t_hls and hls not in candidates:
                t_hls.append(hls)

        ordered = (
            tier_1080_h264 + tier_1080_other +
            tier_720_h264 + tier_720_other +
            tier_sd_h264 + tier_sd_other +
            tier_ld_h264 + tier_ld_other +
            tier_other
        )
        for u in ordered:
            if u not in candidates:
                candidates.append(u)
    except Exception:
        pass
    return candidates

def get_stream_urls(room_id, user, cookies=None, session=None, proxy=None):
    """
    Extract candidate stream URLs (FLV or HLS / m3u8).
    Supports 18+ restricted streams using authenticated sessionid cookies or custom guest session.
    """
    # 0. Phương thức Ưu Tiên Số 1: TikTok Native Live API với TLS impersonation
    if user:
        try:
            from curl_cffi import requests as c_req
            api_url = f"https://www.tiktok.com/api-live/user/room/?aid=1988&app_language=en&app_name=tiktok_web&device_platform=web_pc&uniqueId={user}&sourceType=54"
            for imp in ["safari15_5", "chrome136"]:
                try:
                    api_res = c_req.get(api_url, impersonate=imp, timeout=7)
                    if api_res.status_code == 200:
                        d = api_res.json().get("data", {}).get("liveRoom", {})
                        sd_str = d.get("streamData", {}).get("pull_data", {}).get("stream_data")
                        if sd_str:
                            c_urls = parse_sdk_stream_data(sd_str)
                            if c_urls:
                                return c_urls
                except Exception:
                    continue
        except Exception:
            pass

    if not room_id and not user:
        return []

    owns_session = False
    session_to_close = None
    if session is None:
        owns_session = True

    try:
        if owns_session:
            if cookies is None:
                cookies = load_cookies()
            session_kwargs = {"impersonate": "chrome136"}
            if proxy:
                session_kwargs["proxies"] = {"http": proxy, "https": proxy}
            session = requests.Session(**session_kwargs)
            session_to_close = session
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
                sc = getattr(page_res, "status_code", 200)
                if sc == 200 or not isinstance(sc, (int, float)):
                    raw_text = page_res.text
                    # Guard: Giới hạn xử lý tối đa 3MB để tránh duplicate string nhiều lần làm phình RAM
                    if len(raw_text) <= 3 * 1024 * 1024:
                        # 1. Trích xuất trực tiếp từ thẻ SIGI_STATE (chứa luồng gốc 1080p chuẩn xác nhất)
                        m_sigi = re.search(r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', raw_text, re.DOTALL)
                        if m_sigi:
                            try:
                                sigi_d = json.loads(m_sigi.group(1))
                                lr = sigi_d.get("LiveRoom", {}).get("liveRoomUserInfo", {}).get("liveRoom", {})
                                sd = lr.get("streamData", {})
                                if isinstance(sd, dict):
                                    p_data = sd.get("pull_data", {})
                                    if isinstance(p_data, dict):
                                        sd_str = p_data.get("stream_data")
                                        if sd_str:
                                            sigi_urls = parse_sdk_stream_data(sd_str)
                                            if sigi_urls:
                                                return sigi_urls
                                    flv_p = sd.get("flv_pull_url")
                                    if isinstance(flv_p, dict) and "FULL_HD1" in flv_p:
                                        return [flv_p["FULL_HD1"]]
                                h_sd = lr.get("hevcStreamData", {})
                                if isinstance(h_sd, dict):
                                    hp_data = h_sd.get("pull_data", {})
                                    if isinstance(hp_data, dict):
                                        hsd_str = hp_data.get("stream_data")
                                        if hsd_str:
                                            sigi_urls = parse_sdk_stream_data(hsd_str)
                                            if sigi_urls:
                                                return sigi_urls
                            except Exception:
                                pass

                        content = raw_text.replace('\\"', '"').replace('\\u0026', '&').replace('&amp;', '&').replace('\\/', '/')
                        
                        flv_matches = re.findall(r'https?://[^\s"\'<>]+\.flv\?[^\s"\'<>]+', content)
                        if flv_matches:
                            sorted_flv = classify_stream_urls(flv_matches)
                            if sorted_flv:
                                return sorted_flv

                        hls_matches = re.findall(r'https?://[^\s"\'<>]+\.m3u8\?[^\s"\'<>]*', content)
                        if hls_matches:
                            sorted_hls = classify_stream_urls(hls_matches)
                            if sorted_hls:
                                return sorted_hls
            except Exception:
                pass

        candidates = []
        stream_url_obj = {}

        # Second attempt: Webcast room/info API (chỉ gọi khi có room_id)
        if room_id:
            url = f"https://webcast.tiktok.com/webcast/room/info/?aid=1988&room_id={room_id}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.127 Safari/537.36",
                "Referer": "https://www.tiktok.com/",
                "Accept": "*/*",
                "Origin": "https://www.tiktok.com",
            }
            res = session.get(url, headers=headers, timeout=10)
            try:
                data = res.json()
            except Exception:
                data = {}

            status_code = data.get("status_code", -1) if isinstance(data, dict) else -1
            if status_code == 4003110:
                return "AGE_RESTRICTED"

            room_data = data.get("data") if isinstance(data, dict) else {}
            room_data = room_data if isinstance(room_data, dict) else {}
            stream_url_obj = room_data.get("stream_url") if isinstance(room_data, dict) else {}
            stream_url_obj = stream_url_obj if isinstance(stream_url_obj, dict) else {}

            live_sdk = stream_url_obj.get("live_core_sdk_data")
            live_sdk = live_sdk if isinstance(live_sdk, dict) else {}
            pull_data = live_sdk.get("pull_data")
            pull_data = pull_data if isinstance(pull_data, dict) else {}
            sdk_data_str = pull_data.get("stream_data")
            if sdk_data_str and isinstance(sdk_data_str, str):
                candidates.extend(parse_sdk_stream_data(sdk_data_str))

            # Fallback to direct URLs: FULL_HD1 (1080p) phải là ưu tiên số 1!
            flv_pull = stream_url_obj.get("flv_pull_url") or {}
            if isinstance(flv_pull, dict):
                full_hd = flv_pull.get("FULL_HD1")
                if full_hd:
                    if full_hd in candidates:
                        candidates.remove(full_hd)
                    candidates.insert(0, full_hd)
                for k in ("HD1", "SD2", "SD1"):
                    u = flv_pull.get(k)
                    if u and u not in candidates:
                        candidates.append(u)

            hls_pull = stream_url_obj.get("hls_pull_url")
            if isinstance(hls_pull, str) and hls_pull and hls_pull not in candidates:
                candidates.append(hls_pull)
            elif isinstance(hls_pull, dict):
                hls_fhd = hls_pull.get("FULL_HD1")
                if hls_fhd and hls_fhd not in candidates:
                    candidates.append(hls_fhd)
                for k in ("HD1", "SD2", "SD1"):
                    u = hls_pull.get(k)
                    if u and u not in candidates:
                        candidates.append(u)

        return candidates
    except Exception:
        return []
    finally:
        if session_to_close:
            try:
                session_to_close.close()
            except Exception:
                pass

def _safe_stop_ffmpeg(proc, timeout=8):
    """
    Dừng tiến trình ffmpeg an toàn:
    1. Gửi 'q\n' qua stdin và đóng stdin để ffmpeg chốt moov atom chuẩn MP4.
    2. Chờ tiến trình kết thúc trong timeout giây.
    3. Nếu chưa kết thúc, gọi terminate() rồi kill() để không bị treo vĩnh viễn.
    """
    if not proc or proc.poll() is not None:
        return
    if proc.stdin:
        try:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=timeout)
        return
    except Exception:
        pass

    try:
        proc.terminate()
        proc.wait(timeout=4)
        return
    except Exception:
        pass

    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass

def record_stream_ffmpeg(stream_url, output_filename=None, target_user="islizanx", duration=None, stop_event=None, auto_sync_gdrive=True, is_sub_only: bool = False):
    """
    Records a live stream URL to an MP4 file using ffmpeg without re-encoding,
    then automatically ensures it is in standard H.264 (AVC) format.
    """
    if not os.path.exists(FFMPEG_PATH) and not shutil.which(FFMPEG_PATH):
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
        "-rw_timeout", "60000000",
        "-reconnect", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "15",
        "-fflags", "+genpts+discardcorrupt",
        "-analyzeduration", "10000000",
        "-probesize", "10000000",
        "-headers", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36\r\n"
            "Referer: https://www.tiktok.com/\r\n"
        ),
        "-i", stream_url,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "copy",
        "-c:a", "copy",
        "-sn",
        "-dn",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
    ]
    if duration:
        cmd.extend(["-t", str(duration)])
    cmd.append(output_filename)

    start_time = time.time()
    proc = None
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
        consecutive_offline_checks = 0

        while proc.poll() is None:
            if stop_event and stop_event.is_set():
                print(f"\n[!] Nhận tín hiệu dừng từ hệ thống. Đang đóng gói file video cho @{target_user}...")
                _safe_stop_ffmpeg(proc, timeout=8)
                break

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

            # Tự động chốt phân đoạn khi đạt thời lượng tối đa (mặc định 1 tiếng = 3600s)
            if duration and elapsed >= duration:
                h_desc = f"{duration // 3600} tiếng" if duration >= 3600 else f"{duration}s"
                print(f"\n\n[⏱️ Tối đa {h_desc}] [@{target_user}] Đã đạt thời lượng phân đoạn ({hours:02d}:{mins:02d}:{secs:02d}). Đang chốt file để tải lên Cloud...")
                _safe_stop_ffmpeg(proc, timeout=8)
                break

            # Quick Watchdog cho VIP Sub-Only: khi dung lượng không tăng trong >= 6 giây, lập tức chốt file preview nhanh chóng
            if is_sub_only and size_bytes >= 250 * 1024 and stagnant_seconds >= 6:
                print(f"\n\n[⚡ VIP Preview] [@{target_user}] Luồng preview Sub-Only dừng truyền tải sau {stagnant_seconds}s ({size_mb:.2f} MB). Đang chốt file preview nhanh chóng...")
                _safe_stop_ffmpeg(proc, timeout=4)
                break

            # Kiểm tra trạng thái streamer khi dữ liệu chững lại (chỉ với luồng thường, không phải VIP Sub-Only)
            if not is_sub_only and size_bytes >= 250 * 1024 and stagnant_seconds >= 20:
                if (now - last_live_check_time) >= 10:
                    last_live_check_time = now
                    if target_user:
                        try:
                            is_still_live, _ = check_live_status(target_user)
                            if not is_still_live:
                                consecutive_offline_checks += 1
                                if consecutive_offline_checks >= 3:
                                    print(f"\n\n[✓] [@{target_user}] Streamer ĐÃ XUỐNG LIVE (Xác nhận 3 lần liên tiếp không còn tín hiệu sau {stagnant_seconds}s).")
                                    print(f"[*] [@{target_user}] Đang chốt file video MP4 và lưu trữ...")
                                    _safe_stop_ffmpeg(proc, timeout=8)
                                    break
                            else:
                                consecutive_offline_checks = 0
                        except Exception:
                            pass

            # Nếu dữ liệu ngắt quãng: tối đa 30s nếu file chưa đủ 250 KB, tối đa 90s nếu đang ghi dở
            max_stagnant = 30 if size_bytes < 250 * 1024 else 90
            if stagnant_seconds >= max_stagnant:
                offline_confirmed = False
                if target_user:
                    try:
                        st_check, _ = check_live_status(target_user)
                        offline_confirmed = not st_check
                    except Exception:
                        offline_confirmed = True
                if offline_confirmed or stagnant_seconds >= 120:
                    print(f"\n\n[!] [@{target_user}] Tín hiệu live ngắt quãng {stagnant_seconds}s. Tự động chốt phân đoạn...")
                    _safe_stop_ffmpeg(proc, timeout=6)
                    break

            sys.stdout.write(f"\r🔴 Đang ghi hình: [{hours:02d}:{mins:02d}:{secs:02d}] - Dung lượng: {size_mb:.2f} MB")
            sys.stdout.flush()

    except KeyboardInterrupt:
        print("\n\n[!] Nhận lệnh dừng từ người dùng. Đang đóng gói file video...")
    except Exception as e:
        print(f"\n[!] Lỗi trong tiến trình ghi hình: {e}")
    finally:
        if proc:
            _safe_stop_ffmpeg(proc, timeout=6)

    if os.path.exists(output_filename):
        from auto_h264 import validate_playable_video
        is_valid, reason, dur = validate_playable_video(output_filename, min_duration=5.0, min_size_bytes=250000)
        if is_valid:
            final_size = os.path.getsize(output_filename) / (1024 * 1024)
            print(f"\n[✓] Ghi hình thành công! File đạt chuẩn ({dur:.1f}s, {final_size:.2f} MB):")
            print(f"    --> {output_filename}\n")
            
            # Tự động kiểm tra và chuyển sang H.264 nếu cần thiết
            try:
                from auto_h264 import ensure_h264
                output_filename = ensure_h264(output_filename)
            except Exception as e:
                print(f"[!] Lỗi khi tự động kiểm tra định dạng H.264: {e}")

            # Validate lại sau khi chuyển đổi định dạng
            is_valid_after, reason_after, _ = validate_playable_video(output_filename, min_duration=5.0, min_size_bytes=250000)
            if not is_valid_after:
                print(f"[!] File sau khi chuyển mã không đạt chuẩn ({reason_after}). Hủy file lỗi.")
                if os.path.exists(output_filename):
                    try:
                        os.remove(output_filename)
                    except Exception:
                        pass
                return None

            # Tự động gửi thông báo Telegram & đồng bộ Google Drive nếu được yêu cầu
            if auto_sync_gdrive:
                try:
                    from notifier import send_telegram, sync_to_gdrive
                    final_mb = os.path.getsize(output_filename) / (1024 * 1024) if os.path.exists(output_filename) else 0
                    fname = os.path.basename(output_filename)
                    send_telegram(
                        f"✅ <b>Đã lưu thành công phiên Live!</b>\n"
                        f"👤 Streamer: <code>@{target_user}</code>\n"
                        f"📁 File: <code>{fname}</code>\n"
                        f"💾 Dung lượng: <b>{final_mb:.2f} MB</b> ({dur:.0f}s, Chuẩn H.264)"
                    )
                    sync_to_gdrive(output_filename, target_user)
                except Exception as e:
                    print(f"[!] Lỗi khi gửi thông báo/đồng bộ đám mây: {e}")

            return output_filename
        else:
            print(f"\n[!] Video ghi hình không đạt chuẩn ({reason}). Tự động hủy file lỗi để tránh rác ổ cứng và ngăn gửi video 0:00s.")
            try:
                os.remove(output_filename)
            except Exception:
                pass
            return None
    else:
        print("\n[!] Stream ngắt hoặc không nhận được dữ liệu hợp lệ.")
        return None


