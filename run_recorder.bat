@echo off
chcp 65001 >nul
title TikTok Live Auto Recorder & Downloader
cd /d "%~dp0"

echo ===================================================
echo   TIKTOK LIVE RECORDER - TỰ ĐỘNG THU & LƯU STREAM
echo   Thư mục lưu: %~dp0
echo ===================================================
echo.

python tiktok_recorder.py %*

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] Có lỗi xảy ra khi thực thi.
    pause
)
