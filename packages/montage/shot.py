"""镜头检测与运动分析。"""

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from packages.core_types.models import MotionData, Shot

from .media import decode_args, encode_args, ffmpeg_bin, run_ffmpeg, ffprobe_bin
from .paths import CACHE_DIR

# 切片/清理阶段的工作分辨率（16:9）。由 pipeline 在拿到素材后设置，
# 默认 720p 只是为了兼容单独调用 ShotDetector 的场景。
# 之前这里写死 1280x720，导致 1080p/4K 素材在第一步就被压成 720p，
# 后面无论怎么调编码参数都救不回来。
_WORK_SIZE = (1280, 720)


def set_work_size(width: int, height: int) -> None:
    """设置切片阶段的工作分辨率（由 pipeline 调用）。"""
    global _WORK_SIZE
    w = max(2, int(width) // 2 * 2)
    h = max(2, int(height) // 2 * 2)
    _WORK_SIZE = (w, h)


def get_work_size() -> tuple:
    return _WORK_SIZE


class ShotDetector:
    """镜头检测 - 使用 FFmpeg scene detect"""

    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR / "shots"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def detect(self, video_path: str, threshold: float = 0.3, video_index: int = 0) -> list:
        """检测镜头并切割"""
        print(f"  检测镜头: {video_path}")

        if not Path(video_path).exists():
            print(f"  文件不存在: {video_path}")
            return []

        # 获取视频时长
        try:
            duration = self._get_duration(video_path)
        except Exception as e:
            print(f"  获取时长失败: {e}")
            return []

        if duration <= 0:
            print(f"  视频时长为0: {video_path}")
            return []

        # 使用 FFmpeg 检测场景变化
        scenes = self._detect_scenes(video_path, threshold)

        # 如果没检测到场景变化，整个视频作为一个镜头
        if not scenes:
            scenes = [(0.0, duration)]

        # 切割视频
        shots = []
        for i, (start, end) in enumerate(scenes):
            if end - start < 0.1:
                continue

            shot_id = video_index * 10000 + i
            # 切片文件名带上**源区间**：同一 id 在不同参数/不同源下可能对应不同内容，
            # 只按 id 命名会命中上次的旧切片（画面完全对不上）。带上区间后，
            # 重复运行时命中缓存就直接复用，省掉重新编码。
            shot_path = self.cache_dir / (
                f"shot_{shot_id:05d}_{start:.2f}-{end:.2f}.mp4")

            if shot_path.exists() and shot_path.stat().st_size > 1000:
                shots.append(Shot(
                    shot_id=shot_id,
                    start_time=start,
                    end_time=end,
                    duration=end - start,
                    file_path=str(shot_path),
                    source_video=video_path,
                ))
                continue

            # ⚠️ 必须重编码，不能用 `-c copy`。
            # 原来写的是 `-ss <start> -i in -to <dur> -c copy`：copy 模式无法丢帧，
            # ffmpeg 只能从 `<= start` 的**关键帧**开始拷，于是镜头文件的**实际内容**
            # 比记录的 `start_time` 提前一整个 GOP —— 本片实测中位 -0.77s、最大 -4.3s
            # （源的关键帧间隔中位 1.2s、最大 2.0s）。后果是「标称上互相分离」的两个
            # 镜头，实际画面仍大面积重叠；源上同一时刻被最多 4~6 个镜头文件覆盖 →
            # 成片里看着就是"同一个片段用了 4 次"。
            # 重编码时 ffmpeg 会 seek 到关键帧后**解码并丢弃**到 start 之前的帧，切点精确。
            # 另外 `-to` 在"输入 -ss"语境下语义含糊，改用相对时长 `-t`。
            cmd = [
                "ffmpeg", "-y", *decode_args(),
                "-ss", str(start),
                "-i", video_path,
                "-t", str(end - start),
                # 直接在切镜头时降到成片工作分辨率 720p：
                # 源常见 4K/60fps，若原样保留，后续每个镜头都要反复解码
                # 4K，磁盘占用也大（单镜头可达数百 MB）。成片本来就是
                # 1280x720，这里降采样不损失最终画质。
                "-vf", (f"scale={_WORK_SIZE[0]}:{_WORK_SIZE[1]}:"
                        "force_original_aspect_ratio=decrease:flags=lanczos,"
                        f"pad={_WORK_SIZE[0]}:{_WORK_SIZE[1]}:(ow-iw)/2:(oh-ih)/2:color=black,"
                        "setsar=1,fps=30"),
                *encode_args(18, "fast"),
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                str(shot_path),
            ]
            try:
                result = run_ffmpeg(cmd, capture_output=True, timeout=180)
                if result.returncode != 0 or not shot_path.exists():
                    print(f"  镜头切割失败: shot_{i:04d}")
                    continue
            except subprocess.TimeoutExpired:
                print(f"  镜头切割超时: shot_{i:04d}")
                continue

            shots.append(Shot(
                shot_id=shot_id,
                start_time=start,
                end_time=end,
                duration=end - start,
                file_path=str(shot_path),
                source_video=video_path,
            ))

        # 如果切割全部失败，直接复制原视频作为单镜头
        if not shots and duration > 0:
            print(f"  切割失败，使用原视频作为单镜头")
            shots.append(Shot(
                shot_id=video_index * 10000,
                start_time=0.0,
                end_time=duration,
                duration=duration,
                file_path=video_path,
                source_video=video_path,
            ))

        print(f"  检测到 {len(shots)} 个镜头")
        return shots

    def _detect_scenes(self, video_path: str, threshold: float) -> list:
        """使用 FFmpeg 检测场景变化

        先缩到 640 宽再算 scene 分数：4K 素材的分析耗时主要花在滤镜上，
        降规格后快一个数量级，而切点时间（pts）与全分辨率完全一致——
        这样才敢把"分析前 N 秒"放宽到几分钟，不再因为怕慢而只用 2 分钟素材。
        """
        cmd = [
            ffmpeg_bin(), "-hide_banner", "-nostdin", "-i", video_path,
            "-vf", f"scale=640:-2,select='gt(scene,{threshold})',showinfo",
            "-an", "-f", "null", "-",
        ]

        result = run_ffmpeg(cmd, capture_output=True)

        # 解析时间点（用 bytes 处理避免编码问题）
        stderr_text = result.stderr.decode('utf-8', errors='ignore') if result.stderr else ""
        times = []
        for line in stderr_text.split('\n'):
            if 'pts_time' in line:
                match = re.search(r'pts_time:(\d+\.?\d*)', line)
                if match:
                    times.append(float(match.group(1)))

        # 生成场景列表
        scenes = []
        for i in range(len(times)):
            start = times[i]
            end = times[i + 1] if i + 1 < len(times) else self._get_duration(video_path)
            scenes.append((start, end))

        # 添加第一个场景
        if scenes and scenes[0][0] > 0:
            scenes.insert(0, (0.0, scenes[0][0]))

        return scenes

    def _get_duration(self, video_path: str) -> float:
        """获取视频时长"""
        cmd = [
            ffprobe_bin(), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
        result = subprocess.run(cmd, capture_output=True)
        return float(result.stdout.decode('utf-8', errors='ignore').strip())


# 运动分析缓存：key = (源文件, 源内起止, 源 mtime) -> MotionData
_MOTION_CACHE: Dict[Any, MotionData] = {}


class MotionAnalyzer:
    """运动分析 - 使用 OpenCV 光流（采样帧优化 + 可选 CUDA）

    优化点：
    1. 默认把画面缩到 640 宽再算 Farneback 光流（1080p 下约 4~9 倍提速），
       幅度再乘回缩放比例，评分口径基本不变。
    2. 装了带 CUDA 的 OpenCV 时自动走 ``cv2.cuda.FarnebackOpticalFlow``；
       pip 官方 wheel 通常不带 CUDA，会自动回退 CPU。
    3. 结果按源视频 + 时间区间缓存，重复混剪同一素材不重复分析。
    """

    _cuda_flow = None
    _cuda_checked = False

    @classmethod
    def _get_cuda_flow(cls):
        if cls._cuda_checked:
            return cls._cuda_flow
        cls._cuda_checked = True
        try:
            import cv2
            if int(cv2.cuda.getCudaEnabledDeviceCount()) <= 0:
                return None
            factory = getattr(cv2.cuda, "FarnebackOpticalFlow_create", None) \
                or getattr(cv2.cuda.FarnebackOpticalFlow, "create", None)
            if factory is None:
                return None
            cls._cuda_flow = factory(3, 0.5, False, 15, 3, 5, 1.2, 0)
        except Exception:
            cls._cuda_flow = None
        return cls._cuda_flow

    def _flow(self, cv2, prev_gray, gray):
        """优先 CUDA 光流，失败退回 CPU。返回 (flow, 是否用了 GPU)。"""
        cuda_flow = self._get_cuda_flow()
        if cuda_flow is not None:
            try:
                gpu_mat = getattr(cv2, "cuda_GpuMat", None)
                if gpu_mat is None:
                    gpu_mat = cv2.cuda.GpuMat
                g_prev = gpu_mat()
                g_prev.upload(prev_gray)
                g_cur = gpu_mat()
                g_cur.upload(gray)
                g_flow = cuda_flow.calc(g_prev, g_cur, None)
                return g_flow.download(), True
            except Exception:
                pass
        return cv2.calcOpticalFlowFarneback(
            prev_gray, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0
        ), False

    def analyze(self, shot_path: str, max_frames: int = 30,
                cache_key: Any = None) -> MotionData:
        """分析镜头运动（只分析前 max_frames 帧）"""
        if cache_key is not None and cache_key in _MOTION_CACHE:
            return _MOTION_CACHE[cache_key]

        try:
            import cv2

            cap = cv2.VideoCapture(shot_path)
            if not cap.isOpened():
                return MotionData()

            ret, prev_frame = cap.read()
            if not ret:
                return MotionData()

            # 统一缩到 640 宽，光流在 CPU 上快一个数量级
            target_w = 640
            scale = 1.0
            if prev_frame.shape[1] > target_w:
                scale = prev_frame.shape[1] / float(target_w)
                prev_frame = cv2.resize(
                    prev_frame,
                    (target_w, max(2, int(prev_frame.shape[0] / scale))),
                    interpolation=cv2.INTER_AREA,
                )

            prev_gray = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)

            magnitudes = []
            frame_count = 0
            while frame_count < max_frames:
                ret, frame = cap.read()
                if not ret:
                    break

                if scale > 1.0:
                    frame = cv2.resize(
                        frame,
                        (target_w, max(2, int(frame.shape[0] / scale))),
                        interpolation=cv2.INTER_AREA,
                    )
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                flow, _ = self._flow(cv2, prev_gray, gray)
                magnitude = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
                # 缩放后的像素位移乘回原分辨率，保持评分口径
                magnitudes.append(float(np.mean(magnitude)) * scale)

                prev_gray = gray
                frame_count += 1

            cap.release()

            if not magnitudes:
                return MotionData()

            avg_magnitude = float(np.mean(magnitudes))
            shake = float(np.std(magnitudes))
            result = MotionData(magnitude=avg_magnitude, shake=shake)
            if cache_key is not None:
                _MOTION_CACHE[cache_key] = result
            return result

        except ImportError:
            # 如果没有 OpenCV，返回默认值
            return MotionData(magnitude=0.5, shake=0.1)
