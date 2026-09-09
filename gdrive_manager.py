import os
import sys
import json
import time
import threading
import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

_CACHED_ACCESS_TOKEN = {"token": None, "expires_at": 0}
_TOKEN_LOCK = threading.Lock()
_FOLDER_CACHE = {}
_FOLDER_CACHE_LOCK = threading.Lock()
_DRIVE_STATUS_LOCK = threading.Lock()

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def get_access_token(force_refresh=False):
    global _CACHED_ACCESS_TOKEN
    now = time.time()
    with _TOKEN_LOCK:
        if not force_refresh and _CACHED_ACCESS_TOKEN["token"] and now < _CACHED_ACCESS_TOKEN["expires_at"]:
            return _CACHED_ACCESS_TOKEN["token"]

        cfg = load_config()
        refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN") or cfg.get("gdrive_refresh_token")
        if not refresh_token:
            return None

        client_id = os.environ.get("GOOGLE_CLIENT_ID") or cfg.get("google_client_id") or CLIENT_ID
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or cfg.get("google_client_secret") or CLIENT_SECRET

        url = "https://oauth2.googleapis.com/token"
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }
        try:
            res = requests.post(url, data=data, timeout=15)
            if res.status_code == 200:
                res_data = res.json()
                token = res_data.get("access_token")
                expires_in = res_data.get("expires_in", 3600)
                _CACHED_ACCESS_TOKEN["token"] = token
                _CACHED_ACCESS_TOKEN["expires_at"] = now + max(expires_in - 120, 60)
                return token
            else:
                print(f"[!] Lỗi khi lấy Access Token từ Google: {res.text}")
        except Exception as e:
            print(f"[!] Lỗi kết nối Google OAuth: {e}")
        return None

def find_or_create_folder(folder_name, parent_id=None, access_token=None):
    cache_key = (folder_name, parent_id)
    with _FOLDER_CACHE_LOCK:
        if cache_key in _FOLDER_CACHE:
            return _FOLDER_CACHE[cache_key]

    if not access_token:
        access_token = get_access_token()
    if not access_token:
        raise ValueError("Chưa có Access Token Google Drive!")

    headers = {"Authorization": f"Bearer {access_token}"}
    
    # Check if folder exists
    q = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        q += f" and '{parent_id}' in parents"

    url = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q)}&fields=files(id,name)"
    res = requests.get(url, headers=headers, timeout=15)
    if res.status_code == 200:
        files = res.json().get("files", [])
        if files:
            fid = files[0]["id"]
            with _FOLDER_CACHE_LOCK:
                _FOLDER_CACHE[cache_key] = fid
            return fid

    # Create folder if not found
    meta = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder"
    }
    if parent_id:
        meta["parents"] = [parent_id]

    create_res = requests.post(
        "https://www.googleapis.com/drive/v3/files",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json=meta,
        timeout=15
    )
    if create_res.status_code in [200, 201]:
        fid = create_res.json()["id"]
        with _FOLDER_CACHE_LOCK:
            _FOLDER_CACHE[cache_key] = fid
        return fid
    else:
        raise RuntimeError(f"Không thể tạo folder '{folder_name}' trên Drive: {create_res.text}")

