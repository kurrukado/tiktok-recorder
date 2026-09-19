# -*- coding: utf-8 -*-
"""
staging_queue.py - Hệ thống Hàng đợi Đệm Cloud Staging Queue (Google Drive)
- Gom các video ngắn dưới 1 tiếng (< 50 phút / 3000s) vào tiktok-record/_staging/{user}/ trên Drive.
- Khi tổng thời lượng trong queue đạt >= 50 phút (3000s - chênh lệch 10p so với 60p):
  Tự động ghép nối lossless (FFmpeg concat copy) thành 1 video duy nhất, trích xuất thumbnail,
  tải lên tiktok-record/{user}/ chính thức, đồng bộ Supabase và dọn sạch hàng đợi.
- Nếu streamer không live tiếp trong vòng 24 giờ:
  Tự động xả queue, đóng gói các video đang dở và tải lên Drive chính + Supabase.
"""

import os
import sys
import time
import json
import shutil
import requests
import threading
from datetime import datetime

import gdrive_manager
import recorder_core
import supabase_sync
from auto_h264 import validate_playable_video

TARGET_QUEUE_SECONDS = 3000       # 50 phút (~1 tiếng chênh lệch 10p)
MAX_IDLE_SECONDS = 3600           # 1 tiếng không live mới thì tự động xả queue
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_STAGING_LOCK = threading.RLock()
_USER_STAGING_LOCKS = {}
_USER_LOCKS_MUTEX = threading.Lock()

def _get_user_staging_lock(user: str) -> threading.RLock:
    user = (user or "").strip().replace("@", "").lower()
    with _USER_LOCKS_MUTEX:
        if user not in _USER_STAGING_LOCKS:
            _USER_STAGING_LOCKS[user] = threading.RLock()
        return _USER_STAGING_LOCKS[user]

def _log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [StagingQueue] {msg}", flush=True)

def get_or_create_staging_folder(user: str, access_token: str = None) -> tuple:
    """
    Tìm hoặc tạo thư mục tiktok-record/_staging/<user> trên Google Drive.
    Trả về (user_staging_folder_id, root_staging_folder_id).
    """
    if not access_token:
        access_token = gdrive_manager.get_access_token()
    if not access_token:
        raise ValueError("Chưa có Access Token Google Drive!")

    user = user.strip().replace("@", "").lower()
    root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=access_token)
    staging_root_id = gdrive_manager.find_or_create_folder("_staging", parent_id=root_id, access_token=access_token)
    user_staging_id = gdrive_manager.find_or_create_folder(user, parent_id=staging_root_id, access_token=access_token)
    return user_staging_id, staging_root_id

def get_staging_manifest(user: str, user_staging_id: str, access_token: str = None) -> dict:
    """
    Đọc file staging_manifest.json trong thư mục _staging/<user> trên Drive.
    Ném ngoại lệ nếu gặp lỗi mạng/API để tránh ghi đè làm mất dữ liệu manifest cũ.
    """
    if not access_token:
        access_token = gdrive_manager.get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}
    q = f"name = 'staging_manifest.json' and '{user_staging_id}' in parents and trashed = false"
    url = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q)}&fields=files(id,name)"
    res = None
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            files = res.json().get("files", [])
            if files:
                f_id = files[0]["id"]
                down_url = f"https://www.googleapis.com/drive/v3/files/{f_id}?alt=media"
                d_res = None
                try:
                    d_res = requests.get(down_url, headers=headers, timeout=10)
                    if d_res.status_code == 200:
                        data = d_res.json()
                        data["manifest_file_id"] = f_id
                        return data
                    else:
                        raise IOError(f"Lỗi tải staging_manifest.json ({d_res.status_code}): {d_res.text}")
                finally:
                    if d_res and hasattr(d_res, "close"):
                        d_res.close()
            else:
                # ponytail: Tự động gom các video phân đoạn mồ côi nếu manifest bị mất/desync
                recovered_segments = []
                q_mp4 = f"'{user_staging_id}' in parents and mimeType = 'video/mp4' and trashed = false"
                url_mp4 = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q_mp4)}&fields=files(id,name,size)&orderBy=createdTime"
                mp4_res = None
                try:
                    mp4_res = requests.get(url_mp4, headers=headers, timeout=10)
                    if mp4_res.status_code == 200:
                        for f in mp4_res.json().get("files", []):
                            sz = int(f.get("size", 0))
                            if sz >= 250000:
                                recovered_segments.append({
                                    "file_id": f["id"],
                                    "filename": f["name"],
                                    "duration": 600.0,
                                    "size_bytes": sz,
                                    "created_at": int(time.time()),
                                    "local_path": None
                                })
                except Exception:
                    pass
                finally:
                    if mp4_res and hasattr(mp4_res, "close"):
                        mp4_res.close()

                return {
                    "user": user,
                    "segments": recovered_segments,
                    "last_live_time": int(time.time()),
                    "manifest_file_id": None
                }
        else:
            raise IOError(f"Lỗi truy vấn staging_manifest.json ({res.status_code}): {res.text}")
    except Exception as e:
        _log(f"Lỗi đọc manifest của @{user}: {e}")
        raise
    finally:
        if res and hasattr(res, "close"):
            res.close()

