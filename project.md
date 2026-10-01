# TÀI LIỆU DỰ ÁN — TikTok 24/7 Auto Recorder (Cloud Multi-Stream Engine)

> Tài liệu kỹ thuật toàn diện: **kiến trúc**, **cách hoạt động** và **cách `D:\web-truyen`
> sử dụng các chức năng của tool**.
> Tổng hợp trực tiếp từ mã nguồn `D:\tiktok-recorder` (đã đối chiếu line-by-line)
> và khảo sát `D:\web-truyen` (Next.js 16 "Kuru Hub").

---

## MỤC LỤC

1. [Tổng quan & nguyên tắc hoạt động](#1-tổng-quan--nguyên-tắc-hoạt-động)
2. [Kiến trúc hệ thống](#2-kiến-trúc-hệ-thống)
3. [Cấu trúc dự án](#3-cấu-trúc-dự-án)
4. [Cách hoạt động — Pipeline ghi hình end-to-end](#4-cách-hoạt-động--pipeline-ghi-hình-end-to-end)
5. [Giải thích từng module](#5-giải-thích-từng-module)
6. [REST API đầy đủ (FastAPI :8000)](#6-rest-api-đầy-đủ-fastapi-8000)
7. [Cấu hình & biến môi trường](#7-cấu-hình--biến-môi-trường)
8. [Lưu trữ: Google Drive + Supabase](#8-lưu-trữ-google-drive--supabase)
9. [GitHub Actions — chạy 24/7 miễn phí](#9-github-actions--chạy-247-miễn-phí)
10. [Kiểm thử & vận hành cục bộ](#10-kiểm-thử--vận-hành-cục-bộ)
11. [Cách `D:\web-truyen` sử dụng các tính năng của tool](#11-cách-dweb-truyen-sử-dụng-các-tính-năng-của-tool)
12. [Cảnh báo bảo mật & độ lệch phiên bản](#12-cảnh-báo-bảo-mật--độ-lệch-phiên-bản)

---

## 1. TỔNG QUAN & NGUYÊN TẮC HOẠT ĐỘNG

Tool là **hệ thống ghi hình livestream TikTok tự động, chạy 24/7**, chuẩn H.264/AVC,
không re-encode (copy stream), tự đóng gói phân đoạn, upload Google Drive và đồng
bộ metadata vào Supabase.

Ba chế độ vận hành:

| Chế độ | Lệnh | Mục đích |
| :--- | :--- | :--- |
| **Cloud Runner (GitHub Actions)** | `python -u cloud_daemon.py --duration-minutes 46 --interval 20` | Chạy free 24/7 trên Public Repo, relay tự kích hoạt phiên kế tiếp |
| **Local Daemon (máy cá nhân)** | `run_cloud_daemon.bat` | Tận dụng GPU NVENC, watchdog tự khởi động lại sau 5s |
| **REST API Server** | `python api_server.py` → `uvicorn` `0.0.0.0:8000` | Cung cấp API cho website điều khiển/đọc dữ liệu |

Nguyên tắc cốt lõi:

* **Không re-encode**: `-c:v copy -c:a copy` → CPU thấp, chất lượng nguyên bản.
* **Ưu tiên HLS H.264 (MPEG-TS)**, FLV chỉ là fallback → tránh lỗi NAL unit/FLV header.
* **Chia mốc 1 giờ/part** (`MAX_CHUNK_SECONDS = 3600`) + **Upload trực tiếp Drive** → mỗi segment đủ 1h upload ngay lên Drive, segment cuối upload sau khi xác nhận offline 5 phút.
* **Auto-record khi add user**: thêm streamer qua web → tool tự kiểm tra live → ghi hình ngay lập tức nếu đang live.
* **Giới hạn ghi hình**: 1080p (upscale nếu cần), FPS nguyên gốc, tối đa 1:00:00/segment.
* **Zero-bandwidth playback**: website chỉ redirect/`Range 206` sang Google Edge CDN, không kéo binary qua server trung gian.
* **Self-healing**: watchdog 6 lần/ngày sửa bitstream + bù thumbnail.
* **Chống trùng giữa nhiều runner**: heartbeat trên Drive (TTL 180s) + khóa liên tiến trình `streamer_recording_lock`.

---

## 2. KIẾN TRÚC HỆ THỐNG

```text
                         ┌──────────────────────────────────────────────────┐
                         │              NGUỒN DỮ LIỆU CHUNG               │
                         │  Supabase: tiktok_streamers / tiktok_recordings  │
                         │  Drive:    tiktok-record/streamers.json          │
                         │            tiktok-record/active_recordings.json  │
                         └───────────────┬──────────────────────────────────┘
                                         │ load_monitored_users() cache 15s
        ┌────────────────────────────────┼─────────────────────────────────┐
        │                                │                                 │
┌───────▼────────┐            ┌──────────▼──────────┐            ┌─────────▼─────────┐
│ GitHub Actions │            │  cloud_daemon.py    │            │  api_server.py    │
│ recorder.yml   │───────────►│  (orchestrator,     │            │  (FastAPI :8000,  │
│ cron */50' +   │  46'/lần   │   10 worker thread) │            │   bg_record_worker│
│ relay PAT      │            └──────────┬──────────┘            │   song song)      │
└────────────────┘                       │                       └─────────┬─────────┘
                              1 luồng / streamer                           │
                                         │                                  │
                          ┌──────────────▼───────────────┐                  │
                          │       recorder_core.py       │◄─────────────────┘
                          │ check_live → candidates →    │
                          │ FFmpeg -c copy (mỗi segment) │
                          └──────────────┬───────────────┘
                                          │ file .mp4 thô
                          ┌──────────────▼───────────────┐
                          │         auto_h264.py         │
                          │ sanitize bitstream +faststart│
                          │ upscale 1080p + thumbnail    │
                          └──────────────┬───────────────┘
                                         │
                          ┌──────────────▼───────────────┐
                          │   Upload trực tiếp Drive     │
                          │   MỌI segment (bất kể dur)   │
                          │   tiktok-record/<user>/      │
                          └──────────────┬───────────────┘
                                   ▼
              ┌────────────────────────────────────────────────┐
              │ gdrive_manager.py (resumable 5MB chunk ×5 retry)│
              │ → Google Drive → Google Edge CDN URL            │
              │ supabase_sync.py → tiktok_recordings (upsert)   │
              │              → Storage covers/record-thumbnails │
              └────────────────────────────────────────────────┘
                                   ▲
        ┌──────────────────────────┴───────────────────────────────────┐
        │           D:\web-truyen (Next.js "Kuru Hub") — XEM MỤC 11    │
        │  /record page ─► proxy /api/record/* ─► API :8000 / Render    │
        │  phát video ──► stream-video-id ──► 302 Google Edge CDN       │
        └──────────────────────────────────────────────────────────────┘
```

**Trục dữ liệu**: `Supabase/Drive (danh sách)` → `daemon/API (điều phối)` → `recorder_core (ghi)`
→ `auto_h264 (chuẩn hóa)` → `Drive (upload trực tiếp mọi segment)` + `Supabase (metadata)`
→ `web-truyen (hiển thị/phát)`.

---

## 3. CẤU TRÚC DỰ ÁN

```text
D:\tiktok-recorder\
├── .github/workflows/
│   ├── recorder.yml              # Ghi hình 24/7: cron */50 * * * * + relay qua Actions API
│   └── repair-watchdog.yml       # Tự lành 6 lần/ngày: repair_cloud_videos_engine.py --today
├── cloud_daemon.py               # Bộ điều phối chính (orchestrator, đa luồng 10 streamer)
├── recorder_core.py              # Lõi trích xuất luồng TikTok + FFmpeg recorder
├── auto_h264.py                  # Bitstream sanitizer, validate, upscale 1080p, thumbnail
├── staging_queue.py              # Hàng đợi staging chống mất dữ liệu + ghép file
├── api_server.py                 # FastAPI REST API (port 8000)
├── supabase_sync.py              # Khách PostgREST + Storage của Supabase
├── gdrive_manager.py             # Google Drive API v3 (upload 5MB chunk, phân quyền CDN)
├── gdrive_auth.py                # OAuth2 flow cấp refresh token (localhost:8080)
├── repair_cloud_videos_engine.py # Động cơ sửa chữa video lỗi trên Drive
├── notifier.py                   # Gửi Telegram + sync file lên Drive
├── config_lock.py                # Khóa file ghi config an toàn đa tiến trình
├── tiktok_recorder.py            # CLI menu tương tác (auto/now/status/cookie)
├── config.json                   # Cấu hình runtime (đã .gitignore, CHỨA SECRET thật)
├── config.example.json           # Mẫu cấu hình
├── requirements.txt              # fastapi, uvicorn, pydantic, curl_cffi, requests, psutil, imageio-ffmpeg
├── run_cloud_daemon.bat          # Watchdog local (restart sau 5s)
├── run_recorder.bat              # Chạy CLI tiktok_recorder.py
├── test_audit_suite.py           # Bộ test tổng (67 bài)
├── test_challenger_concurrency.py# Bộ test đối kháng đa luồng (26 bài)
├── README.md / HD_SU_DUNG.md     # Tài liệu sẵn có (tiếng Việt)
└── project.md                    # Tài liệu này
```

Phụ thuộc (`requirements.txt` — 7 gói): `fastapi>=0.110.0`, `uvicorn[standard]>=0.28.0`,
`pydantic>=2.0.0`, `curl_cffi>=0.16.0` (TLS-impersonate Chrome khi gọi TikTok),
`requests>=2.31.0`, `psutil>=5.9.0`, `imageio-ffmpeg>=0.4.9`.
FFmpeg lấy theo thứ tự: `ffmpeg.exe` cạnh source → `shutil.which("ffmpeg")` →
`imageio_ffmpeg.get_ffmpeg_exe()` → `/usr/bin/ffmpeg`.

---

## 4. CÁCH HOẠT ĐỘNG — PIPELINE GHI HÌNH END-TO-END

```text
[Supabase tiktok_streamers] ──┐
[Google Drive streamers.json] ─┼─► load_monitored_users() (cache 15s, gộp 3 nguồn)
[config.json monitored_users] ─┘            │
                                            ▼
                        run_daemon(): vòng lặp poll mỗi ~20s (jitter ±)
                          │  - ThreadPool check_live tối đa 6 luồng song song
                          │  - Tránh trùng: heartbeat trên Drive (TTL 180s)
                          │    + khóa liên tiến trình .locks/<user>.recording.lock
                          │    + cooldown 180s/user (USER_START_COOLDOWN_SECONDS)
                          │  - Giới hạn 10 luồng song song (MAX_CONCURRENT_RECORDERS)
                          ▼
               streamer_recording_worker(user, room_id)   ← 1 thread/streamer
                          │
       ┌──────────────────┴─────────────────────┐
       │ 1. check_live_details → VIP Sub-Only?  │
       │ 2. discover_new_streamers (PK/Co-host) │
       │ 3. get_stream_candidates → HLS → FLV   │
       │ 4. record_stream_ffmpeg (copy, -t 1h)  │
       │ 5. validate (≥5s, ≥250KB), thử ≤4 URL │
       └──────────────────┬─────────────────────┘
                          ▼
        concat segments → auto_h264: ensure_h264 + sanitize + faststart + upscale 1080p
                          ▼
        Upload trực tiếp lên Google Drive + Supabase cho MỌI segment
        (đủ 1h → upload ngay + ghi tiếp, cuối buổi → upload sau 5p offline)
                                             ▼
        thumbnail giữa video (50%)
        → upload tiktok-record/<user>/<user>_YYYY-MM-DD_HH-MM-SS.mp4 + .jpg
                                             ▼
        supabase_sync.sync_recording_to_supabase()  +  upload_thumbnail_to_supabase()
        (bucket covers/record-thumbnails/<user>/…, upsert on_conflict=filename)
                                             ▼
        Xóa file tạm local
```

Chi tiết các chặng quan trọng:

1. **Nạp danh sách** — `cloud_daemon.py:58 load_monitored_users()`: mỗi 15s đọc Drive
   (`streamers.json`) **và** Supabase; gộp cache; nếu 1 nguồn lỗi thì **giữ cache cũ**
   (không xóa nhầm streamer → không rmtree thư mục local).
2. **Phát hiện live** — `recorder_core.py:182 check_live_details()` trả
   `{is_live, room_id, is_sub_only, is_preview, paid_type, preview_duration, ...}`
   (Native API với `impersonate` safari15_5/chrome136, timeout 6s); cache 60s ở `api_server.py:227`.
3. **Chọn luồng** — `recorder_core.py:452 get_stream_candidates()` +
   `classify_stream_urls():487`: ưu tiên HLS/H.264, sắp hạng theo codec/bandwidth
   (Tier-1 = 1080p/`_or4`/origin), fallback FLV; thử tối đa 4 ứng viên khi ghi.
4. **Ghi** — `recorder_core.py:869 record_stream_ffmpeg()`:
   `-rw_timeout 60000000 -reconnect* -reconnect_delay_max 15 -fflags +genpts+discardcorrupt+nobuffer
   -map 0:v:0 -map 0:a:0? -c:v copy -c:a copy -sn -dn -avoid_negative_ts make_zero -t <s>`.
   Vòng trong giám sát: dung lượng không tăng ≥20s (≥250KB) → check live
   (3 lần liên tiếp = xuống live), VIP preview dừng ở 14s, hard-limit 30/90/120s → cắt file.
   Dừng mềm `_safe_stop_ffmpeg()` (`:816`, leo thang 8→4→2s rồi kill) để FFmpeg ghi `moov atom`.
 5. **Chuẩn hóa** — `auto_h264.py`: `validate_playable_video():111` (≥5s, ≥250KB),
    `check_h264_stream_health():360` (quét nhiều cửa sổ, ngưỡng lỗi macroblock — xem 5.3),
    `ensure_h264():687`, `sanitize_mp4_bitstream():485` (gõ AVCC, bỏ byte rác `01 64 00 1f…`,
    kiểm tra biên SPS/PPS + `lengthSizeMinusOne`),
    `has_faststart():300`, `upscale_to_1080p_if_needed():200` (NVENC/x264, Lanczos, timeout 1200s),
    `extract_middle_thumbnail():1067` (seek 50% → 25% → 5/3/1s).
6. **Upload trực tiếp** — Mọi segment (bất kể thời lượng) đều được upload trực tiếp lên
   Google Drive `tiktok-record/<user>/` + đồng bộ Supabase ngay sau khi chuẩn hóa.
   Segment đủ 1h → upload ngay + bắt đầu record phần mới. Segment cuối → upload sau
   khi xác nhận offline 5 phút (`offline_confirm_seconds=300`). Không sử dụng staging queue.
7. **Xoay vòng phiên (DRAIN)** — `cloud_daemon.py:708 run_daemon()`: quá thời lượng phiên →
   không khởi tạo luồng mới, chờ `DRAIN_GRACE_SECONDS = 360s` để upload,
   hard-limit +10 phút (GitHub Actions `timeout-minutes: 65`).
8. **Chống trùng lặp** — heartbeat `active_recordings.json` trên Drive (`gdrive_manager.py:678`),
   runner khác thấy heartbeat <180s (`cloud_daemon.py:835`, `api_server.py:1412`) sẽ bỏ qua
   user đó; zombie cleaner cho phép heartbeat tới 600s (`api_server.py:316`) rồi mới kiểm tra
   trực tiếp TikTok; đồng thời `streamer_recording_lock` (`config_lock.py:125`) chặn 2 process
   cùng ghi 1 user.

---

## 5. GIẢI THÍCH TỪNG MODULE

### 5.1 `cloud_daemon.py` (922 dòng) — bộ điều phối
* `load_monitored_users():58` — hợp nhất Drive + Supabase + config, cache 15s (cả 2 nguồn
  trả rỗng **hợp lệ** thì dùng rỗng, không rơi vào nhánh "giữ cache cũ").
* `discover_new_streamers():133` — quét HTML trang live bằng `curl_cffi` (impersonate chrome136),
  regex `"anchor_info"…"uniqueId"` → tự thêm đối thủ PK/co-host vào danh sách.
* `streamer_recording_worker():188` — vòng đời 1 streamer: nhận `streamer_recording_lock`,
  tạo folder Drive, phát hiện VIP Sub-Only (guest session, preview tối đa 300s, ≤5 lần thử),
  thu mảnh đến 3600s, chờ xác nhận offline `offline_confirm_seconds` (300s, recheck mỗi 15s),
  part 2, part 3…; ≤4 lần thất bại liên tiếp thì bỏ part.
* `run_daemon():708` — vòng lặp poll (`--duration-minutes` mặc định 210, `--interval` 25s,
  `--no-discover`), khởi tạo tối đa 10 worker, gửi Telegram khi phát hiện live,
  chống hot-loop `USER_START_COOLDOWN_SECONDS=180`; mỗi 15 vòng: GC cooldown;
  shutdown join tất cả trong 7 phút.
* Worker upload trực tiếp mọi segment lên Drive + Supabase (không qua staging queue).
  Supabase row chỉ tạo khi đã có `drive_file_id`.

### 5.2 `recorder_core.py` (1201 dòng) — lõi trích xuất
* `generate_guest_session():146`, `load_cookies():119` (cookie `sessionid_ss` cho live 18+/VIP).
* `check_live_details():182`, `check_live_status():412`, `check_user_live():423`.
* `get_stream_urls():622`, `parse_sdk_stream_data():538` (giải mã JSON SDK của TikTok).
* `record_stream_ffmpeg():869` (globale `GLOBAL_RECORDING_PROCS:855` + `atexit` dọn process),
  `concat_mp4_segments():1092` (concat demuxer, `-c copy`, timeout 180s, bỏ segment <50KB;
  **probe đầu/giữa/cuối 3 segment để cảnh báo đổi độ phân giải giữa chừng** — `-c copy` giữ
  nguyên SPS/PPS từng mẫu nên segment 1080p + 720p trong cùng part cho file đổi chuẩn giữa chừng),
  `_safe_stop_ffmpeg():816`.
* `DEFAULT_CONFIG:38` — `check_interval_seconds=20`, `max_recording_seconds=3600`,
  `offline_confirm_seconds=300`, `timezone_offset_hours=7`, `auto_upscale_1080p=true`.

### 5.3 `auto_h264.py` (1147 dòng) — chuẩn hóa H.264
`get_best_h264_encoder():31` (NVENC → QSV → AMF → MF → libx264), `get_video_codec():53`,
`validate_playable_video():111`, `check_h264_stream_health():360`,
`sanitize_mp4_bitstream():485` (theo ISO/IEC 14496-15),
`ensure_h264():687`, `convert_all_videos_in_folder():878`, `get_video_duration():925`.

**Vá lỗi "khung hình khối xám + nhiễu macroblock" (corrupt frame):**

| Lỗ hổng | Trước | Sau |
|---|---|---|
| Quét quá hẹm | `check_h264_stream_health` chỉ decode **6 giây đầu** (`-t 6`) → lỗi ở phút 30 không bao giờ bị thấy, `validate_playable_video` vẫn trả "Hợp lệ" | `_build_scan_windows():350` quét **3 cửa sổ** (giây 0 / 40% / 75% thời lượng, `-ss` trước `-i` = keyframe seek, mỗi cửa sổ 4s). File <90s chỉ quét cửa sổ đầu. Nhận `duration` truyền sẵn để không tốn thêm lần `ffmpeg -i` |
| Pattern thiếu | `corrupt_patterns` không chứa chính xác câu chữ FFmpeg phát ra khi vỡ macroblock | `_FATAL_PATTERNS:322` giữ 5 pattern "hỏng triệt để"; thêm `_MB_ERROR_PATTERNS:333` (`error while decoding MB`, `concealing`, `reference picture missing`, `Missing reference picture`, `mmco: unref short failure`, `non-existing/PPS/SPS`, `decode_slice_header error`) |
| Chẩn đoán sai | Lỗi MB nói chung là **bỏ qua hoàn toàn** | Ngưỡng `_MB_ERROR_THRESHOLD = 40` lỗi/cửa sổ 4s. Kết luận hỏng khi **≥2 cửa sổ** vượt ngưỡng **hoẶC** 1 cửa sổ ≥4× ngưỡng. Lẻ tẻ → vẫn "Hợp lệ" (tránh `os.remove()` nhầm video đã ghi tốt — lỗi từng xảy ra với cảnh báo timing/POC) |
| Cửa sổ avcC 96 byte | `moov[avcc_idx+4 : avcc_idx+100]` — avcC dài hơn thì SPS/PPS **bị cụt âm thầm** (slice Python không ném lỗi) → ghi header toàn cục sai → decoder cấu hình sai → khối xám | Đọc `moov[avcc_idx+4 : +4096]`, **kiểm tra biên từng NAL**; không đọc đủ → để `global_headers` **trống** (không ghi header sai) và rơi vào nhánh remux/transcode an toàn |
| `lengthSizeMinusOne` | **Không đọc bao giờ** — mọi nơi mặc định tiền tố NAL là 4 byte → stream dùng 1/2/3 byte thì cắt sai ranh giới NAL → garbage = khối nhiễu màu | `_parse_avcc_record():426` trả thêm `nalu_len_bytes = (byte[4] & 0x03) + 1`; `_write_length_prefixed_nals():467` dùng đúng độ dài đó; `_is_avcc_record():416` tách riêng "đúng là avcC" khỏi "avcC parse hỏng" → parse hỏng thì **bỏ mẫu**, không ghi rác |
| Box walker | `sz < 8` → `content_sz` âm → `f.seek()` lùi → có thể quay vô hạn | `sz < 8` / box type không phải fourcc / box mở rộng <16 byte → `break` ngay |
| Audio gating | `is_healthy` chỉ True khi `audio_codec in ("aac","none")` → file H.264 + audio mp3/opus rơi vào `sanitize_mp4_bitstream` (tự viết lại bitstream thủ công) | `ensure_h264():687` tách bạch: **sức khỏe bitstream video** quyết định `is_healthy`; audio khác AAC → Case 1 remux (giữ nguyên video, chỉ transcode audio). Gộp 2 lần gọi health check thành 1, truyền `duration` xuống |

### 5.4 `staging_queue.py` (654 dòng) — ~~chống mất dữ liệu~~ **NGỪNG SỬ DỤNG**
Mọi segment giờ đều upload trực tiếp lên Google Drive ngay sau khi chuẩn hóa, bất kể
thời lượng. `api_server.py` **không còn gọi** module này (đã bỏ `add_to_staging_queue`
trong `bg_record_worker` và bỏ `package_and_publish_queue` ở nhánh finalize).

Còn đúng **1 lời gọi thừa** cần dọn: `cloud_daemon.py:815-816` vẫn
`import staging_queue` + `staging_queue.check_and_flush_idle_queues()` ở cuối
`run_daemon()` — mục đích còn lại là xả nốt hàng đợi legacy còn sót trên Drive.
Sau khi xả hết hàng đợi cũ, xoá khối này thì `staging_queue.py` mới thực sự chết.

### 5.5 `gdrive_manager.py` (1057 dòng) + `gdrive_auth.py` (204 dòng)
* `get_access_token():49` (cache token, tự refresh, tự làm mới khi 401 giữa chừng).
* `find_or_create_folder():117` (cache folder, retry 3 lần), `upload_file_to_drive():199`
  (resumable upload, chunk 5MB, retry 5 lần/chunk, chặn file MP4 <250KB, `os.path.getsize`
  được bọc try/except OSError → file biến mất giữa chừng không làm sập luồng upload).
* `is_recorder_owned_file():426` — xác minh `file_id` nằm trong `tiktok-record/` (kể cả
  con cháu, tối đa 8 cấp cha, cache TTL 600s, cache fail 30s). **Bắt buộc** trước mọi
  thao tác public/redirect (xem mục 12).
* `make_file_public():500` → `get_cdn_download_url():530`
  (`https://drive.usercontent.google.com/download?id=<id>&export=download&authuser=0&confirm=t`).
* `load_streamers_from_drive():597` / `save_streamers_to_drive():626`,
  `load_active_recordings_from_drive():678` + `set_users_recording_status_drive():728`
  (heartbeat) / `set_user_recording_status_drive():823`,
  `delete_file_drive():963`, `clean_corrupt_files_from_drive():978`.
* `gdrive_auth.py:102 start_oauth_flow()` — server `localhost:8080` nhận code OAuth, chờ 300s.

### 5.6 `supabase_sync.py` (370 dòng) — đồng bộ dữ liệu cho web
* `_secret_from_config():19` — đọc bí mật từ env trước, fallback khóa trong `config.json`
  (**không còn hardcode key trong source**, xem mục 12).
* `fetch_streamers_from_supabase():50` — `GET /rest/v1/tiktok_streamers?select=username`.
* `add_streamer_to_supabase():64` — `POST` với `Prefer: resolution=merge-duplicates`.
* `upload_thumbnail_to_supabase():85` — nhận file path / bytes / URL / `gdrive:<id>`,
  upsert vào `covers/record-thumbnails/{user}/{name}.jpg`, retry 3 lần, trả URL public.
* `sync_recording_to_supabase():184` — upsert `tiktok_recordings` theo `on_conflict=filename`;
  **tự động bỏ qua video <250KB**; chỉ gửi cột có giá trị (không ghi đè `thumbnail_url` cũ);
  retry 3 lần, backoff `1.0*(attempt+1)`s. Nếu upload Drive thất bại thì **không** tạo row
  (tránh link chết), file local được giữ lại.
* `delete_streamer_data_supabase():315` — xóa recordings + streamer + thumbnail Storage.

### 5.7 `api_server.py` (1978 dòng) — REST API cho website (xem mục 6)
* CORS `allow_origins=["*"]` (`:91`) + **middleware PIN opt-in** cho POST/PUT/PATCH/DELETE
  (`:135`, xem mục 12) — GET luôn công khai.
* Live cache 60s (`LIVE_CACHE_TTL`, `:227`), recordings cache 60s (`_RECORDINGS_CACHE`, `:52`).
* `POST /api/users` — **tự động kiểm tra live + ghi hình ngay** khi thêm streamer mới.
* `bg_record_worker():879` — bản sao của `streamer_recording_worker` chạy trong process API
  (ngưỡng 3600s/part, heartbeat 60s, offline xác nhận 5 phút), upload trực tiếp mọi segment
  lên Drive + Supabase (không qua staging queue), `source="api_server"`; thumbnail/`getsize`
  được bọc try/except riêng để file biến mất không làm sập worker.
* `clean_zombie_recordings():292` — dọn task zombie sau 10 phút không heartbeat.
* Dọn RAM: purge cache khi RSS >300MB, cảnh báo >350MB, trần `render_limit_mb=512`.

### 5.8 `repair_cloud_videos_engine.py` (394 dòng) — tự lành
`process_single_repair():182` (tải → sửa bitstream → `patch_file_to_drive():59` →
cập nhật Supabase `update_supabase_metadata():158`), `run_repair_engine():276`
(cờ `--today` = cửa sổ 28h, `--priority-only` = lỗi `NAL_UNIT_SIZE_ERROR`),
lưu tiến độ `cloud_repair_progress.json` (`repaired_ids` / `failed_ids`), retry ≤3.

### 5.9 Tiện ích khác
* `notifier.py:25 send_telegram()` (cần `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`),
  `notifier.py:52 sync_to_gdrive()` (tôn trọng công tắc `gdrive_enabled`).
* `config_lock.py:70 config_transaction()` (khóa `config.lock`, timeout 10s, thread RLock +
  byte-range file lock `msvcrt`/`fcntl`), `config_lock.py:125 streamer_recording_lock(user)`
  (khóa `.locks/<user>.recording.lock` non-blocking chống 2 process cùng ghi).
* `tiktok_recorder.py` — menu CLI: `--auto`, `--now`, `--status`, `--set-cookie`,
  `--convert-h264`, `--set-vps-cloud`, `--interval`.

---

## 6. REST API ĐẦY ĐỦ (FASTAPI :8000)

Khởi động: `python api_server.py` → `uvicorn.run(app, host="0.0.0.0", port=8000)`
(`api_server.py:1975`). FastAPI `version="2.1.0"` (`:86`), endpoint `/api/version`
trả `{"version":"2.2.0","build":"native-safari-tls-sync"}` (`:784`).

| Method | Endpoint | Request | Trả về / chức năng |
| :--- | :--- | :--- | :--- |
| GET | `/api/health` | — | `{status, time, active_recordings[], active_count}` (`:347`) |
| GET | `/api/recordings/active` | — | Danh sách streamer đang ghi thật (Drive heartbeat + task local, dọn zombie trước) (`:366`) |
| GET | `/api/users?check_live=true` | query | Danh sách user (Supabase → Drive → config) + trạng thái live/đang ghi; chỉ probe user chưa có trong `active_users` (`:412`) |
| POST | `/api/users` | `{username}` | Thêm streamer (config, Drive, Supabase) (`:576`) |
| DELETE | `/api/users/{username}?delete_files=true` | query | Xóa streamer + (tuỳ chọn) file/DB (`:654`) |
| GET | `/api/memory` | — | Giám sát RAM/swap, purge cache nếu RSS >300MB (`:530`) |
| GET | `/api/stream/{username}` | — | URL stream + thông tin live (`:751`) |
| GET | `/api/version` | — | `{version:"2.2.0", build, time}` (`:782`) |
| GET | `/api/test-live/{username}` | — | Chẩn đoán chi tiết: room_id, codec, danh sách URL (`:786`) |
| POST | `/api/record` **(== `/api/record/start`)** | `{username, duration_seconds?}` | Thêm user và **bắt đầu ghi ngay** (background worker) (`:1357`/`:1358`) |
| POST | `/api/record/stop` | `{username}` | Dừng ghi an toàn (dừng FFmpeg để ghi moov) (`:1494`) |
| GET | `/api/recordings` | — | Gộp Drive + máy local: `{filename, user, size_bytes, size_mb, recorded_at, thumbnail_url, download_url, cdn_download_url, drive_file_id, drive_thumb_id, stream_url, source}` (cache 60s) (`:1611`) |
| POST | `/api/recordings/sync-supabase` | — | Đẩy toàn bộ video + thumbnail lên Supabase (background) (`:1688`) |
| GET | `/api/thumbnail/{user}/{filename}?redirect=true` | query | Ảnh giữa video; `redirect=true` → 302 CDN (`:1720`) |
| GET | `/api/download/{user}/{filename}` | — | Link tải CDN tốc độ cao (hỗ trợ Range/IDM) (`:1801`) |
| GET | `/api/cdn/{user}/{filename}` | — | Trả thẳng URL Google Edge CDN (`:1842`) |
| GET | `/api/stream-video-id/{file_id}?redirect=true` | query | 302 sang Drive CDN → **phát HTML5, tua mượt, 0 bandwidth server**; **chặn `file_id` không thuộc `tiktok-record/` → 403** (`:1889`) |
| GET | `/api/stream-video/{user}/{filename}` | — | Phát theo user/filename, path traversal bị chặn bằng `is_relative_to(BASE_DIR)` (`:1914`) |
| POST | `/api/gdrive/sync` | — | Kích hoạt đẩy local → Drive (`:1966`) |

Body models: `AddUserRequest {username}`, `RecordRequest {username, duration_seconds?}`
(`api_server.py:340`/`:343`).

> Mọi POST/PUT/PATCH/DELETE đi qua middleware PIN (`api_server.py:135`) nếu đã đặt
> biến môi trường `RECORD_PIN` hoặc khóa `api_pin` trong `config.json`; client gửi
> header `x-record-pin` hoặc `Authorization: Bearer <pin>` (xem mục 12).

Ví dụ:

```bash
curl -X POST http://127.0.0.1:8000/api/users -H "Content-Type: application/json" -d '{"username":"islizanx"}'
curl -X POST http://127.0.0.1:8000/api/record  -H "Content-Type: application/json" -d '{"username":"islizanx"}'
curl http://127.0.0.1:8000/api/recordings | head -c 2000
```

---

## 7. CẤU HÌNH & BIẾN MÔI TRƯỜNG

### `config.json` (mẫu `config.example.json`)
| Khóa | Ý nghĩa |
| :--- | :--- |
| `target_user` | User mặc định cho CLI (`islizanx`) |
| `monitored_users[]` | Danh sách streamer cục bộ (bị gộp bởi Drive + Supabase) |
| `check_interval_seconds` | Chu kỳ poll (20) |
| `quality` | `best` |
| `output_dir` | Thư mục xuất (`.`) |
| `auto_upscale_1080p` | Tự upscale lên 1080p (true) |
| `max_recording_seconds` | Giới hạn mỗi đoạn (3600) |
| `offline_confirm_seconds` | Chờ xác nhận offline (300) |
| `timezone_offset_hours` | Múi giờ đặt tên file (7) |
| `google_client_id` / `google_client_secret` / `gdrive_refresh_token` / `gdrive_enabled` | Cấu hình Google Drive |
| `supabase_key` | Supabase anon key (fallback khi không có env `SUPABASE_*`) — **không hardcode trong source** |
| `api_pin` | PIN bảo vệ POST/PUT/PATCH/DELETE; để rỗng = tắt auth (xem mục 12) |

### Secrets / env (GitHub Secrets hoặc biến hệ thống)
| Tên | Bắt buộc | Dùng ở đâu |
| :--- | :--- | :--- |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN` | ✔ | `gdrive_manager.py:19`, workflow ghi vào `config.json` |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` (hoặc `SUPABASE_KEY`) | ✔ | `supabase_sync.py:31-36` — env trước, fallback `config.json:supabase_key` |
| `RECORD_PIN` | ✖ | Bật xác thực PIN cho API (`api_server.py:110`), tương đương `config.json:api_pin` |
| `TIKTOK_SESSION_ID` | ✖ | Ghi vào `cookies.json` → live 18+/VIP |
| `TZ`, `TZ_OFFSET_HOURS` | ✖ | Workflow đặt `Asia/Ho_Chi_Minh` + `7` |
| `WORKFLOW_PAT` / `GH_PAT` / `GITHUB_TOKEN` | ✖ | Relay phiên kế tiếp (`recorder.yml:89-142`) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | ✖ | `notifier.py` |

`.gitignore` đã loại `config.json`, `cookies.json`, `*.lock`, mọi file media.

---

## 8. LƯU TRỮ: GOOGLE DRIVE + SUPABASE

### Google Drive
```text
tiktok-record/
├── streamers.json                 # Danh sách streamer (nguồn dự phòng)
├── active_recordings.json         # Heartbeat "đang quay" (ghi mỗi 60s) → chống trùng runner
│                                  #   dedup start_record <180s · zombie cleaner <600s
└── <username>/
    ├── <username>_2026-09-27_10-00-00.mp4
    └── <username>_2026-09-27_10-00-00.jpg
```
URL phát: `https://drive.usercontent.google.com/download?id={drive_file_id}&export=download&authuser=0&confirm=t`

### Supabase
**`tiktok_streamers`**: `id`, `username` (unique), `created_at`.

**`tiktok_recordings`** (khóa upsert: `filename`):
`filename`, `username`, `size_bytes`, `size_mb`, `recorded_at`, `created_at`,
`thumbnail_url`, `download_url`, `cdn_download_url`, `drive_file_id`,
`drive_thumb_id`, `source`.

**Storage bucket `covers`**: `record-thumbnails/{username}/{basename}.jpg`
→ URL public `{SUPABASE_URL}/storage/v1/object/public/covers/record-thumbnails/...`

---

## 9. GITHUB ACTIONS — CHẠY 24/7 MIỄN PHÍ

### `recorder.yml` — "TikTok 24-7 Auto Recorder" (146 dòng)
* Kích hoạt: `workflow_dispatch` + `cron: '*/50 * * * *'`.
* `concurrency: tiktok-recorder-runner` với `cancel-in-progress: false` (không đứt phiên).
* Khai báo môi trường `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: 'true'` chạy Node 24 chuẩn GitHub Actions.
* Job `run-recorder`, `ubuntu-latest`, **`timeout-minutes: 65`**.
* Bước chính: checkout (`actions/checkout@v7` native Node 24) → Python 3.11 (`actions/setup-python@v7` native Node 24) → `apt install ffmpeg` → `pip install -r requirements.txt`
  → ghi secrets vào `config.json`/`cookies.json` (`:42-78`) →
  `python -u cloud_daemon.py --duration-minutes 46 --interval 20`
  với `TZ=Asia/Ho_Chi_Minh`, `TZ_OFFSET_HOURS=7`, `SUPABASE_*`, `TELEGRAM_*` (`:80-90`) →
  **bước cuối `if: always()`** POST `actions/workflows/recorder.yml/dispatches` bằng
  `WORKFLOW_PAT || GH_PAT || GITHUB_TOKEN` để kích hoạt phiên kế tiếp **ngay lập tức**
  (không PAT → fallback cron 50 phút).

### `repair-watchdog.yml` — "Kuru Record Self-Healing Watchdog" (68 dòng)
* `concurrency: kuru-record-watchdog`, `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: 'true'`
* `actions/checkout@v7` + `actions/setup-python@v7` (Node 24 native)
* `cron: '0 1,5,9,13,17,21 * * *'` (6 lần/ngày) → timeout 20 phút →
  `python repair_cloud_videos_engine.py --today`.

---

## 10. KIỂM THỬ & VẬN HÀNH CỤC BỘ

```bash
pip install -r requirements.txt
python -B -m unittest test_audit_suite.py            # 78 test tổng hợp
python -B -m unittest test_challenger_concurrency.py # 26 test đa luồng/đối kháng
python -B -m unittest test_audit_suite.py test_challenger_concurrency.py  # 104/104
run_cloud_daemon.bat                                 # daemon local + watchdog 5s
run_recorder.bat                                     # CLI menu
python api_server.py                                 # REST API :8000
python tiktok_recorder.py --user <name> --now        # ghi ngay
python repair_cloud_videos_engine.py --today         # sửa video lỗi trong ngày (cửa sổ 28h)
python gdrive_auth.py                                # cấp refresh token OAuth
```

Bộ test dùng `unittest` + `mock` thuần — **mọi lời gọi mạng (TikTok scrape, Google Drive
heartbeat/token, Supabase) đều bị mock**, nên chạy được offline và không phụ thuộc mạng.
Chạy xong vẫn **sinh residue** (`.locks/*.recording.lock`, các thư mục `test_filter_user/`,
`vip_*`, `worker_*`) — `.locks/` đã nằm trong `.gitignore`, phần còn lại là thư mục rỗng
không track được, cứ xoá trước khi commit.

---

## 11. CÁCH `D:\web-truyen` SỬ DỤNG CÁC TÍNH NĂNG CỦA TOOL

### 11.1 Hiện trạng tích hợp

`D:\web-truyen` **đã nhúng một bản sao của chính tool này** tại `D:\web-truyen\kuruRecord\`
và có workflow `.github/workflows/recorder.yml` chạy với
`defaults.run.working-directory: ./kuruRecord`.

```text
Browser /record ──PIN──► Next.js /api/record/[...path] ──► TIKTOK_RECORDER_API_URL
                        (RECORD_PIN '2026', timeout 25s,   ▲  fallback
                         chặn video bytes, relay JSON/ảnh)   https://tiktok-api-i0o8.onrender.com
/api/record/dispatch ──GITHUB_PAT──► GitHub Actions recorder.yml ──► kuruRecord/cloud_daemon.py
/api/record/sync ──PIN|CRON_SECRET──► scripts/auto-sync-recordings.mjs ──► Supabase + covers/
app/record/page.js ─────────────────► Supabase (tiktok_*) + /api/record/* + Drive CDN
```

| Thành phần trong web-truyen | Vai trò | Vị trí |
| :--- | :--- | :--- |
| `app/record/page.js` (**1908 dòng**, 87KB) + `page.module.css` | Dashboard: PIN gate, CRUD streamer, trạng thái live, lưới video, modal phát (HTML5 + iframe Drive), cache localStorage → Supabase → API | `app/record/page.js` |
| `app/api/record/[...path]/route.js` | **Proxy mọi endpoint của tool**: GET/POST/DELETE; tự chọn `TIKTOK_RECORDER_API_URL` → ưu tiên `record.kurumieverything.io.vn` (Cloudflare Tunnel) → `localhost:8000` → fallback `tiktok-api-i0o8.onrender.com`; PIN `RECORD_PIN` mặc định `'2026'` | `app/api/record/[...path]/route.js` |
| Proxy special-cases | `stream-video-id/<id>` (`:55`) → proxy chunk có Range 206 (vượt `Cross-Origin-Resource-Policy` của Drive); `download-drive/<id>` (`:135`) bypass cảnh báo virus >100MB; chặn kéo `video/*` qua server (`:252`) — giữ 0-byte bandwidth | `app/api/record/[...path]/route.js` |
| `app/api/record/dispatch/route.js` | Kích hoạt phiên ghi trên GitHub Actions: `GITHUB_PAT||WORKFLOW_PAT`, repo mặc định `kurrukado/tiktok-recorder` (`:6-8`) | `app/api/record/dispatch/route.js` |
| `app/api/record/sync/route.js` | Nhận cron: chấp nhận `CRON_SECRET` hoặc `RECORD_PIN` (`:7-18`) → chạy sync | `app/api/record/sync/route.js` |
| `scripts/auto-sync-recordings.mjs` | Poll `GET /api/recordings` mỗi **30s** (`POLL_INTERVAL_MS:7`), tải thumbnail, upload `covers/record-thumbnails/...`, upsert `tiktok_recordings` | `scripts/auto-sync-recordings.mjs` |
| `lib/record-queue.js` (7.5KB) | Hàng đợi tuần tự hoá mọi call API recorder (coalesce, timeout) | `lib/record-queue.js` |
| `lib/supabase.js` | Client Supabase anon-key (fallback hard-code URL/key `:3-4`) | `lib/supabase.js` |
| `start-all-services.mjs` / `start.bat` | Khởi động song song `server-anh.mjs:4000` + **`api_server.py:8000`** (local & Cloudflare Tunnel) + `auto-sync-recordings.mjs` + cloudflared | root |
| `.github/workflows/recorder.yml` + `repair-watchdog.yml` | Bản sao 2 workflow, `working-directory: ./kuruRecord` | `.github/workflows/` |
| `ProjectDefination\kuruRecord.md` (rev 3.4.0) | Spec A-Z của hệ thống record (topology, DDL, pipeline, invariants) | `ProjectDefination/` |
| `.agents/skills/kuru-audit` | Skill audit 5 assertion (zero bandwidth, queue, memory, bitstream, Supabase) | `.agents/skills/` |

**Bản nhúng `kuruRecord/` CŨ HƠN nguồn `D:\tiktok-recorder`**: mọi file `.py` lõi đều khác
hash (ví dụ `cloud_daemon.py` 36.1KB/25-09 vs 52.9KB/30-09; `api_server.py` 72KB vs 92KB) —
`D:\tiktok-recorder` là bản đang phát triển (thêm `config_lock.py`, staging/DRAIN/heartbeat mới).

### 11.2 Bảng "tính năng của tool → cách web-truyen dùng"

| # | Tính năng của tool | web-truyen dùng thế nào | Trạng thái |
| :--- | :--- | :--- | :--- |
| 1 | Supabase `tiktok_streamers` (follow-list) | `page.js:832-836` upsert / `:955` delete trực tiếp qua PostgREST; runner đọc lại sau ≤20s | ✅ Đã có |
| 2 | Supabase `tiktok_recordings` (metadata) | `page.js:556-559, 692, 956, 1107-1109` đọc; `auto-sync-recordings.mjs:86-88, 117-123` upsert | ✅ Đã có |
| 3 | Thumbnail bucket `covers/record-thumbnails/{user}/…` | Poster `<video>`; detect "sẵn sàng" = URL chứa `supabase.co/storage` + `record-thumbnails` | ✅ Đã có |
| 4 | Phát video Google Edge CDN (`cdn_download_url`, `drive_file_id`) | Modal 2 chế độ: `<video src="/api/record/stream-video-id/<id>">` (Range 206) hoặc iframe Drive | ✅ Đã có |
| 5 | REST API `:8000` (CRUD user, start/stop ghi, health, recordings) | Proxy `/api/record/*` + `TIKTOK_RECORDER_API_URL` | ✅ Đã có |
| 6 | GitHub Actions Dispatch (`recorder.yml`) | `/api/record/dispatch` → nút "Kích hoạt lượt ghi" | ✅ Đã có |
| 7 | Watchdog tự lành 6 lần/ngày | Chưa khai báo/hiển thị trạng thái repair | ⬜ Nên làm |
| 8 | `GET /api/test-live/{u}`, `/api/memory`, `/api/recordings/active`, `/api/version` | Chưa dùng → thêm tab "Chẩn đoán" trong `/record` | ⬜ Nên làm |
| 9 | Thông báo Telegram khi streamer live | Chưa có UI xem log thông báo | ⬜ Tùy chọn |
| 10 | Auto-discover PK/Co-host (`discover_new_streamers`) | Chưa hiện "streamer gợi ý tự động thêm" | ⬜ Nên làm |
| 11 | Live status realtime (`/api/users?check_live`, cache 60s) | Đã poll nhưng chưa tận dụng cache server-side | ✅/⬜ |
| 12 | `POST /api/recordings/sync-supabase` | Script sync tự chạy 30s; thiếu nút thủ công trong UI | ⬜ Bổ sung |
| 13 | Chạy cục bộ GPU NVENC (`run_cloud_daemon.bat`) | Launcher `start-all-services.mjs` đã start `api_server.py:8000` | ✅ Đã có |
| 14 | Bản mới: `config_lock`, staging manifest, VIP sub-only, DRAIN, heartbeat chống trùng | Bản `kuruRecord/` **cũ hơn** | ⬜ Nâng cấp |

### 11.3 Kịch bản tích hợp cụ thể (mã mẫu)

**A. Thêm/xóa streamer — không cần server trung gian (khuyên dùng)**

```js
// lib/supabase.js đã có sẵn client
import { supabase } from '@/lib/supabase';

export async function addStreamer(username) {
  const clean = username.trim().replace('@', '').toLowerCase();
  await supabase.from('tiktok_streamers').upsert({ username: clean });
  // Runner đọc lại danh sách sau tối đa 20s → tự ghi khi live
}
export async function removeStreamer(username) {
  const clean = username.trim().replace('@', '').toLowerCase();
  await supabase.from('tiktok_streamers').delete().eq('username', clean);
}
```

**B. Đọc danh sách video (server component, không tốn bandwidth)**

```js
const { data } = await supabase
  .from('tiktok_recordings')
  .select('filename, username, size_mb, recorded_at, thumbnail_url, cdn_download_url, drive_file_id')
  .order('created_at', { ascending: false })
  .limit(50);

const src = `https://drive.usercontent.google.com/download?id=${data[0].drive_file_id}&export=download&authuser=0&confirm=t`;
```

**C. Gọi REST API của tool qua proxy đã có**

```js
// Thao tác ghi cần PIN (đã có sẵn trong proxy, mặc định '2026')
await fetch('/api/record/record', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'x-record-pin': process.env.NEXT_PUBLIC_RECORD_PIN },
  body: JSON.stringify({ username: 'islizanx' }),
});
const health = await fetch('/api/record/health').then(r => r.json());
// health = { status:'online', active_recordings:[], active_count }
```
> Proxy ghép `{TIKTOK_RECORDER_API_URL}/api/{path}` (hoặc `/{path}` nếu env chứa `/record-api`),
> timeout 25s, retry sang `https://tiktok-api-i0o8.onrender.com` khi primary chết.

**D. Phát video tua mượt, 0 bandwidth server**

```jsx
<video controls playsInline preload="metadata"
       poster={rec.thumbnail_url}
       src={`/api/record/stream-video-id/${rec.drive_file_id}`} />
```
Proxy tự cắt chunk, forward `Range` → `206 Partial Content` từ Drive CDN.

**E. Kích hoạt lượt ghi trên Cloud**

```js
await fetch('/api/record/dispatch', { method: 'POST' });
// → POST https://api.github.com/repos/kurrukado/tiktok-recorder/actions/workflows/recorder.yml/dispatches
```

**F. Cron đồng bộ metadata**

```bash
node scripts/auto-sync-recordings.mjs    # poll 30s, export runSyncCycle() để import
# hoặc:  POST /api/record/sync   (Authorization: Bearer $CRON_SECRET hoặc PIN)
```

**G. Nâng cấp bản nhúng `kuruRecord/` lên nguồn mới nhất**

```powershell
$src='D:\tiktok-recorder'; $dst='D:\web-truyen\kuruRecord'
foreach ($f in 'cloud_daemon.py','recorder_core.py','auto_h264.py','staging_queue.py',
               'supabase_sync.py','gdrive_manager.py','gdrive_auth.py','api_server.py',
               'repair_cloud_videos_engine.py','notifier.py','config_lock.py') {
  Copy-Item "$src\$f" "$dst\$f" -Force
}
# KHÔNG copy: config.json, cookies.json, .env (giữ environment của web-truyen)
```
Sau đó chạy lại `python -B -m unittest test_audit_suite.py` trong `kuruRecord/`.

### 11.4 Biến môi trường web-truyen cần có (`.env.local` / Vercel)

| Tên biến | Ý nghĩa | Đã có? |
| :--- | :--- | :--- |
| `TIKTOK_RECORDER_API_URL` | URL gốc API recorder (local `http://127.0.0.1:8000` hoặc Render) | ✅ `.env.local:13` |
| `RECORD_PIN` | PIN cho POST/DELETE qua proxy (code mặc định `'2026'`) | ⬜ đặt trên Vercel (fallback 2026) |
| `GITHUB_PAT` / `WORKFLOW_PAT` | Kích hoạt workflow `recorder.yml` | ⬜ cần thêm |
| `TIKTOK_RECORDER_REPO_OWNER` / `_REPO_NAME` | Mặc định `kurrukado` / `tiktok-recorder` | ⬜ tùy chỉnh |
| `CRON_SECRET` | Xác thực `POST /api/record/sync` theo cron | ⬜ tùy chọn |
| `NEXT_PUBLIC_SUPABASE_URL` / `_ANON_KEY` | Client Supabase (bảng `tiktok_*`, bucket `covers`) | ✅ |
| `SUPABASE_SERVICE_ROLE_KEY` | Dùng cho `auto-sync-recordings.mjs` (fallback anon) | ⬜ |

### 11.5 Những điểm cần sửa/khớp khi tích hợp

1. **Schema lệch**: `supabase_schema.sql` của web-truyen **không** khai báo
   `tiktok_streamers` / `tiktok_recordings` (0 occurrence "tiktok"); DDL chỉ nằm trong
   `ProjectDefination\kuruRecord.md:88-109` và spec dùng tên cột `file_name`/`streamer_name`
   **không khớp** mã thực tế (`filename`/`username`) → nên bổ sung DDL đúng.
2. **Thumbnails phải đúng chuẩn path** `covers/record-thumbnails/{user}/{base}.jpg`
   để UI nhận là "sẵn sàng".
3. **Không kéo `video/*` qua proxy Vercel/Render** (đã bị chặn có chủ đích tại
   `[...path]/route.js:252`) — luôn phát qua `stream-video-id` hoặc iframe Drive.
4. **Đồng bộ phiên bản**: `kuruRecord/` cũ hơn `D:\tiktok-recorder` (xem mục 11.3 G) —
   tính năng `config_lock`/staging manifest/VIP/DRAIN/heartbeat mới hơn ở bản nguồn.
5. **Batch query `.in()` ≤ 50 item** (quy tắc `data-integrity.md` của web-truyen).
6. **AGENTS.md của web-truyen**: READ BEFORE WRITE, `npm run build` sau mọi sửa,
   diff nguyên tử, không placeholder, ghi changelog vào `ProjectDefination\state.md`.
7. **Workflow đôi**: web-truyen có `recorder.yml`/`repair-watchdog.yml` chạy `./kuruRecord`
   → nếu nâng cấp, cả 2 repo đều cần sync workflow (bản nguồn thêm `TZ` env, không có
   `working-directory`).

---

## 12. CẢNH BÁO BẢO MẬT & ĐỘ LỆCH PHIÊN BẢN

### 12.1 Đã vá trong lần rà soát này
* **Gỡ Supabase anon key khỏi source** — `supabase_sync.py` không còn
  `DEFAULT_KEY` hardcode; đọc theo thứ tự `SUPABASE_ANON_KEY` → `SUPABASE_KEY` (env) →
  `config.json:supabase_key` (`_secret_from_config():19`), thiếu thì in cảnh báo và đồng bộ
  Supabase ngừng hoạt động thay vì âm thầm dùng key rỗng. URL project vẫn là mặc định
  (không phải bí mật). `config.example.json` đã thêm khóa mẫu.
* **PIN auth opt-in cho API** — middleware `api_server.py:135 enforce_pin_on_mutations`
  chặn mọi POST/PUT/PATCH/DELETE khi đặt env `RECORD_PIN` **hoặc** `config.json:api_pin`
  (`get_api_pin():110`, cache TTL 5s). Client gửi `x-record-pin` hoặc
  `Authorization: Bearer <pin>` — đúng chuẩn web-truyen đang dùng (`route.js` mặc định
  `'2026'`). Chưa đặt PIN thì giữ nguyên hành vi cũ (không phá tích hợp) nhưng
  `lifespan` sẽ in cảnh báo khi khởi động. GET không bị chặn; preflight OPTIONS đi thẳng
  vào CORSMiddleware; middleware tự gắn header CORS khi trả 401 để trình duyệt không
  báo lỗi CORS thay vì 401.
* **Chặn ép quyền public cho file lạ** — `GET /api/stream-video-id/{file_id}` gọi
  `gdrive_manager.is_recorder_owned_file():426` trước khi `make_file_public()`; file không
  nằm trong `tiktok-record/` → **403**. Trước đây endpoint không auth này có thể đặt quyền
  `anyone` cho **bất kỳ** file Drive nào token nhìn thấy (lỗ hổng IAM nghiêm trọng).
  Hàm xác minh đi ngược tối đa 8 cấp cha, cache 600s, cache "không xác định" 30s.
* Path traversal (`/api/stream-video/{user}/{filename}`, `/api/download/...`) đã chặn bằng
  `os.path.basename` + `Path.is_relative_to(BASE_DIR)` → 403.

### 12.2 Còn lại / cần làm tiếp
* `config.json` đang chứa **Google client secret + refresh token thật** — file đã
  `.gitignore` và không track trong git, nhưng **không được commit/push**, nên xoay vòng
  (rotate) token nếu repo từng public. `web-truyen/lib/supabase.js:3-4` và
  `scripts/auto-sync-recordings.mjs:4` **vẫn còn hardcode** Supabase key (repo khác,
  không thuộc thay đổi này).
* Mọi bí mật nên nằm ở **GitHub Secrets / env**, workflow tự ghi vào `config.json` lúc chạy.
* API `:8000` vẫn **CORS `*`** và GET không có auth → chỉ mở trong mạng nội bộ, hoặc bật
  `RECORD_PIN` + đặt sau proxy (cách web-truyen đang làm). PIN hiện là opt-in nên **mặc
  định vẫn mở** — phải chủ động đặt mới có tác dụng.
* Bộ test: `test_audit_suite.py` (**78** bài, gồm 11 bài mới ở `TestBitstreamCorruptionDetection`)
  và `test_challenger_concurrency.py` (**26** bài) = **104 test**, đang `OK 104/104` —
  chạy trước mọi thay đổi mã nguồn.
* `staging_queue.py` **đã ngừng được `api_server.py` sử dụng**; `cloud_daemon.py:815-816`
  còn 1 lời gọi `check_and_flush_idle_queues()` để xả hàng đợi legacy trên Drive.
  Sau khi xả hết thì xoá khối import + xoá luôn module (xem mục 5.4).

### 12.3 Vá lỗi khung hình corrupt (khối xám + nhiễu macroblock)
Xem bảng chi tiết ở **mục 5.3**. Tóm tắt nguyên nhân đã xử lý:
1. **Lọt lưới** — quét chỉ 6 giây đầu + pattern thiếu câu chữ của lỗi macroblock →
   `validate_playable_video` trả "Hợp lệ" cho file hỏng và video vẫn được upload.
2. **Tạo lỗi** — cửa sổ avcC 96 byte cắt cụt SPS/PPS, không đọc `lengthSizeMinusOne`,
   bỏ qua kiểm tra biên box/box size âm.
3. **Đường sai** — `is_healthy` bị audio codec gating khiến file H.264 khỏe bị đẩy qua
   `sanitize_mp4_bitstream` (viết lại bitstream thủ công).

Còn lại / cần quyết định:
* **Chưa vá (nguy cơ cao, cần sửa gốc)** — `bg_record_worker` / `streamer_recording_worker`
  vẫn tích lũy segment từ **nhiều stream candidate khác tier trong cùng một part**
  (`stream_candidates[:4]` theo thứ tự 1080p → 720p → 540p → 360p). `concat_mp4_segments`
  hiện **chỉ cảnh báo** (probe 3 segment đầu/giữa/cuối) chứ chưa chốt part khi đổi
  độ phân giải — muốn dứt điểm thì phải tách part ngay khi `get_video_resolution()` đổi.
* **Chưa vá (nguy cơ thấp)** — `record_stream_ffmpeg` bật `+nobuffer` (`recorder_core.py:905`)
  nên có thể bắt đầu ghi **giữa GOP** (frame đầu tham chiếu IDR chưa về) → 1–N frame lỗi.
  `+discardcorrupt` **không bắt được** lỗi này vì gói không bị gắn cờ corrupt, chỉ thiếu
  tham chiếu. Cần cơ chế chờ keyframe đầu (`-ss` / bỏ qua NAL đầu tiên).
* **Chưa xác nhận được từ ảnh** — cần user cho biết ảnh chụp ở đoạn nào của file
  (đầu / giữa / sau upload) và file `.mp4` gốc nếu có, để phân biệt giữa bắt-mới-GOP,
  file đã qua `sanitize_mp4_bitstream`, hay segment khác độ phân giải bị ghép.

---

### 12.4 Vá lỗi nạp Cookie 18+/AI và Lỗi kết nối API Recorder từ Web ("Lỗi API Recorder")

1. **Lỗi Cookie Native Live API (Stream 18+ và AI)**:
   * **Nguyên nhân**: `load_cookies()` trước đây chỉ được gọi ở Method 1 (Scrape HTML), trong khi Method 0 (Native API) chạy trước và bị gọi ẩn danh không có cookie -> TikTok trả về lỗi `4003110` hoặc coi là offline với live 18+/AI.
   * **Đã sửa**: Đưa `if cookies is None: cookies = load_cookies()` lên đầu cả `check_live_details()` và `get_stream_urls()`. Nâng cấp `load_cookies()` tự động tìm kiếm đa đường dẫn: `cookies.json` local -> `D:\web-truyen\kuruRecord\cookies.json` -> env `TIKTOK_SESSION_ID` -> `config.json`.
2. **Lỗi `POST /api/users` dính cache offline cũ**:
   * **Nguyên nhân**: Frontend thêm streamer khi vừa live nhưng `get_user_live_details_cached()` trả về cache offline 60s trước đó -> không tự động bật record ngay.
   * **Đã sửa**: Bổ sung `with LIVE_CACHE_LOCK: LIVE_CACHE.pop(user, None)` để ép truy vấn tươi từ TikTok ngay lúc thêm streamer.
3. **Lỗi "Lỗi API Recorder" & Treo kết nối trên Web**:
   * **Nguyên nhân**: Web Next.js cấu hình gọi Render (`https://tiktok-api-i0o8.onrender.com`). Render Free bị ngủ đông sau 15p không dùng, mất 50-70s khởi động lại trong khi proxy timeout chỉ 25s -> web báo lỗi timeout / ngoại tuyến.
   * **Đã sửa**:
     * Định tuyến Cloudflare Tunnel `https://record.kurumieverything.io.vn` trỏ trực tiếp về `127.0.0.1:8000` (được cấu hình trong `~/.cloudflared/config.yml`).
     * Cập nhật Next.js proxy `route.js` trong `web-truyen` thêm các candidate ưu tiên: Tunnel `record.kurumieverything.io.vn` và `http://127.0.0.1:8000` trước khi fallback sang Render.
     * Cập nhật `D:\web-truyen\start.bat` và `start-all-services.mjs` tự động bật kèm `python d:\tiktok-recorder\api_server.py` trên Port 8000.

---
### 12.5 Nâng cấp GitHub Actions sang Node.js 24 (Khử sạch cảnh báo Deprecation Node 20)

* **Hiện tượng**: GitHub Actions runner cảnh báo `Warning: Node.js 20 is deprecated. The following actions target Node.js 20 but are being forced to run on Node.js 24: actions/checkout@v4, actions/setup-python@v5`.
* **Cơ chế**: Theo [GitHub Changelog 2025-09-19](https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/), Node 20 chạm mốc End-of-Life. GitHub Runner hỗ trợ Node 24 và khuyến nghị người dùng nâng cấp lên action bản mới hoặc kích hoạt cờ môi trường `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true`.
* **Đã thực hiện**:
  1. Nâng cấp toàn diện workflow `.github/workflows/recorder.yml` và `.github/workflows/repair-watchdog.yml`:
     - Chuyển `actions/checkout@v4` → `actions/checkout@v7` (chạy gốc `node24`).
     - Chuyển `actions/setup-python@v5` → `actions/setup-python@v7` (chạy gốc `node24`).
     - Khai báo biến môi trường cấp workflow: `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: 'true'`.
  2. Kết quả: Khử sạch 100% cảnh báo Deprecation của GitHub Runner, tối ưu thời gian khởi tạo và tương thích chuẩn mới nhất của GitHub.

---

### 12.6 Khắc phục Read timed out (timeout=8) khi đồng bộ active_recordings.json trên Drive

* **Hiện tượng**: Log hiển thị `[!] Không đọc được nội dung active_recordings.json (HTTPSConnectionPool... Read timed out. (read timeout=8))` dính vào dòng tiến độ ghi hình ffmpeg.
* **Bản chất**: Đây là cơ chế bảo vệ an toàn (Fail-safe): khi Drive API bị trễ mạng ngắt quãng, tool từ chối ghi đè danh sách trống để không làm mất trạng thái recording của các streamer khác. Video đang quay hoàn toàn KHÔNG bị ảnh hưởng.
* **Đã cải tiến**:
  - Nâng timeout đọc Drive API từ 8s lên 12s trong cả `load_active_recordings_from_drive` và `set_users_recording_status_drive`.
  - Bổ sung cơ chế tự động thử lại (Retry loop 2 lần kèm backoff 1s) trước khi bỏ qua chu kỳ heartbeat.
  - Thêm ký tự xuống dòng `\n` trước các thông báo log của Drive manager để không bị ghi đè/nối đuôi vào dòng `\r` tiến độ thời gian thực của FFmpeg.

---

---

### 12.7 Khắc phục triệt để lỗi Livestream dài chỉ ghi và tải lên được 1 Phần (Multi-part Recording & Session Relay)

* **Hiện tượng**: Streamer live nhiều tiếng liên tục nhưng Google Drive chỉ nhận được Phần 1 (hoặc một phần ngắn), các phần tiếp theo không thấy xuất hiện.
* **Nguyên nhân cốt lõi**:
  1. **Thời lượng phiên Runner quá ngắn (46 phút vs 60 phút/phần)**: Workflow GitHub Actions trước đây cấu hình `--duration-minutes 46`. Sau 46 phút bot chuyển sang trạng thái DRAIN và phát `stop_event.set()` ở phút 52. Khi Phần 1 kết thúc (dù chưa đủ 60 phút hoặc vừa chạm 46 phút), cờ `stop_event.is_set()` được kiểm tra và ngắt luồng ngay lập tức (`cloud_daemon.py:590`), không cho phép chuyển sang Phần 2 (`part_number += 1`).
  2. **Khoảng trễ chuyển tiếp Runner (Heartbeat Ghost)**: Heartbeat trên Drive có cửa sổ bận 180s. Khi Runner 1 tắt, Runner 2 chạy tiếp sức nhưng thấy streamer vẫn còn trong `drive_busy_users` (< 180s) nên bỏ qua. Đến chu kỳ sau streamer có thể đã tắt live hoặc chuyển luồng.
  3. **Độ nhạy ngắt luồng (PK battles & CDN jitter)**: Khi streamer chơi PK hoặc TikTok chuyển cụm máy chủ CDN, `max_consecutive_failures = 4` khiến worker dễ dừng sớm trước khi kịp lấy lại tín hiệu.
* **Đã cải tiến & xử lý triệt để**:
  1. **Tối ưu hóa thời lượng phiên runner**:
     - `.github/workflows/recorder.yml`: Tăng `--duration-minutes 46` ➔ `--duration-minutes 170` và `timeout-minutes 65` ➔ `timeout-minutes 210`.
     - Cho phép mỗi runner ghi liên tục trọn vẹn 2 đến 3 phần đầy đủ (mỗi phần 1 tiếng = 3600s) trước khi chuyển giao an toàn cho runner kế tiếp.
  2. **Nâng cao khả năng phục hồi luồng (Retry Resilience)**:
     - Tăng `max_consecutive_failures` từ 4 lên 8 lần liên tiếp ở cả `cloud_daemon.py` và `api_server.py`.
     - Chống rớt luồng khi streamer PK hoặc mạng chập chờn.
  3. **Rút ngắn cửa sổ bàn giao Runner**:
     - Giảm ngưỡng staleness kiểm tra `drive_busy_users` từ 180s xuống 90s, đảm bảo khi một runner kết thúc phiên, runner mới có thể nhận diện và tiếp quản việc ghi hình ngay lập tức mà không bị "mù" 3 phút.
  4. **Kiểm thử hồi quy toàn diện**:
     - Cập nhật test case `test_audit_suite.py` tương thích với ngưỡng retry mới.
     - Bộ test đạt chuẩn tuyệt đối: **104/104 OK** (`test_audit_suite.py`: 78/78, `test_challenger_concurrency.py`: 26/26).

---

### 12.8 Đồng bộ toàn diện sang web-truyen/kuruRecord và Kiểm định Hệ thống (System Health Check)

* **Mục tiêu**: Đảm bảo cả hai repository (`tiktok-recorder` độc lập và submodule tích hợp trong `web-truyen/kuruRecord`) đều đồng nhất 100% phiên bản, triệt tiêu độ lệch mã nguồn (code drift).
* **Nội dung thực hiện**:
  1. **Đồng bộ mã nguồn cốt lõi**:
     - Sao chép 13 file động cơ từ `D:\tiktok-recorder` sang `D:\web-truyen\kuruRecord`: `cloud_daemon.py`, `recorder_core.py`, `auto_h264.py`, `staging_queue.py`, `supabase_sync.py`, `gdrive_manager.py`, `gdrive_auth.py`, `api_server.py`, `repair_cloud_videos_engine.py`, `notifier.py`, `config_lock.py`, `test_audit_suite.py`, `test_challenger_concurrency.py`.
     - Giữ nguyên các file cấu hình và môi trường cục bộ (`config.json`, `cookies.json`, `.env`).
  2. **Dọn dẹp kiểm soát phiên bản Git**:
     - Cập nhật `.gitignore` của `web-truyen` chặn thư mục `.locks/` và `*.lock` do cơ chế khóa tiến trình `config_lock` sinh ra.
     - Xóa bỏ các lock file tạm bị track nhầm trên git (`kuruRecord/.locks/*`, `config.lock`).
  3. **Kiểm định chất lượng 2 tầng**:
     - **Tầng 1 (kuruRecord Test Suite)**: 104/104 tests vượt qua (`test_audit_suite.py`: 78/78 OK, `test_challenger_concurrency.py`: 26/26 OK).
     - **Tầng 2 (Next.js Frontend Build)**: Chạy `npm run build` thành công trong 10.2s với Turbopack, toàn bộ 37 routes tĩnh và động hợp lệ.
  4. **Kiểm tra End-to-End Trực tiếp**:
     - Local API (`http://127.0.0.1:8000/api/users`): Phản hồi HTTP 200 OK (5.46s, 59 monitored users).
     - Cloudflare Tunnel (`https://record.kurumieverything.io.vn/api/users`): Phản hồi HTTP 200 OK (5.58s), tích hợp xuyên suốt với web frontend.
  5. **Triển khai GitHub**:
     - Đã commit và push đồng bộ lên cả 2 repository:
       - `kurrukado/tiktok-recorder` (nhánh `main`)
       - `kurrukado/web-truyen` (nhánh `main`)

---

### 12.9 Khắc phục triệt để lỗi "Nhận diện Live nhưng không ghi hình ngay / Bị đánh dấu Offline" (HLS FFmpeg Freeze & Watchdog Fix)

* **Hiện tượng**: Tool hoặc Web UI nhận diện streamer đang LIVE (hiển thị trạng thái LIVE), nhưng không tiến hành ghi hình ngay (hoặc đứng ở 0.00 MB). Một lúc sau, phiên live bị đánh dấu là Offline và không có bất kỳ video nào được lưu lên Drive hay Supabase.
* **Nguyên nhân cốt lõi (Root Cause)**:
  1. **Đóng băng tiến trình FFmpeg trên luồng HLS (`-reconnect_at_eof 1` + `-reconnect_streamed 1`)**:
     - Trong `record_stream_ffmpeg`, cờ `-reconnect_at_eof 1` và `-reconnect_streamed 1` được gắn cứng cho mọi luồng.
     - Đối với luồng HLS (`.m3u8`) — vốn là định dạng được thuật toán ưu tiên số 1 — mỗi phân đoạn `.ts` đều có điểm EOF kết thúc file tự nhiên. Khi đọc xong 1 đoạn `.ts`, cờ `-reconnect_at_eof 1` ép HTTP client của FFmpeg coi EOF là đứt kết nối và liên tục kết nối lại chính file `.ts` đó thay vì để demuxer HLS chuyển sang phân đoạn tiếp theo.
     - Hậu quả: FFmpeg rơi vào vòng lặp spin-reconnect vô hạn, file đầu ra đứng im ở **0.00 MB** trong suốt 30–120 giây.
  2. **Watchdog ngắt luồng và xóa file nhầm**:
     - Do FFmpeg bị treo ở 0 byte, cơ chế giám sát dung lượng (`stagnant_seconds >= 30`) kích hoạt và kiểm tra live. Nếu mạng TikTok phản hồi chậm hoặc streamer tạm thời chao đảo tín hiệu, watchdog xác nhận offline và cưỡng chế dừng FFmpeg.
     - File 0 byte / <250KB không đạt chuẩn nên bị xóa bỏ, biến đếm `consecutive_failures` tăng lên.
  3. **Bẫy lặp khi chưa có phân đoạn (`not part_segments` loop trap)**:
     - Khi inner loop kết thúc mà `part_segments = []`, vòng lặp ngoài `while True` trước đây không kiểm tra biến `offline_confirmed` mà chỉ `continue`, khiến worker kẹt lại thêm nhiều chu kỳ thất bại vô ích (chiếm giữ 1 slot ghi hình và blast heartbeat giả lập lên Drive).
  4. **Khóa phạt Cooldown quá dài (180 giây)**:
     - Sau khi worker dừng, `USER_START_COOLDOWN_SECONDS = 180s` (3 phút) chặn không cho daemon thử lại streamer này. Suốt 3 phút đó, Web hiển thị "LIVE" nhưng bot từ chối khởi động lại luồng, khiến người dùng nghĩ tool bị đơ; khi streamer tắt live thì buổi live hoàn toàn bị mất.
* **Các cải tiến & bản vá đã áp dụng**:
  1. **Phân tách cờ mạng FFmpeg chuẩn xác theo giao thức**:
     - Với luồng HLS (`.m3u8`): Loại bỏ hoàn toàn `-reconnect_at_eof 1` và `-reconnect_streamed 1`, chỉ sử dụng `-reconnect 1 -reconnect_on_network_error 1 -reconnect_delay_max 10`. Luồng HLS ghi mượt mà ngay từ giây đầu tiên (đạt 0.67 MB trong 2s).
     - Với luồng FLV (`.flv`): Giữ `-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 -reconnect_delay_max 10` cho luồng liên tục.
  2. **Tối ưu hóa thời gian đệm khởi đầu (Zero-Latency Start)**:
     - Giảm `-analyzeduration` từ 10s xuống 3s (`3000000`) và `-probesize` từ 10MB xuống 2MB (`2000000`), giúp FFmpeg bắt đầu xuất khung hình MP4 chỉ sau <1 giây thay vì phải đợi nạp đủ 10MB.
  3. **Chống chốt phân đoạn nhầm khi mạng dao động**:
     - Nâng cấp watchdog trong `record_stream_ffmpeg` yêu cầu tối thiểu 2 lần kiểm tra offline liên tiếp (`consecutive_offline_checks >= 2`) mới chốt dừng.
  4. **Thoát luồng dứt điểm khi streamer offline**:
     - Bổ sung `if offline_confirmed: break` ngay tại khối `if not part_segments:` trong cả `cloud_daemon.py` và `api_server.py`.
  5. **Rút ngắn Cooldown từ 180s xuống 30s**:
     - Giảm `USER_START_COOLDOWN_SECONDS` xuống 30s, cho phép bot tái kết nối nhanh chóng nếu gặp sự cố mạng ngắt quãng.
  6. **Đồng bộ toàn diện & Kiểm thử hồi quy**:
     - Vượt qua toàn bộ **104/104 tests** ở cả 2 repository (`tiktok-recorder` và `web-truyen/kuruRecord`).

---

### 12.10 Khắc phục triệt để lỗi TikTok Native API trả về HTTP 403 Forbidden khiến Web và Bot không bắt được tín hiệu Live / Recording

* **Hiện tượng**: Streamer đang phát trực tiếp trên TikTok (thậm chí đang live nhiều giờ), nhưng trên Web UI chỉ hiển thị huy hiệu xám "Offline", không nhận diện được là đang LIVE hay đang RECORD. Khi bấm nút "Làm mới", trạng thái vẫn giữ nguyên Offline.
* **Nguyên nhân cốt lõi (Root Cause)**:
  1. **TikTok Web/Akamai WAF chặn 403 Forbidden với `x-tt-system-error: 3` trên endpoint `api-live/user/room`**:
     - Trước đây, `check_live_details` và `get_stream_urls` sử dụng URL có dấu gạch chéo cuối `https://www.tiktok.com/api-live/user/room/?...` kèm chuỗi query cũ `app_language=en&app_name=tiktok_web&device_platform=web_pc`.
     - Hệ thống phòng thủ Akamai của TikTok nhận diện các tham số này khi gọi từ script tự động là bot request không chuẩn và lập tức phản hồi `HTTP 403 Forbidden` (`content-length: 0`, header `x-tt-system-error: 3`).
     - Khi Method 0 (Native API) gặp lỗi 403, engine fallback sang Method 1 (HTML scrape `@user/live`). Tuy nhiên, trong cấu trúc mới của TikTok, trang HTML `@user/live` chỉ trả về shell JavaScript tối giản (không chứa thẻ `SIGI_STATE`).
     - Hậu quả: Toàn bộ quá trình kiểm tra trả về `is_live: False` cho tất cả streamer, ngay cả khi streamer đang live thực tế!
  2. **Bộ nhớ đệm `LIVE_CACHE` khóa cứng kết quả Offline trong 60 giây**:
     - Trong `api_server.py`, khi kết quả `is_live: False` được trả về, nó bị lưu vào `LIVE_CACHE` với TTL 60.0s.
     - Endpoint `GET /api/users` trước đây không hỗ trợ cờ `fresh=true` và hàm `get_user_live_details_cached` không có cơ chế bỏ qua cache khi người dùng bấm nút "Làm mới" thủ công trên Web.
     - Vòng lặp duyệt tuần tự trong `get_active_recordings` quét qua 60 streamer làm endpoint này bị nghẽn (timeout) trên các kết nối proxy/tunnel.
* **Các cải tiến & bản vá đã áp dụng**:
  1. **Chuẩn hóa Endpoint và Tham số TikTok Native Live API**:
     - Chuyển sang URL chuẩn không trailing slash: `https://www.tiktok.com/api-live/user/room`.
     - Bổ sung tham số chống cache và định danh chuẩn: `params={"aid": 1988, "sourceType": 54, "staleTime": 600000, "uniqueId": user.lower()}`.
     - Đặt header `Referer: https://www.tiktok.com/@{user}/live` khớp chính xác với luồng người dùng thật truy cập phòng live.
     - Bổ sung kiểm tra cả 2 định dạng stream: `streamData` và `hevcStreamData`.
     - Kết quả: Endpoint trả về `HTTP 200 OK` tức thì (<300ms) với đầy đủ metadata, thumbnail, `roomId` và danh sách luồng stream phân cấp không cần session đăng nhập.
  2. **Phân tách TTL bộ nhớ đệm thông minh & Hỗ trợ Real-Time Fresh Probe**:
     - Bổ sung `LIVE_CACHE_OFFLINE_TTL = 15.0s`: Khi streamer offline, chỉ cache tối đa 15s để bắt tín hiệu streamer vừa bật live trong chu kỳ poll tiếp theo (25s) của Web UI.
     - Cập nhật `get_users(check_live=True, fresh=False)` và `get_user_live_details_cached(user, force_refresh=False)`: Khi Web gửi `fresh=true` (người dùng bấm "Làm mới"), cache lập tức bị bỏ qua và probe trực tiếp tín hiệu thật từ TikTok.
     - Tối ưu hóa `get_active_recordings`: Đọc trực tiếp danh sách live từ `LIVE_CACHE` trong RAM thay vì quét tuần tự 60 streamer qua mạng, thời gian phản hồi đạt **0ms**.
  3. **Đồng bộ Web Frontend (`app/record/page.js`)**:
     - Cập nhật hàm `fetchLiveStatus`: Khi `isManualRefresh` là `true`, thêm tham số `&fresh=true` vào request gửi đến API recorder, đảm bảo trạng thái LIVE và REC phản ánh chuẩn xác 100% ngay khi người dùng bấm nút làm mới.
  4. **Kiểm thử hồi quy & Triển khai toàn diện**:
     - Kiểm tra trực tiếp trên các streamer đang live thực tế (`@tami.com.vn`, `@nicaswrld`, `@huong_linh97`): Cả 3 endpoint (`http://127.0.0.1:8000/api/users`, `https://record.kurumieverything.io.vn/api/users`, `https://kurumieverything.io.vn/api/record/users`) phản hồi tức thì với `status: recording`, `is_live: true`, `is_recording: true`.
     - Chạy `npm run build` trên `web-truyen`: Thành công hoàn hảo 37/37 routes.
     - Chạy toàn bộ **104/104 tests** trên cả hai kho mã nguồn: **100% PASSED**.

---

*Tài liệu tạo từ việc đọc mã nguồn tại `D:\\tiktok-recorder` và khảo sát `D:\\web-truyen`.*