def upload_file_to_drive(file_path, parent_folder_id, access_token=None):
    if not access_token:
        access_token = get_access_token()
    if not access_token:
        return False

    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    file_size_mb = file_size / (1024 * 1024)

    headers = {"Authorization": f"Bearer {access_token}"}

    # Check if file already exists in this folder
    q = f"name = '{file_name}' and '{parent_folder_id}' in parents and trashed = false"
    check_url = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q)}&fields=files(id,size)"
    try:
        c_res = requests.get(check_url, headers=headers, timeout=15)
        if c_res.status_code == 200:
            existing = c_res.json().get("files", [])
            if existing:
                print(f"  [-] File đã tồn tại trên Drive: {file_name} ({file_size_mb:.2f} MB), bỏ qua.")
                return True
    except Exception:
        pass

    init_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
    mime_type = "image/jpeg" if file_name.lower().endswith((".jpg", ".jpeg")) else "video/mp4"
    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Upload-Content-Type": mime_type,
        "X-Upload-Content-Length": str(file_size)
    }
    meta = {
        "name": file_name,
        "parents": [parent_folder_id]
    }

    try:
        init_res = requests.post(init_url, headers=init_headers, json=meta, timeout=30)
        if init_res.status_code != 200:
            print(f"  [!] Lỗi khởi tạo upload session: {init_res.text}")
            return False

        upload_url = init_res.headers.get("Location")
        if not upload_url:
            return False

        chunk_size = 5 * 1024 * 1024  # 5MB chunks
        start_time = time.time()
        uploaded_bytes = 0

        with open(file_path, "rb") as f:
            while uploaded_bytes < file_size:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                chunk_len = len(chunk)
                end_byte = uploaded_bytes + chunk_len - 1
                headers_chunk = {
                    "Content-Range": f"bytes {uploaded_bytes}-{end_byte}/{file_size}",
                    "Content-Length": str(chunk_len)
                }

                chunk_ok = False
                put_res = None
                for attempt in range(5):
                    try:
                        put_res = requests.put(upload_url, headers=headers_chunk, data=chunk, timeout=60)
                        if put_res.status_code in (200, 201, 308):
                            chunk_ok = True
                            break
                        elif put_res.status_code in (500, 502, 503, 504):
                            time.sleep(1.5 * (attempt + 1))
                            continue
                        else:
                            print(f"\n  [!] Google Drive trả về mã {put_res.status_code}: {put_res.text}")
                            break
                    except Exception as ex:
                        time.sleep(1.5 * (attempt + 1))

                if not chunk_ok or put_res is None:
                    print(f"\n  [!] Không thể upload chunk {uploaded_bytes}-{end_byte} sau 5 lần thử.")
                    return False

                uploaded_bytes += chunk_len
                curr_mb = uploaded_bytes / (1024 * 1024)
                pct = (uploaded_bytes / file_size) * 100
                speed = curr_mb / (time.time() - start_time + 0.001)
                sys.stdout.write(f"\r  --> Đang tải lên Drive: {file_name} ({curr_mb:.1f}/{file_size_mb:.1f} MB - {pct:.1f}%) [{speed:.2f} MB/s]")
                sys.stdout.flush()

        if put_res is None or put_res.status_code not in (200, 201):
            print(f"\n  [!] Upload chưa hoàn tất hợp lệ (mã: {put_res.status_code if put_res else 'None'})")
            return False

        print(f"\r  [✓] Đã tải lên Drive thành công: {file_name} ({file_size_mb:.2f} MB)                 ")
        try:
            res_data = put_res.json()
            f_id = res_data.get("id")
            if f_id:
                make_file_public(f_id, access_token=access_token)
        except Exception:
            pass
        return True

    except Exception as e:
        print(f"\n  [!] Lỗi khi tải file {file_name}: {e}")
        return False

def make_file_public(file_id, access_token=None):
    """Cấp quyền đọc công khai (anyoneWithLink) để tải qua CDN tốc độ cao không cần đăng nhập."""
    try:
        if not access_token:
            access_token = get_access_token()
        if not access_token:
            return False
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions"
        requests.post(url, headers=headers, json={"role": "reader", "type": "anyone"}, timeout=10)
        return True
    except Exception:
        return False

def get_cdn_download_url(file_id):
    """Tạo link CDN tải trực tiếp tốc độ cao tối đa từ máy chủ Edge của Google."""
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0"

