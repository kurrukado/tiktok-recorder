import os
import sys
import json
import time
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

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def get_access_token():
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
            return res.json().get("access_token")
        else:
            print(f"[!] Lỗi khi lấy Access Token từ Google: {res.text}")
    except Exception as e:
        print(f"[!] Lỗi kết nối Google OAuth: {e}")
    return None

def find_or_create_folder(folder_name, parent_id=None, access_token=None):
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
            return files[0]["id"]

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
        return create_res.json()["id"]
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

    # Initiate Resumable Upload Session
    init_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Upload-Content-Type": "video/mp4",
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
                put_res = requests.put(upload_url, headers=headers_chunk, data=chunk, timeout=60)
                uploaded_bytes += chunk_len

                curr_mb = uploaded_bytes / (1024 * 1024)
                pct = (uploaded_bytes / file_size) * 100
                speed = curr_mb / (time.time() - start_time + 0.001)
                sys.stdout.write(f"\r  --> Đang tải lên Drive: {file_name} ({curr_mb:.1f}/{file_size_mb:.1f} MB - {pct:.1f}%) [{speed:.2f} MB/s]")
                sys.stdout.flush()

        print(f"\r  [✓] Đã tải lên Drive thành công: {file_name} ({file_size_mb:.2f} MB)                 ")
        return True

    except Exception as e:
        print(f"\n  [!] Lỗi khi tải file {file_name}: {e}")
        return False

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

    users = ["islizanx", "itsme_kate0110", "urielhui38"]
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
