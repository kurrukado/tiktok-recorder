import os
import sys
import time
import argparse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from recorder_core import (
    load_config,
    save_config,
    load_cookies,
    save_cookies,
    check_live_status,
    get_stream_urls,
    record_stream_ffmpeg,
    BASE_DIR,
    COOKIES_FILE,
    CONFIG_FILE
)
from download_archives import download_all_archives


BANNER = r"""
  ╔═══════════════════════════════════════════════════════════════╗
  ║            TIKTOK LIVE AUTO-RECORDER & DOWNLOADER             ║
  ║                   Tự Động Thu & Lưu Video                     ║
  ╚═══════════════════════════════════════════════════════════════╝
"""

def print_banner():
    print(BANNER)

def run_auto_mode(target_user, interval):
    user_dir = os.path.join(BASE_DIR, target_user)
    os.makedirs(user_dir, exist_ok=True)
    print(f"\n[*] BẮT ĐẦU CHẾ ĐỘ TỰ ĐỘNG THEO DÕI: @{target_user}")
    print(f"[*] Tần suất kiểm tra: mỗi {interval} giây.")
    print(f"[*] Video quay được sẽ tự động lưu vào: {user_dir}")
    print(f"[*] Video sẽ tự động được chuẩn hóa sang H.264 (AVC) sau khi ghi hình.")
    print(f"[*] Nhấn [Ctrl + C] bất kỳ lúc nào để dừng chương trình.\n")

    consecutive_errors = 0
    while True:
        timestamp = time.strftime("%H:%M:%S")
        try:
            is_live, room_id = check_live_status(target_user)
            if is_live and room_id:
                print(f"\n[{timestamp}] 🔴 PHÁT HIỆN @{target_user} ĐANG PHÁT TRỰC TIẾP!")
                print(f"[{timestamp}] ID Phòng: {room_id}")
                
                try:
                    from notifier import send_telegram
                    send_telegram(
                        f"🔴 <b>PHÁT HIỆN LIVESTREAM MỚI!</b>\n"
                        f"👤 Streamer: <code>@{target_user}</code>\n"
                        f"🆔 Room ID: <code>{room_id}</code>\n"
                        f"⏳ Tool đang tự động kết nối và thu luồng video..."
                    )
                except Exception:
                    pass

                # Fetch stream urls
                stream_res = get_stream_urls(room_id, target_user)
                if stream_res == "AGE_RESTRICTED":
                    print(f"[{timestamp}] ⚠️ CẢNH BÁO: Phiên Live của @{target_user} bị giới hạn độ tuổi 18+ (Age-Restricted).")
                    cookies = load_cookies()
                    if not cookies.get("sessionid") and not cookies.get("sessionid_ss"):
                        print(f"[{timestamp}] ⚠️ Bạn chưa cài đặt Cookie TikTok nên không thể tải luồng 18+.")
                        print(f"[{timestamp}] 👉 Vui lòng mở file 'cookies.json' hoặc chọn menu [4] để nhập sessionid_ss!")
                    time.sleep(interval)
                    continue

                if isinstance(stream_res, list) and stream_res:
                    stream_url = stream_res[0]
                    print(f"[{timestamp}] [✓] Đã lấy được link stream chất lượng cao nhất.")
                    record_stream_ffmpeg(stream_url=stream_url, target_user=target_user)
                    print(f"\n[{time.strftime('%H:%M:%S')}] Phiên live đã kết thúc hoặc bị ngắt. Tiếp tục chờ phiên live mới...")
                else:
                    print(f"[{timestamp}] Không tìm thấy link luồng stream hợp lệ. Thử lại sau {interval}s...")
                
                consecutive_errors = 0
            else:
                sys.stdout.write(f"\r[{timestamp}] ⏳ Đang quét @{target_user}... Trạng thái: [Offline]. Sẽ kiểm tra lại sau {interval}s.")
                sys.stdout.flush()
                consecutive_errors = 0

        except KeyboardInterrupt:
            print("\n\n[!] Đã dừng chế độ tự động.")
            break
        except Exception as e:
            consecutive_errors += 1
            print(f"\n[{timestamp}] Lỗi tạm thời: {e}")
            if consecutive_errors > 5:
                time.sleep(30)

        time.sleep(interval)

