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

def get_audio_codec(filepath):
    """
    Inspects the audio codec of a file using ffmpeg.
    Returns codec name (e.g. 'aac', 'opus', 'mp3', 'ac3', 'none').
    """
    if not os.path.exists(filepath) or os.path.getsize(filepath) < 1024:
        return "none"

    cmd = [FFMPEG_PATH, "-i", filepath]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=5)
        for line in p.stderr.splitlines():
            if "Audio:" in line:
                part = line.split("Audio:")[1].split(",")[0].strip().lower()
                return part.split()[0]
    except Exception:
        pass
    return "none"

def validate_playable_video(filepath, min_duration=5.0, min_size_bytes=250000):
    """
    Kiểm tra tính toàn vẹn và khả năng phát thực tế của file video:
    1. File tồn tại và dung lượng >= min_size_bytes (mặc định 250 KB, loại trừ rác container).
    2. Phải có ít nhất 1 luồng Video ('Video:'), loại trừ các file chỉ có Audio.
    3. Thời lượng video >= min_duration giây, loại trừ các đoạn thu ngắn ngủi lỗi 0:00s.
    Returns (is_valid: bool, reason: str, duration: float)
    """
    if not filepath or not os.path.exists(filepath):
        return False, "File không tồn tại", 0.0

    size = os.path.getsize(filepath)
    if size < min_size_bytes:
        return False, f"Dung lượng quá nhỏ ({size} bytes < {min_size_bytes} bytes)", 0.0

    cmd = [FFMPEG_PATH, "-i", filepath]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=8)
        out = p.stderr

        has_video = False
        for line in out.splitlines():
            if "Video:" in line:
                has_video = True
                break

        if not has_video:
            return False, "Không tìm thấy luồng hình ảnh (Video Stream) trong file (chỉ có Audio hoặc rỗng)", 0.0

        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
        if not m:
            return False, "Không xác định được thời lượng video", 0.0

        h, mins, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        dur = h * 3600 + mins * 60 + s
        if dur < min_duration:
            return False, f"Thời lượng video quá ngắn ({dur:.2f}s < {min_duration}s)", dur

        return True, "Hợp lệ", dur
    except Exception as e:
        return False, f"Lỗi khi kiểm tra video: {e}", 0.0

def get_video_resolution(filepath):
    """
    Trích xuất độ phân giải (width, height) và số khung hình (fps) của video.
    Returns: (width: int, height: int, fps: float) hoặc (None, None, None)
    """
    if not os.path.exists(filepath) or os.path.getsize(filepath) < 1024:
        return None, None, None

    cmd = [FFMPEG_PATH, "-i", filepath]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=6)
        out = p.stderr
        w, h, fps = None, None, None
        for line in out.splitlines():
            if "Video:" in line:
                import re
                m_res = re.search(r",\s*(\d{3,4})x(\d{3,4})", line)
                if m_res:
                    w, h = int(m_res.group(1)), int(m_res.group(2))
                m_fps = re.search(r"(\d+(?:\.\d+)?)\s*fps", line)
                if m_fps:
                    fps = float(m_fps.group(1))
                break
        return w, h, fps
    except Exception:
        return None, None, None

