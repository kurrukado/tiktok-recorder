import sys
import os
import time
import requests
import subprocess
import json
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import gdrive_manager
import supabase_sync
import auto_h264

PROGRESS_FILE = os.path.join(BASE_DIR, "cloud_repair_progress.json")

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"repaired_ids": [], "failed_ids": {}}

def save_progress(progress):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

def download_file_from_drive(file_id, dest_path, access_token=None):
    if not access_token:
        access_token = gdrive_manager.get_access_token()
    if not access_token:
        return False
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = requests.get(url, headers=headers, stream=True, timeout=60)
        if r.status_code == 401:
            access_token = gdrive_manager.get_access_token(force_refresh=True)
            headers["Authorization"] = f"Bearer {access_token}"
            r = requests.get(url, headers=headers, stream=True, timeout=60)
        if r.status_code != 200:
            print(f"  [!] Lỗi tải file từ Drive: HTTP {r.status_code}")
            return False
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=2 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
        return os.path.exists(dest_path) and os.path.getsize(dest_path) > 1024
    except Exception as e:
        print(f"  [!] Lỗi exception khi tải: {e}")
        return False

def patch_file_to_drive(file_path, file_id, access_token=None):
    if not os.path.exists(file_path):
        return False
    file_size = os.path.getsize(file_path)
    if not access_token:
        access_token = gdrive_manager.get_access_token()
    if not access_token:
        return False

    init_url = f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=resumable"
    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Upload-Content-Type": "video/mp4",
        "X-Upload-Content-Length": str(file_size)
    }

    try:
        init_res = requests.patch(init_url, headers=init_headers, timeout=30)
        if init_res.status_code == 401:
            access_token = gdrive_manager.get_access_token(force_refresh=True)
            init_headers["Authorization"] = f"Bearer {access_token}"
            init_res = requests.patch(init_url, headers=init_headers, timeout=30)
        if init_res.status_code != 200:
            print(f"  [!] Lỗi khởi tạo patch session ({init_res.status_code}): {init_res.text}")
            return False

        upload_url = init_res.headers.get("Location")
        if not upload_url:
            return False

        chunk_size = 8 * 1024 * 1024  # 8MB chunk
        uploaded_bytes = 0

        with open(file_path, "rb") as f:
            while uploaded_bytes < file_size:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                chunk_len = len(chunk)
                end_byte = uploaded_bytes + chunk_len - 1
                headers = {
                    "Content-Range": f"bytes {uploaded_bytes}-{end_byte}/{file_size}",
                    "Content-Length": str(chunk_len)
                }

                chunk_ok = False
                put_res = None
                for attempt in range(5):
                    try:
                        put_res = requests.put(upload_url, headers=headers, data=chunk, timeout=60)
                        if put_res.status_code in (200, 201):
                            uploaded_bytes += chunk_len
                            chunk_ok = True
                            break
                        elif put_res.status_code == 308:
                            range_hdr = put_res.headers.get("Range")
                            if range_hdr and "-" in range_hdr:
                                try:
                                    last_byte = int(range_hdr.split("-")[-1])
                                    uploaded_bytes = last_byte + 1
                                    f.seek(uploaded_bytes)
                                except Exception:
                                    uploaded_bytes += chunk_len
                            else:
                                uploaded_bytes = 0
                                f.seek(0)
                            chunk_ok = True
                            break
                        elif put_res.status_code in (429, 500, 502, 503, 504):
                            time.sleep(2.0 * (attempt + 1))
                            continue
                        else:
                            print(f"\n  [!] Drive PATCH mã {put_res.status_code}")
                            break
                    except Exception:
                        time.sleep(1.5 * (attempt + 1))

                if not chunk_ok or put_res is None:
                    return False

        return put_res is not None and put_res.status_code in (200, 201)
    except Exception as e:
        print(f"  [!] Lỗi patch lên Drive: {e}")
        return False