def sync_all_to_gdrive():
    cfg = load_config()
    if not cfg.get("gdrive_refresh_token"):
        print("\n[!] Bạn chưa liên kết tài khoản Google Drive!")
        print("[*] Vui lòng chạy lệnh: python gdrive_auth.py để liên kết 1 lần duy nhất.")
        return

    print("\n" + "=" * 65)
    print("     ĐỒNG BỘ TOÀN BỘ VIDEO LÊN GOOGLE DRIVE: tiktok-record/")
    print("=" * 65)

    token = get_access_token()
    if not token:
        print("[!] Không thể lấy token truy cập Google Drive.")
        return

    print("[1/3] Đang định vị/tạo thư mục gốc: 'tiktok-record' trên Google Drive...")
    root_id = find_or_create_folder("tiktok-record", access_token=token)
    print(f"  [✓] ID thư mục gốc: {root_id}")

    users_set = set()
    try:
        drive_u = load_streamers_from_drive(access_token=token)
        if drive_u and isinstance(drive_u, list):
            users_set.update(drive_u)
    except Exception:
        pass
    cfg = load_config()
    users_set.update(cfg.get("monitored_users", []))
    for item in os.listdir(BASE_DIR):
        item_path = os.path.join(BASE_DIR, item)
        if os.path.isdir(item_path) and not item.startswith((".", "_")):
            users_set.add(item)
    users = list(users_set)
    total_uploaded = 0
    total_files = 0

    for user in users:
        user_folder = os.path.join(BASE_DIR, user)
        if not os.path.exists(user_folder):
            continue

        files = [f for f in os.listdir(user_folder) if f.endswith(".mp4") and not f.endswith(".tmp.mp4")]
        if not files:
            continue

        print(f"\n[2/3] Đang xử lý thư mục của @{user} ({len(files)} video)...")
        subfolder_id = find_or_create_folder(user, parent_id=root_id, access_token=token)
        print(f"  [✓] Thư mục: tiktok-record/{user}/ (ID: {subfolder_id})")

        for f in files:
            fp = os.path.join(user_folder, f)
            total_files += 1
            ok = upload_file_to_drive(fp, subfolder_id, access_token=token)
            if ok:
                total_uploaded += 1

    print("\n" + "=" * 65)
    print(f"[✓] HOÀN TẤT ĐỒNG BỘ LÊN GOOGLE DRIVE:")
    print(f"    - Đã lưu vào thư mục : tiktok-record/ trên Google Drive của bạn")
    print(f"    - Tổng số video      : {total_uploaded}/{total_files} file đã sẵn sàng")
    print("=" * 65 + "\n")

def load_streamers_from_drive(access_token=None):
    """Đọc danh sách streamer từ file streamers.json trong thư mục tiktok-record trên Google Drive (Thread-Safe)."""
    with _DRIVE_STATUS_LOCK:
        try:
            if not access_token:
                access_token = get_access_token()
            if not access_token:
                return None
            root_id = find_or_create_folder("tiktok-record", access_token=access_token)
            headers = {"Authorization": f"Bearer {access_token}"}
            q = f"name = 'streamers.json' and '{root_id}' in parents and trashed = false"
            url = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q)}&fields=files(id,name)"
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                files = res.json().get("files", [])
                if files:
                    file_id = files[0]["id"]
                    down_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
                    d_res = requests.get(down_url, headers=headers, timeout=10)
                    if d_res.status_code == 200:
                        data = d_res.json()
                        if isinstance(data, list):
                            return data
                        if isinstance(data, dict) and "streamers" in data:
                            return data["streamers"]
        except Exception as e:
            print(f"[!] Lỗi đọc streamers.json từ Drive: {e}")
        return None

def save_streamers_to_drive(streamers_list, access_token=None):
    """Lưu danh sách streamer vào file streamers.json trong thư mục tiktok-record trên Google Drive (Thread-Safe)."""
    with _DRIVE_STATUS_LOCK:
        try:
            if not access_token:
                access_token = get_access_token()
            if not access_token:
                return False
            root_id = find_or_create_folder("tiktok-record", access_token=access_token)
            headers = {"Authorization": f"Bearer {access_token}"}
            q = f"name = 'streamers.json' and '{root_id}' in parents and trashed = false"
            url = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q)}&fields=files(id,name)"
            res = requests.get(url, headers=headers, timeout=10)
            file_id = None
            if res.status_code == 200:
                files = res.json().get("files", [])
                if files:
                    file_id = files[0]["id"]

            content_bytes = json.dumps(streamers_list, indent=2, ensure_ascii=False).encode("utf-8")
            if file_id:
                up_url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media"
                up_res = requests.patch(
                    up_url,
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                    data=content_bytes,
                    timeout=15
                )
                return up_res.status_code == 200
            else:
                meta = {
                    "name": "streamers.json",
                    "parents": [root_id]
                }
                files = {
                    "data": ("metadata", json.dumps(meta), "application/json; charset=UTF-8"),
                    "file": ("streamers.json", content_bytes, "application/json")
                }
                create_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
                c_res = requests.post(create_url, headers={"Authorization": f"Bearer {access_token}"}, files=files, timeout=15)
                return c_res.status_code in [200, 201]
        except Exception as e:
            print(f"[!] Lỗi ghi streamers.json lên Drive: {e}")
        return False