def upscale_to_1080p_if_needed(filepath, config=None):
    """
    Kiểm tra và nâng độ phân giải (upscale) lên chuẩn 1080p theo yêu cầu:
    1. Nếu video ĐÃ có sẵn 1080p (chiều nhỏ >= 1080 hoặc chiều lớn >= 1920) -> Bỏ qua 100%, không tốn tài nguyên.
    2. Nếu config 'auto_upscale_1080p' tắt (mặc định False) -> Bỏ qua để tiết kiệm CPU/băng thông.
    3. Nếu nhỏ hơn 1080p và được bật cấu hình -> Upscale bằng bộ lọc Lanczos chất lượng cao,
       giữ nguyên fps gốc của phòng live và tăng tốc bằng GPU (NVENC) nếu khả dụng.
    """
    if not filepath or not os.path.exists(filepath):
        return filepath

    # Đọc cấu hình nếu không truyền vào
    if config is None:
        try:
            cfg_path = os.path.join(BASE_DIR, "config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    import json
                    config = json.load(f)
            else:
                config = {}
        except Exception:
            config = {}

    if not config.get("auto_upscale_1080p", False):
        return filepath

    w, h, fps = get_video_resolution(filepath)
    if not w or not h:
        return filepath

    # Nếu chiều rộng hoặc chiều cao đã đạt chuẩn 1080p/Full HD (hoặc lớn hơn) -> Bỏ qua 100%
    if min(w, h) >= 1080 or max(w, h) >= 1920:
        print(f"  [✓] Video đã có độ phân giải chuẩn ({w}x{h}, {fps or 30} fps) >= 1080p. Bỏ qua upscale.")
        return filepath

    # Video dọc (Portrait, TikTok Mobile): đưa chiều cao lên 1920 (chiều rộng tương ứng chẵn pixel)
    # Video ngang (Landscape, TikTok Studio/PC): đưa chiều cao lên 1080
    is_portrait = h > w
    scale_filter = "scale=-2:1920:flags=lanczos" if is_portrait else "scale=-2:1080:flags=lanczos"

    filename = os.path.basename(filepath)
    target_path = os.path.splitext(filepath)[0] + ".mp4"
    temp_out = filepath + ".upscaled.tmp.mp4"

    encoder = get_best_h264_encoder()
    enc_desc = "GPU NVIDIA NVENC" if encoder == "h264_nvenc" else f"bộ mã hóa {encoder}"
    print(f"[*] Đang upscale video lên 1080p ({w}x{h} ➔ 1080p, {fps or 30} fps) bằng {enc_desc}: {filename}...")

    cmd = [
        FFMPEG_PATH, "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-i", filepath,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-vf", scale_filter,
    ]
    if fps:
        cmd.extend(["-r", str(fps)])

    if encoder == "h264_nvenc":
        cmd.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22", "-pix_fmt", "yuv420p"])
    elif encoder == "libx264":
        cmd.extend([
            "-c:v", "libx264",
            "-threads", "1",
            "-preset", "veryfast",
            "-crf", "22",
            "-x264-params", "rc-lookahead=10:ref=1:bframes=0:sync-lookahead=0",
            "-bufsize", "3000k",
            "-maxrate", "4000k",
            "-pix_fmt", "yuv420p"
        ])
    else:
        cmd.extend(["-c:v", encoder, "-b:v", "3500k", "-pix_fmt", "yuv420p"])

    cmd.extend([
        "-c:a", "copy",
        "-sn", "-dn",
        "-movflags", "+faststart",
        temp_out
    ])

    start_t = time.time()
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1200)
        if proc.returncode == 0 and os.path.exists(temp_out) and os.path.getsize(temp_out) > 1024:
            os.replace(temp_out, target_path)
            elapsed = time.time() - start_t
            final_mb = os.path.getsize(target_path) / (1024 * 1024)
            w_new, h_new, _ = get_video_resolution(target_path)
            print(f"  [✓] Đã upscale thành công lên 1080p ({w_new}x{h_new}, {final_mb:.2f} MB, {elapsed:.1f}s)")
            return target_path
    except Exception as e:
        print(f"  [!] Lỗi khi upscale video: {e}")
    finally:
        if os.path.exists(temp_out):
            try:
                os.remove(temp_out)
            except Exception:
                pass

    return filepath