def save_staging_manifest(user: str, user_staging_id: str, manifest_data: dict, access_token: str = None) -> bool:
    """
    Ghi / Cập nhật file staging_manifest.json trong thư mục _staging/<user> trên Drive.
    """
    if not access_token:
        access_token = gdrive_manager.get_access_token()
    headers = {"Authorization": f"Bearer {access_token}"}
    manifest_file_id = manifest_data.get("manifest_file_id")

    payload = {
        "user": user,
        "segments": manifest_data.get("segments", []),
        "last_live_time": manifest_data.get("last_live_time", int(time.time())),
        "updated_at": int(time.time())
    }
    content_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

    try:
        if manifest_file_id:
            up_url = f"https://www.googleapis.com/upload/drive/v3/files/{manifest_file_id}?uploadType=media"
            res = None
            try:
                res = requests.patch(up_url, headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}, data=content_bytes, timeout=10)
                if res.status_code == 200:
                    return True
                if res.status_code != 404:
                    return False
            finally:
                if res and hasattr(res, "close"):
                    res.close()

        # Fallback: Tạo mới file manifest nếu chưa có hoặc file cũ bị xóa (404)
        meta = {"name": "staging_manifest.json", "parents": [user_staging_id]}
        files_data = {
            "data": ("metadata", json.dumps(meta), "application/json; charset=UTF-8"),
            "file": ("staging_manifest.json", content_bytes, "application/json")
        }
        create_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
        res_post = None
        try:
            res_post = requests.post(create_url, headers={"Authorization": f"Bearer {access_token}"}, files=files_data, timeout=10)
            return res_post.status_code in [200, 201]
        finally:
            if res_post and hasattr(res_post, "close"):
                res_post.close()
    except Exception as e:
        _log(f"Lỗi lưu manifest của @{user}: {e}")
        return False

