import os
import sys
import json
import urllib.parse
import urllib.request
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = "http://localhost:8080"
SCOPES = "https://www.googleapis.com/auth/drive"

AUTH_CODE = None

class OAuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global AUTH_CODE
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        if "code" in params:
            AUTH_CODE = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("""
            <html><body style="font-family: Arial; text-align: center; padding-top: 50px; background: #111; color: #fff;">
                <h1 style="color: #4ade80;">✓ Kết nối Google Drive thành công!</h1>
                <p>Bạn có thể đóng tab này và quay lại cửa sổ dòng lệnh để tiến hành upload video.</p>
            </body></html>
            """.encode("utf-8"))
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Authorization failed.")

    def log_message(self, format, *args):
        pass

def get_config_credentials():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
    client_id = cfg.get("google_client_id") or CLIENT_ID
    client_secret = cfg.get("google_client_secret") or CLIENT_SECRET
    return client_id, client_secret

def get_authorization_url(client_id, redirect_uri=REDIRECT_URI):
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "select_account consent"
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"

def exchange_code_for_tokens(code, client_id, client_secret, redirect_uri=REDIRECT_URI):
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"
    }).encode("utf-8")

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))

def start_oauth_flow(client_id=None, client_secret=None, redirect_uri=REDIRECT_URI):
    global AUTH_CODE
    default_id, default_secret = get_config_credentials()
    client_id = client_id or default_id
    client_secret = client_secret or default_secret

    auth_url = get_authorization_url(client_id, redirect_uri)
    print("\n" + "=" * 65)
    print("      LIÊN KẾT TÀI KHOẢN GOOGLE DRIVE ĐỂ TỰ ĐỘNG LƯU VIDEO")
    print("=" * 65)
    print(f"[*] Client ID đang dùng: {client_id[:25]}...")
    print("\n[👉] Vui lòng mở đường link sau trên trình duyệt và đăng nhập / cấp quyền:\n")
    print(auth_url)
    print("\n" + "=" * 65)
    print("[*] Đang chờ bạn xác nhận trên trình duyệt (cổng 8080)...")

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    server = HTTPServer(("0.0.0.0", 8080), OAuthHandler)
    while not AUTH_CODE:
        server.handle_request()

    print("\n[+] Đã nhận mã ủy quyền! Đang lấy Refresh Token...")
    tokens = exchange_code_for_tokens(AUTH_CODE, client_id, client_secret, redirect_uri)
    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")

    if refresh_token:
        print("[✓] Đã lấy Refresh Token thành công!")
        cfg = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        cfg["gdrive_refresh_token"] = refresh_token
        cfg["google_client_id"] = client_id
        cfg["google_client_secret"] = client_secret
        cfg["gdrive_enabled"] = True
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)

        hf_cfg = os.path.join(BASE_DIR, "huggingface_space", "config.json")
        if os.path.exists(os.path.dirname(hf_cfg)):
            with open(hf_cfg, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)

        print("[✓] Đã lưu thông tin cấu hình vào config.json và huggingface_space/config.json!")
        return access_token, refresh_token
    else:
        print(f"[!] Không nhận được refresh_token: {tokens}")
        return access_token, None

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Google Drive OAuth Setup")
    parser.add_argument("--client-id", help="Google OAuth Client ID")
    parser.add_argument("--client-secret", help="Google OAuth Client Secret")
    parser.add_argument("--redirect-uri", default=REDIRECT_URI, help="Redirect URI")
    args = parser.parse_args()
    start_oauth_flow(client_id=args.client_id, client_secret=args.client_secret, redirect_uri=args.redirect_uri)
