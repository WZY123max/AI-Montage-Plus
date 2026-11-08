"""FFmpeg 后端选择：优先 NVIDIA NVENC 编码 + CUDA/D3D11 硬件解码。

这个项目里真正耗时的不是 OpenCV 分析，而是成百上千次 FFmpeg 转码：
镜头切割、逐段重编码、转场、最终输出。把 ``libx264`` 换成 ``h264_nvenc``
可以让 RTX 显卡接管编码；``-hwaccel auto`` 让解码也走 GPU（NVDEC/D3D11VA），
CPU 只负责 scale/pad/xfade 这类滤镜。

所有函数都做了“探测 + 回退”：显卡/驱动/FFmpeg 构建不支持时自动退回
libx264，不会让混剪任务直接失败。

环境变量：
    MONTAGE_VIDEO_ENCODER = auto | nvenc | libx264   （默认 auto）
    MONTAGE_HWACCEL       = auto | cuda | off        （默认 auto）
    MONTAGE_CRF           = 0~51                      （默认 18，越小越清晰）
    FFMPEG_PATH / FFPROBE_PATH                       （默认 PATH 中的 ffmpeg/ffprobe）
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from typing import List, Optional

# 这些参数是 NVENC 与 libx264 的“同名不同义”部分：
# - libx264 用 -crf / -preset
# - NVENC 用 -cq / -preset p1..p7 / -rc vbr
_NVENC_PRESET = {"fast": "p3", "balanced": "p5", "quality": "p6"}
_X264_PRESET = {"fast": "veryfast", "balanced": "fast", "quality": "medium"}

# 画质档位。混剪要经历「切片 → 转场 → 调色/防抖/字幕」多代编码，
# 每一代都有损失，所以中间态必须用低 CRF（高质量）保存；
# 以前统一用 23，叠加两三代后画面就会发糊、暗部起块。
QUALITY_CRF = {"high": 18, "standard": 21, "small": 25}
DEFAULT_QUALITY = "high"


def quality_crf(quality: Optional[str] = None) -> int:
    """取当前画质档位对应的 CRF/CQ 值。

    优先级：显式参数 > 环境变量 ``MONTAGE_CRF`` > 档位默认（high=18）。
    """
    env = (os.environ.get("MONTAGE_CRF") or "").strip()
    if env:
        try:
            return max(0, min(51, int(float(env))))
        except ValueError:
            pass
    return QUALITY_CRF.get((quality or DEFAULT_QUALITY).lower(), QUALITY_CRF[DEFAULT_QUALITY])


def ffmpeg_bin() -> str:
    """返回 ffmpeg 可执行文件路径（支持环境变量覆盖）。"""
    return os.environ.get("FFMPEG_PATH") or "ffmpeg"


def ffprobe_bin() -> str:
    """返回 ffprobe 可执行文件路径（支持环境变量覆盖）。

    只设置了 FFMPEG_PATH 时，优先找同目录的 ffprobe，避免 PATH 里
    ffmpeg/ffprobe 版本不一致。
    """
    explicit = os.environ.get("FFPROBE_PATH")
    if explicit:
        return explicit
    ff = os.environ.get("FFMPEG_PATH")
    if ff:
        from pathlib import Path
        candidate = Path(ff).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if candidate.exists():
            return str(candidate)
    return "ffprobe"


def _encoder_mode() -> str:
    return (os.environ.get("MONTAGE_VIDEO_ENCODER") or "auto").strip().lower()


@functools.lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """探测 h264_nvenc 是否真的能用（不是“编译进去了”就算）。

    实测踩坑：FFmpeg 9.0.2 的 NVENC API 13.1 要求 NVIDIA 610+ 驱动，
    而 591.86 只提供 13.0，编码器会报 "Driver does not support the
    required nvenc API version"。所以必须做一次真实编码探测。
    """
    mode = _encoder_mode()
    if mode in {"0", "false", "off", "cpu", "libx264", "x264"}:
        return False
    if not shutil.which(ffmpeg_bin()):
        return False

    cmd = [
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "color=black:s=320x240:d=0.2",
        "-c:v", "h264_nvenc", "-preset", "p1",
        "-f", "null", "-",
    ]
    try:
        ok = subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if mode in {"nvenc", "h264_nvenc", "gpu"} and not ok:
        print("  [GPU] 指定了 NVENC，但当前 FFmpeg/驱动不可用，回退 libx264")
    return ok


def encoder_name() -> str:
    """当前实际使用的视频编码器。"""
    return "h264_nvenc" if nvenc_available() else "libx264"


def encoder_label() -> str:
    return "NVIDIA NVENC (GPU)" if nvenc_available() else "libx264 (CPU)"


def encode_args(crf: Optional[int] = None, speed: str = "balanced") -> List[str]:
    """返回视频编码参数（不含 ``-c:v`` 之外的音频参数）。

    Args:
        crf: 画质（x264 的 CRF / NVENC 的 CQ，数值越小越清晰）。
             不传时用 :func:`quality_crf`（默认 18）。
        speed: fast / balanced / quality。
    """
    if crf is None:
        crf = quality_crf()
    crf = int(crf)
    if nvenc_available():
        return [
            "-c:v", "h264_nvenc",
            "-preset", _NVENC_PRESET.get(speed, "p5"),
            "-tune", "hq",
            "-rc", "vbr",
            "-cq", str(int(crf)),
            "-b:v", "0",
            "-spatial-aq", "1",
            "-profile:v", "high",
            "-pix_fmt", "yuv420p",
        ]
    return [
        "-c:v", "libx264",
        "-crf", str(int(crf)),
        "-preset", _X264_PRESET.get(speed, "fast"),
        "-pix_fmt", "yuv420p",
    ]


# 输出画布（16:9）的常见档位。工作分辨率与成片分辨率都用这套档位。
CANVAS_LADDER = (480, 720, 1080, 1440, 2160)


def probe_dimensions(path: str) -> tuple:
    """读取视频分辨率，失败返回 (0, 0)。"""
    cmd = [
        ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except (ValueError, IndexError, OSError, subprocess.TimeoutExpired):
        return 0, 0


def resolve_canvas(paths: List[str], width: Optional[int] = None,
                   height: Optional[int] = None) -> tuple:
    """决定工作/输出画布尺寸（16:9，偶数对齐）。

    Args:
        paths: 素材路径列表
        width/height: 用户显式指定时直接采用

    取所有素材短边的**中位数**再对齐到档位：
    - 1080p 素材 -> 1920x1080（旧版本无条件压到 1280x720，白丢一半像素）
    - 4K 素材 -> 3840x2160
    - 一两个 4K 不会把整片顶到 4K，一两个低清也不会把整片拉低
    - 单一素材永远不放大（480p 不会变 720p）；混合素材取偏高档位，
      宁可把少数低清片段放大，也不把整片压到最低清的那一档
    """
    if width and height and width > 1 and height > 1:
        return int(width) // 2 * 2, int(height) // 2 * 2

    sizes = [(w, h) for w, h in (probe_dimensions(p) for p in paths) if w and h]
    if not sizes:
        return 1280, 720

    shorts = sorted(min(w, h) for w, h in sizes)
    median = shorts[len(shorts) // 2]
    fitted = [hh for hh in CANVAS_LADDER if hh <= median]
    target = fitted[-1] if fitted else max(2, median // 2 * 2)
    target = min(target, 2160)
    return max(2, int(round(target * 16 / 9)) // 2 * 2), int(target)


def decode_args() -> List[str]:
    """返回硬件解码参数（放在 ``-i`` 之前）。

    ``-hwaccel auto`` 在 Windows 上通常选 d3d11va/cuda；CPU 滤镜仍然可用
    （ffmpeg 会自动把帧下载回内存）。
    """
    mode = (os.environ.get("MONTAGE_HWACCEL") or "auto").strip().lower()
    if mode in {"0", "false", "off", "none", "cpu"}:
        return []
    if mode == "cuda":
        return ["-hwaccel", "cuda"]
    return ["-hwaccel", "auto"]


def _strip_hwaccel(cmd: List[str]) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(cmd):
        if cmd[i] == "-hwaccel":
            i += 2
            continue
        out.append(cmd[i])
        i += 1
    return out


def run_ffmpeg(cmd: List[str], **kwargs):
    """执行 FFmpeg；硬件解码失败时自动去掉 ``-hwaccel`` 重试一次。

    调用方仍按 ``subprocess.run`` 的语义使用：默认不 check，需要时传
    ``check=True``（重试后仍失败才抛异常）。
    """
    check = bool(kwargs.pop("check", False))
    try:
        return subprocess.run(cmd, check=check, **kwargs)
    except subprocess.CalledProcessError:
        if "-hwaccel" not in cmd:
            raise
        return subprocess.run(_strip_hwaccel(cmd), check=check, **kwargs)


def gpu_status() -> dict:
    """供诊断脚本使用：返回 FFmpeg / NVENC / OpenCV CUDA 状态。"""
    status = {
        "ffmpeg": ffmpeg_bin(),
        "ffmpeg_found": bool(shutil.which(ffmpeg_bin())),
        "encoder": encoder_name(),
        "nvenc": nvenc_available(),
        "hwaccel": decode_args(),
    }
    try:
        import cv2  # noqa: F401
        status["opencv"] = cv2.__version__
        status["opencv_cuda_devices"] = int(cv2.cuda.getCudaEnabledDeviceCount())
    except Exception as exc:  # pragma: no cover - 仅诊断用
        status["opencv_error"] = str(exc)
    return status


def clear_cache() -> None:
    """清空探测缓存（测试/切换环境后使用）。"""
    nvenc_available.cache_clear()
