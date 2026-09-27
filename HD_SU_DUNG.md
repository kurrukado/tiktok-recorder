# 📖 HƯỚNG DẪN SỬ DỤNG HỆ THỐNG TIKTOK RECORDER 24/7 & CLOUD API (v66)

Hệ thống tự động theo dõi và ghi hình livestream TikTok chuẩn HD H.264 (AVC), tự động đóng gói qua **Staging Queue**, đồng bộ dữ liệu vào **Supabase** và lưu trữ video vĩnh viễn trên **Google Drive**.

Hệ thống hoạt động **hoàn toàn miễn phí 24/7 trên đám mây (GitHub Actions Public Repository - Không giới hạn quota 2.000 phút)**, hỗ trợ chạy song song tại máy cá nhân với GPU NVIDIA NVENC và cung cấp API kết nối trực tiếp với website.

---

## 📑 MỤC LỤC
1. [Kiến trúc Hệ thống 24/7 Không Giới Hạn Quota](#1-kiến-trúc-hệ-thống-247-không-giới-hạn-quota)
2. [Các Tính Năng Nổi Bật](#2-các-tính-năng-nổi-bật)
3. [Cấu Trúc Lưu Trữ (Google Drive & Supabase)](#3-cấu-trúc-lưu-trữ-google-drive--supabase)
4. [Vận hành 24/7 trên GitHub Actions (Cloud Runner)](#4-vận-hành-247-trên-github-actions-cloud-runner)
5. [Vận hành Cục bộ trên Máy tính Cá nhân (GPU NVENC)](#5-vận-hành-cục-bộ-trên-máy-tính-cá-nhân-gpu-nvenc)
6. [Tích hợp API vào Website & Ứng dụng](#6-tích-hợp-api-vào-website--ứng-dụng)
7. [Xem Video Trực Tuyến & Tải Tốc Độ Cao (Google Edge CDN)](#7-xem-video-trực-tuyến--tải-tốc-độ-cao-google-edge-cdn)
8. [Giải thích Chi tiết Các File trong Mã Nguồn](#8-giải-thích-chi-tiết-các-file-trong-mã-nguồn)
9. [Cập nhật Cookie TikTok Khi Cần](#9-cập-nhật-cookie-tiktok-khi-cần)

---

## 1. KIẾN TRÚC HỆ THỐNG 24/7 KHÔNG GIỚI HẠN QUOTA

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        WEBSITE CỦA BẠN                                 │
│   (Quản lý Streamer, Xem Video HTML5 trực tiếp, Tải CDN HTTP Range 206)│
└───────────────────▲──────────────────────────────▲─────────────────────┘
                    │                              │
         Supabase PostgREST API            Google Edge CDN URL
       (Thêm/Xóa Streamer, Đọc Video)    (Phát video tua mượt mà)
                    │                              │
                    ▼                              │
┌──────────────────────────────────────┐           │
│           SUPABASE CLOUD             │           │
│  - Table: tiktok_streamers (Danh sách)│           │
│  - Table: tiktok_recordings (Video)  │           │
│  - Storage Bucket: covers/           │           │
│    (Ảnh Thumbnail sắc nét CDN)       │           │
└───────────────────▲──────────────────┘           │
                    │                              │
      Đồng bộ trạng thái & metadata                │
                    │                              │
┌───────────────────┴──────────────────┐           │
│    GITHUB ACTIONS (PUBLIC RUNNER)    │           │
│  - Chạy 24/7 vĩnh viễn (UNLIMITED)   │           │
│  - Quét streamers từ Supabase mỗi 20s│           │
│  - Ghi đồng thời lên đến 10 streamer │           │
│  - Continuous Relay + Cron 50 phút   │           │
│  - Ưu tiên HLS H.264 chống lỗi NAL   │           │
│  - Watchdog tự lành (6 giờ/lần)      │           │
└───────────────────┬──────────────────┘           │
                    │                              │
                    │ Tải video MP4 & Thumbnail    │
                    ▼                              │
┌──────────────────────────────────────────────────┴─────────────────────┐
│                             GOOGLE DRIVE                               │
│  - Thư mục gốc: tiktok-record/<tên_streamer>/                           │
│  - Staging Queue (phân đoạn ngắn chống mất video khi rớt mạng/hết giờ) │
│  - Video chính thức MP4 (+faststart) & Thumbnail JPG                   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. CÁC TÍNH NĂNG NỔI BẬT

* ♾️ **100% Unlimited Minutes (Public Repo):**
  - Chạy trên GitHub Actions ở chế độ **Public Repository** được hưởng chính sách **Không giới hạn phút chạy miễn phí** của GitHub (giải quyết triệt để hạn mức 2.000 phút/tháng của repo Private).
  - Toàn bộ thông tin nhạy cảm (`GDRIVE_REFRESH_TOKEN`, `SUPABASE_KEY`...) được bảo vệ tuyệt đối qua **GitHub Secrets**, `.gitignore` ngăn chặn 100% rò rỉ mã bí mật.
* 🛡️ **Khắc Phục Triệt Để Lỗi Bitstream NAL Unit (FLV Sequence Header):**
  - Cơ chế nhận diện luồng thông minh: Luôn ưu tiên luồng chuẩn **HLS H.264 (MPEG-TS)** thay vì FLV.
  - Tích hợp bộ tiền xử lý **ISO/IEC 14496-15 AVCC Bitstream Sanitizer** trong `auto_h264.py`: Tự động loại bỏ mã rác `01 64 00 1f...` do TikTok nhúng vào đầu keyframe, giải quyết dứt điểm lỗi màn hình đen hoặc thông báo *"Video file is processing slowly due to exceeding upload quota limits"* trên Google Drive.
* 📦 **Zero-Loss Staging Queue:**
  - Tự động chia nhỏ video thành các phân đoạn ngắn lưu trữ tạm trong thư mục `staging/` trên Google Drive. Khi streamer ngắt kết nối đột ngột hoặc runner hết phiên 46 phút, không bao giờ bị mất video.
  - Tự động ghép nối video hoàn chỉnh ngay khi live kết thúc, trích xuất ảnh thumbnail chính giữa video và truyền `drive_thumb_id` lên Supabase.
* 🩺 **Watchdog Tự Lành (Self-Healing Engine):**
  - Workflow `.github/workflows/repair-watchdog.yml` chạy tự động mỗi 6 giờ (`0 */6 * * *`) kích hoạt `repair_cloud_videos_engine.py --today`.
  - Tự động rà soát mọi bản ghi trong ngày, sửa chữa bitstream in-place trên Google Drive và bù thumbnail vào Supabase Storage nếu có phiên bị lỗi mạng.
* ⚡ **Live-End Watchdog & Tách File 1 Tiếng:**
  - Tự động nhận biết phòng live tắt sóng chỉ trong 15-30 giây.
  - Hỗ trợ tách file 1 giờ (`MAX_CHUNK_SECONDS = 3600`) cho các phiên phát trực tiếp xuyên đêm, tránh tràn bộ nhớ đệm ổ cứng.
* 🌐 **Google Edge CDN Tua Video Tức Thì (HTTP Range 206):**
  - Tự động gán cờ `+faststart` (đưa atom `moov` lên đầu file) cho phép trình duyệt HTML5 phát ngay lập tức và tua mượt mà đến mọi giây của video.

---

## 3. CẤU TRÚC LƯU TRỮ (GOOGLE DRIVE & SUPABASE)

### 1. Trên Google Drive
```text
Google Drive
└── tiktok-record/
    ├── active_recordings.json          <- Trạng thái phiên ghi hình đang hoạt động (TTL 180s)
    ├── islizanx/
    │   ├── staging/                    <- Thư mục tạm Staging Queue (tự dọn dẹp khi xong)
    │   ├── islizanx_2026-09-27_10-00-00.mp4
    │   └── islizanx_2026-09-27_10-00-00.jpg
    └── triuthnga770/
        ├── triuthnga770_2026-09-27_15-02-14.mp4
        └── triuthnga770_2026-09-27_15-02-14.jpg
```

### 2. Trên Supabase Cloud
* **Table `tiktok_streamers`:** Chứa danh sách các streamer được theo dõi (`username`, `created_at`).
* **Table `tiktok_recordings`:** Lưu trữ lịch sử toàn bộ video (`id`, `username`, `filename`, `size_mb`, `drive_file_id`, `thumbnail_url`, `created_at`).
* **Storage Bucket `covers`:** Lưu ảnh thumbnail chất lượng cao tại đường dẫn `record-thumbnails/{user}/{filename}.jpg`.

---

## 4. VẬN HÀNH 24/7 TRÊN GITHUB ACTIONS (CLOUD RUNNER)

### Bước 1: Thiết lập Quyền Workflow
1. Mở repository: `https://github.com/kurrukado/tiktok-recorder`
2. Vào **Settings** ➔ Menu bên trái chọn **Actions** ➔ **General**.
3. Kéo xuống mục **Workflow permissions** ➔ Chọn: **Read and write permissions** ➔ Bấm **Save**.

### Bước 2: Cài đặt 5 Biến Secrets Bắt Buộc
Vào **Settings** ➔ **Secrets and variables** ➔ **Actions** ➔ Bấm **New repository secret**:

| Tên Secret | Giá trị |
| :--- | :--- |
| `GOOGLE_CLIENT_ID` | Client ID ứng dụng Google Cloud |
| `GOOGLE_CLIENT_SECRET` | Client Secret ứng dụng Google Cloud |
| `GDRIVE_REFRESH_TOKEN` | OAuth2 Refresh Token Google Drive |
| `SUPABASE_URL` | URL dự án Supabase (VD: `https://jetwtqakyxjcffbwhhot.supabase.co`) |
| `SUPABASE_KEY` | Supabase Anon Key hoặc Service Role Key |
| `WORKFLOW_PAT` | *(Tùy chọn)* GitHub Personal Access Token để kích hoạt relay ngay lập tức |
| `TIKTOK_SESSION_ID` | *(Tùy chọn)* Cookie `sessionid_ss` nếu cần ghi live 18+/VIP |

### Bước 3: Kích hoạt Runner
1. Vào tab **Actions** trên GitHub.
2. Chọn workflow **TikTok 24-7 Auto Recorder** ở cột bên trái.
3. Bấm **Run workflow** ➔ **Run workflow**.
*(Sau khi kích hoạt, runner sẽ tự động relay nối tiếp nhau và duy trì 24/7 bằng cron fallback 50 phút).*

---

## 5. VẬN HÀNH CỤC BỘ TRÊN MÁY TÍNH CÁ NHÂN (GPU NVENC)

Nếu bạn muốn chạy ghi hình trực tiếp tại máy tính cá nhân để tận dụng card đồ họa rời NVIDIA:
1. Cài đặt thư viện Python:
   ```cmd
   pip install -r requirements.txt
   ```
2. Cấu hình file `config.json` (tham khảo mẫu `config.example.json`).
3. Khởi động daemon tự phục hồi:
   ```cmd
   run_cloud_daemon.bat
   ```
   *File batch này tích hợp sẵn bộ canh chừng (Watchdog), tự động khởi động lại sau 5 giây nếu gặp sự cố mạng.*

---

## 6. TÍCH HỢP API VÀO WEBSITE & ỨNG DỤNG

### Cách 1: Serverless qua Supabase API (Khuyên dùng - 0 Giây Chờ Đợi)
Website của bạn không cần dựng server trung gian, gọi trực tiếp Supabase REST API:

```javascript
import { createClient } from '@supabase/supabase-js';

const supabase = createClient('SUPABASE_URL', 'SUPABASE_KEY');

// 1. Thêm streamer mới cần theo dõi
async function addStreamer(username) {
  const clean = username.trim().replace('@', '').toLowerCase();
  await supabase.from('tiktok_streamers').upsert({ username: clean });
  console.log(`Đã thêm @${clean}. Bot sẽ tự động quét và ghi sau tối đa 20 giây!`);
}

// 2. Xóa streamer
async function removeStreamer(username) {
  const clean = username.trim().replace('@', '').toLowerCase();
  await supabase.from('tiktok_streamers').delete().eq('username', clean);
}

// 3. Lấy danh sách video mới nhất
async function getRecordings() {
  const { data } = await supabase
    .from('tiktok_recordings')
    .select('*')
    .order('created_at', { ascending: false });
  return data;
}
```

### Cách 2: Kích hoạt Ghi hình Tức thì qua GitHub Actions Dispatch API
Gửi request từ Next.js / Node.js backend:
```javascript
await fetch('https://api.github.com/repos/kurrukado/tiktok-recorder/actions/workflows/recorder.yml/dispatches', {
  method: 'POST',
  headers: {
    'Accept': 'application/vnd.github+v3+json',
    'Authorization': `Bearer ${GITHUB_PAT}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({ ref: 'main' })
});
```

---

## 7. XEM VIDEO TRỰC TUYẾN & TẢI TỐC ĐỘ CAO (GOOGLE EDGE CDN)

Mỗi bản ghi trả về mã `drive_file_id`. URL phát video chuẩn Google Edge CDN:
`https://drive.usercontent.google.com/download?id={drive_file_id}&export=download&authuser=0&confirm=t`

### Nhúng phát trực tiếp HTML5:
```html
<video width="720" height="1280" controls poster="THUMBNAIL_URL_SUPABASE">
  <source src="https://drive.usercontent.google.com/download?id=DRIVE_FILE_ID&export=download&authuser=0&confirm=t" type="video/mp4">
  Trình duyệt của bạn không hỗ trợ phát video HTML5.
</video>
```

---

## 8. GIẢI THÍCH CHI TIẾT CÁC FILE TRONG MÃ NGUỒN

| Tên file | Chức năng chính |
| :--- | :--- |
| `cloud_daemon.py` | Tiến trình điều phối chính, đồng bộ streamer từ Supabase, quản lý đa luồng tối đa 10 streamer |
| `recorder_core.py` | Lõi trích xuất luồng TikTok (ưu tiên HLS H.264), Live-End Watchdog, FFmpeg recorder |
| `auto_h264.py` | Bộ xử lý bitstream ISO/IEC 14496-15, sửa lỗi NAL Unit, trích xuất thumbnail chính giữa |
| `staging_queue.py` | Đóng gói phân đoạn video an toàn chống mất mát dữ liệu, ghép nối file hoàn chỉnh |
| `supabase_sync.py` | Khách REST client đồng bộ dữ liệu hai chiều với Supabase Database & Storage |
| `repair_cloud_videos_engine.py` | Động cơ quét và tự lành video trên Google Drive, bù thumbnail thiếu |
| `gdrive_manager.py` | Quản lý Google Drive API v3: Upload đa phân đoạn, phân quyền CDN, dọn rác |
| `gdrive_auth.py` | Bộ cấp phép và tự động làm mới OAuth2 Refresh Token Google Drive |
| `api_server.py` | Máy chủ REST API FastAPI cung cấp các endpoint bổ trợ và kiểm thử |
| `run_cloud_daemon.bat` | Script chạy ngầm cục bộ với GPU NVIDIA NVENC, tự khởi động lại sau 5s |
| `test_audit_suite.py` | Bộ kiểm thử tự động toàn diện 61 bài test (Đạt 61/61 OK) |
| `test_challenger_concurrency.py`| Bộ kiểm thử đối kháng đa luồng 26 bài test (Đạt 26/26 OK) |
| `.github/workflows/recorder.yml` | Workflow GitHub Actions chạy liên tục 24/7 (Relay + Cron 50 phút) |
| `.github/workflows/repair-watchdog.yml` | Workflow GitHub Actions tự phục hồi chạy định kỳ mỗi 6 giờ |

---

## 9. CẬP NHẬT COOKIE TIKTOK KHI CẦN

Khi cần ghi hình các phòng live giới hạn độ tuổi (18+) hoặc VIP Sub-Only:
1. Đăng nhập tài khoản TikTok trên trình duyệt máy tính.
2. Bấm phím **F12** ➔ Chọn tab **Application** (hoặc **Bộ nhớ**) ➔ Chọn mục **Cookies** (`https://www.tiktok.com`).
3. Tìm dòng có tên **`sessionid_ss`** và sao chép chuỗi ký tự giá trị.
4. Cập nhật vào secret **`TIKTOK_SESSION_ID`** trong **GitHub Secrets** của repository `kurrukado/tiktok-recorder`.
