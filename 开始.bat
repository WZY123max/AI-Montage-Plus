@echo off
chcp 65001 >nul
cd /d "C:\Users\lenovo\Desktop\ai-montage-agent-main"

rem 使用支持 NVENC（且与 591.x 驱动兼容）的 FFmpeg 7.1.1。
rem FFmpeg 9.0.2 要求 610+ 驱动，在这台机器上 h264_nvenc 会直接报错。
set "PATH=C:\Users\lenovo\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-7.1.1-full_build\bin;%PATH%"

rem auto = 自动探测 NVENC，失败自动回退 libx264
set MONTAGE_VIDEO_ENCODER=auto
set MONTAGE_HWACCEL=auto

start "" http://localhost:8000
C:\Users\lenovo\AppData\Local\Programs\Python\Python312\python.exe pipeline.py --webui
pause
