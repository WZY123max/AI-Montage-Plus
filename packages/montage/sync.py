"""卡点同步：镜头时长量化到节拍、选择转场。"""

import bisect
import random
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from packages.core_types.models import Beat, Shot, TimelineEntry

from .media import ffprobe_bin


# 卡点转场的**硬上限**（秒）。
# xfade 会吞掉转场时长，导致后面的切点整体提前；这里用"前一镜头尾部
# 多准备 d 秒"来补偿，使成片总长仍等于时间线标称长度。
# 旧值 0.18 让所有转场都短到肉眼看不见，用户反馈「转场只有快闪」。
# 现在只是安全上限，实际时长由 creative.plan_dynamic 按高级设置决定
# （默认 0.40s，可在 0.05~0.80 之间调）。
MAX_BEAT_XFADE = 0.80

# 这些转场会制造黑场/闪黑（实测 circlecrop/rectcrop 中间帧 mean≈0，
# flash/blur/motion_blur/shake 原先映射到 fadeblack），统一降级为 fade。
BLACK_UNSAFE_TRANSITIONS = {
    "fadeblack", "flash", "blur", "motion_blur", "shake",
    "circlecrop", "rectcrop", "zoom",
}

# 单个镜头在成片里最多占多少秒。长目标（10 分钟）需要长镜头，
# 但超过 10s 的单一画面会明显拖沓，所以这里封顶。
MAX_SHOT_SECONDS = 10.0