def load_active_recordings_from_drive(access_token=None, as_details=False):
    """
    Đọc danh sách các streamer đang được ghi hình thời gian thực từ Google Drive (Thread-Safe).
    Bao gồm trường updated_at để theo dõi TTL.
    Nếu as_details=False: trả về danh sách username [str] để tương thích ngược 100%.
    Nếu as_details=True: trả về danh sách chi tiết [{'username': str, 'updated_at': int}].
    """
    with _DRIVE_STATUS_LOCK:
        try:
            if not access_token:
                access_token = get_access_token()
            if not access_token:
                return []
            root_id = find_or_create_folder("tiktok-record", access_token=access_token)
            headers = {"Authorization": f"Bearer {access_token}"}
            q = f"name = 'active_recordings.json' and '{root_id}' in parents and trashed = false"
            url = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q)}&fields=files(id,name)"
            res = requests.get(url, headers=headers, timeout=8)
            if res.status_code == 200:
                files = res.json().get("files", [])
                if files:
                    file_id = files[0]["id"]
                    down_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
                    d_res = requests.get(down_url, headers=headers, timeout=8)
                    if d_res.status_code == 200:
                        raw_data = d_res.json()
                        if not isinstance(raw_data, list):
                            return []

                        details = []
                        now_ts = int(time.time())
                        for item in raw_data:
                            if isinstance(item, dict) and "username" in item:
                                details.append({
                                    "username": str(item["username"]).strip().replace("@", "").lower(),
                                    "updated_at": item.get("updated_at", now_ts)
                                })
                            elif isinstance(item, str) and item.strip():
                                details.append({
                                    "username": item.strip().replace("@", "").lower(),
                                    "updated_at": now_ts
                                })

                        if as_details:
                            return details
                        return [d["username"] for d in details]
        except Exception as e:
            print(f"[!] Lỗi đọc active_recordings.json từ Drive: {e}")
        return []

def set_user_recording_status_drive(user: str, is_recording: bool, access_token=None):
    """
    Cập nhật trạng thái đang quay của một streamer lên Google Drive (Thread-Safe).
    Tự động lưu kèm trường updated_at (epoch timestamp) để theo dõi TTL.
    """
    with _DRIVE_STATUS_LOCK:
        try:
            if not access_token:
                access_token = get_access_token()
            if not access_token:
                return False
            root_id = find_or_create_folder("tiktok-record", access_token=access_token)
            headers = {"Authorization": f"Bearer {access_token}"}
            
            q = f"name = 'active_recordings.json' and '{root_id}' in parents and trashed = false"
            url = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q)}&fields=files(id,name)"
            res = requests.get(url, headers=headers, timeout=8)
            file_id = None
            current_raw = []
            if res.status_code == 200:
                files = res.json().get("files", [])
                if files:
                    file_id = files[0]["id"]
                    try:
                        d_res = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media", headers=headers, timeout=8)
                        if d_res.status_code == 200:
                            current_raw = d_res.json()
                            if not isinstance(current_raw, list):
                                current_raw = []
                    except Exception:
                        pass

            user = user.strip().replace("@", "").lower()
            now_ts = int(time.time())

            # Chuẩn hóa dữ liệu sang danh sách dict với username và updated_at
            normalized_active = []
            for it in current_raw:
                if isinstance(it, dict) and "username" in it:
                    normalized_active.append({
                        "username": str(it["username"]).strip().replace("@", "").lower(),
                        "updated_at": it.get("updated_at", now_ts)
                    })
                elif isinstance(it, str) and it.strip():
                    normalized_active.append({
                        "username": it.strip().replace("@", "").lower(),
                        "updated_at": now_ts
                    })

            if is_recording:
                found = False
                for entry in normalized_active:
                    if entry["username"] == user:
                        entry["updated_at"] = now_ts
                        found = True
                        break
                if not found:
                    normalized_active.append({"username": user, "updated_at": now_ts})
            else:
                normalized_active = [it for it in normalized_active if it["username"] != user]

            content_bytes = json.dumps(normalized_active, indent=2, ensure_ascii=False).encode("utf-8")
            if file_id:
                up_url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media"
                requests.patch(up_url, headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}, data=content_bytes, timeout=10)
            else:
                meta = {"name": "active_recordings.json", "parents": [root_id]}
                files_data = {
                    "data": ("metadata", json.dumps(meta), "application/json; charset=UTF-8"),
                    "file": ("active_recordings.json", content_bytes, "application/json")
                }
                create_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
                requests.post(create_url, headers={"Authorization": f"Bearer {access_token}"}, files=files_data, timeout=10)
            return True
        except Exception as e:
            print(f"[!] Lỗi cập nhật active_recordings lên Drive: {e}")
            return False

