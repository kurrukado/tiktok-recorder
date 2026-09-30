import os
import sys
import subprocess
import shutil
import time
import struct
import re

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
    except subprocess.TimeoutExpired:
        # CPU đang bận (đang ghi/ghép video song song) -> probe lại với ngân sách dài hơn
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=15)
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
    out = None
    for attempt_timeout in (8, 20):
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=attempt_timeout)
            out = p.stderr
            break
        except subprocess.TimeoutExpired:
            # CPU/đĩa đang bận (ghi/ghép video song song) -> thử lại với ngân sách dài hơn.
            continue
        except Exception as e:
            # Lỗi hệ thống tạm thời, không phải bằng chứng file hỏng.
            print(f"[auto_h264] Không probe được {os.path.basename(filepath)} (lỗi: {e}). Thử lại...")
            time.sleep(1)

    if out is None:
        # KHÔNG được trả False ở đây: mọi caller đều os.remove() khi validate thất bại.
        # Probe không chạy được = sự cố tài nguyên, không phải file hỏng.
        print(f"[auto_h264] Không chẩn đoán được {os.path.basename(filepath)} (probe timeout). Coi là HỢP LỆ để tránh xóa video đã ghi.")
        return True, "Không chẩn đoán được (probe timeout) - giữ file để tránh xóa nhầm", 0.0

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

    if not check_h264_stream_health(filepath, duration=dur):
        return False, "Bitstream hỏng hoặc lỗi NAL unit (màn hình đen / không thể giải mã hình ảnh)", dur

    return True, "Hợp lệ", dur

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
                if not m_fps:
                    m_fps = re.search(r"(\d+(?:\.\d+)?)\s*tbr", line)
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
    2. Nếu config 'auto_upscale_1080p' tắt -> Bỏ qua để tiết kiệm CPU/băng thông.
    3. Nếu nhỏ hơn 1080p và được bật cấu hình (mặc định BẬT) -> Upscale bằng bộ lọc Lanczos chất lượng cao,
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

    if not config.get("auto_upscale_1080p", True):
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
        cmd.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-rc:v", "vbr", "-cq", "22", "-pix_fmt", "yuv420p"])
    elif encoder == "libx264":
        cmd.extend([
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
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

def has_faststart(filepath):
    try:
        with open(filepath, "rb") as f:
            header = f.read(128 * 1024)
            # Parse MP4 atom headers: [4-byte size][4-byte type]
            pos = 0
            while pos + 8 <= len(header):
                box_size = int.from_bytes(header[pos:pos+4], "big")
                box_type = header[pos+4:pos+8]
                if box_type == b"moov":
                    return True
                if box_type == b"mdat":
                    return False
                if box_size < 8:
                    break
                pos += box_size
    except Exception:
        pass
    return False

# Lỗi "file hỏng triệt để": decoder không chạy được / container sai cấu trúc.
# Chỉ cần xuất hiện ở BẤT KỲ cửa sổ quét nào -> file hỏng.
_FATAL_PATTERNS = (
    "Invalid NAL unit size",
    "Error splitting the input into NAL units",
    "missing picture in access unit",
    "Decoding error",
    "Invalid data found when processing input",
)
# Vỡ macroblock / thiếu tham chiếu: đúng kiểu khung hình xám + khối nhiễu macroblock màu.
# Lẻ tẻ là bình thường trên luồng live (mất gói đã được che) nên KHÔNG được chặn ngay
# một lần xuất hiện, mà chỉ kết luận "hỏng" khi mật độ lỗi trong một cửa sổ quét đạt
# _MB_ERROR_THRESHOLD (xem check_h264_stream_health).
_MB_ERROR_PATTERNS = (
    "error while decoding MB",
    "concealing",
    "reference picture missing",
    "Missing reference picture",
    "mmco: unref short failure",
    "non-existing PPS",
    "non-existing SPS",
    "decode_slice_header error",
)
# Số lỗi MB tối đa trong một cửa sổ quét (giây _SCAN_WINDOW_SECONDS) vẫn coi là lành.
# Cửa sổ ~4s ≈ 100 frame @25fps -> 40 MB hỏng là 40% khung hình, chắc chắn không ổn.
_MB_ERROR_THRESHOLD = 40
_SCAN_WINDOW_SECONDS = 4
# Cửa sổ quét bổ sung (tỉ lệ trên tổng thời lượng). File < 90s chỉ quét cửa sổ đầu.
_EXTRA_SCAN_FRACTIONS = (0.4, 0.75)

def _build_scan_windows(duration):
    """Chọn các mốc -ss cần quét để không bỏ sót lỗi ở giữa/cuối file."""
    windows = [0.0]
    if duration and duration > 90:
        for frac in _EXTRA_SCAN_FRACTIONS:
            t = duration * frac
            if t > 10:
                windows.append(round(t, 2))
    return windows

def check_h264_stream_health(filepath, duration=None):
    if not os.path.exists(filepath) or os.path.getsize(filepath) < 1024:
        return False
    codec = get_video_codec(filepath)
    # "unknown" = không probe được codec (timeout/tài nguyên) -> KHÔNG được kết luận file hỏng.
    # Chỉ loại trừ khi xác định được rõ ràng là codec không phải H.264/HEVC.
    if codec != "unknown" and codec not in ("h264", "hevc"):
        return False
    if codec == "unknown":
        # Thử lại một lần nữa trước khi bỏ qua cánh cửa codec.
        codec = get_video_codec(filepath)
        if codec != "unknown" and codec not in ("h264", "hevc"):
            return False
    # "non monotonically increasing dts" và "co located POCs unavailable" là cảnh báo
    # timing/POC rất phổ biến trên luồng HLS TikTok, KHÔNG đồng nghĩa bitstream hỏng.
    # Chúng từng nằm trong corrupt_patterns và khiến validate_playable_video trả False ->
    # cloud_daemon/api_server os.remove() những video đã ghi thành công.
    windows = _build_scan_windows(duration if duration is not None else get_video_duration(filepath))
    mb_per_window = []
    probed_any = False
    last_err = None
    for ss in windows:
        cmd = [FFMPEG_PATH, "-v", "error"]
        if ss > 0:
            # -ss trước -i = input seek (nhảy đúng keyframe, gần như không tốn giải mã).
            cmd += ["-ss", f"{ss}"]
        cmd += ["-i", filepath, "-map", "0:v:0", "-t", str(_SCAN_WINDOW_SECONDS), "-f", "null", "-"]
        for _attempt in range(2):
            try:
                p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace", timeout=30)
            except Exception as ex:
                # Timeout/Ngoại lệ là sự cố TÀI NGUYÊN (CPU/IO đang bận), không phải bằng chứng file hỏng.
                last_err = ex
                continue
            probed_any = True
            err = p.stderr.strip()
            for pat in _FATAL_PATTERNS:
                if pat in err:
                    print(f"[auto_h264] Bitstream hỏng ở giây {ss:.1f} của {os.path.basename(filepath)} ({pat})")
                    return False
            if p.returncode != 0:
                return False
            mb_per_window.append(sum(err.count(pat) for pat in _MB_ERROR_PATTERNS))
            break
    if not probed_any:
        print(f"[auto_h264] Không chẩn đoán được bitstream {os.path.basename(filepath)} (lỗi: {last_err}). Coi là HỢP LỆ để tránh xóa video đã ghi.")
        return True
    # Mật độ lỗi MB: hỏng ở >=2 cửa sổ (hỏng kéo dài) HOẶC một cửa sổ kinh khủng
    # (>=4x ngưỡng) -> file thực sự hỏng. 1 cửa sổ lẻ thì bỏ qua để không xóa nhầm.
    bad_windows = sum(1 for n in mb_per_window if n >= _MB_ERROR_THRESHOLD)
    catastrophic = any(n >= _MB_ERROR_THRESHOLD * 4 for n in mb_per_window)
    if bad_windows >= 2 or catastrophic:
        print(f"[auto_h264] {os.path.basename(filepath)}: mật độ lỗi macroblock {mb_per_window} vượt ngưỡng -> đánh dấu hỏng.")
        return False
    return True

def _is_avcc_record(s_data):
    """avcC record nhúng trong FLV: version 0x01, lengthSizeMinusOne ở byte[4], cấu hình level ở byte[5]."""
    return (
        isinstance(s_data, (bytes, bytearray))
        and len(s_data) > 7
        and s_data[0] == 0x01
        and (s_data[4] & 0xfc) == 0xfc
        and (s_data[5] & 0xe0) == 0xe0
    )

def _parse_avcc_record(s_data):
    """
    Parses ISO/IEC 14496-15 AVCDecoderConfigurationRecord prepended in FLV streams.
    Returns (clean_offset, sps_list, pps_list, nalu_len_bytes) hoặc None.
    nalu_len_bytes = lengthSizeMinusOne + 1 (1..4) đọc từ byte[4] của record — trước đây
    code mặc định mọi nơi đều là 4 byte, cắt sai ranh giới NAL nếu stream dùng 1/2/3 byte.
    Mọi phép slice đều được kiểm tra biên; SPS/PPS bị cắt cụt sẽ trả về None thay vì
    âm thầm trả NAL không hoàn chỉnh.
    """
    if _is_avcc_record(s_data):
        nalu_len_bytes = (s_data[4] & 0x03) + 1
        ptr = 5
        num_sps = s_data[ptr] & 0x1f
        ptr += 1
        sps_list = []
        for _ in range(num_sps):
            if ptr + 2 > len(s_data):
                return None
            sps_l = int.from_bytes(s_data[ptr:ptr+2], "big")
            ptr += 2
            if sps_l == 0 or ptr + sps_l > len(s_data):
                return None
            sps_list.append(s_data[ptr:ptr+sps_l])
            ptr += sps_l
        if ptr >= len(s_data):
            return None
        num_pps = s_data[ptr]
        ptr += 1
        pps_list = []
        for _ in range(num_pps):
            if ptr + 2 > len(s_data):
                return None
            pps_l = int.from_bytes(s_data[ptr:ptr+2], "big")
            ptr += 2
            if pps_l == 0 or ptr + pps_l > len(s_data):
                return None
            pps_list.append(s_data[ptr:ptr+pps_l])
            ptr += pps_l
        return ptr, sps_list, pps_list, nalu_len_bytes
    return None

def _write_length_prefixed_nals(out_f, data, nalu_len_bytes):
    """
    Ghi dữ liệu NALU theo tiền tố độ dài (mẫu MP4/avcC) ra Annex-B.
    Đọc đúng nalu_len_bytes (1..4) thay vì mặc định 4; cắt đúng ranh giới NAL,
    dừng ngay khi dữ liệu không khớp để không ghi NAL rác vào bitstream.
    """
    n = nalu_len_bytes
    if n not in (1, 2, 3, 4) or not data:
        return
    p = 0
    total = len(data)
    while p + n <= total:
        nal_len = int.from_bytes(data[p:p+n], "big")
        if nal_len == 0 or p + n + nal_len > total:
            break
        out_f.write(b"\x00\x00\x00\x01" + data[p+n:p+n+nal_len])
        p += n + nal_len

def sanitize_mp4_bitstream(input_path, output_path=None):
    """
    Tự động chuẩn hóa bitstream MP4: phát hiện và loại bỏ các header rác FLV AVC Sequence Header (01 64 00 1f...)
    do máy chủ TikTok nhúng vào đầu keyframe, chuyển đổi NAL units sang Annex-B và remux siêu tốc (0% re-encode, giữ nguyên 100% chất lượng).
    """
    if not input_path or not os.path.exists(input_path):
        return None
    if not output_path:
        output_path = input_path + ".sanitized.tmp.mp4"

    raw_h264 = output_path + ".raw.h264"
    try:
        with open(input_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            f.seek(0)
            moov_data = None
            while f.tell() < file_size:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                sz, a_type = struct.unpack(">I4s", hdr)
                # Box type phải là fourcc in được; size âm/dưới 8 sẽ làm seek lùi -> lặp vô hạn.
                if not all(32 <= b < 127 for b in a_type):
                    break
                if sz == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    sz = struct.unpack(">Q", ext)[0]
                    if sz < 16:
                        break
                    cur = f.tell()
                    content_sz = sz - 16
                elif sz == 0:
                    content_sz = file_size - f.tell()
                    cur = f.tell()
                else:
                    if sz < 8:
                        break
                    cur = f.tell()
                    content_sz = sz - 8
                if a_type == b"moov":
                    f.seek(cur)
                    moov_data = f.read(content_sz)
                    break
                f.seek(cur + content_sz)

            if not moov_data:
                return None

            vide_idx = moov_data.find(b"vide")
            if vide_idx == -1:
                return None

            # Lấy global SPS / PPS từ avcC trong moov để đảm bảo demuxer nhận diện dimensions ngay từ Sample 0.
            # Trước đây cắt cửa sổ cố định 96 byte (avcc_idx+4:avcc_idx+100): avcC dài hơn thế thì
            # SPS/PPS bị CỤT âm thầm (Python slice không ném lỗi) -> ghi header toàn cục sai ->
            # decoder cấu hình sai -> khối xám + nhiễu macroblock. Nay đọc toàn bộ và kiểm tra biên;
            # nếu không đọc đủ thì để trống (KHÔNG ghi header sai) và rơi vào đường remux/transcode.
            global_headers = b""
            nalu_len_bytes = 4
            avcc_idx = moov_data.find(b"avcC")
            if avcc_idx != -1:
                avcc = moov_data[avcc_idx+4 : avcc_idx+4+4096]
                if len(avcc) >= 7 and avcc[0] == 0x01:
                    nalu_len_bytes = (avcc[4] & 0x03) + 1  # lengthSizeMinusOne + 1
                    try:
                        _ok = True
                        _p = 6
                        _sps_list = []
                        for _ in range(avcc[5] & 0x1F):
                            if _p + 2 > len(avcc):
                                _ok = False
                                break
                            _l = int.from_bytes(avcc[_p:_p+2], "big")
                            _p += 2
                            if _l == 0 or _p + _l > len(avcc):
                                _ok = False
                                break
                            _sps_list.append(avcc[_p:_p+_l])
                            _p += _l
                        _pps_list = []
                        if _ok and _p < len(avcc):
                            _num_pps = avcc[_p]
                            _p += 1
                            for _ in range(_num_pps):
                                if _p + 2 > len(avcc):
                                    _ok = False
                                    break
                                _l = int.from_bytes(avcc[_p:_p+2], "big")
                                _p += 2
                                if _l == 0 or _p + _l > len(avcc):
                                    _ok = False
                                    break
                                _pps_list.append(avcc[_p:_p+_l])
                                _p += _l
                        if _ok and _sps_list and _pps_list:
                            global_headers = b"".join(b"\x00\x00\x00\x01" + s for s in _sps_list) \
                                + b"".join(b"\x00\x00\x00\x01" + x for x in _pps_list)
                    except Exception:
                        global_headers = b""

            trak_start = moov_data.rfind(b"trak", 0, vide_idx)
            trak_end = moov_data.find(b"trak", vide_idx)
            if trak_end == -1:
                trak_end = len(moov_data)
            v_trak = moov_data[trak_start:trak_end]

            stsz_pos = v_trak.find(b"stsz")
            stco_pos = v_trak.find(b"stco")
            co64_pos = v_trak.find(b"co64")
            stsc_pos = v_trak.find(b"stsc")
            if -1 in (stsz_pos, stsc_pos) or (stco_pos == -1 and co64_pos == -1):
                return None

            _, _, s_count = struct.unpack(">III", v_trak[stsz_pos+4:stsz_pos+16])
            sample_sizes = [struct.unpack(">I", v_trak[stsz_pos+16+i*4:stsz_pos+20+i*4])[0] for i in range(s_count)]

            if stco_pos != -1:
                _, c_count = struct.unpack(">II", v_trak[stco_pos+4:stco_pos+12])
                chunk_offsets = [struct.unpack(">I", v_trak[stco_pos+12+i*4:stco_pos+16+i*4])[0] for i in range(c_count)]
            else:
                _, c_count = struct.unpack(">II", v_trak[co64_pos+4:co64_pos+12])
                chunk_offsets = [struct.unpack(">Q", v_trak[co64_pos+12+i*8:co64_pos+20+i*8])[0] for i in range(c_count)]

            _, sc_count = struct.unpack(">II", v_trak[stsc_pos+4:stsc_pos+12])
            entries = [struct.unpack(">III", v_trak[stsc_pos+12+i*12:stsc_pos+24+i*12]) for i in range(sc_count)]
            chunk_samples = []
            for i in range(sc_count):
                f_chunk, s_per_chunk, _ = entries[i]
                next_f = entries[i+1][0] if i+1 < sc_count else c_count + 1
                for _ in range(f_chunk, next_f):
                    chunk_samples.append(s_per_chunk)

            s_idx = 0
            with open(raw_h264, "wb") as out_f:
                if global_headers:
                    out_f.write(global_headers)
                for c_idx in range(min(c_count, len(chunk_samples))):
                    off = chunk_offsets[c_idx]
                    num_s = chunk_samples[c_idx]
                    f.seek(off)
                    for _ in range(num_s):
                        if s_idx >= len(sample_sizes):
                            break
                        sz = sample_sizes[s_idx]
                        s_idx += 1
                        s_data = f.read(sz)
                        if not s_data:
                            continue
                        if _is_avcc_record(s_data):
                            avcc_res = _parse_avcc_record(s_data)
                            if not avcc_res:
                                # avcC hỏng/cụt: bỏ mẫu này, TUYỆT ĐỐI không ghi rác ra bitstream.
                                continue
                            ptr, sps_list, pps_list, rec_nalu = avcc_res
                            for sps in sps_list:
                                out_f.write(b"\x00\x00\x00\x01" + sps)
                            for pps in pps_list:
                                out_f.write(b"\x00\x00\x00\x01" + pps)
                            clean = s_data[ptr:]
                            # Một số máy chủ nhúng thêm 1 tiền tố độ dài rác ở đầu sample:
                            # nếu byte đầu không phải NAL header hợp lệ mà byte sau đó có thì bỏ nó đi.
                            if len(clean) > rec_nalu * 2:
                                t0 = clean[rec_nalu] & 0x1f
                                if t0 not in (1, 5, 6, 7, 8):
                                    t1 = clean[rec_nalu * 2] & 0x1f
                                    if t1 in (1, 5, 6, 7, 8):
                                        _l1 = int.from_bytes(clean[rec_nalu:rec_nalu * 2], "big")
                                        if _l1 and rec_nalu * 2 + _l1 <= len(clean):
                                            clean = clean[rec_nalu:]
                            _write_length_prefixed_nals(out_f, clean, rec_nalu)
                        else:
                            _write_length_prefixed_nals(out_f, s_data, nalu_len_bytes)

        cmd = [
            FFMPEG_PATH, "-y",
            "-fflags", "+genpts",
            "-i", raw_h264,
            "-i", input_path,
            "-map", "0:v:0",
            "-map", "1:a:0?",
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_path
        ]
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if p.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            return output_path
    except Exception:
        pass
    finally:
        if os.path.exists(raw_h264):
            try: os.remove(raw_h264)
            except Exception: pass
    if os.path.exists(output_path):
        try: os.remove(output_path)
        except Exception: pass
    return None

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

    # Sức khỏe BITSTREAM của video là điều kiện để bỏ qua remux — KHÔNG phụ thuộc audio codec.
    # Trước đây thêm "audio_codec in (aac,none)" vào đây nên file H.264 + audio mp3/opus
    # bị coi là unhealthy -> rơi vào sanitize_mp4_bitstream (tự viết lại bitstream thủ công).
    # Nay: video khỏe + audio aac/none + đã faststart -> dùng ngay; video khỏe nhưng
    # audio khác -> vào Case 1 để remux (giữ nguyên video, chỉ transcode audio).
    is_healthy = False
    file_dur = None
    if filepath.lower().endswith(".mp4") and codec == "h264":
        file_dur = get_video_duration(filepath)
        is_healthy = check_h264_stream_health(filepath, duration=file_dur)
        if is_healthy and audio_codec in ("aac", "none") and has_faststart(filepath):
            return filepath

    # Case 1: Video is already H.264 and bitstream is healthy -> Fast remux with +faststart, strip all subtitles (-sn -dn)
    if codec == "h264" and is_healthy:
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

    # Case 1.5: Video is H.264 but bitstream has NAL unit size errors -> Fast in-stream bitstream sanitization
    if codec == "h264" and not is_healthy:
        sanitized = sanitize_mp4_bitstream(filepath, temp_out)
        if sanitized and os.path.exists(sanitized) and check_h264_stream_health(sanitized, duration=file_dur):
            if target_path == filepath:
                os.replace(sanitized, target_path)
            else:
                os.replace(sanitized, target_path)
                if os.path.exists(filepath):
                    try: os.remove(filepath)
                    except Exception: pass
            print(f"  [✓] Đã làm sạch bitstream NAL Unit & chuẩn hóa H.264 (+faststart): Tương thích 100% mọi thiết bị.")
            return target_path

    # Case 2: Video is HEVC/H.265 or other -> Full transcode to standard H.264 (AVC)
    encoder = get_best_h264_encoder()
    enc_desc = "GPU NVIDIA NVENC" if encoder == "h264_nvenc" else f"bộ mã hóa {encoder}"
    print(f"[*] Chuyển đổi định dạng ({codec.upper()} ➔ H.264) bằng {enc_desc}: {filename} (loại bỏ toàn bộ phụ đề, chuẩn hóa YUV420p)...")

    def build_transcode_cmd(selected_encoder):
        c = [
            FFMPEG_PATH, "-y",
            "-err_detect", "ignore_err",
            "-fflags", "+genpts+discardcorrupt",
            "-i", filepath,
            "-map", "0:v:0",
            "-map", "0:a:0?",
        ]
        if selected_encoder == "h264_nvenc":
            c.extend(["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22", "-pix_fmt", "yuv420p"])
        elif selected_encoder == "libx264":
            c.extend([
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "22",
                "-threads", "1",
                "-x264-params", "rc-lookahead=10:ref=1:bframes=0:sync-lookahead=0",
                "-maxrate", "4000k",
                "-bufsize", "3000k",
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
            valid_vid, reason_vid, _ = validate_playable_video(temp_out)
            if not valid_vid:
                print(f"  [!] Video transcode không hợp lệ ({reason_vid}). Giữ nguyên file gốc.")
                if os.path.exists(temp_out):
                    try: os.remove(temp_out)
                    except Exception: pass
                return filepath
            try:
                os.replace(temp_out, target_path)
                if target_path != filepath and os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                elapsed = time.time() - start_t
                final_mb = os.path.getsize(target_path) / (1024 * 1024) if os.path.exists(target_path) else 0
                print(f"  [✓] Đã chuyển đổi thành công sang H.264 MP4 (+faststart, không phụ đề) ({final_mb:.2f} MB, {elapsed:.1f}s)")
                return target_path
            except Exception as e:
                # os.replace thất bại (file đang bị khóa / thiếu quyền) -> target_path KHÔNG tồn tại.
                # Trả về target_path sẽ làm caller báo lỗi.FileNotFound và crash cả batch.
                print(f"  [!] Không ghi đè được {os.path.basename(target_path)} ({e}). Giữ nguyên file gốc.")
                if os.path.exists(temp_out):
                    try:
                        os.remove(temp_out)
                    except Exception:
                        pass
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
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=15)
        import re
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr)
        if m:
            hours, mins, secs = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return hours * 3600 + mins * 60 + secs
    except Exception:
        pass
    return None

def _extract_raw_keyframe_thumb(video_path, output_thumb):
    """
    Trích xuất trực tiếp NALU SPS/PPS/IDR từ bảng mẫu MP4 (stsz/stco),
    giải mã tức thì ra JPEG kể cả khi file MP4 chứa raw FLV sequence header (01 64 00 1f...).
    """
    import struct
    try:
        file_size = os.path.getsize(video_path)
        if file_size < 10000:
            return None
        with open(video_path, "rb") as f:
            moov_sample = f.read(min(file_size, 4 * 1024 * 1024))
            stsz_idx = moov_sample.find(b"stsz")
            stco_idx = moov_sample.find(b"stco")
            if stsz_idx == -1 or stco_idx == -1:
                return None
            ver, s_size, s_count = struct.unpack(">III", moov_sample[stsz_idx + 4 : stsz_idx + 16])
            ver, c_count = struct.unpack(">II", moov_sample[stco_idx + 4 : stco_idx + 12])
            
            chunk_offsets = [struct.unpack(">I", moov_sample[stco_idx + 12 + i * 4 : stco_idx + 16 + i * 4])[0] for i in range(min(25, c_count))]
            sample_sizes = [struct.unpack(">I", moov_sample[stsz_idx + 16 + i * 4 : stsz_idx + 20 + i * 4])[0] for i in range(min(25, s_count))]
            
            saved_sps, saved_pps = None, None
            for offset, sz in zip(chunk_offsets, sample_sizes):
                if offset + sz > file_size or sz < 3000:
                    continue
                f.seek(offset)
                chunk_data = f.read(sz)
                
                annexb = bytearray()
                # Pattern 1: Sample begins with FLV AVC Sequence Header (01 64 00 1f ff e1...)
                if chunk_data.startswith(b"\x01") and len(chunk_data) > 40 and chunk_data[4:6] in (b"\xff\xe1", b"\x03\xe1"):
                    try:
                        sps_len = int.from_bytes(chunk_data[6:8], "big")
                        if 10 < sps_len < 100:
                            sps = chunk_data[8 : 8 + sps_len]
                            pps_offset = 8 + sps_len + 1
                            pps_len = int.from_bytes(chunk_data[pps_offset : pps_offset + 2], "big")
                            if 1 < pps_len < 50:
                                pps = chunk_data[pps_offset + 2 : pps_offset + 2 + pps_len]
                                saved_sps, saved_pps = sps, pps
                                payload_offset = pps_offset + 2 + pps_len
                                payload = chunk_data[payload_offset:]

                                # Tự động tìm offset bắt đầu của NAL chain (bỏ qua 4-byte FLV Tag Header nếu có)
                                best_pos = None
                                for skip in (0, 4, 1, 2, 3, 5):
                                    test_pos = skip
                                    has_idr = False
                                    while test_pos + 4 < len(payload):
                                        nal_sz = int.from_bytes(payload[test_pos : test_pos + 4], "big")
                                        if nal_sz == 0 or test_pos + 4 + nal_sz > len(payload):
                                            break
                                        if (payload[test_pos + 4] & 0x1F) == 5:
                                            has_idr = True
                                        test_pos += 4 + nal_sz
                                    if has_idr and test_pos == len(payload):
                                        best_pos = skip
                                        break

                                start_pos = best_pos if best_pos is not None else 0
                                pos = start_pos
                                nal_units = []
                                while pos + 4 < len(payload):
                                    nal_sz = int.from_bytes(payload[pos : pos + 4], "big")
                                    if pos + 4 + nal_sz > len(payload) or nal_sz == 0:
                                        break
                                    nal_units.append(payload[pos + 4 : pos + 4 + nal_sz])
                                    pos += 4 + nal_sz

                                if nal_units:
                                    annexb.extend(b"\x00\x00\x00\x01" + sps)
                                    annexb.extend(b"\x00\x00\x00\x01" + pps)
                                    for nu in nal_units:
                                        annexb.extend(b"\x00\x00\x00\x01" + nu)
                    except Exception:
                        pass

                # Pattern 2: AVCC NAL units directly in chunk
                if not annexb:
                    sps, pps, idr = None, None, None
                    pos = 0
                    while pos + 4 < len(chunk_data):
                        nal_sz = int.from_bytes(chunk_data[pos : pos + 4], "big")
                        if pos + 4 + nal_sz > len(chunk_data) or nal_sz == 0:
                            pos += 1
                            continue
                        nal_type = chunk_data[pos + 4] & 0x1F
                        if nal_type == 7 and not sps:
                            sps = chunk_data[pos + 4 : pos + 4 + nal_sz]
                        elif nal_type == 8 and not pps:
                            pps = chunk_data[pos + 4 : pos + 4 + nal_sz]
                        elif nal_type == 5 and not idr:
                            idr = chunk_data[pos + 4 : pos + 4 + nal_sz]
                        pos += 4 + nal_sz
                    active_sps = sps or saved_sps
                    active_pps = pps or saved_pps
                    if active_sps and active_pps and idr:
                        annexb.extend(b"\x00\x00\x00\x01" + active_sps + b"\x00\x00\x00\x01" + active_pps + b"\x00\x00\x00\x01" + idr)

                if annexb:
                    raw_tmp = output_thumb + ".raw.h264"
                    with open(raw_tmp, "wb") as rf:
                        rf.write(annexb)
                    try:
                        p = subprocess.run([
                            FFMPEG_PATH, "-y",
                            "-i", raw_tmp,
                            "-vframes", "1",
                            "-strict", "unofficial",
                            "-vf", "format=yuv420p",
                            "-q:v", "2",
                            output_thumb
                        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                        if p.returncode == 0 and os.path.exists(output_thumb) and os.path.getsize(output_thumb) > 500:
                            return output_thumb
                    finally:
                        if os.path.exists(raw_tmp):
                            try: os.remove(raw_tmp)
                            except Exception: pass
    except Exception:
        pass
    return None

def extract_middle_thumbnail(video_path, output_thumb=None):
    """
    Trích xuất khung hình thumbnail chất lượng cao tại chính giữa video (hoặc keyframe hợp lệ gần nhất).
    Tự động xử lý pixel format limited range (smpte170m) và bitstream có lỗi NAL unit.
    """
    if not os.path.exists(video_path):
        return None
    if not output_thumb:
        base, _ = os.path.splitext(video_path)
        output_thumb = base + ".jpg"
    if os.path.exists(output_thumb) and os.path.getsize(output_thumb) > 1024:
        return output_thumb

    duration = get_video_duration(video_path)
    seek_targets = []
    if duration and duration > 1.0:
        # -ss nằm TRƯỚC -i nên đây là INPUT seek (chỉ đọc tới keyframe gần nhất), rất rẻ.
        # Clamp 60s cũ khiến video 1 tiếng lấy thumbnail ở giây 60 (1.7% thời lượng),
        # trái với cam kết "50% thời lượng" của mọi caller.
        seek_targets.append(duration * 0.5)
        if duration > 20:
            seek_targets.append(duration * 0.25)
    seek_targets.extend([5.0, 3.0, 1.0])

    for target_sec in seek_targets:
        mins, secs = divmod(target_sec, 60)
        hours, mins = divmod(mins, 60)
        ts_str = f"{int(hours):02d}:{int(mins):02d}:{secs:06.3f}"

        cmd = [
            FFMPEG_PATH, "-y",
            "-err_detect", "ignore_err",
            "-fflags", "+genpts+discardcorrupt",
            "-ss", ts_str,
            "-i", video_path,
            "-vframes", "1",
            "-strict", "unofficial",
            "-vf", "format=yuvj420p",
            "-q:v", "2",
            output_thumb
        ]
        try:
            p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            if p.returncode == 0 and os.path.exists(output_thumb) and os.path.getsize(output_thumb) > 500:
                return output_thumb
        except Exception:
            pass

    # Fallback 1: Sequential decode past any corrupt initial Sample 0
    try:
        cmd_seq = [
            FFMPEG_PATH, "-y",
            "-err_detect", "ignore_err",
            "-fflags", "+genpts+discardcorrupt",
            "-i", video_path,
            "-ss", "00:00:01.000",
            "-vframes", "1",
            "-strict", "unofficial",
            "-vf", "format=yuvj420p",
            "-q:v", "2",
            output_thumb
        ]
        p = subprocess.run(cmd_seq, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if p.returncode == 0 and os.path.exists(output_thumb) and os.path.getsize(output_thumb) > 500:
            return output_thumb
    except Exception:
        pass

    # Fallback 2: Direct keyframe bitstream extraction
    try:
        thumb_result = _extract_raw_keyframe_thumb(video_path, output_thumb)
        if thumb_result and os.path.exists(thumb_result) and os.path.getsize(thumb_result) > 500:
            return thumb_result
    except Exception:
        pass

    return None

if __name__ == "__main__":
    convert_all_videos_in_folder(BASE_DIR)