def add_to_staging_queue(user: str, local_file_path: str, duration_seconds: float, access_token: str = None) -> dict:
    """
    Đẩy một video ngắn (< 50 phút) vào Hàng đợi Đệm Cloud Staging Queue của streamer.
    Nếu tổng thời lượng sau khi thêm >= 50 phút (TARGET_QUEUE_SECONDS), tự động kích hoạt đóng gói và tải lên!
    """
    user = user.strip().replace("@", "").lower()
    if not os.path.exists(local_file_path):
        return {"status": "error", "message": "File video cục bộ không tồn tại"}

    file_size = os.path.getsize(local_file_path)
    if file_size < 250 * 1024:
        return {"status": "rejected", "message": f"Từ chối file rác < 250KB ({file_size} bytes)"}

    lock = _get_user_staging_lock(user)
    if not lock.acquire(timeout=30.0):
        return {"status": "error", "message": f"Hàng đợi staging của @{user} đang bận (timeout 30s)"}
    try:
        if not access_token:
            access_token = gdrive_manager.get_access_token()
        if not access_token:
            return {"status": "error", "message": "Chưa có Access Token Google Drive"}

        user_staging_id, _ = get_or_create_staging_folder(user, access_token=access_token)
        filename = os.path.basename(local_file_path)

        _log(f"[@{user}] Đang tải phân đoạn ngắn ({duration_seconds:.1f}s) vào Staging Queue trên Google Drive...")
        ok = gdrive_manager.upload_file_to_drive(local_file_path, user_staging_id, access_token=access_token)
        if not ok:
            return {"status": "error", "message": "Upload phân đoạn vào Staging Queue thất bại"}

        file_id = ok if isinstance(ok, str) else None
        if not file_id:
            headers = {"Authorization": f"Bearer {access_token}"}
            safe_name = filename.replace("'", "\'")
            q_file = f"name = '{safe_name}' and '{user_staging_id}' in parents and trashed = false"
            url_f = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q_file)}&fields=files(id,size)"
            f_res = None
            try:
                f_res = requests.get(url_f, headers=headers, timeout=10)
                if f_res.status_code == 200:
                    fl = f_res.json().get("files", [])
                    if fl:
                        file_id = fl[0]["id"]
            finally:
                if f_res and hasattr(f_res, "close"):
                    f_res.close()

        manifest = get_staging_manifest(user, user_staging_id, access_token=access_token)
        segments = manifest.get("segments", [])

        # Thêm phân đoạn mới (chống trùng filename)
        segments = [s for s in segments if s.get("filename") != filename]
        segments.append({
            "file_id": file_id,
            "filename": filename,
            "duration": float(duration_seconds),
            "size_bytes": file_size,
            "created_at": int(time.time()),
            "local_path": local_file_path
        })
        manifest["segments"] = segments
        manifest["last_live_time"] = int(time.time())

        save_staging_manifest(user, user_staging_id, manifest, access_token=access_token)

        total_duration = sum(s.get("duration", 0.0) for s in segments)
        _log(f"[@{user}] Đã lưu vào Queue Drive. Hiện có {len(segments)} đoạn, tổng: {total_duration/60:.1f} phút / {TARGET_QUEUE_SECONDS/60:.0f} phút mục tiêu.")

        # Kiểm tra xem đã đủ 50-70 phút (>= 3000s) để đóng gói hay chưa
        if total_duration >= TARGET_QUEUE_SECONDS:
            _log(f"🎉 [@{user}] Hàng đợi Staging đã tích lũy đủ {total_duration/60:.1f} phút! Bắt đầu ghép nối và tải lên thư mục chính...")
            pkg_res = package_and_publish_queue(user, access_token=access_token)
            return {"status": "packaged", "details": pkg_res}

        return {
            "status": "queued",
            "segments_count": len(segments),
            "total_duration": total_duration,
            "target_duration": TARGET_QUEUE_SECONDS
        }
    except Exception as e:
        _log(f"Lỗi thêm vào Staging Queue của @{user}: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        lock.release()

def package_and_publish_queue(user: str, access_token: str = None) -> dict:
    """
    Gom tất cả các video trong Staging Queue của streamer:
    1. Tải về máy nếu chưa có sẵn.
    2. Dùng FFmpeg Concat Demuxer ghép thành 1 video duy nhất.
    3. Trích xuất thumbnail tại 50% thời lượng.
    4. Tải video tổng và thumbnail lên tiktok-record/<user>/ chính thức.
    5. Đồng bộ Supabase Storage & Database.
    6. Xóa toàn bộ file phân đoạn và manifest trong _staging/<user>/ trên Drive và dọn file local.
    """
    user = (user or "").strip().replace("@", "").lower()
    lock = _get_user_staging_lock(user)
    if not lock.acquire(timeout=30.0):
        return {"ok": False, "error": f"Hàng đợi staging của @{user} đang bận (timeout 30s)"}
    try:
        if not access_token:
            access_token = gdrive_manager.get_access_token()
        if not access_token:
            return {"ok": False, "error": "Chưa có Access Token Google Drive"}

        try:
            user_staging_id, _ = get_or_create_staging_folder(user, access_token=access_token)
            manifest = get_staging_manifest(user, user_staging_id, access_token=access_token)
            segments = manifest.get("segments", [])
            if not segments:
                return {"ok": True, "message": "Queue trống, không có phân đoạn nào"}

            merge_dir = os.path.join(BASE_DIR, user, "_staging_merge")
            os.makedirs(merge_dir, exist_ok=True)

            local_segment_files = []
            merged_file_ids = []
            merged_filenames = set()
            final_output_file = None
            thumb_f = None
            upload_succeeded = False
            try:
                for seg in segments:
                    fn = seg["filename"]
                    f_id = seg.get("file_id")
                    cached_local = seg.get("local_path")

                    target_local = os.path.join(merge_dir, fn)
                    # Ưu tiên lấy file local có sẵn lúc thu
                    if cached_local and os.path.exists(cached_local) and os.path.getsize(cached_local) > 250000:
                        try:
                            if os.path.abspath(cached_local) != os.path.abspath(target_local):
                                shutil.copy2(cached_local, target_local)
                        except Exception:
                            target_local = cached_local
                    elif not os.path.exists(target_local) or os.path.getsize(target_local) < 250000:
                        if f_id:
                            _log(f"[@{user}] Đang tải phân đoạn {fn} từ Google Drive về để ghép nối...")
                            ok_dl = gdrive_manager.download_file_from_drive(f_id, target_local, access_token=access_token)
                            if not ok_dl:
                                _log(f"[!] Không thể tải phân đoạn {fn} từ Drive. Bỏ qua phân đoạn này.")
                                continue

                    if os.path.exists(target_local) and os.path.getsize(target_local) > 250000:
                        local_segment_files.append(target_local)
                        merged_filenames.add(fn)
                        if f_id:
                            merged_file_ids.append(f_id)

                if not local_segment_files:
                    return {"ok": False, "error": "Không có phân đoạn hợp lệ để ghép nối"}

                # Xác định tên file đầu ra theo thời gian của phân đoạn đầu tiên
                first_seg_fn = os.path.basename(local_segment_files[0])
                first_base = os.path.splitext(first_seg_fn)[0].replace("_part1", "").replace("_seg1", "")
                final_output_file = os.path.join(os.path.join(BASE_DIR, user), f"{first_base}_full.mp4")

                # Ghép nối
                if len(local_segment_files) == 1:
                    shutil.copy2(local_segment_files[0], final_output_file)
                else:
                    from cloud_daemon import concat_mp4_segments
                    _log(f"[@{user}] Đang ghép nối lossless {len(local_segment_files)} phân đoạn thành 1 video duy nhất...")
                    concat_res = concat_mp4_segments(local_segment_files, final_output_file)
                    if not concat_res or not os.path.exists(concat_res):
                        return {"ok": False, "error": "Lỗi ghép nối FFmpeg concat demuxer"}
                    final_output_file = concat_res

                is_valid, v_reason, total_dur = validate_playable_video(final_output_file, min_duration=5.0, min_size_bytes=250000)
                if not is_valid:
                    if final_output_file and os.path.exists(final_output_file):
                        try:
                            os.remove(final_output_file)
                        except Exception:
                            pass
                    return {"ok": False, "error": f"Video ghép không hợp lệ: {v_reason}"}

                _log(f"[✓] [@{user}] Đã ghép nối hoàn tất: {os.path.basename(final_output_file)} ({total_dur/60:.1f} phút)")

                # 1. Trích xuất thumbnail tại chính giữa video (50% thời lượng)
                try:
                    from auto_h264 import extract_middle_thumbnail
                    thumb_f = extract_middle_thumbnail(final_output_file)
                except Exception as th_err:
                    _log(f"Lỗi tạo thumbnail: {th_err}")

                # 2. Tải lên thư mục chính tiktok-record/<user>/ trên Google Drive
                root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=access_token)
                main_user_folder_id = gdrive_manager.find_or_create_folder(user, parent_id=root_id, access_token=access_token)

                _log(f"[@{user}] Đang tải video hoàn chỉnh lên Google Drive chính thức: tiktok-record/{user}/...")
                up_ok = gdrive_manager.upload_file_to_drive(final_output_file, main_user_folder_id, access_token=access_token)
                if not up_ok:
                    return {"ok": False, "error": "Không thể upload video hoàn chỉnh lên Google Drive chính"}
                upload_succeeded = True

                if thumb_f and os.path.exists(thumb_f):
                    gdrive_manager.upload_file_to_drive(thumb_f, main_user_folder_id, access_token=access_token)

                # 3. Đồng bộ Supabase kèm drive_file_id
                sz_bytes = os.path.getsize(final_output_file)
                fn_basename = os.path.basename(final_output_file)
                drive_file_id = up_ok if isinstance(up_ok, str) else None
                try:
                    supabase_sync.sync_recording_to_supabase(
                        user=user,
                        filename=fn_basename,
                        size_bytes=sz_bytes,
                        thumb_source=thumb_f,
                        drive_file_id=drive_file_id,
                        source="staging_queue"
                    )
                    _log(f"[✓] [@{user}] Đã đồng bộ video hoàn chỉnh lên Supabase Database & Storage!")
                except Exception as sb_err:
                    _log(f"[!] Lỗi đồng bộ Supabase: {sb_err}")

                # 4. Dọn sạch các phân đoạn đã được ghép nối trong Staging Queue trên Google Drive
                _log(f"[@{user}] Đang dọn dẹp các phân đoạn tạm trong Staging Queue trên Google Drive...")
                for f_id in merged_file_ids:
                    gdrive_manager.delete_file_drive(f_id, access_token=access_token)

                remaining_segs = [s for s in segments if s.get("filename") not in merged_filenames]
                if remaining_segs:
                    manifest["segments"] = remaining_segs
                    save_staging_manifest(user, user_staging_id, manifest, access_token=access_token)
                else:
                    if manifest.get("manifest_file_id"):
                        gdrive_manager.delete_file_drive(manifest["manifest_file_id"], access_token=access_token)

                _log(f"✨ [@{user}] Hoàn tất trọn vẹn chu trình Staging Queue -> Video chính thức!")
                return {
                    "ok": True,
                    "filename": fn_basename,
                    "duration_minutes": round(total_dur / 60.0, 1),
                    "size_mb": round(sz_bytes / (1024 * 1024), 2)
                }
            finally:
                try:
                    shutil.rmtree(merge_dir, ignore_errors=True)
                    if upload_succeeded:
                        for seg in segments:
                            if seg.get("filename") not in merged_filenames:
                                continue
                            lp = seg.get("local_path")
                            if lp and os.path.exists(lp):
                                try:
                                    os.remove(lp)
                                except Exception:
                                    pass
                        if final_output_file and os.path.exists(final_output_file):
                            try:
                                os.remove(final_output_file)
                            except Exception:
                                pass
                        if thumb_f and os.path.exists(thumb_f):
                            try:
                                os.remove(thumb_f)
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception as e:
            _log(f"Lỗi đóng gói Staging Queue của @{user}: {e}")
            return {"ok": False, "error": str(e)}
    finally:
        lock.release()