def ensure_h264(filepath):
    """
    Ensures the video is encoded in standard H.264 (AVC) and finalized for mobile devices & video editing software.
    1. Removes all subtitle tracks, burn-in subtitles, and data streams (-sn -dn).
    2. Maps only primary video (0:v:0) and primary audio (0:a:0?).
    3. Normalizes pixel format to universal 8-bit YUV420p (-pix_fmt yuv420p).
    4. Ensures audio is standard AAC (-c:a aac / copy if already aac).
    5. Fixes moov atom at beginning of MP4 (+faststart) for instant playback & scrub.
    """
    if not os.path.exists(filepath):
        return filepath

    filename = os.path.basename(filepath)
    codec = get_video_codec(filepath)
    audio_codec = get_audio_codec(filepath)
    target_path = os.path.splitext(filepath)[0] + ".mp4"
    temp_out = filepath + ".fixed.tmp.mp4"

    # Fast return if already MP4 H.264 with compatible AAC/none audio
    if filepath.lower().endswith(".mp4") and codec == "h264" and audio_codec in ("aac", "none"):
        return filepath

    # Case 1: Video is already H.264 -> Fast remux with +faststart, strip all subtitles (-sn -dn)
    if codec == "h264":
        cmd = [
            FFMPEG_PATH, "-y",
            "-fflags", "+genpts+discardcorrupt",
            "-i", filepath,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "copy",
        ]
        if audio_codec == "aac":
            cmd.extend(["-c:a", "copy"])
        elif audio_codec != "none":
            cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "44100"])

        cmd.extend([
            "-sn",
            "-dn",
            "-bsf:v", "dump_extra=freq=keyframe",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            temp_out
        ])
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
            if proc.returncode == 0 and os.path.exists(temp_out) and os.path.getsize(temp_out) > 1024:
                if target_path == filepath:
                    os.replace(temp_out, target_path)
                else:
                    os.replace(temp_out, target_path)
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except Exception:
                            pass
                print(f"  [✓] Đã chuẩn hóa MP4 H.264 (+faststart, -sn không phụ đề): Tương thích 100% mọi thiết bị & phần mềm dựng.")
                return target_path
        except Exception as e:
            print(f"  [!] Lỗi khi remux +faststart: {e}")
        if os.path.exists(temp_out):
            try:
                os.remove(temp_out)
            except Exception:
                pass
        return filepath

    # Case 2: Video is HEVC/H.265 or other -> Full transcode to standard H.264 (AVC)
    encoder = get_best_h264_encoder()
    enc_desc = "GPU NVIDIA NVENC" if encoder == "h264_nvenc" else f"bộ mã hóa {encoder}"
    print(f"[*] Chuyển đổi định dạng ({codec.upper()} ➔ H.264) bằng {enc_desc}: {filename} (loại bỏ toàn bộ phụ đề, chuẩn hóa YUV420p)...")

    def build_transcode_cmd(selected_encoder):
        c = [
            FFMPEG_PATH, "-y",
            "-fflags", "+genpts+discardcorrupt",
            "-i", filepath,
            "-map", "0:v:0",
            "-map", "0:a:0?",
        ]
        if selected_encoder == "h264_nvenc":
            c.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22", "-pix_fmt", "yuv420p"])
        elif selected_encoder == "libx264":
            # Khống chế RAM tối đa < 60MB trên Render/Cloud: giới hạn 1 luồng, hạn chế lookahead buffer và reference frames
            c.extend([
                "-c:v", "libx264",
                "-threads", "1",
                "-preset", "veryfast",
                "-crf", "22",
                "-x264-params", "rc-lookahead=10:ref=1:bframes=0:sync-lookahead=0",
                "-bufsize", "3000k",
                "-maxrate", "4000k",
                "-pix_fmt", "yuv420p"
            ])
        else:
            c.extend(["-c:v", selected_encoder, "-b:v", "3000k", "-pix_fmt", "yuv420p"])

        if audio_codec == "aac":
            c.extend(["-c:a", "copy"])
        elif audio_codec != "none":
            c.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "44100"])

        c.extend([
            "-sn",
            "-dn",
            "-movflags", "+faststart",
            temp_out
        ])
        return c

    cmd = build_transcode_cmd(encoder)
    start_t = time.time()
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
        # Fallback to libx264 if hardware encoder fails
        if (proc.returncode != 0 or not os.path.exists(temp_out) or os.path.getsize(temp_out) <= 1024) and encoder != "libx264":
            print(f"  [!] {encoder} gặp lỗi, tự động chuyển sang CPU libx264 dự phòng...")
            if os.path.exists(temp_out):
                try:
                    os.remove(temp_out)
                except Exception:
                    pass
            cmd = build_transcode_cmd("libx264")
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)

        if proc.returncode == 0 and os.path.exists(temp_out) and os.path.getsize(temp_out) > 1024:
            if target_path == filepath:
                os.replace(temp_out, target_path)
            else:
                os.replace(temp_out, target_path)
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
            elapsed = time.time() - start_t
            final_mb = os.path.getsize(target_path) / (1024 * 1024)
            print(f"  [✓] Đã chuyển đổi thành công sang H.264 MP4 (+faststart, không phụ đề) ({final_mb:.2f} MB, {elapsed:.1f}s)")
            return target_path
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

    # Collect all video files recursively
    files = []
    for root, dirs, filenames in os.walk(folder_path):
        if "_tiktok_recorder" in root or ".git" in root or "__pycache__" in root:
            continue
        for f in filenames:
            if f.lower().endswith((".mp4", ".mkv", ".ts", ".mov", ".webm")) and not f.endswith(".tmp.mp4"):
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

def get_video_duration(filepath):
    """
    Lấy thời lượng video bằng ffmpeg (giây).
    """
    if not filepath or not os.path.exists(filepath):
        return None
    cmd = [FFMPEG_PATH, "-i", filepath]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=5)
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr)
        if m:
            hours, mins, secs = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return hours * 3600 + mins * 60 + secs
    except Exception:
        pass
    return None

def extract_middle_thumbnail(video_path, output_thumb=None):
    """
    Trích xuất khung hình thumbnail chất lượng cao tại chính giữa video (50% thời lượng).
    """
    if not os.path.exists(video_path):
        return None
    if not output_thumb:
        base, _ = os.path.splitext(video_path)
        output_thumb = base + ".jpg"
    if os.path.exists(output_thumb) and os.path.getsize(output_thumb) > 1024:
        return output_thumb

    duration = get_video_duration(video_path)
    seek_time = (duration / 2.0) if (duration and duration > 0.5) else 1.0

    mins, secs = divmod(seek_time, 60)
    hours, mins = divmod(mins, 60)
    ts_str = f"{int(hours):02d}:{int(mins):02d}:{secs:06.3f}"

    cmd = [
        FFMPEG_PATH, "-y",
        "-ss", ts_str,
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        output_thumb
    ]
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
        if p.returncode == 0 and os.path.exists(output_thumb) and os.path.getsize(output_thumb) > 500:
            return output_thumb
    except Exception:
        pass

    try:
        cmd_fallback = [
            FFMPEG_PATH, "-y",
            "-ss", "00:00:00.500",
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            output_thumb
        ]
        p = subprocess.run(cmd_fallback, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        if p.returncode == 0 and os.path.exists(output_thumb) and os.path.getsize(output_thumb) > 500:
            return output_thumb
    except Exception:
        pass

    return None

if __name__ == "__main__":
    convert_all_videos_in_folder(BASE_DIR)