def create_streamer_folder_drive(user: str, access_token=None):
    """
    Tạo thư mục tiktok-record/<user> trên Google Drive ngay khi thêm streamer.
    Trả về (True, folder_id) hoặc (False, error_msg).
    """
    try:
        if not access_token:
            access_token = get_access_token()
        if not access_token:
            return False, "Chưa có Access Token Google Drive (Vui lòng kiểm tra biến môi trường GDRIVE_REFRESH_TOKEN)"
        
        user = user.strip().replace("@", "").lower()
        root_id = find_or_create_folder("tiktok-record", access_token=access_token)
        subfolder_id = find_or_create_folder(user, parent_id=root_id, access_token=access_token)
        return True, subfolder_id
    except Exception as e:
        return False, str(e)

def delete_streamer_folder_drive(user: str, access_token=None):
    """
    Xóa vĩnh viễn thư mục tiktok-record/<user> cùng toàn bộ video bên trong trên Google Drive.
    Trả về (True, message) hoặc (False, error_msg).
    """
    try:
        if not access_token:
            access_token = get_access_token()
        if not access_token:
            return False, "Chưa có Access Token Google Drive (Vui lòng kiểm tra biến môi trường GDRIVE_REFRESH_TOKEN)"
        
        user = user.strip().replace("@", "").lower()
        root_id = find_or_create_folder("tiktok-record", access_token=access_token)
        headers = {"Authorization": f"Bearer {access_token}"}
        
        q = f"name = '{user}' and mimeType = 'application/vnd.google-apps.folder' and '{root_id}' in parents and trashed = false"
        url = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q)}&fields=files(id,name)"
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            files = res.json().get("files", [])
            if files:
                folder_id = files[0]["id"]
                del_url = f"https://www.googleapis.com/drive/v3/files/{folder_id}"
                del_res = requests.delete(del_url, headers=headers, timeout=15)
                if del_res.status_code in [200, 204]:
                    return True, f"Đã xóa thành công thư mục 'tiktok-record/{user}/' trên Google Drive"
                else:
                    return False, f"Lỗi từ Google Drive: {del_res.text}"
            else:
                return True, f"Thư mục 'tiktok-record/{user}/' không tồn tại trên Google Drive"
        else:
            return False, f"Lỗi tìm kiếm thư mục trên Google Drive: {res.text}"
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Google Drive Sync Manager")
    parser.add_argument("--upload-all", action="store_true", help="Đồng bộ toàn bộ video lên Google Drive")
    parser.add_argument("--file", help="Đường dẫn file video cần upload")
    parser.add_argument("--user", help="Tên user tương ứng")
    args = parser.parse_args()

    if args.upload_all:
        sync_all_to_gdrive()
    elif args.file and args.user:
        token = get_access_token()
        root_id = find_or_create_folder("tiktok-record", access_token=token)
        sub_id = find_or_create_folder(args.user, parent_id=root_id, access_token=token)
        upload_file_to_drive(args.file, sub_id, access_token=token)
    else:
        sync_all_to_gdrive()