class BeatSyncEngine:
    """卡点同步引擎 - 镜头时长严格量化到整拍，结束点对齐节拍边界"""

    # 转场类型映射
    TRANSITION_TYPES = ["cut", "fade", "dissolve", "wipe", "flash", "zoom", "blur"]

    def __init__(self):
        # 最近一次规划的可观测数据（供 pipeline 打印"素材不足"警告）
        self.last_stats: Dict[str, Any] = {}

    @staticmethod
    def _shot_seconds(shot) -> float:
        """镜头文件**实际可用画面**的秒数；取不到返回 None（表示放弃约束）"""
        try:
            d = float(getattr(shot, "duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if d > 0:
            return d
        path = getattr(shot, "file_path", None)
        if path and Path(path).exists():
            cmd = [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace").stdout
            try:
                v = float((out or "").strip())
                return v if v > 0 else None
            except ValueError:
                return None
        return None

    def sync(
        self,
        shots: List[Shot],
        beats: List[Beat],
        style: str = "dynamic",
        transition_pattern: dict = None,
        target_duration: Optional[float] = None,
        rng: Optional[random.Random] = None,
        allow_reuse: bool = True,
        max_shot_seconds: float = MAX_SHOT_SECONDS,
    ) -> List[TimelineEntry]:
        """把镜头排成时间线。

        - 给了 ``target_duration``：按目标时长规划（推荐，成片长度可控）。
        - 没给：保留旧的"每镜头 1~4 拍"行为（向后兼容）。

        旧实现只按节拍数累加，镜头用完就结束：21 个镜头 × 1~2 拍 ≈ 6~10 秒，
        所以用户填 60s/600s 也只能得到 6~10 秒。新实现按"目标时长"分配每个
        镜头的秒数，并允许从镜头内部随机取子区间，长度和画面多样性都可控。
        """
        self.last_stats = {}
        if not shots or not beats:
            return []

        if target_duration and float(target_duration) > 0:
            return self._sync_to_target(
                shots, beats, float(target_duration), style,
                transition_pattern, rng, allow_reuse, max_shot_seconds,
            )
        return self._sync_by_beats(shots, beats, style, transition_pattern)

    # ------------------------------------------------------------------
    # 目标时长规划（新路径）
    # ------------------------------------------------------------------
    def _sync_to_target(
        self,
        shots: List[Shot],
        beats: List[Beat],
        target: float,
        style: str,
        transition_pattern: Optional[dict],
        rng: Optional[random.Random],
        allow_reuse: bool,
        max_shot_seconds: float,
    ) -> List[TimelineEntry]:
        rng = rng or random.Random()

        # 节拍间隔：用中位数，避免 BPM 检测的异常值
        if len(beats) >= 2:
            intervals = [beats[i + 1].time - beats[i].time
                         for i in range(len(beats) - 1)]
            intervals = [x for x in intervals if x > 0.05]
            beat_interval = sorted(intervals)[len(intervals) // 2] if intervals else 0.5
        else:
            beat_interval = 0.5
        beat_interval = max(0.15, float(beat_interval))

        style_cfg = self._get_style_config(style)
        ideal = {"dynamic": 2.2, "calm": 3.2, "intense": 1.4}.get(style, 2.2)

        # 过滤掉没有可用画面的镜头
        pool = [s for s in shots
                if (self._shot_seconds(s) or 0.0) >= 0.2]
        if not pool:
            return []

        available_total = sum(float(self._shot_seconds(s) or 0.0) for s in pool)
        beat_times = [float(b.time) for b in beats]

        timeline: List[TimelineEntry] = []
        cursor = 0.0
        shot_i = 0
        cycle = 0
        max_cycles = 50 if allow_reuse else 0

        # 最多 5000 段，防止异常参数导致死循环
        while cursor < target - max(0.05, beat_interval * 0.5) and len(timeline) < 5000:
            if shot_i >= len(pool):
                if not allow_reuse or cycle >= max_cycles:
                    break
                cycle += 1
                shot_i = 0
                # 复用同一批镜头时打乱顺序，并让每个镜头重新随机取子区间，
                # 这样第二次循环至少不会和第一次逐帧一样。
                rng.shuffle(pool)

            shot = pool[shot_i]
            shot_i += 1
            avail = float(self._shot_seconds(shot) or 0.0)
            if avail < 0.2:
                continue

            remaining = target - cursor
            left = max(1, len(pool) - shot_i + 1)
            # 平均分给剩余镜头；1.03 是留给节拍取整的余量
            desired = remaining / left * 1.03
            # 太短的切点会碎，给一个风格下限；太长的单镜头会拖，封顶
            desired = max(desired, min(ideal * 0.75, remaining))
            beat_count = max(1, int(round(desired / beat_interval)))
            duration = beat_count * beat_interval
            duration = min(duration, max_shot_seconds)
            duration = min(duration, remaining) if remaining > 0 else duration
            duration = max(min(duration, remaining), min(beat_interval, remaining))
            if duration <= 0.05:
                break

            # 画面窗口：优先 1.0x 播放、从镜头内部随机取一段；
            # 镜头不够长才降速（最低 0.5x），避免末帧冻结。
            if duration <= avail:
                speed = 1.0
                window = duration
                # 轻微变速让每次剪辑的质感不完全一样（不影响总时长）
                jitter = rng.choice((1.0, 1.0, 1.0, 1.04, 0.96))
                if duration * jitter <= avail:
                    speed = jitter
                    window = duration * jitter
            else:
                speed = max(0.5, avail / duration)
                duration = min(duration, avail / 0.5)
                window = avail
            window = max(0.2, min(window, avail))
            source_in = rng.uniform(0.0, max(0.0, avail - window))

            beat_idx = min(max(0, bisect.bisect_left(beat_times, cursor)),
                           len(beats) - 1)
            beat = beats[beat_idx]

            transition_type = self._pick_transition(
                beat, shot, style, transition_pattern)
            if transition_type in BLACK_UNSAFE_TRANSITIONS:
                transition_type = "fade"
            if transition_pattern and transition_type != "cut":
                dur_ratio = transition_pattern.get(
                    "strong_duration_ratio"
                    if beat.beat_type == "strong" else "weak_duration_ratio",
                    0.5,
                )
                transition_duration = min(
                    beat_interval * float(dur_ratio), MAX_BEAT_XFADE)
            else:
                transition_duration = (
                    min(beat_interval * 0.5, MAX_BEAT_XFADE)
                    if transition_type != "cut" else 0.0
                )

            timeline.append(TimelineEntry(
                shot_id=shot.shot_id,
                shot_path=shot.file_path or "",
                start_time=cursor,
                end_time=cursor + duration,
                duration=duration,
                beat_time=cursor,
                speed_factor=speed,
                transition_type=transition_type,
                transition_duration=transition_duration,
                source_in=float(source_in),
                source_out=float(source_in + window),
            ))
            cursor += duration

        unique = len({e.shot_id for e in timeline})
        self.last_stats = {
            "target": target,
            "actual": cursor,
            "shots": len(timeline),
            "unique_shots": unique,
            "reused": max(0, len(timeline) - unique),
            "cycles": cycle,
            "available": available_total,
            "short": cursor < target * 0.95,
        }
        return timeline

    # ------------------------------------------------------------------
    # 旧路径：没有目标时长时按节拍数排
    # ------------------------------------------------------------------
    def _sync_by_beats(
        self,
        shots: List[Shot],
        beats: List[Beat],
        style: str,
        transition_pattern: Optional[dict],
    ) -> List[TimelineEntry]:
        if len(beats) >= 2:
            intervals = [beats[i + 1].time - beats[i].time
                         for i in range(len(beats) - 1)]
            beat_interval = sorted(intervals)[len(intervals) // 2]
        else:
            beat_interval = 0.5

        style_cfg = self._get_style_config(style)
        timeline = []
        current_beat_idx = 0

        for shot in shots:
            if current_beat_idx >= len(beats):
                break

            beat_time = beats[current_beat_idx].time
            beat = beats[current_beat_idx]
            beat_count = self._calc_beat_count(
                shot.highlight_score, beat.beat_type, style_cfg)
            speed_factor = self._calc_speed(
                shot.highlight_score, beat.beat_type, style_cfg)

            avail = self._shot_seconds(shot)
            min_speed = float(style_cfg.get("speed_range", (0.8, 1.2))[0])
            if avail is not None:
                fit_beats = int(avail / max(0.1, min_speed) / beat_interval + 1e-6)
                if fit_beats < 1:
                    continue
                beat_count = min(beat_count, fit_beats)

            end_idx = min(current_beat_idx + beat_count, len(beats) - 1)
            duration = float(beats[end_idx].time) - float(beat_time)
            if duration <= 0:
                duration = beat_count * beat_interval

            if avail is not None:
                while (duration > 0 and avail / duration < min_speed - 1e-4
                       and beat_count > 1):
                    beat_count -= 1
                    end_idx = min(current_beat_idx + beat_count, len(beats) - 1)
                    duration = float(beats[end_idx].time) - float(beat_time)
                if duration <= 0 or avail / duration < min_speed - 1e-4:
                    continue
                max_speed = avail / duration
                speed_factor = max(min_speed, min(speed_factor, max_speed))

            transition_type = self._pick_transition(
                beat, shot, style, transition_pattern)
            if transition_type in BLACK_UNSAFE_TRANSITIONS:
                transition_type = "fade"
            if transition_pattern and transition_type != "cut":
                dur_ratio = transition_pattern.get(
                    "strong_duration_ratio"
                    if beat.beat_type == "strong" else "weak_duration_ratio",
                    0.5,
                )
                transition_duration = min(
                    beat_interval * dur_ratio, MAX_BEAT_XFADE)
            else:
                transition_duration = (
                    min(beat_interval * 0.5, MAX_BEAT_XFADE)
                    if transition_type != "cut" else 0.0
                )

            timeline.append(TimelineEntry(
                shot_id=shot.shot_id,
                shot_path=shot.file_path or "",
                start_time=beat_time,
                end_time=beat_time + duration,
                duration=duration,
                beat_time=beat_time,
                speed_factor=speed_factor,
                transition_type=transition_type,
                transition_duration=transition_duration,
            ))
            current_beat_idx += beat_count

        timeline.sort(key=lambda x: x.start_time)
        self.last_stats = {
            "target": None,
            "actual": sum(e.duration for e in timeline),
            "shots": len(timeline),
            "unique_shots": len({e.shot_id for e in timeline}),
            "reused": 0,
            "cycles": 0,
            "available": sum(float(self._shot_seconds(s) or 0.0) for s in shots),
            "short": False,
        }
        return timeline

    def _get_style_config(self, style: str) -> Dict[str, Any]:
        """获取风格配置"""
        configs = {
            "dynamic": {
                "strong_beats": (1, 2),      # 强拍镜头：1-2 拍
                "normal_beats": (2, 3),       # 普通镜头：2-3 拍
                "weak_beats": (3, 4),         # 弱拍镜头：3-4 拍
                "speed_range": (0.85, 1.15),  # 速度范围
                "strong_speed": 0.9,          # 强拍速度（略慢，强调）
                "weak_speed": 1.1,            # 弱拍速度（略快，紧凑）
            },
            "calm": {
                "strong_beats": (2, 3),
                "normal_beats": (3, 4),
                "weak_beats": (4, 6),
                "speed_range": (0.9, 1.1),
                "strong_speed": 0.95,
                "weak_speed": 1.05,
            },
            "intense": {
                "strong_beats": (1, 1),
                "normal_beats": (1, 2),
                "weak_beats": (2, 3),
                "speed_range": (0.8, 1.2),
                "strong_speed": 0.85,
                "weak_speed": 1.15,
            },
        }
        return configs.get(style, configs["dynamic"])

    def _calc_beat_count(
        self, highlight_score: float, beat_type: str, cfg: Dict
    ) -> int:
        """
        根据高光分数和拍类型决定镜头占几拍
        高光分数越高 + 拍越强 -> 拍数越少（镜头越短，节奏越快）
        """
        if beat_type == "strong":
            # 强拍：高分镜头用最少拍数
            if highlight_score > 0.7:
                beat_range = cfg["strong_beats"]
            else:
                beat_range = (cfg["strong_beats"][0] + 1, cfg["strong_beats"][1] + 1)
        elif beat_type == "weak":
            # 弱拍：用较多拍数
            beat_range = cfg["weak_beats"]
        else:
            # 普通拍
            beat_range = cfg["normal_beats"]

        # 在范围内随机选（增加变化感）
        min_b, max_b = beat_range
        if min_b == max_b:
            return min_b
        # 高分偏短，低分偏长
        if highlight_score > 0.6:
            return min_b
        elif highlight_score > 0.3:
            return (min_b + max_b) // 2
        else:
            return max_b

    def _calc_speed(
        self, highlight_score: float, beat_type: str, cfg: Dict
    ) -> float:
        """
        计算速度因子（范围 0.8~1.2）
        强拍略慢（强调），弱拍略快（紧凑）
        """
        base_speed = cfg["strong_speed"] if beat_type == "strong" else cfg["weak_speed"]

        # 根据高光分数微调
        if highlight_score > 0.7:
            # 高光镜头：强拍更慢，弱拍更快
            adjustment = 0.05 if beat_type == "strong" else -0.05
        else:
            adjustment = 0.0

        speed = base_speed + adjustment

        # 限制范围
        min_speed, max_speed = cfg["speed_range"]
        return max(min_speed, min(speed, max_speed))

    def _pick_transition(self, beat: Beat, shot: Shot, style: str,
                         transition_pattern: dict = None) -> str:
        """根据拍类型、风格和转场模板选择转场效果"""
        if transition_pattern:
            from packages.montage_engine.src.transition_patterns import pick_transition
            motion = getattr(shot, 'motion_score', 0.0) or 0.0
            trans_type, _ = pick_transition(transition_pattern, beat.beat_type, motion)
            return trans_type

        # 旧逻辑：根据风格随机选
        if beat.beat_type == "strong":
            if style == "intense":
                return random.choice(["flash", "zoom", "cut"])
            elif style == "calm":
                return random.choice(["fade", "dissolve"])
            else:
                return random.choice(["cut", "fade", "flash"])

        if style == "intense":
            return "cut"
        elif style == "calm":
            return random.choice(["dissolve", "fade"])
        else:
            return random.choice(["cut", "dissolve"])
