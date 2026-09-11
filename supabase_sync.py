import os
import re
import json
import time
from datetime import datetime
from typing import Optional, Union, Tuple
import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://jetwtqakyxjcffbwhhot.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_KEY", "sb_publishable_GBb8NqVZfF7_jeyzKvRbiA_yKF4KQ76")
STORAGE_BUCKET = "covers"

def get_supabase_headers(content_type: str = "application/json"):
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type
    }

def fetch_streamers_from_supabase() -> list:
    """Đọc danh sách streamer đang theo dõi trực tiếp từ table 'tiktok_streamers'."""
    try:
        headers = get_supabase_headers()
        url = f"{SUPABASE_URL}/rest/v1/tiktok_streamers?select=username&order=id.asc"
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list):
                return [d["username"].strip().replace("@", "").lower() for d in data if "username" in d and d["username"]]
    except Exception as e:
        print(f"[SUPABASE-SYNC] [!] Lỗi nạp streamers từ Supabase: {e}")
    return []

def clean_filename(filename: str) -> str:
    """Loại bỏ phần mở rộng file để lấy base name sạch sẽ"""
    base = os.path.basename(filename)
    clean = re.sub(r'\.(mp4|jpg|jpeg|png|webp)$', '', base, flags=re.IGNORECASE)
    return clean

def upload_thumbnail_to_supabase(
    thumb_source: Union[str, bytes],
    user: str,
    filename: str
) -> Optional[str]:
    """
    Tải ảnh thumbnail lên Supabase Storage bucket 'covers/record-thumbnails/{user}/{clean_name}.jpg'.
    Hỗ trợ thumb_source là:
    - Đường dẫn file cục bộ (str)
    - Dữ liệu bytes ảnh (bytes)
    - URL ảnh tải từ web/drive (str bắt đầu bằng http)
    Trả về URL công khai Supabase CDN nếu thành công.
    """
    try:
        user = user.strip().replace("@", "").lower()
        base_name = clean_filename(filename)
        storage_path = f"record-thumbnails/{user}/{base_name}.jpg"
        target_upload_url = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{storage_path}"

        img_bytes = None

        if isinstance(thumb_source, bytes):
            img_bytes = thumb_source
        elif isinstance(thumb_source, str):
            if thumb_source.startswith("http://") or thumb_source.startswith("https://"):
                resp = requests.get(thumb_source, timeout=15)
                if resp.status_code == 200 and len(resp.content) > 200:
                    img_bytes = resp.content
            elif os.path.exists(thumb_source) and os.path.getsize(thumb_source) > 200:
                with open(thumb_source, "rb") as f:
                    img_bytes = f.read()

        if not img_bytes or len(img_bytes) < 200:
            print(f"[SUPABASE-SYNC] [!] Không thể đọc dữ liệu ảnh thumbnail cho {user}/{filename}")
            return None

        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "image/jpeg",
            "x-upsert": "true"
        }

        res = requests.post(target_upload_url, headers=headers, data=img_bytes, timeout=20)
        if res.status_code in (200, 201):
            public_url = f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{storage_path}"
            print(f"[SUPABASE-SYNC] [✓] Đã tải thumbnail lên Supabase Storage: {storage_path}")
            return public_url
        else:
            print(f"[SUPABASE-SYNC] [!] Lỗi upload Supabase Storage ({res.status_code}): {res.text}")
            return None
    except Exception as e:
        print(f"[SUPABASE-SYNC] [!] Ngoại lệ khi upload thumbnail lên Supabase: {e}")
        return None