def run_record_now(target_user):
    print(f"\n[*] Đang kiểm tra livestream của @{target_user} ngay bây giờ...")
    is_live, room_id = check_live_status(target_user)
    if not is_live or not room_id:
        print(f"[!] Hiện tại @{target_user} KHÔNG phát trực tiếp (Offline).")
        print(f"[*] Hãy dùng chế độ tự động [1] để tool tự canh và tự quay ngay khi họ lên Live!")
        return

    print(f"[✓] 🔴 @{target_user} ĐANG LIVE! (Room ID: {room_id})")
    stream_res = get_stream_urls(room_id, target_user)
    
    if stream_res == "AGE_RESTRICTED":
        print("\n" + "=" * 65)
        print("⚠️ CẢNH BÁO: Livestream của streamer này có hạn chế độ tuổi 18+.")
        print("TikTok yêu cầu tài khoản đã đăng nhập để xem/thu livestream này.")
        print("Để tải được, bạn cần nhập sessionid vào file cookies.json:")
        print("  1. Đăng nhập https://www.tiktok.com trên trình duyệt")
        print("  2. Nhấn F12 -> Application -> Cookies -> https://www.tiktok.com")
        print("  3. Copy giá trị của 'sessionid_ss' và dán vào menu [4]")
        print("=" * 65 + "\n")
        return

    if isinstance(stream_res, list) and stream_res:
        stream_url = stream_res[0]
        print(f"[✓] Đã kết nối luồng livestream.")
        record_stream_ffmpeg(stream_url=stream_url, target_user=target_user)
    else:
        print("[!] Không tìm thấy URL phát sóng khả dụng.")

def check_status_cli(target_user):
    print(f"\n[*] Đang lấy thông tin từ TikTok cho: @{target_user}...")
    is_live, room_id = check_live_status(target_user)
    print("=" * 50)
    print(f"Người dùng   : @{target_user}")
    print(f"Trạng thái   : {'🔴 ĐANG PHÁT TRỰC TIẾP (LIVE)' if is_live else '⚪ OFFLINE'}")
    if room_id:
        print(f"Room ID      : {room_id}")
    cookies = load_cookies()
    has_cookie = bool(cookies.get("sessionid_ss") or cookies.get("sessionid"))
    print(f"Cookie 18+   : {'[✓] Đã cài đặt' if has_cookie else '[!] Chưa có (chưa vượt được 18+)'}")
    print("=" * 50)

def set_cookie_interactive():
    print("\n" + "=" * 60)
    print("      HƯỚNG DẪN CÀI ĐẶT COOKIE TIKTOK (VƯỢT GIỚI HẠN 18+)")
    print("=" * 60)
    print("1. Mở trình duyệt (Chrome, Edge, Cốc Cốc, Brave...)")
    print("2. Vào https://www.tiktok.com và đăng nhập nick TikTok của bạn")
    print("3. Nhấn phím F12 (hoặc Chuột phải -> Kiểm tra / Inspect)")
    print("4. Chuyển sang tab [Application] (Ứng dụng) ở trên cùng")
    print("5. Nhìn cột trái: Storage -> Cookies -> chọn https://www.tiktok.com")
    print("6. Tìm dòng có tên là 'sessionid_ss' (hoặc 'sessionid')")
    print("7. Nhấp đúp vào cột 'Value' của nó và Copy toàn bộ chuỗi mã đó.")
    print("=" * 60)
    
    current = load_cookies()
    curr_val = current.get("sessionid_ss", "") or current.get("sessionid", "")
    if curr_val:
        print(f"Cookie hiện tại: {curr_val[:12]}...{curr_val[-6:]}")
    
    val = input("\nDán mã sessionid_ss vào đây (hoặc nhấn Enter để bỏ qua): ").strip()
    if val:
        new_cookies = {
            "sessionid_ss": val,
            "sessionid": val,
            "tt-target-idc": "useast2a"
        }
        save_cookies(new_cookies)
        print("[✓] Đã lưu Cookie thành công vào file cookies.json!")
    else:
        print("[*] Giữ nguyên cấu hình cookie hiện tại.")

