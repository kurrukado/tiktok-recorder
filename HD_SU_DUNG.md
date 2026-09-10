# 📖 HƯỚNG DẪN SỬ DỤNG HỆ THỐNG TIKTOK RECORDER 24/7 & REST API

Hệ thống tự động theo dõi, ghi hình livestream TikTok chuẩn HD H.264 (AVC) và tự động đồng bộ lên Google Drive. Hệ thống hoạt động hoàn toàn miễn phí 24/7 trên đám mây (GitHub Actions & Render) và cung cấp REST API tốc độ cao để tích hợp vào bất kỳ website nào.

---

## 📑 MỤC LỤC
1. [Kiến trúc hệ thống Cloud 100% Miễn phí](#1-kiến-trúc-hệ-thống-cloud-100-miễn-phí)
2. [Tính năng nổi bật mới cập nhật](#2-tính-năng-nổi-bật-mới-cập-nhật)
3. [Cấu trúc thư mục lưu trữ](#3-cấu-trúc-thư-mục-lưu-trữ)
4. [Cách 1: Vận hành 24/7 trên Đám mây (Không cần bật máy tính)](#4-cách-1-vận-hành-247-trên-đám-mây-không-cần-bật-máy-tính)
5. [Cách 2: Chạy trực tiếp trên máy tính cá nhân](#5-cách-2-chạy-trực-tiếp-trên-máy-tính-cá-nhân)
6. [Cách 3: Tích hợp REST API vào Website khác](#6-cách-3-tích-hợp-rest-api-vào-website-khác)
7. [Xem Video Trực Tuyến & Tải Tốc Độ Cao (Google Edge CDN)](#7-xem-video-trực-tuyến--tải-tốc-độ-cao-google-edge-cdn)
8. [Giải thích các file trong mã nguồn](#8-giải-thích-các-file-trong-mã-nguồn)
9. [Cách cập nhật Cookie TikTok (Khi cần)](#9-cách-cập-nhật-cookie-tiktok-khi-cần)

---

## 1. KIẾN TRÚC HỆ THỐNG CLOUD 100% MIỄN PHÍ

```text
               ┌────────────────────────────────────────────────────────┐
               │              GIAO DIỆN WEB CỦA BẠN                     │
               │   (Hiển thị Streamer, Xem Video HTML5, Tải IDM CDN)    │
               └───────────────▲────────────────────────▲───────────────┘
                               │                        │
                    REST API (JSON)            Google Edge CDN URL
                               │                 (Tua video Range 206)
                               ▼                        │
┌──────────────────────────────────────────────────┐    │
│              RENDER.COM (API SERVER)             │    │
│  - Endpoint: https://tiktok-api-as2y.onrender.com│    │
│  - Tiếp nhận thêm/xóa streamer                   │    │
│  - Tự động tạo/xóa thư mục trên Google Drive     │    │
│  - Cung cấp link phát CDN và ảnh Thumbnail 50%   │    │
└──────────────────────┬───────────────────────────┘    │
                       │ Đồng bộ danh sách              │
                       ▼ streamers.json                 │
┌──────────────────────────────────────────────────┐    │
│                 GOOGLE DRIVE                     │────┘
│  - Thư mục gốc: tiktok-record/                   │
│  - Chứa streamers.json (danh sách theo dõi)      │
│  - Lưu trữ video MP4 và ảnh Thumbnail JPG        │
└──────────────────────▲───────────────────────────┘
                       │ Upload video tự động (sau 30s)
┌──────────────────────┴───────────────────────────┐
│           GITHUB ACTIONS (CLOUD RUNNER)          │
│  - Chạy ngầm 24/7 vĩnh viễn (Ubuntu + FFmpeg)    │
│  - Kiểm tra live đa luồng mỗi 15 giây            │
│  - Live-End Watchdog (nhận biết tắt live tức thì)│
│  - Chuyển mã H.264 (AVC) + Đóng file MP4 sạch sẽ │
└──────────────────────────────────────────────────┘
```

---

## 2. TÍNH NĂNG NỔI BẬT MỚI CẬP NHẬT

- ⚡ **Live-End Watchdog (Tải lên Drive sau 30-45 giây):**
  - Khắc phục triệt để lỗi streamer tắt live nhưng video không tải lên (do kết nối mạng giữ trạng thái treo).
  - Tích hợp bộ giám sát dữ liệu đọng (Stagnant Data Watchdog): Khi file ngừng tăng dung lượng quá 12 giây, hệ thống tự động kiểm tra trạng thái phòng live. Nếu streamer đã tắt live, bot lập tức đóng luồng FFmpeg sạch sẽ (ghi đầy đủ `moov atom`), cắt thumbnail và **tải video lên Google Drive chỉ trong 30-45 giây**.
- 🚀 **Phát hiện & Ghi hình Thần tốc (15 - 35 giây):**
  - Khi bạn thêm streamer mới trên Website (`POST /api/users`), hệ thống đồng bộ ngay lên Google Drive. Bot đám mây quét danh sách mỗi 15 giây và sẽ bắt đầu ghi hình ngay sau 15-35 giây nếu người đó đang phát trực tiếp.
- 🌐 **Google Edge CDN & Tua Video Tức Thì (HTTP Range 206):**
  - Tất cả video được tự động phân quyền công khai và sinh link tải trực tiếp qua máy chủ Google Edge CDN (`drive.usercontent.google.com`).
  - Hỗ trợ chuẩn **HTTP 206 Partial Content**: Bạn có thể nhúng trực tiếp vào thẻ `<video controls>` trên website để xem và tua đến bất kỳ đoạn nào mà không cần đợi tải toàn bộ video về.
  - Hỗ trợ tải đa luồng qua IDM, Aria2 với băng thông tối đa của đường truyền Internet (100MB/s+).
- 📁 **Quản lý Vòng đời Thư mục Tự động trên Google Drive:**
  - `POST /api/users`: Tự động tạo thư mục riêng `tiktok-record/<tên_streamer>/` trên Google Drive ngay tức thì.
  - `DELETE /api/users/{username}`: Tự động xóa vĩnh viễn thư mục của streamer trên Google Drive (kèm toàn bộ video đã lưu) để tự động giải phóng dung lượng bộ nhớ.
- 🖼️ **Thumbnail 50% Thời Lượng & Timestamp Chuẩn:**
  - Mỗi video được tự động cắt ảnh đại diện tại thời điểm chính giữa (50% thời lượng video) bằng FFmpeg.
  - API `GET /api/recordings` trả về trường `recorded_at` (`YYYY-MM-DD HH:MM:SS`) và `thumbnail_url` giúp website hiển thị lịch sử ghi hình trực quan, đẹp mắt.
- 🤖 **Vận Hành Tự Động 100% 24/7 (Không Cần Thao Tác Thủ Công):**
  - Loại bỏ hoàn toàn nút "Ghi ngay" và "Dừng ghi" thủ công. Bot đám mây (GitHub Actions) tự động quét và ghi hình ngay khi streamer phát sóng, tự đóng luồng và dọn dẹp khi tắt live.
- ✂️ **Tự Động Cắt Tách Video Tối Đa 2 Tiếng (2-Hour Chunking):**
  - Mỗi video giới hạn tối đa 2 giờ (`MAX_CHUNK_SECONDS = 7200`). Nếu live kéo dài > 2h, bot tự chốt file, chuyển mã H.264, upload Drive/Supabase và nối tiếp ghi Part 2, Part 3... liền mạch không gián đoạn.
- 🎬 **Trình Phát Video Kép Trực Tiếp Trên Web (Dual Player):**
  - Tích hợp xem trực tiếp trên Web gồm Google Cloud Player (1080p iframe) và HTML5 Direct Player hỗ trợ HTTP Range 206 tua mượt mà, không cần mở Google Drive thủ công.
- 🛡️ **Chuẩn Hóa H.264 MP4 Không Phụ Đề & Chống Lỗi 0:00s:**
  - Tự động chuyển mã H.264 (8-bit YUV420p) không dính phụ đề (`-sn -dn`), tương thích mọi thiết bị; bộ thẩm định `validate_playable_video` ngăn chặn 100% video rác 0:00s.
- 🔴 **Vượt Rào Cản 18+ & Không Bị Chặn:**
  - Bóc tách dữ liệu SIGI_STATE kết hợp Cookie `sessionid_ss`, không bị lỗi giới hạn độ tuổi của TikTok.
- 🔒 **Hỗ Trợ VIP Sub-Only & Thread-Safe Drive:**
  - Nhận diện phòng live giới hạn Hội viên Sub-Only và tự sinh guest session fingerprint để trích xuất link xem trước.
  - Khóa luồng `_DRIVE_STATUS_LOCK` đồng bộ danh sách `active_recordings.json` trên Google Drive an toàn đa luồng kèm TTL tự dọn dẹp.

---

## 3. CẤU TRÚC THƯ MỤC LƯU TRỮ

### Trên Google Drive của bạn:
```text
Google Drive
└── tiktok-record/
    ├── streamers.json                  <- Danh sách streamer đang được theo dõi
    ├── islizanx/
    │   ├── islizanx_2026-09-09_22-51-16.mp4
    │   └── islizanx_2026-09-09_22-51-16.jpg   (Thumbnail 50%)
    ├── itsme_kate0110/
    │   ├── itsme_kate0110_2026-08-21_09-35-12.mp4
    │   └── itsme_kate0110_2026-08-21_09-35-12.jpg
    └── urielhui38/
        ├── urielhui38_2026-09-08_23-43-48.mp4
        └── urielhui38_2026-09-08_23-43-48.jpg
```

---

## 4. CÁCH 1: VẬN HÀNH 24/7 TRÊN ĐÁM MÂY (KHÔNG CẦN BẬT MÁY TÍNH)

Đây là chế độ khuyên dùng: toàn bộ quá trình canh live, ghi hình, nén file và tải lên Google Drive đều do máy chủ Microsoft/GitHub thực hiện hoàn toàn miễn phí.

### Bước 1: Cấp quyền Workflow trên GitHub
1. Mở repository: `https://github.com/kurrukado/tiktok-recorder`
2. Vào **Settings** ➔ Menu bên trái chọn **Actions** ➔ **General**.
3. Kéo xuống mục **Workflow permissions** ➔ Tích chọn: **Read and write permissions** ➔ Bấm **Save**.

### Bước 2: Cài đặt 4 biến Secrets trên GitHub
Vào **Settings** ➔ **Secrets and variables** ➔ **Actions** ➔ Bấm **New repository secret**:
- `GOOGLE_CLIENT_ID`: Nhập Client ID từ file `config.json`
- `GOOGLE_CLIENT_SECRET`: Nhập Client Secret từ file `config.json`
- `GDRIVE_REFRESH_TOKEN`: Nhập Refresh Token Google Drive từ file `config.json`
- `TIKTOK_SESSION_ID`: Nhập sessionid_ss từ file `cookies.json`

### Bước 3: Kích hoạt Bot
1. Vào tab **Actions** trên GitHub.
2. Chọn workflow **TikTok 24-7 Auto Recorder** ở cột bên trái.
3. Bấm **Run workflow** ➔ **Run workflow**.

> 💡 **Cơ chế tự duy trì:** Bot chạy liên tục theo chu kỳ. Khi phiên runner sắp chạm giới hạn thời gian của GitHub, quy trình tự kích hoạt phiên tiếp theo để duy trì trạng thái 24/7 vĩnh viễn không ngắt quãng.

---

## 5. CÁCH 2: CHẠY TRỰC TIẾP TRÊN MÁY TÍNH CÁ NHÂN

Nếu bạn muốn chạy trực tiếp trên máy bàn hoặc laptop cá nhân:

```powershell
# 1. Bật tự động canh live liên tục:
python tiktok_recorder.py

# 2. Ghi hình ngay lập tức một streamer cụ thể:
python tiktok_recorder.py --user islizanx --now

# 3. Quét và tải thủ công toàn bộ video lên Google Drive:
python gdrive_manager.py --upload-all
```

---

## 6. CÁCH 3: TÍCH HỢP REST API VÀO WEBSITE KHÁC

Hệ thống cung cấp REST API trực tuyến chuẩn OpenAPI/Swagger được triển khai sẵn trên Cloud (Render):
- **Base URL:** `https://tiktok-api-as2y.onrender.com`
- **Tài liệu trực quan & Test API (Swagger UI):** [`https://tiktok-api-as2y.onrender.com/docs`](https://tiktok-api-as2y.onrender.com/docs)
- **Hỗ trợ CORS:** `*` (Cho phép mọi frontend gọi trực tiếp qua AJAX / Fetch).

### Bảng chi tiết các API:

| Phương thức | Endpoint URL | Mô tả chức năng |
| :--- | :--- | :--- |
| `GET` | `/api/users` | Lấy danh sách streamer, kèm trạng thái live và `status: "recording"` |
| `POST` | `/api/users` | **Thêm streamer mới & Tự động tạo thư mục riêng trên Google Drive** |
| `DELETE` | `/api/users/{username}` | **Xóa streamer & Tự động xóa vĩnh viễn thư mục trên Google Drive** |
| `GET` | `/api/recordings/active` | **Xem ai đang được bot quay thời gian thực** (Kèm phòng live và file tạm) |
| `GET` | `/api/recordings` | Lấy danh sách video đã quay (kèm `recorded_at`, link ảnh 50%, link CDN) |
| `GET` | `/api/cdn/{user}/{filename}` | **Lấy link Google Edge CDN trực tiếp** (Phục vụ stream Range 206 / IDM) |
| `GET` | `/api/download/{user}/{filename}` | Tải video (Redirect 302 trực tiếp sang Google Edge CDN) |
| `GET` | `/api/thumbnail/{user}/{filename}` | Lấy ảnh đại diện cắt tại mốc 50% video |
| `GET` | `/api/stream/{username}` | Lấy link stream gốc của TikTok để phát trực tiếp |

---

### Cấu trúc dữ liệu mẫu (JSON Response):

#### 1. Lấy danh sách streamer (`GET /api/users`):
```json
{
  "streamers": ["islizanx", "urielhui38"],
  "details": [
    {
      "username": "islizanx",
      "is_live": true,
      "is_recording": true,
      "status": "recording",
      "room_id": "7683563035756694279"
    },
    {
      "username": "urielhui38",
      "is_live": false,
      "is_recording": false,
      "status": "offline",
      "room_id": null
    }
  ]
}
```

#### 2. Lấy danh sách video đã lưu (`GET /api/recordings`):
```json
{
  "count": 1,
  "recordings": [
    {
      "user": "islizanx",
      "filename": "islizanx_2026-09-09_22-51-16.mp4",
      "size": 154201840,
      "size_human": "147.06 MB",
      "recorded_at": "2026-09-09 22:51:16",
      "url": "https://drive.google.com/uc?id=1AbCdEfGh...",
      "cdn_download_url": "https://drive.usercontent.google.com/download?id=1AbCdEfGh...&export=download&authuser=0",
      "thumbnail_url": "https://tiktok-api-as2y.onrender.com/api/thumbnail/islizanx/islizanx_2026-09-09_22-51-16.jpg"
    }
  ]
}
```

---

### Code JavaScript mẫu tích hợp Frontend:

```javascript
const API_BASE = "https://tiktok-api-as2y.onrender.com";

// 1. Thêm một streamer mới (Tự tạo thư mục trên Drive & Bot tự ghi sau 15-35s)
async function addStreamer(username) {
  const cleanUser = username.trim().replace('@', '').toLowerCase();
  const res = await fetch(`${API_BASE}/api/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: cleanUser })
  });
  const data = await res.json();
  console.log(data.message);
}

// 2. Xóa một streamer (Tự dọn dẹp & giải phóng dung lượng Drive)
async function deleteStreamer(username) {
  const cleanUser = username.trim().replace('@', '').toLowerCase();
  const res = await fetch(`${API_BASE}/api/users/${cleanUser}`, {
    method: "DELETE"
  });
  const data = await res.json();
  console.log(data.message);
}

// 3. Tải danh sách video và hiển thị lên giao diện
async function loadRecordedVideos() {
  const res = await fetch(`${API_BASE}/api/recordings`);
  const data = await res.json();
  
  data.recordings.forEach(video => {
    console.log(`Video: ${video.filename}`);
    console.log(`Thời gian ghi: ${video.recorded_at}`);
    console.log(`Ảnh đại diện: ${video.thumbnail_url}`);
    console.log(`Link phát tốc độ cao: ${video.cdn_download_url}`);
  });
}
```

---

## 7. XEM VIDEO TRỰC TUYẾN & TẢI TỐC ĐỘ CAO (GOOGLE EDGE CDN)

Hệ thống sử dụng cơ chế liên kết trực tiếp Google Edge CDN:
`https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0`

### 1. Nhúng phát trực tiếp trên Website bằng thẻ `<video>` HTML5:
Nhờ hỗ trợ chuẩn **HTTP 206 Partial Content**, người dùng xem web có thể phát ngay và tua tiến/lùi mượt mà mà không phải tải toàn bộ video về máy:

```html
<video width="640" height="360" controls poster="https://tiktok-api-as2y.onrender.com/api/thumbnail/islizanx/islizanx_2026-09-09_22-51-16.jpg">
  <source src="https://drive.usercontent.google.com/download?id=MÃ_FILE_ID&export=download&authuser=0" type="video/mp4">
  Trình duyệt của bạn không hỗ trợ thẻ video.
</video>
```

### 2. Tải bằng Internet Download Manager (IDM) hoặc Trình duyệt:
- Tự động bỏ qua màn hình cảnh báo virus của Google Drive đối với file lớn trên 100MB.
- Cho phép chia nhỏ luồng tải (multithreading), đạt tốc độ tối đa của gói cước Internet.

---

## 8. GIẢI THÍCH CÁC FILE TRONG MÃ NGUỒN

| Tên file | Chức năng chính |
| :--- | :--- |
| `cloud_daemon.py` | Tiến trình điều phối chạy ngầm 24/7 trên GitHub Actions, kiểm tra live đa luồng mỗi 15s |
| `recorder_core.py` | Lõi thu sóng TikTok, giải mã 18+, tích hợp Live-End Watchdog tự ngắt khi hết live |
| `api_server.py` | Máy chủ REST API FastAPI trên Render, quản lý streamer, thư mục Drive và link CDN |
| `gdrive_manager.py`| Điều khiển Google Drive API: tạo/xóa thư mục streamer, upload file, sinh link CDN công khai |
| `gdrive_auth.py` | Tiện ích hỗ trợ cấp phép xác thực OAuth 2.0 cho Google Drive |
| `auto_h264.py` | Hỗ trợ kiểm tra và chuyển mã H.264 (AVC) + AAC nếu cần thiết |
| `config.json` | Chứa danh sách cấu hình và thông tin định danh Google Drive |
| `cookies.json` | Chứa cookie phiên đăng nhập TikTok (`sessionid_ss`) |
| `.github/workflows/recorder.yml` | Kịch bản tự động hóa điều phối vòng lặp 24/7 của GitHub Actions |

---

## 9. CÁCH CẬP NHẬT COOKIE TIKTOK (KHI CẦN)

Nếu sau thời gian dài cookie TikTok bị hết hạn hoặc bạn đổi mật khẩu:
1. Đăng nhập tài khoản TikTok trên trình duyệt máy tính.
2. Bấm phím **F12** ➔ Chọn tab **Application** (hoặc **Bộ nhớ**) ➔ Chọn mục **Cookies** (`https://www.tiktok.com`).
3. Tìm dòng có tên **`sessionid_ss`** và sao chép chuỗi ký tự giá trị.
4. Cập nhật lại vào:
   - File `cookies.json` trên máy tính: `{"sessionid_ss": "CHUỖI_MỚI"}`
   - Biến `TIKTOK_SESSION_ID` trong **GitHub Secrets** của repository.