def sync_recording_to_supabase(
    user: str,
    filename: str,
    size_bytes: int = 0,
    recorded_at: Optional[str] = None,
    created_at: Optional[str] = None,
    thumb_source: Optional[Union[str, bytes]] = None,
    existing_thumb_url: Optional[str] = None,
    download_url: Optional[str] = None,
    cdn_download_url: Optional[str] = None,
    drive_file_id: Optional[str] = None,
    drive_thumb_id: Optional[str] = None,
    source: str = "cloud_daemon"
) -> bool:
    """
    Đồng bộ hoàn chỉnh 1 bản ghi video vào Supabase Storage (ảnh) và PostgREST table 'tiktok_recordings'.
    """
    try:
        user = user.strip().replace("@", "").lower()
        fname = os.path.basename(filename)
        if not fname.endswith(".mp4"):
            fname = f"{fname}.mp4"

        # Từ chối lưu các video rác lỗi 0:00s hoặc 0-byte (< 250 KB)
        if not size_bytes or int(size_bytes) < 250 * 1024:
            print(f"[SUPABASE-SYNC] [!] Từ chối đồng bộ video lỗi dung lượng nhỏ ({size_bytes} bytes): {fname}")
            return False

        if not recorded_at:
            m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})", fname)
            if m:
                d_str, t_str = m.groups()
                recorded_at = f"{d_str} {t_str.replace('-', ':')}"
            else:
                recorded_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        final_thumb_url = existing_thumb_url
        if thumb_source:
            uploaded_url = upload_thumbnail_to_supabase(thumb_source, user, fname)
            if uploaded_url:
                final_thumb_url = uploaded_url

        if not final_thumb_url:
            base_name = clean_filename(fname)
            final_thumb_url = f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/record-thumbnails/{user}/{base_name}.jpg"

        if not download_url:
            download_url = f"/api/download/{user}/{fname}"

        if not cdn_download_url and drive_file_id:
            cdn_download_url = f"https://drive.usercontent.google.com/download?id={drive_file_id}&export=download&authuser=0"

        size_mb = round(float(size_bytes) / (1024 * 1024), 2) if size_bytes else 0.0

        row_data = {
            "filename": fname,
            "username": user,
            "size_bytes": int(size_bytes),
            "size_mb": size_mb,
            "recorded_at": recorded_at,
            "created_at": created_at or datetime.utcnow().isoformat(),
            "thumbnail_url": final_thumb_url,
            "download_url": download_url,
            "cdn_download_url": cdn_download_url,
            "drive_file_id": drive_file_id,
            "drive_thumb_id": drive_thumb_id,
            "source": source
        }

        rest_url = f"{SUPABASE_URL}/rest/v1/tiktok_recordings"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=representation"
        }

        res = requests.post(rest_url, headers=headers, json=[row_data], timeout=15)
        if res.status_code in (200, 201):
            print(f"[SUPABASE-SYNC] [✓] Đã upsert video vào Supabase DB: {fname}")
            return True
        else:
            print(f"[SUPABASE-SYNC] [!] Lỗi upsert Supabase DB ({res.status_code}): {res.text}")
            return False
    except Exception as e:
        print(f"[SUPABASE-SYNC] [!] Ngoại lệ khi đồng bộ video lên Supabase: {e}")
        return False

def delete_streamer_data_supabase(user: str) -> bool:
    """
    Xóa toàn bộ bản ghi video của streamer trong table 'tiktok_recordings',
    xóa streamer trong table 'tiktok_streamers', và dọn dẹp ảnh thumbnail trong Supabase Storage.
    """
    try:
        user = user.strip().replace("@", "").lower()
        headers = get_supabase_headers()

        # 1. Xóa toàn bộ video của user trong table tiktok_recordings
        rec_url = f"{SUPABASE_URL}/rest/v1/tiktok_recordings?username=eq.{user}"
        res_rec = requests.delete(rec_url, headers=headers, timeout=10)
        if res_rec.status_code in (200, 204):
            print(f"[SUPABASE-SYNC] [✓] Đã xóa toàn bộ video của @{user} trong tiktok_recordings")
        else:
            print(f"[SUPABASE-SYNC] [!] Lỗi xóa tiktok_recordings ({res_rec.status_code}): {res_rec.text}")

        # 2. Xóa streamer trong table tiktok_streamers
        usr_url = f"{SUPABASE_URL}/rest/v1/tiktok_streamers?username=eq.{user}"
        res_usr = requests.delete(usr_url, headers=headers, timeout=10)
        if res_usr.status_code in (200, 204):
            print(f"[SUPABASE-SYNC] [✓] Đã xóa streamer @{user} trong tiktok_streamers")
        else:
            print(f"[SUPABASE-SYNC] [!] Lỗi xóa tiktok_streamers ({res_usr.status_code}): {res_usr.text}")

        # 3. Dọn dẹp ảnh thumbnail trong Supabase Storage bucket 'covers/record-thumbnails/{user}/'
        try:
            list_url = f"{SUPABASE_URL}/storage/v1/object/list/{STORAGE_BUCKET}"
            payload = {"prefix": f"record-thumbnails/{user}/", "limit": 100}
            list_res = requests.post(list_url, headers=headers, json=payload, timeout=10)
            if list_res.status_code == 200:
                items = list_res.json()
                if items and isinstance(items, list):
                    prefixes = [f"record-thumbnails/{user}/{item['name']}" for item in items if 'name' in item]
                    if prefixes:
                        del_url = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}"
                        requests.delete(del_url, headers=headers, json={"prefixes": prefixes}, timeout=10)
                        print(f"[SUPABASE-SYNC] [✓] Đã xóa {len(prefixes)} thumbnail của @{user} trong Storage")
        except Exception as st_err:
            print(f"[SUPABASE-SYNC] [!] Lỗi dọn dẹp Storage thumbnail: {st_err}")

        return True
    except Exception as e:
        print(f"[SUPABASE-SYNC] [!] Ngoại lệ khi xóa dữ liệu streamer {user} trên Supabase: {e}")
        return False