def set_vps_cloud_interactive():
    cfg = load_config()
    print("\n" + "=" * 65)
    print("   CÀI ĐẶT THÔNG BÁO TELEGRAM & GOOGLE DRIVE (CHO VPS / MÁY TÍNH)")
    print("=" * 65)
    print("1. Telegram Bot Token:")
    curr_token = cfg.get("telegram_bot_token", "")
    print(f"   Hiện tại: {curr_token[:10]}...{curr_token[-5:]}" if curr_token else "   Hiện tại: [Chưa cài đặt]")
    new_token = input("   Nhập Token mới (hoặc Enter để giữ nguyên): ").strip()
    if new_token:
        cfg["telegram_bot_token"] = new_token

    print("\n2. Telegram Chat ID (ID người nhận tin nhắn):")
    curr_chat = cfg.get("telegram_chat_id", "")
    print(f"   Hiện tại: {curr_chat}" if curr_chat else "   Hiện tại: [Chưa cài đặt]")
    new_chat = input("   Nhập Chat ID mới (hoặc Enter để giữ nguyên): ").strip()
    if new_chat:
        cfg["telegram_chat_id"] = new_chat

    if cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"):
        cfg["telegram_enabled"] = True
        save_config(cfg)
        test = input("\nBạn có muốn gửi tin nhắn thử nghiệm tới Telegram không? (y/n): ").strip().lower()
        if test == "y":
            from notifier import send_telegram
            if send_telegram("🤖 <b>TikTok Recorder:</b> Kết nối Telegram thành công!"):
                print("[✓] Đã gửi tin nhắn thử nghiệm thành công tới Telegram của bạn!")
            else:
                print("[!] Gửi thất bại. Vui lòng kiểm tra lại Token hoặc Chat ID.")

    print("\n3. Đồng bộ Google Drive (thông qua Rclone):")
    gdrive_en = cfg.get("gdrive_enabled", False)
    print(f"   Trạng thái hiện tại: {'[BẬT]' if gdrive_en else '[TẮT]'}")
    toggle = input("   Bật tự động đẩy video lên Google Drive sau khi quay? (y/n/Enter giữ nguyên): ").strip().lower()
    if toggle == "y":
        cfg["gdrive_enabled"] = True
    elif toggle == "n":
        cfg["gdrive_enabled"] = False

    save_config(cfg)
    print("[✓] Đã lưu cấu hình thông báo và đám mây!")