def check_and_flush_idle_queues(access_token: str = None) -> list:
    """
    Quét toàn bộ hàng đợi Staging trên Google Drive:
    Nếu một streamer không live trong vòng 24 tiếng (MAX_IDLE_SECONDS) và có video dở dang trong Queue:
    Tự động đóng gói và xuất bản lên Drive chính thức + Supabase!
    """
    flushed_users = []
    if not access_token:
        access_token = gdrive_manager.get_access_token()
    if not access_token:
        return flushed_users

    try:
        root_id = gdrive_manager.find_or_create_folder("tiktok-record", access_token=access_token)
        staging_root_id = gdrive_manager.find_or_create_folder("_staging", parent_id=root_id, access_token=access_token)

        # Lấy tất cả các thư mục con trong _staging
        headers = {"Authorization": f"Bearer {access_token}"}
        q = f"'{staging_root_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        url = f"https://www.googleapis.com/drive/v3/files?q={requests.utils.quote(q)}&fields=files(id,name)"
        res = None
        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code != 200:
                return flushed_users
            folders = res.json().get("files", [])
        finally:
            if res and hasattr(res, "close"):
                res.close()

        now_ts = int(time.time())

        for folder in folders:
            user = folder["name"].strip().lower()
            u_staging_id = folder["id"]

            manifest = get_staging_manifest(user, u_staging_id, access_token=access_token)
            segments = manifest.get("segments", [])
            if not segments:
                continue

            last_live_time = manifest.get("last_live_time", now_ts)
            idle_seconds = now_ts - last_live_time

            # Nếu đã quá 24 tiếng không có phiên live mới
            if idle_seconds >= MAX_IDLE_SECONDS:
                # Kiểm tra lại xem streamer hiện có đang online không
                is_live, _ = recorder_core.check_live_status(user)
                if not is_live:
                    _log(f"⏰ [@{user}] Đã quá {idle_seconds // 3600} tiếng không live mới. Tự động xả Staging Queue ({len(segments)} phân đoạn) lên Google Drive chính thức...")
                    res_pkg = package_and_publish_queue(user, access_token=access_token)
                    if res_pkg.get("ok"):
                        flushed_users.append(user)
                else:
                    _log(f"⏩ [@{user}] Quá 24h nhưng streamer ĐANG LIVE trở lại. Giữ lại Queue để tiếp tục tích lũy.")
    except Exception as e:
        _log(f"Lỗi quét và xả idle queues: {e}")

    return flushed_users
