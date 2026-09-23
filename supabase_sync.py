import os
import re
import json
import time
import sys
from datetime import datetime, timezone
from typing import Optional, Union, Tuple
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEFAULT_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImpldHd0cWFreXhqY2ZmYndoaG90Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzUzNjg0MDcsImV4cCI6MjA5MDk0NDQwN30.7QxzLxJs1gdNMG_ruiYcYo_5_1sX0t5Wb9hM92ix4j8"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://jetwtqakyxjcffbwhhot.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_KEY") or DEFAULT_KEY
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
        with requests.get(url, headers=headers, timeout=10) as res:
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list):
                    return [d["username"].strip().replace("@", "").lower() for d in data if "username" in d and d["username"]]
    except Exception as e:
        print(f"[SUPABASE-SYNC] [!] Lỗi nạp streamers từ Supabase: {e}")
    return []

def add_streamer_to_supabase(user: str) -> bool:
    """Thêm streamer vào table 'tiktok_streamers' trên Supabase."""
    try:
        user = user.strip().replace("@", "").lower()
        if not user:
            return False
        headers = get_supabase_headers()
        headers["Prefer"] = "resolution=merge-duplicates"
        url = f"{SUPABASE_URL}/rest/v1/tiktok_streamers"
        with requests.post(url, headers=headers, json={"username": user}, timeout=10) as res:
            return res.status_code in (200, 201, 204)
    except Exception as e:
        print(f"[SUPABASE-SYNC] [!] Lỗi thêm streamer vào Supabase: {e}")
        return False

def clean_filename(filename: str) -> str:
    """Loại bỏ phần mở rộng file để lấy base name sạch sẽ"""
    base = os.path.basename(filename)
    clean = re.sub(r'\.(mp4|jpg|jpeg|png|webp)$', '', base, flags=re.IGNORECASE)
    return clean

def upload_thumbnail_to_supabase(*args, **kwargs) -> Optional[str]:
    """
    Tải ảnh thumbnail lên Supabase Storage bucket 'covers/record-thumbnails/{user}/{clean_name}.jpg'.
    Hỗ trợ thumb_source là:
    - Đường dẫn file cục bộ (str)
    - Dữ liệu bytes ảnh (bytes)
    - URL ảnh tải từ web/drive (str bắt đầu bằng http)
    Hỗ trợ cả hai kiểu gọi:
    1. upload_thumbnail_to_supabase(thumb_source, user, filename)
    2. upload_thumbnail_to_supabase(user, filename, thumb_source=...)
    Trả về URL công khai Supabase CDN nếu thành công.
    """
    thumb_source = kwargs.get("thumb_source")
    user = kwargs.get("user")
    filename = kwargs.get("filename")

    if len(args) == 3:
        thumb_source, user, filename = args[0], args[1], args[2]
    elif len(args) == 2:
        if thumb_source is not None:
            user, filename = args[0], args[1]
        else:
            thumb_source, user = args[0], args[1]
    elif len(args) == 1:
        if thumb_source is None:
            thumb_source = args[0]
        elif user is None:
            user = args[0]

    try:
        user = str(user or "").strip().replace("@", "").lower()
        base_name = clean_filename(str(filename or ""))
        storage_path = f"record-thumbnails/{user}/{base_name}.jpg"
        target_upload_url = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{storage_path}"

        img_bytes = None

        if isinstance(thumb_source, bytes):
            img_bytes = thumb_source
        elif isinstance(thumb_source, str):
            if thumb_source.startswith("http://") or thumb_source.startswith("https://"):
                resp = requests.get(thumb_source, timeout=15)
                try:
                    if resp.status_code == 200 and len(resp.content) > 200:
                        img_bytes = resp.content
                finally:
                    resp.close()
            elif os.path.exists(thumb_source) and os.path.getsize(thumb_source) > 200:
                with open(thumb_source, "rb") as f:
                    img_bytes = f.read()
            elif thumb_source.startswith("gdrive:"):
                # ponytail: Tải thumbnail từ Google Drive API chính ngạch qua Access Token (alt=media)
                drive_id = thumb_source.replace("gdrive:", "").strip()
                try:
                    import gdrive_manager
                    tok = gdrive_manager.get_access_token()
                    if tok and drive_id:
                        d_url = f"https://www.googleapis.com/drive/v3/files/{drive_id}?alt=media"
                        with requests.get(d_url, headers={"Authorization": f"Bearer {tok}"}, timeout=15) as d_res:
                            if d_res.status_code == 200 and len(d_res.content) > 200:
                                img_bytes = d_res.content
                except Exception:
                    pass

        if not img_bytes or len(img_bytes) < 200:
            print(f"[SUPABASE-SYNC] [!] Không thể đọc dữ liệu ảnh thumbnail cho {user}/{filename}")
            return None

        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "image/jpeg",
            "x-upsert": "true"
        }

        for attempt in range(3):
            res = None
            try:
                res = requests.post(target_upload_url, headers=headers, data=img_bytes, timeout=20)
                if res.status_code in (200, 201):
                    public_url = f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{storage_path}"
                    print(f"[SUPABASE-SYNC] [✓] Đã tải thumbnail lên Supabase Storage: {storage_path}")
                    return public_url
                else:
                    print(f"[SUPABASE-SYNC] [!] Lỗi upload Supabase Storage (lần {attempt+1}/3, status {res.status_code}): {res.text}")
            except Exception as req_err:
                print(f"[SUPABASE-SYNC] [!] Ngoại lệ upload thumbnail (lần {attempt+1}/3): {req_err}")
            finally:
                if res is not None and hasattr(res, "close"):
                    res.close()
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
        return None
    except Exception as e:
        print(f"[SUPABASE-SYNC] [!] Ngoại lệ khi upload thumbnail lên Supabase: {e}")
        return None