def update_supabase_metadata(rec_id, file_path, user=None, fn=None):
    try:
        new_size = os.path.getsize(file_path)
        new_size_mb = round(new_size / (1024 * 1024), 2)
        patch_data = {"size_bytes": new_size, "size_mb": new_size_mb}

        if user and fn:
            thumb_path = auto_h264.extract_middle_thumbnail(file_path)
            if thumb_path and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
                thumb_url = supabase_sync.upload_thumbnail_to_supabase(thumb_path, user, fn)
                if thumb_url:
                    patch_data["thumbnail_url"] = thumb_url
                try:
                    os.remove(thumb_path)
                except Exception:
                    pass

        headers = supabase_sync.get_supabase_headers()
        url = f"{supabase_sync.SUPABASE_URL}/rest/v1/tiktok_recordings?id=eq.{rec_id}"
        requests.patch(url, headers=headers, json=patch_data, timeout=10)
        return True
    except Exception:
        return False

def process_single_repair(rec, access_token=None):
    rec_id = rec["id"]
    user = rec["username"]
    fn = rec["filename"]
    fid = rec["drive_file_id"]
    size_mb = rec.get("size_mb", 0)

    print(f"\n=======================================================")
    print(f"[*] ĐANG XỬ LÝ [ID: {rec_id}] @{user} - {fn}")
    print(f"[*] Dung lượng hiện tại: {size_mb} MB | Google Drive ID: {fid}")
    print(f"=======================================================")

    temp_raw = os.path.join(BASE_DIR, f"temp_repair_{rec_id}.mp4")

    # 1. Download
    print(f"[1/4] Đang tải video từ Google Drive API...")
    t0 = time.time()
    ok_dl = download_file_from_drive(fid, temp_raw, access_token=access_token)
    if not ok_dl:
        print(f"  ❌ Không thể tải video ID {rec_id} từ Drive.")
        if os.path.exists(temp_raw):
            try: os.remove(temp_raw)
            except: pass
        return False
    dl_time = time.time() - t0
    raw_size_mb = os.path.getsize(temp_raw) / (1024 * 1024)
    print(f"  [✓] Tải thành công {raw_size_mb:.2f} MB ({dl_time:.1f}s)")

    # 2. Transcode / Sanitize
    print(f"[2/4] Đang chuẩn hóa bitstream & H.264 qua GPU NVENC...")
    t1 = time.time()
    repaired_path = auto_h264.ensure_h264(temp_raw)
    trans_time = time.time() - t1
    repaired_size_mb = os.path.getsize(repaired_path) / (1024 * 1024)
    has_fs = auto_h264.has_faststart(repaired_path)
    is_healthy = auto_h264.check_h264_stream_health(repaired_path)
    val_ok, val_reason, val_dur = auto_h264.validate_playable_video(repaired_path)
    print(f"  [✓] Đã làm sạch: {repaired_size_mb:.2f} MB ({trans_time:.1f}s) | FastStart: {has_fs} | Bitstream Healthy: {is_healthy} | Playable: {val_ok}")
    if not is_healthy or not val_ok:
        print(f"  ❌ File sau khi xử lý vẫn không đạt chuẩn ({val_reason}). Hủy patch Drive.")
        if os.path.exists(repaired_path):
            try: os.remove(repaired_path)
            except: pass
        if os.path.exists(temp_raw):
            try: os.remove(temp_raw)
            except: pass
        return False

    # 3. In-Place PATCH to Google Drive (Chỉ ghi đè khi cần làm sạch/sửa lỗi bitstream)
    if repaired_path != temp_raw:
        print(f"[3/4] Đang ghi đè nội dung sạch lên Google Drive (giữ nguyên file ID: {fid})...")
        t2 = time.time()
        patch_ok = patch_file_to_drive(repaired_path, fid, access_token=access_token)
        if not patch_ok:
            print(f"  ❌ Ghi đè lên Google Drive thất bại.")
            if os.path.exists(repaired_path):
                try: os.remove(repaired_path)
                except: pass
            if os.path.exists(temp_raw):
                try: os.remove(temp_raw)
                except: pass
            return False
        patch_time = time.time() - t2
        print(f"  [✓] Ghi đè Drive thành công ({patch_time:.1f}s)")
    else:
        print(f"[3/4] [✓] File trên Google Drive đã đạt chuẩn toàn vẹn (H.264 + FastStart). Bỏ qua upload Drive.")

    # 4. Update Supabase
    print(f"[4/4] Cập nhật metadata & thumbnail Supabase...")
    update_supabase_metadata(rec_id, repaired_path, user=user, fn=fn)
    print(f"  [✓] Đã cập nhật metadata & thumbnail: {repaired_size_mb:.2f} MB")

    # Cleanup
    if os.path.exists(repaired_path):
        try: os.remove(repaired_path)
        except: pass
    if os.path.exists(temp_raw):
        try: os.remove(temp_raw)
        except: pass

    total_time = time.time() - t0
    print(f"✨ HOÀN TẤT XỬ LÝ ID {rec_id} TRONG {total_time:.1f} GIÂY!\n")
    return True

