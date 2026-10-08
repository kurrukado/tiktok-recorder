# TikTok 24/7 Auto Recorder (Cloud Multi-Stream Engine)

Hệ thống ghi hình tự động TikTok Live 24/7 vận hành **100% trên đám mây (GitHub Actions + Supabase + Google Drive + Vercel)** — **HOÀN TOÀN TỰ ĐỘNG, KHÔNG CẦN MỞ MÁY TÍNH CÁ NHÂN**.

> [!IMPORTANT]
> **TẮT MÁY TÍNH CÁ NHÂN HỆ THỐNG VẪN CHẠY 24/7 LIÊN TỤC!**
> Quá trình theo dõi streamer, phát hiện live sau 20s, thu luồng H.264, lưu Google Drive 5TB, đồng bộ Supabase và điều khiển qua Web hoàn toàn chạy độc lập trên đám mây. Bạn có thể tắt máy tính đi ngủ thoải mái.

---

## 🚀 Tính năng Nổi bật

* ☁️ **Vận Hành 100% Đám Mây 24/7 (Không Phụ Thuộc Máy Local):**
  - Chạy liên tục trên GitHub Actions với cơ chế Continuous Relay kết hợp lưới an toàn Cron (`*/50 * * * *`).
  - Hỗ trợ ghi đồng thời lên đến **10 streamer cùng lúc**.
  - Hoàn toàn miễn phí, không giới hạn số phút (Unlimited Minutes trên Public Repository).
* ⚡ **Serverless Cloud Failover 24/7 Trên Web:**
  - Web API (Vercel) tự động chuyển mạch trong 0.05 giây sang Supabase Cloud khi máy tính cá nhân tắt, đảm bảo trạng thái Web luôn trực tuyến (Online) 24/7.
* 🗑️ **Dọn Sạch 100% Cover Thumbnail Supabase Storage:**
  - Xóa streamer: Tự động dọn sạch thư mục ảnh `record-thumbnails/{user}/` trong bucket `covers` trên Supabase Storage cùng với dữ liệu database.
  - Xóa video: Tự động xóa file `.jpg` thumbnail tương ứng trong bucket `covers` song song với việc xóa file trên Google Drive.
* 📦 **Zero-Loss Staging Queue:**
  - Tự động chia nhỏ video thành các phân đoạn ngắn tải lên Google Drive Staging Queue để chống mất mát dữ liệu khi streamer ngắt kết nối đột ngột hoặc runner hết giờ.
  - Tự động ghép nối video hoàn chỉnh và upload thumbnail khi kết thúc live.
* 🛡️ **Chuẩn hóa H.264 & Chống Lỗi Bitstream:**
  - Ưu tiên luồng HLS H.264 MPEG-TS, loại bỏ nguy cơ lỗi header NAL Unit FLV (`01 64 00 1f...`).
  - Tự động gắn cờ `+faststart` (moov box ở đầu file) để trình duyệt web phát ngay lập tức.
* 🩺 **Tự Phục hồi (Self-Healing Watchdog):**
  - Workflow chạy định kỳ mỗi 6 giờ (`0 */6 * * *`) tự động vá lỗi bitstream và bù thumbnail vào Supabase Storage nếu có phiên bị gián đoạn.
* 🌐 **Tích hợp Supabase & Web API:**
  - Đồng bộ danh sách theo dõi qua bảng `tiktok_streamers`.
  - Lưu trữ thông tin video và link thumbnail công khai qua bảng `tiktok_recordings`.

---

## ⚙️ Hướng dẫn Cài đặt GitHub Secrets (Bắt buộc)

Sau khi tạo repository công khai (Public) trên GitHub, vào **Settings $\to$ Secrets and variables $\to$ Actions $\to$ New repository secret**, thêm các biến sau:

| Secret Name | Mô tả |
| :--- | :--- |
| `GOOGLE_CLIENT_ID` | Client ID ứng dụng Google Cloud (Google Drive API) |
| `GOOGLE_CLIENT_SECRET` | Client Secret ứng dụng Google Cloud |
| `GDRIVE_REFRESH_TOKEN` | OAuth2 Refresh Token Google Drive |
| `SUPABASE_URL` | URL dự án Supabase (VD: `https://xxxx.supabase.co`) |
| `SUPABASE_KEY` | Supabase Anon Key hoặc Service Role Key |
| `WORKFLOW_PAT` | *(Tùy chọn)* GitHub Personal Access Token có quyền `repo`/`workflow` để kích hoạt phiên nối tiếp ngay lập tức |
| `TIKTOK_SESSION_ID` | *(Tùy chọn)* Giá trị cookie `sessionid_ss` khi cần ghi hình phòng live giới hạn độ tuổi |
| `TELEGRAM_BOT_TOKEN` | *(Tùy chọn)* Token bot Telegram gửi thông báo |
| `TELEGRAM_CHAT_ID` | *(Tùy chọn)* Chat ID nhận thông báo Telegram |

---

## 🌐 Tích hợp Web & Gọi API từ Website

### 1. Quản lý Streamer (Không cần qua server trung gian)
Website của bạn có thể thêm/xóa streamer trực tiếp thông qua **Supabase PostgREST API** hoặc Supabase Client:
* **Thêm streamer:** `POST /rest/v1/tiktok_streamers` với payload `{"username": "ten_streamer"}`.
* **Xóa streamer:** `DELETE /rest/v1/tiktok_streamers?username=eq.ten_streamer`.
* **Runner sẽ tự động đọc danh sách streamer mới nhất từ Supabase mỗi 20 giây!**

### 2. Kích hoạt Runner thủ công từ Website
Khi cần kích hoạt ghi hình tức thì từ website, gửi request đến GitHub Actions Dispatch API:
```bash
POST https://api.github.com/repos/{OWNER}/{REPO}/actions/workflows/recorder.yml/dispatches
Authorization: Bearer <GITHUB_PAT>
Content-Type: application/json

{"ref": "main"}
```

### 3. Hiển thị Video trên Web
Website chỉ cần đọc bảng `tiktok_recordings` từ Supabase:
* `thumbnail_url`: Link ảnh đại diện từ Supabase Storage.
* `drive_file_id`: ID file trên Google Drive để phát video HTML5 hoặc nhúng iframe player.

---

## 🧪 Kiểm thử Cục bộ (Tùy chọn dành cho Lập trình / Debug)

> *Lưu ý: Bạn không cần chạy các lệnh này nếu chỉ muốn sử dụng hệ thống. Hệ thống đã tự động chạy 24/7 trên GitHub Actions.*

```bash
# Cài đặt thư viện
pip install -r requirements.txt

# Chạy bộ kiểm thử toàn diện (78 bài test đạt chuẩn)
python -B -m unittest test_audit_suite.py

# (Tùy chọn) Chạy thử daemon ghi hình tại máy cá nhân với GPU NVENC
run_cloud_daemon.bat
```