sync_thumbnail_to_supabase = upload_thumbnail_to_supabase

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
                recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        final_thumb_url = existing_thumb_url
        if thumb_source:
            uploaded_url = upload_thumbnail_to_supabase(thumb_source, user, fname)
            if uploaded_url:
                final_thumb_url = uploaded_url
        elif not final_thumb_url and drive_thumb_id:
            # ponytail: Nếu chưa có thumbnail nhưng có drive_thumb_id, tải trực tiếp từ Drive lên Supabase Storage
            uploaded_url = upload_thumbnail_to_supabase(f"gdrive:{drive_thumb_id}", user, fname)
            if uploaded_url:
                final_thumb_url = uploaded_url

        if not download_url:
            download_url = f"/api/download/{user}/{fname}"

        if not drive_file_id:
            try:
                import gdrive_manager
                t_lookup = gdrive_manager.get_access_token()
                if t_lookup:
                    safe_fname = fname.replace("\\", "\\\\").replace("'", "\\'")
                    q_lookup = f"name = '{safe_fname}' and trashed = false"
                    u_lookup = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q_lookup)}&fields=files(id)"
                    r_lookup = requests.get(u_lookup, headers={"Authorization": f"Bearer {t_lookup}"}, timeout=8)
                    if r_lookup.status_code == 200:
                        f_list = r_lookup.json().get("files", [])
                        if f_list and f_list[0].get("id"):
                            drive_file_id = f_list[0].get("id")
            except Exception:
                pass

        if not cdn_download_url and drive_file_id:
            cdn_download_url = f"https://drive.usercontent.google.com/download?id={drive_file_id}&export=download&authuser=0&confirm=t"

        size_mb = round(float(size_bytes) / (1024 * 1024), 2) if size_bytes else 0.0

        row_data = {
            "filename": fname,
            "username": user,
            "size_bytes": int(size_bytes),
            "size_mb": size_mb,
            "recorded_at": recorded_at,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
            "thumbnail_url": final_thumb_url,
            "download_url": download_url,
            "cdn_download_url": cdn_download_url,
            "drive_file_id": drive_file_id,
            "drive_thumb_id": drive_thumb_id,
            "source": source
        }

        rest_url = f"{SUPABASE_URL}/rest/v1/tiktok_recordings?on_conflict=filename"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=representation"
        }

        for attempt in range(3):
            res = None
            try:
                res = requests.post(rest_url, headers=headers, json=[row_data], timeout=15)
                if res.status_code in (200, 201):
                    print(f"[SUPABASE-SYNC] [✓] Đã upsert video vào Supabase DB: {fname}")
                    return True
                else:
                    print(f"[SUPABASE-SYNC] [!] Lỗi upsert Supabase DB (lần {attempt+1}/3, status {res.status_code}): {res.text}")
            except Exception as req_err:
                print(f"[SUPABASE-SYNC] [!] Ngoại lệ upsert Supabase (lần {attempt+1}/3): {req_err}")
            finally:
                if res is not None and hasattr(res, "close"):
                    res.close()
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
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
        with requests.delete(rec_url, headers=headers, timeout=10) as res_rec:
            ok_rec = res_rec.status_code in (200, 204)
            if ok_rec:
                print(f"[SUPABASE-SYNC] [✓] Đã xóa toàn bộ video của @{user} trong tiktok_recordings")
            else:
                print(f"[SUPABASE-SYNC] [!] Lỗi xóa tiktok_recordings ({res_rec.status_code}): {res_rec.text}")

        # 2. Xóa streamer trong table tiktok_streamers
        usr_url = f"{SUPABASE_URL}/rest/v1/tiktok_streamers?username=eq.{user}"
        with requests.delete(usr_url, headers=headers, timeout=10) as res_usr:
            ok_usr = res_usr.status_code in (200, 204)
            if ok_usr:
                print(f"[SUPABASE-SYNC] [✓] Đã xóa streamer @{user} trong tiktok_streamers")
            else:
                print(f"[SUPABASE-SYNC] [!] Lỗi xóa tiktok_streamers ({res_usr.status_code}): {res_usr.text}")

        # 3. Dọn dẹp ảnh thumbnail trong Supabase Storage bucket 'covers/record-thumbnails/{user}/'
        try:
            list_url = f"{SUPABASE_URL}/storage/v1/object/list/{STORAGE_BUCKET}"
            del_url = f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}"
            max_pages = 20
            while max_pages > 0:
                max_pages -= 1
                payload = {"prefix": f"record-thumbnails/{user}/", "limit": 100}
                with requests.post(list_url, headers=headers, json=payload, timeout=10) as list_res:
                    if list_res.status_code == 200:
                        items = list_res.json()
                        if not items or not isinstance(items, list):
                            break
                        prefixes = [f"record-thumbnails/{user}/{item['name']}" for item in items if 'name' in item]
                        if prefixes:
                            with requests.delete(del_url, headers=headers, json={"prefixes": prefixes}, timeout=10) as del_thumb_res:
                                pass
                            print(f"[SUPABASE-SYNC] [✓] Đã xóa {len(prefixes)} thumbnail của @{user} trong Storage")
                        if len(items) < 100:
                            break
                    else:
                        break
        except Exception as st_err:
            print(f"[SUPABASE-SYNC] [!] Lỗi dọn dẹp Storage thumbnail: {st_err}")

        return ok_rec and ok_usr
    except Exception as e:
        print(f"[SUPABASE-SYNC] [!] Ngoại lệ khi xóa dữ liệu streamer {user} trên Supabase: {e}")
        return False
