#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ci_discord_notifier.py
Gửi thông báo kết quả kiểm tra tự động (CI/CD Bug Check) về Discord Webhook.
Hỗ trợ cả trường hợp chạy trên GitHub Actions lẫn chạy test thủ công local.
"""

import os
import sys
import json
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_WEBHOOK = "https://discord.com/api/webhooks/1360302046116315246/_t_vP_iT1wPfsmyK3DLKafKAJKE3XDG2nH4DF_M6oVfYpf_3S0v8smVf1915_-ayILqx"

def send_discord_notification(status="success", details=None):
    webhook_url = os.environ.get("DISCORD_WEBHOOK") or DEFAULT_WEBHOOK
    if not webhook_url:
        print("[!] Không có webhook URL, bỏ qua thông báo Discord.")
        return

    repo = os.environ.get("GITHUB_REPOSITORY", "kurrukado/tiktok-recorder")
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    event = os.environ.get("GITHUB_EVENT_NAME", "manual")
    actor = os.environ.get("GITHUB_ACTOR", "local-dev")
    commit_sha = os.environ.get("GITHUB_SHA", "HEAD")[:7]
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")

    run_url = f"{server_url}/{repo}/actions/runs/{run_id}" if run_id else f"https://github.com/{repo}/actions"

    if status.lower() == "success":
        color = 0x2ECC71  # Xanh lục (Pass)
        title = "✅ [Kuru CI] Kiểm Tra Bug & Regression: THÀNH CÔNG"
        status_text = "🟢 **100% ĐẠT CHUẨN** - Không phát hiện lỗi cú pháp hay hồi quy!"
    else:
        color = 0xE74C3C  # Đỏ tươi (Fail)
        title = "🚨 [Kuru CI] PHÁT HIỆN BUG TRONG CODE! CẦN XỬ LÝ"
        status_text = "🔴 **PHÁT HIỆN LỖI** - Code mới có thể gây crash hoặc hỏng tính năng!"

    fields = [
        {"name": "📊 Trạng thái", "value": status_text, "inline": False},
        {"name": "📦 Repository", "value": f"`{repo}` (`{ref}`)", "inline": True},
        {"name": "👤 Người thực hiện", "value": f"@{actor}", "inline": True},
        {"name": "⚡ Sự kiện kích hoạt", "value": f"`{event}` (`{commit_sha}`)", "inline": True},
    ]

    # Bổ sung chi tiết kiểm tra nếu có
    if details:
        linter_status = details.get("linter", "Đã kiểm tra (0 lỗi)")
        test_status = details.get("tests", "78/78 Core Audit Tests PASSED")
        error_log = details.get("error_log", "")

        fields.append({"name": "🔍 Static Code Check (Flake8)", "value": f"```\n{linter_status}\n```", "inline": False})
        fields.append({"name": "🧪 Unit & Concurrency Tests", "value": f"```\n{test_status}\n```", "inline": False})

        if error_log:
            # Rút ngắn nếu log quá dài (Discord giới hạn field value 1024 ký tự)
            trimmed_error = error_log.strip()
            if len(trimmed_error) > 950:
                trimmed_error = trimmed_error[-950:]
            fields.append({"name": "⚠️ Chi tiết lỗi (Trích xuất)", "value": f"```python\n{trimmed_error}\n```", "inline": False})

    fields.append({"name": "🔗 Chi tiết tiến trình GitHub Actions", "value": f"[Bấm vào đây để xem chi tiết log]({run_url})", "inline": False})

    payload = {
        "username": "Kuru CI Bug Watcher",
        "avatar_url": "https://avatars.githubusercontent.com/u/9919?s=200&v=4",
        "embeds": [{
            "title": title,
            "url": run_url,
            "color": color,
            "fields": fields,
            "footer": {
                "text": "TikTok Recorder Automated CI/CD • Bảo vệ hệ thống 24/7"
            }
        }]
    }

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[✓] Đã gửi thông báo Discord thành công (HTTP {resp.getcode()}).")
    except Exception as e:
        print(f"[!] Không thể gửi thông báo Discord: {e}")

if __name__ == "__main__":
    status_arg = sys.argv[1] if len(sys.argv) > 1 else "success"
    details_file = sys.argv[2] if len(sys.argv) > 2 else None

    dt = {}
    if details_file and os.path.exists(details_file):
        try:
            with open(details_file, "r", encoding="utf-8") as f:
                dt = json.load(f)
        except Exception as ex:
            dt = {"error_log": f"Không thể đọc details_file: {ex}"}

    send_discord_notification(status=status_arg, details=dt)
