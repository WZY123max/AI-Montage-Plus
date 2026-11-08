"""GPU / FFmpeg 加速自检。

用法：
    python scripts\\gpu_check.py

会检查：
- NVIDIA 显卡与驱动
- FFmpeg 版本、硬件解码列表、h264_nvenc 是否真的能编码
- OpenCV 是否带 CUDA
- 给出针对当前机器的修复建议
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:  # Windows 控制台默认 GBK，中文输出统一用 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from packages.montage.media import (  # noqa: E402
    encode_args, encoder_label, ffmpeg_bin, ffprobe_bin, nvenc_available,
)


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=30, errors="replace")
    except Exception as exc:  # pragma: no cover - 仅诊断
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": str(exc)})()


def main() -> int:
    print("=" * 62)
    print("AI Montage Agent - GPU 自检")
    print("=" * 62)

    ok = True

    print("\n[1] NVIDIA 显卡 / 驱动")
    if shutil.which("nvidia-smi"):
        r = _run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                  "--format=csv,noheader"])
        print("   ", (r.stdout or r.stderr).strip() or "(无输出)")
    else:
        print("    未找到 nvidia-smi：可能没有 NVIDIA 显卡或驱动未安装")
        ok = False

    print("\n[2] FFmpeg")
    print("    ffmpeg :", ffmpeg_bin(), "->",
          shutil.which(ffmpeg_bin()) or "未找到")
    print("    ffprobe:", ffprobe_bin(), "->",
          shutil.which(ffprobe_bin()) or "未找到")
    r = _run([ffmpeg_bin(), "-version"])
    if r.returncode == 0:
        print("    ", (r.stdout or "").splitlines()[0])
    else:
        print("    无法运行 ffmpeg:", (r.stderr or "").strip()[:300])
        ok = False

    r = _run([ffmpeg_bin(), "-hide_banner", "-hwaccels"])
    print("    硬件解码:", " ".join((r.stdout or "").split()) or "(无)")

    print("\n[3] NVENC 真实编码测试")
    nvenc = nvenc_available()
    print("    可用:", "是" if nvenc else "否")
    print("    当前编码器:", encoder_label())
    print("    编码参数:", " ".join(encode_args(23, "balanced")))
    if not nvenc:
        ok = False
        print("    提示: 先运行 `ffmpeg -hide_banner -f lavfi -i color=black:s=320x240:d=0.2 "
              "-c:v h264_nvenc -preset p1 -f null -` 看具体报错。")
        print("          若报 nvenc API version，请换与驱动匹配的 FFmpeg 构建"
              "（本机 591.86 驱动用 7.1.1 可以）。")

    print("\n[4] OpenCV CUDA")
    try:
        import cv2
        devices = int(cv2.cuda.getCudaEnabledDeviceCount())
        print("    OpenCV:", cv2.__version__)
        print("    CUDA 设备数:", devices)
        if devices <= 0:
            print("    提示: pip 官方 wheel 不带 CUDA；本项目会自动回退到"
                  " 640 宽 CPU 光流，不影响成片。")
    except Exception as exc:
        print("    OpenCV 不可用:", exc)
        ok = False

    print("\n" + "=" * 62)
    print("结论:", "GPU 编码可用" if nvenc else "GPU 编码不可用（见上方提示）")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
