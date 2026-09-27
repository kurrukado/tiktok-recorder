@echo off
chcp 65001 >nul
title Kuru Record 24-7 Cloud Daemon
cd /d "%~dp0"

echo ========================================================
echo   KURU RECORD 24/7 CLOUD DAEMON (LOCAL RUNNER)
echo   GPU NVENC Accelerated - Auto-restart Watchdog
echo ========================================================
echo.

:loop
echo [%DATE% %TIME%] Khởi động cloud_daemon.py...
python -u cloud_daemon.py --duration-minutes 360 --interval 20

echo.
echo [!] Tiến trình daemon đã dừng (exit code %ERRORLEVEL%).
echo [*] Tự động khởi động lại sau 5 giây...
timeout /t 5 /nobreak >nul
goto loop