def run_repair_engine(priority_only=False, today_only=False):
    print("=" * 65)
    print("      KURU RECORD CLOUD VIDEO AUTO-REPAIR ENGINE (v61)")
    print("=" * 65)

    progress = load_progress()
    repaired_set = set(progress.get("repaired_ids", []))

    targets = []
    if today_only:
        headers = supabase_sync.get_supabase_headers()
        since_time = (datetime.now(timezone.utc) - timedelta(hours=28)).strftime("%Y-%m-%dT%H:%M:%S")
        url = f"{supabase_sync.SUPABASE_URL}/rest/v1/tiktok_recordings?select=id,username,filename,drive_file_id,size_mb&created_at=gte.{since_time}&order=id.asc"
        r = requests.get(url, headers=headers)
        targets = r.json() if r.status_code == 200 else []
    elif os.path.exists(scan_report_path):
        with open(scan_report_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            priority_items = []
            standard_items = []
            for item in data.get("needs_repair", []):
                reasons = item.get("reasons", [])
                user = item.get("user", "")
                if "NAL_UNIT_SIZE_ERROR" in reasons or user in ("urielhui38", "linh.khanh5351", "nicaswrld", "wyhnnhu_03", "kieu86953", "islizanx", "triuthnga770"):
                    priority_items.append(item)
                else:
                    standard_items.append(item)
            
            targets = priority_items if priority_only else (priority_items + standard_items)
    else:
        # Fallback query Supabase
        headers = supabase_sync.get_supabase_headers()
        url = f"{supabase_sync.SUPABASE_URL}/rest/v1/tiktok_recordings?select=id,username,filename,drive_file_id,size_mb&order=id.desc"
        r = requests.get(url, headers=headers)
        targets = r.json()

    # Filter out already repaired
    queue = []
    for t in targets:
        rec_id = t["id"]
        if rec_id in repaired_set:
            continue
        queue.append({
            "id": rec_id,
            "username": t.get("user") or t.get("username"),
            "filename": t["filename"],
            "drive_file_id": t["drive_file_id"],
            "size_mb": t.get("size_mb", 0)
        })

    print(f"[*] Tìm thấy {len(queue)} video cần xử lý sửa đổi trên Drive.")
    if not queue:
        print("[✓] Toàn bộ video trong danh sách đã được sửa chữa hoàn tất!")
        return

    token = gdrive_manager.get_access_token()
    if not token:
        print("[!] Không lấy được access token Google Drive.")
        return

    success_count = 0
    fail_count = 0

    for idx, rec in enumerate(queue, 1):
        print(f"\n[TIẾN ĐỘ: {idx}/{len(queue)}]")
        ok = process_single_repair(rec, access_token=token)
        if ok:
            success_count += 1
            progress["repaired_ids"].append(rec["id"])
            save_progress(progress)
        else:
            fail_count += 1
            progress["failed_ids"][str(rec["id"])] = "Failed during repair"
            save_progress(progress)

    print("\n" + "=" * 65)
    print(f"             KẾT QUẢ SỬA CHỮA TOÀN BỘ TRÊN GOOGLE DRIVE")
    print("=" * 65)
    print(f"Thành công: {success_count} video")
    print(f"Thất bại:   {fail_count} video")
    print(f"Tổng đã sửa tích lũy: {len(progress['repaired_ids'])} video")
    print("=" * 65)

if __name__ == "__main__":
    priority = "--priority" in sys.argv
    today = "--today" in sys.argv or "--morning" in sys.argv
    run_repair_engine(priority_only=priority, today_only=today)
