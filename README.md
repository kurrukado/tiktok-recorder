# 🎥 TikTok 24/7 Cloud Auto Recorder & REST API

Hệ thống tự động theo dõi, ghi hình livestream TikTok chuẩn HD H.264 (AVC) và tự động đồng bộ lên Google Drive. Hệ thống vận hành hoàn toàn miễn phí 24/7 trên đám mây (GitHub Actions & Render) và cung cấp REST API tốc độ cao để tích hợp vào website khác.

---

## 🌟 TÍNH NĂNG NỔI BẬT

- 🟢 **Chạy ngầm 24/7 trên Cloud Miễn phí:** Hoạt động bền bỉ trên máy chủ GitHub Actions, tự động canh live và ghi hình mà bạn không cần mở máy tính cá nhân.
- ⚡ **Live-End Watchdog (Upload Drive sau 30-45s):** Bộ giám sát dữ liệu thời gian thực tự động phát hiện streamer tắt live, ngắt FFmpeg sạch sẽ (ghi hoàn chỉnh `moov atom`), tạo thumbnail và đẩy video lên Google Drive trong vòng 30-45 giây.
- 🚀 **Bắt Live Thần Tốc (15-35s):** Chu kỳ đồng bộ Google Drive tối ưu xuống 15 giây. Khi thêm streamer mới trên Web, bot sẽ phát hiện và kích hoạt luồng ghi hình chỉ sau 15-35 giây nếu streamer đang live.
- 🌐 **Google Edge CDN & Tua Video Range 206:** Tự động mở quyền public và cung cấp link Direct Google Edge CDN (`drive.usercontent.google.com`). Hỗ trợ chuẩn HTTP 206 Partial Content (tua video tức thì trên thẻ `<video>` HTML5) và tải đa luồng IDM với tốc độ tối đa của đường truyền.
- 📁 **Vòng Đời Thư Mục Google Drive Tự Động:**
  - Thêm streamer trên web ➔ Tự tạo thư mục `tiktok-record/<user>/` trên Drive.
  - Xóa streamer trên web ➔ Tự xóa thư mục và video trên Drive để giải phóng dung lượng.
- 🖼️ **Thumbnail 50% & Lịch Sử Ghi Hình:** Tự động cắt ảnh đại diện tại điểm 50% thời lượng video, API cung cấp `recorded_at` theo chuẩn ngày giờ để hiển thị trực quan trên giao diện web.
- ⚡ **Ghi Hình Đa Luồng Song Song:** Ghi hình đồng thời lên tới 10 streamer cùng một thời điểm.
- ✂️ **Tự Động Cắt File Dưới 2 Tiếng:** Tự động ngắt file Part 1 tải lên Drive và tiếp tục ghi Part 2 nếu streamer phát trực tiếp xuyên đêm dài nhiều tiếng.
- 🔴 **Vượt Rào Cản 18+:** Sử dụng thuật toán bóc tách dữ liệu kết hợp cookie `sessionid_ss`, không bị chặn lứa tuổi.

---

## 🏗️ KIẾN TRÚC HỆ THỐNG

```text
               ┌────────────────────────────────────────────────────────┐
               │                  WEBSITE FRONTEND                      │
               │   (Quản lý Streamer, Xem Video HTML5, Tải Tốc Độ Cao)  │
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

## 🌐 REST API TRỰC TUYẾN

- **Base URL:** `https://tiktok-api-as2y.onrender.com`
- **Swagger Documentation:** [`https://tiktok-api-as2y.onrender.com/docs`](https://tiktok-api-as2y.onrender.com/docs)

| Method | Endpoint | Mô tả |
| :--- | :--- | :--- |
| `GET` | `/api/users` | Lấy danh sách streamer và trạng thái live (`recording`/`offline`) |
| `POST` | `/api/users` | Thêm streamer mới & tự tạo thư mục riêng trên Google Drive |
| `DELETE` | `/api/users/{username}` | Xóa streamer & tự xóa sạch thư mục trên Google Drive |
| `GET` | `/api/recordings/active` | Xem danh sách các streamer đang được ghi hình thời gian thực |
| `GET` | `/api/recordings` | Lấy danh sách video (kèm `recorded_at`, thumbnail và link CDN) |
| `GET` | `/api/cdn/{user}/{filename}` | Lấy link Google Edge CDN trực tiếp (HTTP 206 / IDM) |
| `GET` | `/api/download/{user}/{filename}` | Tải video (Redirect 302 trực tiếp sang Google Edge CDN) |
| `GET` | `/api/thumbnail/{user}/{filename}` | Lấy ảnh thumbnail 50% thời lượng của video |
| `GET` | `/api/stream/{username}` | Lấy link stream gốc TikTok để xem trực tiếp |

---

## 🚀 HƯỚNG DẪN CÀI ĐẶT NHANH

### 1. Vận hành 24/7 trên GitHub Actions (Khuyên dùng)
1. Fork hoặc clone repository này về GitHub của bạn.
2. Cấp quyền **Read and write permissions** tại **Settings** ➔ **Actions** ➔ **General**.
3. Cài đặt 4 biến Secrets tại **Settings** ➔ **Secrets and variables** ➔ **Actions**:
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `GDRIVE_REFRESH_TOKEN`
   - `TIKTOK_SESSION_ID`
4. Vào tab **Actions** ➔ Chọn workflow **TikTok 24-7 Auto Recorder** ➔ Bấm **Run workflow**.

### 2. Chạy dưới máy tính cá nhân
```powershell
# Cài đặt thư viện:
pip install -r requirements.txt

# Bật tự động canh live:
python tiktok_recorder.py

# Khởi chạy server API nội bộ:
python api_server.py
```

---

## 📁 CẤU TRÚC THƯ MỤC GOOGLE DRIVE

```text
Google Drive
└── tiktok-record/
    ├── streamers.json
    ├── islizanx/
    │   ├── islizanx_2026-09-09_22-51-16.mp4
    │   └── islizanx_2026-09-09_22-51-16.jpg
    └── urielhui38/
        ├── urielhui38_2026-09-08_23-43-48.mp4
        └── urielhui38_2026-09-08_23-43-48.jpg
```

---

## 📖 TÀI LIỆU CHI TIẾT

Vui lòng xem hướng dẫn sử dụng đầy đủ và code JavaScript mẫu tích hợp Web tại:  
👉 [**HD_SU_DUNG.md**](HD_SU_DUNG.md)