def main_menu():
    cfg = load_config()
    target_user = cfg.get("target_user", "islizanx")
    interval = cfg.get("check_interval_seconds", 20)

    while True:
        user_dir = os.path.join(BASE_DIR, target_user)
        print_banner()
        print(f"  Mục tiêu theo dõi  : @{target_user}")
        print(f"  Thư mục lưu video  : {user_dir}")
        print(f"  Tần suất quét      : Mỗi {interval} giây")
        tg_status = "Đã bật" if cfg.get("telegram_enabled") and cfg.get("telegram_bot_token") else "Tắt"
        gd_status = "Đã bật" if cfg.get("gdrive_enabled") else "Tắt"
        print(f"  Thông báo Telegram : [{tg_status}] | Google Drive: [{gd_status}]")
        print("─" * 65)
        print(f"  [1] 🔴 Bắt đầu TỰ ĐỘNG THEO DÕI & RECORD khi @{target_user} Live (Khuyên dùng)")
        print(f"  [2] ⏺️  Kiểm tra và Record ngay lập tức (nếu đang Live)")
        print(f"  [3] 🔍 Kiểm tra trạng thái hiện tại của @{target_user}")
        print("  [4] 🍪 Cài đặt Cookie TikTok (vượt giới hạn 18+ / tài khoản riêng tư)")
        print(f"  [5] 📥 Tải các video livestream trước đó đã được lưu trữ của @{target_user}")
        print("  [6] 🎞️  Quét và chuẩn hóa toàn bộ video sang định dạng H.264 (AVC)")
        print("  [7] ⚙️  Cài đặt (Đổi User mục tiêu, thời gian lặp)")
        print("  [8] 📱 Cài đặt Telegram Bot & Google Drive (cho VPS / Máy tính)")
        print("  [0] 🚪 Thoát")
        print("─" * 65)
        
        choice = input("Vui lòng chọn (0-8): ").strip()
        if choice == "1":
            run_auto_mode(target_user, interval)
        elif choice == "2":
            run_record_now(target_user)
            input("\nNhấn Enter để quay lại menu chính...")
        elif choice == "3":
            check_status_cli(target_user)
            input("\nNhấn Enter để quay lại menu chính...")
        elif choice == "4":
            set_cookie_interactive()
            input("\nNhấn Enter để quay lại menu chính...")
        elif choice == "5":
            ans = input(f"\nBạn muốn tải bao nhiêu video gần nhất của @{target_user}? (Ví dụ: 5, 10, hoặc gõ 'all' để tải tất cả): ").strip()
            limit = None if ans.lower() in ["all", "tat ca", "0"] else (int(ans) if ans.isdigit() else 5)
            download_all_archives(username=target_user, limit=limit, output_dir=user_dir)
            input("\nNhấn Enter để quay lại menu chính...")
        elif choice == "6":
            from auto_h264 import convert_all_videos_in_folder
            convert_all_videos_in_folder(BASE_DIR)
            input("\nNhấn Enter để quay lại menu chính...")
        elif choice == "7":
            new_u = input(f"Nhập username TikTok mới (hiện tại: {target_user}): ").strip().replace("@", "")
            if new_u:
                target_user = new_u
                cfg["target_user"] = target_user
            new_int = input(f"Nhập khoảng cách quét lại tính bằng giây (hiện tại: {interval}s): ").strip()
            if new_int.isdigit() and int(new_int) >= 5:
                interval = int(new_int)
                cfg["check_interval_seconds"] = interval
            save_config(cfg)
            print("[✓] Đã cập nhật cấu hình!")
            time.sleep(1)
        elif choice == "8":
            set_vps_cloud_interactive()
            cfg = load_config()
            input("\nNhấn Enter để quay lại menu chính...")

        elif choice == "0":
            print("\nTạm biệt!")
            break
        else:
            print("[!] Lựa chọn không hợp lệ.")
            time.sleep(1)

def parse_args():
    parser = argparse.ArgumentParser(description="TikTok Live Recorder Tool")
    parser.add_argument("--user", default=None, help="TikTok username (không cần dấu @)")
    parser.add_argument("--auto", action="store_true", help="Chạy chế độ tự động theo dõi và ghi hình")
    parser.add_argument("--now", action="store_true", help="Ghi hình ngay lập tức nếu đang live")
    parser.add_argument("--status", action="store_true", help="Kiểm tra trạng thái live của user")
    parser.add_argument("--set-cookie", action="store_true", help="Nhập cookie TikTok")
    parser.add_argument("--convert-h264", action="store_true", help="Quét và chuyển đổi toàn bộ video sang H.264")
    parser.add_argument("--interval", type=int, default=None, help="Tần suất quét (giây)")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    cfg = load_config()
    target_user = args.user or cfg.get("target_user", "islizanx")
    interval = args.interval or cfg.get("check_interval_seconds", 20)

    if args.convert_h264:
        from auto_h264 import convert_all_videos_in_folder
        convert_all_videos_in_folder(BASE_DIR)
    elif args.auto:
        print_banner()
        run_auto_mode(target_user, interval)
    elif args.now:
        print_banner()
        run_record_now(target_user)
    elif args.status:
        check_status_cli(target_user)
    elif args.set_cookie:
        set_cookie_interactive()
    else:
        main_menu()
