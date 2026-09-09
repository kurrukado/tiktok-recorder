# 📖 HƯỚNG DẪN SỬ DỤNG HỆ THỐNG TIKTOK RECORDER 24/7 & REST API

Hệ thống tự động theo dõi, ghi hình livestream TikTok chuẩn HD H.264 (AVC) và tự động đồng bộ lên Google Drive. Hỗ trợ chạy ngầm 24/7 trên đám mây miễn phí và cung cấp REST API để tích hợp vào website khác.

---

## 📑 MỤC LỤC
1. [Tính năng nổi bật](#1-tính-năng-nổi-bật)
2. [Cấu trúc thư mục lưu trữ](#2-cấu-trúc-thư-mục-lưu-trữ)
3. [Cách 1: Chạy 24/7 trên Đám mây GitHub Actions (Tắt máy tính vẫn chạy)](#3-cách-1-chạy-247-trên-đám-mây-github-actions-tắt-máy-tính-vẫn-chạy)
4. [Cách 2: Chạy trực tiếp trên máy tính của bạn](#4-cách-2-chạy-trực-tiếp-trên-máy-tính-của-bạn)
5. [Cách 3: Tích hợp REST API vào Website khác](#5-cách-3-tích-hợp-rest-api-vào-website-khác)
6. [Tải xuống video với tốc độ cao](#6-tải-xuống-video-với-tốc-độ-cao)
7. [Giải thích các file trong mã nguồn](#7-giải-thích-các-file-trong-mã-nguồn)
8. [Cách cập nhật Cookie TikTok (Khi cần)](#8-cách-cập-nhật-cookie-tiktok-khi-cần)

---

## 1. TÍNH NĂNG NỔI BẬT

- 🟢 **Chạy ngầm 24/7 trên Cloud:** Chạy trên máy chủ Microsoft/GitHub miễn phí, không tốn điện, bạn tắt máy tính vẫn tự động quay.
- ⚡ **Ghi hình Đa Luồng Song Song (Tối đa 10 streamer cùng lúc):** Không bị chặn tuần tự; nếu 5-10 người cùng live một lúc, bot sẽ tạo 10 luồng riêng để quay đồng thời tất cả mọi người.
- ✂️ **Tự động Cắt Chia Nhỏ Dưới 2 Tiếng (< 2h/phần):** Mỗi video live tối đa 1 tiếng 56 phút. Nếu live dài 6-8 tiếng, bot tự động đóng file Part 1, cắt ảnh thumbnail, up lên Google Drive, và **ngay lập tức ghi tiếp Part 2** hoàn toàn không làm gián đoạn bot.
- 🔴 **Bỏ qua giới hạn 18+:** Sử dụng thuật toán bóc tách SIGI_STATE kết hợp cookie `sessionid_ss`, không bị lỗi chặn lứa tuổi của TikTok.
- 🎬 **Chuẩn nén H.264 (AVC) + AAC:** Xuất file MP4 chuẩn quốc tế, mở xem được ngay trên mọi điện thoại (iPhone, Android) và máy tính mà không cần cài thêm phần mềm.
- ☁️ **Đồng bộ tự động lên Google Drive:** Tự động tạo thư mục riêng cho từng streamer trên Google Drive của bạn (`tiktok-record/<user>/`).
- ⚡ **REST API đầy đủ CORS & Giám sát Live Status:** Cung cấp API trực tuyến trên Cloud (Render) để xem ai đang được quay, thêm/xóa streamer và tải video tốc độ cao.

---

## 2. CẤU TRÚC THƯ MỤC LƯU TRỮ

### Trên Google Drive của bạn:
```text
Google Drive
└── tiktok-record/
    ├── islizanx/
    │   └── islizanx_2026-09-09_00-33-27.mp4
    ├── itsme_kate0110/
    │   └── itsme_kate0110_live_2026-08-21_09-35-12.mp4
    └── urielhui38/
        └── urielhui38_2026-09-08_23-43-48.mp4
```

### Trên máy tính cá nhân:
```text
D:\New folder\
├── islizanx/           <- Chứa video của @islizanx
├── itsme_kate0110/     <- Chứa video của @itsme_kate0110
└── urielhui38/         <- Chứa video của @urielhui38
```

---

## 3. CÁCH 1: CHẠY 24/7 TRÊN ĐÁM MÂY GITHUB ACTIONS (TẮT MÁY TÍNH VẪN CHẠY)

Đây là cách tốt nhất để bạn không cần bật máy tính.

### Bước 1: Cấp quyền Workflow trên GitHub
1. Vào kho lưu trữ của bạn: https://github.com/kurrukado/tiktok-recorder
2. Vào tab **Settings** ➔ Menu bên trái chọn **Actions** ➔ **General**.
3. Kéo xuống mục **Workflow permissions** ➔ Tích chọn: **Read and write permissions** ➔ Bấm **Save**.

### Bước 2: Thêm 4 biến bí mật (Secrets)
Vào **Settings** ➔ **Secrets and variables** ➔ **Actions** ➔ Bấm **New repository secret**:
- `GOOGLE_CLIENT_ID`: Nhập Google OAuth Client ID của bạn
- `GOOGLE_CLIENT_SECRET`: Nhập Google OAuth Client Secret của bạn
- `GDRIVE_REFRESH_TOKEN`: Nhập Refresh Token Google Drive của bạn
- `TIKTOK_SESSION_ID`: Nhập sessionid_ss từ cookie TikTok của bạn

### Bước 3: Bấm chạy
1. Vào tab **Actions** trên GitHub.
2. Bấm vào quy trình **TikTok 24-7 Auto Recorder** ở cột bên trái.
3. Bấm **Run workflow** ➔ **Run workflow**.

> 💡 **Cơ chế:** Máy chủ Microsoft sẽ chạy liên tục, tự canh live, tự tải video vào Google Drive, và tự kích hoạt phiên tiếp theo để duy trì 24/7 vĩnh viễn.

---

## 4. CÁCH 2: CHẠY TRỰC TIẾP TRÊN MÁY TÍNH CỦA BẠN

Nếu bạn đang bật máy tính và muốn ghi hình trực tiếp bằng card màn hình GPU:

### 1. Bật chế độ tự động canh live:
```powershell
python tiktok_recorder.py
```
*(Chương trình sẽ kiểm tra danh sách người dùng mỗi 20 giây, ai live là tự quay ngay).*

### 2. Quay ngay lập tức một người:
```powershell
python tiktok_recorder.py --user islizanx --now
```

### 3. Tải toàn bộ video hiện có lên Google Drive:
```powershell
python gdrive_manager.py --upload-all
```

---

## 5. CÁCH 3: TÍCH HỢP REST API VÀO WEBSITE KHÁC

API được lập trình bằng FastAPI và đã được triển khai chạy trực tuyến 24/7 trên Cloud (Render.com), hỗ trợ đầy đủ CORS và chứng chỉ bảo mật HTTPS. Bạn không cần bật máy tính ở nhà mà website tích hợp vẫn hoạt động bình thường.

### 🌐 Địa chỉ Server API Trực Tuyến:
- **API Base URL:** `https://tiktok-api-as2y.onrender.com`
- **Giao diện thử nghiệm trực quan (Swagger UI):** [`https://tiktok-api-as2y.onrender.com/docs`](https://tiktok-api-as2y.onrender.com/docs)

*(Nếu bạn muốn chạy server trực tiếp dưới máy tính cá nhân, chỉ cần chạy lệnh: `python api_server.py`, server sẽ lắng nghe tại `http://localhost:8000`).*

---

### 🔄 Cơ chế Đồng bộ & Quản lý Thư mục Tự động (Google Drive $\leftrightarrow$ Web $\leftrightarrow$ Bot GitHub):
1. **Khi bạn bấm "Thêm streamer" (`POST /api/users`) trên Website:**
   - API tự động **tạo ngay lập tức thư mục `tiktok-record/<tên_streamer>/` trên Google Drive** của bạn. Bạn mở Google Drive ra là thấy thư mục xuất hiện ngay!
   - Cập nhật streamer mới vào file `streamers.json` trên Google Drive.
   - Bot **GitHub Actions** (đang chạy ngầm 24/7) tự động nhận diện streamer mới này và bắt đầu ghi hình ngay nếu họ đang phát trực tiếp.
2. **Khi bạn bấm "Xóa streamer" (`DELETE /api/users/{username}`) trên Dashboard Web:**
   - API tự động **xóa vĩnh viễn thư mục `tiktok-record/<tên_streamer>/` trên Google Drive** (kèm toàn bộ video bên trong nếu có) để giải phóng dung lượng Google Drive cho bạn.
   - Xóa streamer khỏi danh sách theo dõi, bot GitHub Actions sẽ ngừng quay streamer đó.

> ⚠️ **LƯU Ý QUAN TRỌNG (Để API Render có quyền truy cập Google Drive):**
> Trong [Render Dashboard](https://dashboard.render.com/) ➔ Chọn dịch vụ `tiktok-api-as2y` ➔ Mục **Environment** ➔ Bắt buộc phải có 3 biến môi trường sau để Render có quyền tạo/xóa thư mục trên Google Drive:
> - `GOOGLE_CLIENT_ID`: (Nhập giá trị `google_client_id` trong file config.json)
> - `GOOGLE_CLIENT_SECRET`: (Nhập giá trị `google_client_secret` trong file config.json)
> - `GDRIVE_REFRESH_TOKEN`: (Nhập giá trị `gdrive_refresh_token` trong file config.json)

---

### Bảng chi tiết các API chính:

| Phương thức | Endpoint URL | Chức năng |
| :--- | :--- | :--- |
| `GET` | `/api/recordings/active` | **Xem ai đang được ghi hình thời gian thực** (Đồng bộ trực tiếp từ Cloud Bot) |
| `GET` | `/api/users` | Lấy danh sách streamer đang theo dõi (kèm trạng thái `is_recording` chuẩn 100%) |
| `POST` | `/api/users` | **Thêm streamer & Tự động tạo thư mục riêng trên Google Drive** |
| `DELETE` | `/api/users/{username}` | **Xóa streamer & Tự động xóa vĩnh viễn thư mục trên Google Drive** |
| `GET` | `/api/stream/{username}` | Lấy link stream CDN trực tiếp để tải/xem tốc độ cao |
| `POST` | `/api/record/start` | Kích hoạt bắt đầu ghi hình ngay lập tức |
| `GET` | `/api/recordings` | Lấy danh sách toàn bộ file video đã quay (kèm thời lượng, dung lượng, link thumbnail) |
| `GET` | `/api/thumbnail/{user}/{filename}` | **Lấy ảnh xem trước (Thumbnail)** tự động cắt từ chính giữa video (50% thời lượng) |
| `GET` | `/api/download/{user}/{filename}` | Tải file video về máy (Hỗ trợ IDM, đa luồng) |

---

### Code mẫu JavaScript để nhúng vào Website của bạn:

```javascript
// Đường dẫn API trực tuyến trên Render (Hoạt động 24/7)
const API_BASE = "https://tiktok-api-as2y.onrender.com";

// 1. Lấy danh sách streamer đang theo dõi
async function getStreamers() {
  try {
    const res = await fetch(`${API_BASE}/api/users`);
    const data = await res.json();
    console.log("Danh sách streamer:", data.streamers);
    return data.streamers; // Ví dụ: ['islizanx', 'itsme_kate0110', 'urielhui38']
  } catch (err) {
    console.error("Lỗi khi tải danh sách:", err);
  }
}

// 2. Thêm một streamer mới
async function addStreamer(username) {
  const cleanUser = username.trim().replace('@', '').toLowerCase();
  try {
    const res = await fetch(`${API_BASE}/api/users`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: cleanUser })
    });
    const data = await res.json();
    alert(data.message);
  } catch (err) {
    alert("Không thể kết nối tới server API!");
  }
}

// 3. Xóa một streamer khỏi danh sách
async function deleteStreamer(username) {
  const cleanUser = username.trim().replace('@', '').toLowerCase();
  try {
    const res = await fetch(`${API_BASE}/api/users/${cleanUser}`, {
      method: "DELETE"
    });
    const data = await res.json();
    alert(data.message);
  } catch (err) {
    alert("Không thể kết nối tới server API!");
  }
}

// 4. Lấy link CDN tải tốc độ tối đa đường truyền
async function getHighSpeedDownload(username) {
  const cleanUser = username.trim().replace('@', '').toLowerCase();
  const res = await fetch(`${API_BASE}/api/stream/${cleanUser}`);
  const data = await res.json();
  if (data.is_live) {
    window.open(data.stream_url);
  } else {
    alert('Streamer hiện không online');
  }
}
```

---

## 6. TẢI XUỐNG VIDEO VỚI TỐC ĐỘ CAO

Hệ thống cung cấp 2 giải pháp tải tốc độ cao:

1. **Tải trực tiếp từ máy chủ CDN TikTok (Tối đa đường truyền):**
   - Sử dụng API `GET /api/stream/{username}` để lấy link CDN gốc (`pull-flv-...tiktokcdn.com`).
   - Mở link này bằng trình duyệt hoặc phần mềm IDM để tải với tốc độ tối đa của mạng Internet (hàng chục MB/giây) mà không tiêu tốn băng thông của máy chủ bạn.

2. **Tải từ Google Drive:**
   - Tất cả video được tự động đẩy lên thư mục `tiktok-record/` trên Google Drive của bạn.
   - Bạn có thể bật tính năng chia sẻ link của Google Drive để bất kỳ ai cũng có thể tải về với tốc độ cao không giới hạn từ hạ tầng máy chủ của Google.

---

## 7. GIẢI THÍCH CÁC FILE TRONG MÃ NGUỒN

| Tên file | Chức năng |
| :--- | :--- |
| `cloud_daemon.py` | Kịch bản chạy ngầm 24/7 trên đám mây GitHub Actions |
| `api_server.py` | Máy chủ REST API (FastAPI) để kết nối vào website bên ngoài |
| `recorder_core.py` | Lõi bóc tách livestream TikTok, giải mã 18+ và thu sóng FFmpeg |
| `auto_h264.py` | Tự động kiểm tra và chuyển mã sang chuẩn H.264 (AVC) + AAC |
| `gdrive_manager.py`| Tự động tạo thư mục và tải video lên Google Drive qua OAuth |
| `gdrive_auth.py` | Trình xác thực và lấy Refresh Token của Google Drive |
| `config.json` | Cấu hình danh sách người dùng và mã xác thực Google Drive |
| `cookies.json` | Chứa cookie phiên làm việc TikTok (sessionid_ss) |
| `.github/workflows/recorder.yml` | File cấu hình chạy tự động 24/7 của GitHub Actions |

---

## 8. CÁCH CẬP NHẬT COOKIE TIKTOK (KHI CẦN)

Nếu sau vài tháng cookie bị hết hạn hoặc bạn đổi mật khẩu TikTok:
1. Đăng nhập TikTok trên trình duyệt máy tính.
2. Bấm phím **F12** ➔ Chọn tab **Application** (hoặc **Bộ nhớ**) ➔ Chọn mục **Cookies** (`https://www.tiktok.com`).
3. Tìm dòng có tên **`sessionid_ss`** và copy giá trị chuỗi ký tự đó.
4. Cập nhật lại vào:
   - File `cookies.json` trên máy tính.
   - Biến `TIKTOK_SESSION_ID` trong mục **GitHub Secrets** của bạn.
