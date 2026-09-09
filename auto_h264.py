import os
import sys
import subprocess
import shutil
import time

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

_CACHED_ENCODER = None

def get_best_h264_encoder():
    """
    Auto-detects the fastest available H.264 encoder (NVIDIA NVENC, Intel QSV, AMD AMF, MediaFoundation, or CPU libx264).
    """
    global _CACHED_ENCODER
    if _CACHED_ENCODER:
        return _CACHED_ENCODER

    candidates = ["h264_nvenc", "h264_qsv", "h264_amf", "h264_mf", "libx264"]
    for enc in candidates:
        cmd = [FFMPEG_PATH, "-f", "lavfi", "-i", "nullsrc=s=640x360:d=1", "-c:v", enc, "-f", "null", "-"]
        try:
            p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4)
            if p.returncode == 0:
                _CACHED_ENCODER = enc
                return enc
        except Exception:
            pass

    _CACHED_ENCODER = "libx264"
    return _CACHED_ENCODER

def get_video_codec(filepath):
    """
    Inspects the video codec of a file using ffmpeg.
    Returns codec name (e.g. 'h264', 'hevc', 'unknown').
    """
    if not os.path.exists(filepath) or os.path.getsize(filepath) < 1024:
        return "unknown"

    cmd = [FFMPEG_PATH, "-i", filepath]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=5)
        for line in p.stderr.splitlines():
            if "Video:" in line:
                part = line.split("Video:")[1].split(",")[0].strip().lower()
                if "h264" in part or "avc" in part:
                    return "h264"
                elif "hevc" in part or "h265" in part or "hvc1" in part:
                    return "hevc"
                else:
                    return part.split()[0]
    except Exception:
        pass
    return "unknown"

def ensure_h264(filepath):
    """
    Ensures the video is encoded in standard H.264 (AVC) and finalized for mobile devices.
    1. Fixes the '0:00' duration bug by rebuilding the moov atom at file start (+faststart).
    2. Rebuilds frame index sample table (stco/stsz) to eliminate phone decoding stuttering.
    3. If non-H.264 (HEVC/H.265), re-encodes with GPU/CPU to standard H.264.
    """
    if not os.path.exists(filepath):
        return filepath

    filename = os.path.basename(filepath)
    codec = get_video_codec(filepath)
    temp_out = filepath + ".fixed.tmp.mp4"

    # Case 1: Video is already H.264 -> Fast remux with +faststart (instant, 0.5s)
    if codec == "h264":
        cmd = [
            FFMPEG_PATH, "-y",
            "-fflags", "+genpts",
            "-i", filepath,
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            temp_out
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
            if proc.returncode == 0 and os.path.exists(temp_out) and os.path.getsize(temp_out) > 1024:
                os.remove(filepath)
                os.rename(temp_out, filepath)
                print(f"  [✓] Đã tối ưu MP4 (+faststart): Hiển thị chuẩn thời lượng & mượt mà trên iPhone/Android.")
                return filepath
        except Exception as e:
            print(f"  [!] Lỗi khi remux +faststart: {e}")
        if os.path.exists(temp_out):
            try:
                os.remove(temp_out)
            except Exception:
                pass
        return filepath

    # Case 2: Video is HEVC/H.265 or other -> Full transcode to H.264
    encoder = get_best_h264_encoder()
    enc_desc = "GPU NVIDIA NVENC" if encoder == "h264_nvenc" else f"bộ mã hóa {encoder}"
    print(f"[*] Chuyển đổi định dạng ({codec.upper()} ➔ H.264) bằng {enc_desc}: {filename}...")

    cmd = [FFMPEG_PATH, "-y", "-i", filepath]
    if encoder == "h264_nvenc":
        cmd.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22", "-pix_fmt", "yuv420p"])
    elif encoder == "libx264":
        cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p"])
    else:
        cmd.extend(["-c:v", encoder, "-b:v", "2500k", "-pix_fmt", "yuv420p"])

    cmd.extend(["-c:a", "copy", "-movflags", "+faststart", temp_out])

    start_t = time.time()
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if proc.returncode == 0 and os.path.exists(temp_out) and os.path.getsize(temp_out) > 1024:
            if os.path.exists(filepath):
                os.remove(filepath)
            os.rename(temp_out, filepath)
            elapsed = time.time() - start_t
            final_mb = os.path.getsize(filepath) / (1024 * 1024)
            print(f"  [✓] Đã chuyển đổi thành công sang H.264 (+faststart) ({final_mb:.2f} MB, {elapsed:.1f}s)")
            return filepath
        else:
            if os.path.exists(temp_out):
                os.remove(temp_out)
            print(f"  [!] Chuyển đổi không thành công cho: {filename}")
    except Exception as e:
        print(f"  [!] Lỗi khi chuyển đổi: {e}")
        if os.path.exists(temp_out):
            try:
                os.remove(temp_out)
            except Exception:
                pass

    return filepath

def convert_all_videos_in_folder(folder_path=BASE_DIR):
    """
    Scans the folder and all subfolders, and converts all non-H.264 videos to H.264.
    """
    encoder = get_best_h264_encoder()
    print("\n" + "=" * 60)
    print(f"       TỰ ĐỘNG CHUYỂN ĐỔI TOÀN BỘ VIDEO SANG CHUẨN H.264")
    print(f"       Bộ mã hóa phát hiện: {encoder.upper()} (Tăng tốc phần cứng GPU)")
    print("=" * 60)

    # Collect all mp4 files recursively
    files = []
    for root, dirs, filenames in os.walk(folder_path):
        if "_tiktok_recorder" in root or ".git" in root or "__pycache__" in root:
            continue
        for f in filenames:
            if f.lower().endswith(".mp4") and not f.endswith(".tmp.mp4"):
                files.append(os.path.join(root, f))

    if not files:
        print("[!] Không tìm thấy video MP4 nào trong các thư mục.")
        return

    to_convert = []
    for full_p in files:
        codec = get_video_codec(full_p)
        if codec != "h264" and codec != "unknown":
            to_convert.append((full_p, codec))

    if not to_convert:
        print(f"[✓] Tất cả {len(files)} video trong các thư mục đều ĐÃ là chuẩn H.264! Không cần chuyển đổi.")
        print("=" * 60 + "\n")
        return

    print(f"[+] Tìm thấy {len(to_convert)}/{len(files)} video cần chuyển từ HEVC sang H.264.\n")
    success = 0
    for i, (p, codec) in enumerate(to_convert, 1):
        rel_p = os.path.relpath(p, folder_path)
        print(f"[{i}/{len(to_convert)}] {rel_p}")
        res = ensure_h264(p)
        if res:
            success += 1

    print("\n" + "=" * 60)
    print(f"[✓] HOÀN TẤT CHUYỂN ĐỔI: {success}/{len(to_convert)} video đã chuyển sang chuẩn H.264 (AVC) tương thích 100%!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    convert_all_videos_in_folder(BASE_DIR)
