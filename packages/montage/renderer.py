"""视频渲染：截取/变速/转场/BGM 混音。"""

import subprocess
from pathlib import Path
from typing import List, Optional

from packages.core_types.models import TimelineEntry

from .helpers import _probe_media_duration, _safe_float
from .media import (decode_args, encode_args, ffprobe_bin, quality_crf,
                    run_ffmpeg)
from .paths import OUTPUT_DIR
from .sync import BLACK_UNSAFE_TRANSITIONS, MAX_BEAT_XFADE


def tail_fade_seconds(duration: float, setting="auto") -> float:
    """片尾音乐渐弱时长（秒）。

    auto = 成片时长的 7%，夹在 2.0~4.5s（60s 成片约 4.2s，正好覆盖最后一句）；
    off/0 = 不渐弱；数字 = 用户指定秒数。最多不超过成片时长的 45%，
    免得短视频被淡掉一大半。
    """
    key = str(setting if setting is not None else "auto").strip().lower()
    if key in ("off", "none", "no", "false", "0"):
        return 0.0
    d = float(duration or 0.0)
    if d <= 0:
        return 0.0
    if key in ("auto", "on", "yes", "true", "1"):
        fade = min(4.5, max(2.0, d * 0.07))
    else:
        try:
            fade = float(key)
        except (TypeError, ValueError):
            fade = min(4.5, max(2.0, d * 0.07))
    return max(0.0, min(fade, d * 0.45))


class VideoRenderer:
    """视频渲染器"""

    def __init__(self, output_dir: Optional[str] = None):
        self.output_dir = Path(output_dir) if output_dir else OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def render(
        self,
        timeline: List[TimelineEntry],
        bgm_path: str,
        output_path: str,
        bgm_offset: float = 0.0,
        source_volume: float = 0.2,
        loop_bgm: bool = False,
        apply_look: bool = False,
        enable_ducking: bool = False,
        duck_level_db: float = -12.0,
        bgm_fade_out: str = "auto",
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> str:
        """渲染最终视频（支持转场效果，且不产生黑场）

        enable_ducking: 对白闪避。在**混音这一遍**里用原声做 sidechain 压缩 BGM，
            而不是渲染完再叠一遍 BGM（旧做法会把 BGM 混成两份、闪避也压不到
            已经内嵌进成片的那一份）。

        bgm_fade_out: 片尾音乐渐弱。auto = 按成片时长自适应（默认开启），
            off = 关闭，数字 = 指定渐弱秒数。做在 BGM 归一化那一遍，
            所以闪避/音效/旁白/字幕等后续处理都会继承，不会硬切。
        width/height: 输出画布尺寸。默认 None＝按素材自动挑（跟着素材走 720p/
            1080p/1440p/2160p），不再无条件压到 1280x720。
        """
        print(f"  渲染视频...")

        # 收集有效的镜头（按时间排序）
        valid_entries = [e for e in timeline if e.shot_path and Path(e.shot_path).exists()]
        if not valid_entries:
            raise ValueError("没有有效的镜头文件")

        # 画布尺寸：用户显式指定 > 按素材自适应（只降不升，避免放大造假清晰度）
        canvas_w, canvas_h = self._resolve_canvas(
            [e.shot_path for e in valid_entries], width, height)
        print(f"  输出画布: {canvas_w}x{canvas_h}")

        temp_files = []

        # 清理上一次异常中断（崩溃/被取消）留下的临时切片：
        # 旧实现只在正常收尾时删，任务一旦中断，output 目录就会堆一堆 clip_xx.mp4。
        for stale in self.output_dir.glob(".tmp_*"):
            try:
                stale.unlink()
            except OSError:
                pass

        # Step 0: 卡点转场补偿（关键）
        # xfade 的输出 = 前段 + 后段 - 转场时长，会把后面的切点整体提前。
        # 这里给"前一镜头"尾部多准备 d 秒（d = 后一镜头的转场时长），
        # xfade 吃掉 d 后总长不变；新镜头仍然在节拍点开始，转场只发生在
        # 节拍之后 d 秒内。MAX_BEAT_XFADE 只作安全上限（0.8s），实际时长
        # 由规划器按高级设置给（默认 0.40s），不再被压成看不见的 0.18s。
        for i, entry in enumerate(valid_entries):
            nxt = valid_entries[i + 1] if i + 1 < len(valid_entries) else None
            if nxt is not None and getattr(nxt, "transition_type", "cut") != "cut":
                d = float(getattr(nxt, "transition_duration", 0.0) or 0.0)
                d = max(0.0, min(d, MAX_BEAT_XFADE))
                if d < 0.03:
                    # 太短的转场没有意义，直接硬切，避免无谓的冻结/闪烁
                    nxt.transition_type = "cut"
                    nxt.transition_duration = 0.0

        # Step 1: 截取 + 调整速度（合并为一次 FFmpeg 调用）
        import shutil
        prepared = []
        for idx, entry in enumerate(valid_entries):
            nxt = valid_entries[idx + 1] if idx + 1 < len(valid_entries) else None
            d_out = 0.0
            if nxt is not None and getattr(nxt, "transition_type", "cut") != "cut":
                d_out = float(getattr(nxt, "transition_duration", 0.0) or 0.0)
            # 临时切片统一用 .tmp_ 前缀，和成片文件区分开（正常收尾会删掉）
            clip_path = self.output_dir / f".tmp_clip_{idx}.mp4"
            # 转场要吃掉前一镜头尾部 d 秒：优先从源镜头里多取 d 秒**真实画面**，
            # 而不是让 tpad 克隆末帧。0.40s 转场若用定格帧参与叠化，会明显
            # 看到画面"卡一下"；源到头了 _prepare_clip 会自动兜底补帧。
            src_out = float(getattr(entry, "source_out", 0.0) or 0.0)
            if d_out > 0:
                src_out += d_out * float(getattr(entry, "speed_factor", 1.0) or 1.0)
            self._prepare_clip(
                entry.shot_path, str(clip_path),
                float(entry.duration) + d_out, entry.speed_factor,
                width=canvas_w, height=canvas_h,
                source_in=getattr(entry, "source_in", 0.0),
                source_out=src_out,
                effect=getattr(entry, "effect", "none"),
            )
            temp_files.append(clip_path)
            prepared.append(str(clip_path))

        # Step 2: 检查是否有非 cut 转场
        has_transitions = any(
            getattr(e, "transition_type", "cut") != "cut" and getattr(e, "transition_duration", 0) > 0
            for e in valid_entries
        )

        if has_transitions and len(prepared) > 1:
            # 使用 xfade 滤镜应用转场
            concat_video = self._render_with_transitions(prepared, valid_entries)
        else:
            # 简单拼接（无转场或全部是 cut）
            concat_video = self._render_concat(prepared)

        temp_files.append(concat_video)

        # Step 3: BGM 混音（BGM 为主 + 原始音频低音量叠加）
        # 先归一化 BGM 音量（-14 LUFS 标准响度）；bgm_offset 用于裁掉前奏，
        # 让 BGM 的第一个节拍与成片 0s 对齐，而不是在开头补黑场。
        # 成片比 BGM 长时循环 BGM，避免 -shortest 把画面截断。
        video_duration = self._get_duration(concat_video)
        normalized_bgm = self.output_dir / ".tmp_bgm.wav"
        self._normalize_bgm(
            bgm_path, str(normalized_bgm), start=bgm_offset,
            duration=video_duration, loop=loop_bgm,
            fade_out=bgm_fade_out,
        )
        _fade = tail_fade_seconds(video_duration, bgm_fade_out)
        if _fade > 0.05:
            print(f"  片尾音乐渐弱: 最后 {_fade:.2f}s 平滑收尾")
        temp_files.append(normalized_bgm)

        # 统一成片质感：轻对比 + 轻饱和 + 暗角。用户另选了调色预设时不叠加。
        look_filter = (
            "eq=contrast=1.05:saturation=1.06:brightness=0.004,"
            "vignette=PI/6"
        )
        audio_fc = (
            f"[0:a]volume={max(0.0, float(source_volume))}[orig];"
            f"[1:a]volume=1.0[bgm];"
            f"[orig][bgm]amix=inputs=2:duration=longest:"
            f"dropout_transition=2,apad[aout]"
        )

        # 对白闪避：混音这一遍直接用原声做 sidechain 压 BGM。
        # 旧实现是渲染完再调 duck_audio() 把 BGM 叠第二遍，结果是 BGM 两份、
        # 闪避也压不到已经内嵌的那一份，等于失效。这里一次性做对。
        src_vol = max(0.0, float(source_volume))
        ducking_active = bool(enable_ducking) and src_vol > 0.001
        if ducking_active:
            # ratio 由闪避深度反推：-12dB ≈ 10:1
            ratio = max(2.0, min(20.0, abs(float(duck_level_db)) / 1.2))
            audio_fc = (
                "[0:a]volume={v},aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo[orig];"
                "[1:a]volume=1.0,aresample=48000,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo[bgm];"
                "[orig]asplit=2[origA][sc];"
                "[bgm][sc]sidechaincompress=threshold=0.05:ratio={r:.1f}:"
                "attack=20:release=350:level_sc=1[bgmd];"
                "[origA][bgmd]amix=inputs=2:duration=longest:"
                "dropout_transition=2,apad[aout]"
            ).format(v=src_vol, r=ratio)
        if apply_look:
            filter_complex = f"[0:v]{look_filter}[vout];{audio_fc}"
            maps = ["-map", "[vout]", "-map", "[aout]"]
            video_args = list(encode_args(None, "quality"))
        else:
            filter_complex = audio_fc
            maps = ["-map", "0:v:0", "-map", "[aout]"]
            video_args = ["-c:v", "copy"]

        # 混音：BGM 100% + 原始音频 source_volume（默认 20%，可静音）
        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-i", str(concat_video),
            "-i", str(normalized_bgm),
            "-filter_complex", filter_complex,
            *maps,
            *video_args,
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{video_duration:.3f}",
            "-movflags", "+faststart",
            output_path
        ]
        result = run_ffmpeg(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            # 如果原始视频没有音频轨道，回退到只用 BGM
            print(f"  混音失败（可能无原音轨），使用纯 BGM")
            cmd = [
                "ffmpeg", "-y", "-nostdin",
                "-i", str(concat_video),
                "-i", str(normalized_bgm),
                *video_args,
                "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-t", f"{video_duration:.3f}",
                "-movflags", "+faststart",
                output_path
            ]
            run_ffmpeg(cmd, capture_output=True, check=True)

        # 清理临时文件
        for f in temp_files:
            Path(f).unlink(missing_ok=True)

        print(f"  输出: {output_path}")
        return output_path

    def _render_concat(self, video_paths: List[str]) -> str:
        """简单拼接（无转场）"""
        concat_file = self.output_dir / ".tmp_concat.txt"
        with open(concat_file, 'w') as f:
            for path in video_paths:
                f.write(f"file '{Path(path).resolve().as_posix()}'\n")

        concat_video = self.output_dir / ".tmp_concat.mp4"
        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-fflags", "+genpts",
            "-c", "copy",
            str(concat_video)
        ]
        run_ffmpeg(cmd, capture_output=True, check=True)
        concat_file.unlink(missing_ok=True)
        return str(concat_video)

    def _render_with_transitions(self, video_paths: List[str], entries: List[TimelineEntry]) -> str:
        """渲染视频：cut 用 concat，非 cut 连续段用 xfade"""
        if len(video_paths) < 2:
            return self._render_concat(video_paths)

        durations = [self._get_duration(p) for p in video_paths]

        # _prepare_clip 已把每个 clip 统一成 1280x720 / 30fps / setsar=1，
        # 并补齐了音轨结构；这里直接复用，不再重复转码一遍。
        # 65 个短切如果每个都再 scale+fps+encode，会白烧几十秒。
        scaled_files = list(video_paths)

        # 按转场类型分段：连续非 cut 的 clips 用 xfade，cut 处断开
        segments = []
        current_seg = [0]
        for i in range(1, len(video_paths)):
            entry = entries[i] if i < len(entries) else entries[-1]
            trans_type = getattr(entry, "transition_type", "cut")
            trans_dur = getattr(entry, "transition_duration", 0.5)
            if trans_type != "cut" and trans_dur > 0:
                current_seg.append(i)
            else:
                segments.append(current_seg)
                current_seg = [i]
        segments.append(current_seg)

        # 渲染每个 segment
        segment_videos = []
        xfade_temps = []
        for seg in segments:
            if len(seg) == 1:
                segment_videos.append(scaled_files[seg[0]])
            else:
                sv = self._xfade_segment(scaled_files, seg, durations, entries)
                segment_videos.append(sv)
                xfade_temps.append(sv)

        # concat 所有 segments
        if len(segment_videos) == 1:
            result_path = segment_videos[0]
        else:
            result_path = self._render_concat(segment_videos)

        # 清理临时文件（注意：scaled_files 现在是调用方传入的 clip，
        # 不能在这里删；它们由 render() 的 temp_files 统一清理。
        # 单段 xfade 时 result_path 本身就是临时文件，不能删）
        keep = str(result_path)
        for f in xfade_temps:
            if str(f) != keep:
                Path(f).unlink(missing_ok=True)

        return result_path

    def _xfade_segment(self, scaled_files: List[str], indices: List[int], durations: List[float], entries: List[TimelineEntry]) -> str:
        """对一组连续非-cut clips 应用 xfade 链（**视频 xfade + 音频 acrossfade 同步缩短**）"""
        seg_dur = [durations[i] for i in indices]
        filter_parts = []
        accum_dur = seg_dur[0]      # 视频流累计时长（xfade 语义：输出 = offset + 后段时长）
        accum_audio = seg_dur[0]    # 音频流累计时长（acrossfade 语义：输出 = 前段 + 后段 - d）
        current_label = "[0:v]"
        current_audio = "[0:a]"

        # 注意：这里**不允许**再映射到 fadeblack。
        # 原先把 flash/blur/motion_blur/shake 都映射成 fadeblack，成片里会出现
        # 明显的黑场；改成"有真实对应效果、又不会产生黑场"的 xfade：
        #   flash  -> fadewhite（白闪，MTV 高燃的标准手法）
        #   zoom   -> zoomin    （推近）
        #   blur / motion_blur -> hblur（水平模糊，模拟甩切运动模糊）
        # ffmpeg 7.x 才支持 fadewhite/zoomin/hblur，旧版本会自动回退成 fade。
        xfade_map = {
            "fade": "fade", "dissolve": "dissolve",
            "wipe": "wipeleft", "flash": "fadewhite",
            "zoom": "zoomin", "blur": "hblur",
            "motion_blur": "hblur",
            "wipeleft": "wipeleft", "wiperight": "wiperight",
            "wipeup": "wipeup", "wipedown": "wipedown",
            "slideleft": "slideleft", "slideright": "slideright",
            "slideup": "slideup", "slidedown": "slidedown",
            "smoothleft": "smoothleft", "smoothright": "smoothright",
            "smoothup": "smoothup", "smoothdown": "smoothdown",
            "circleopen": "circleopen", "circleclose": "circleclose",
            # circlecrop/rectcrop 实测转场中间整帧接近全黑（mean≈0），
            # 不能满足"不要黑场"，统一退化成 fade。
            "circlecrop": "fade", "rectcrop": "fade",
            "diagtl": "diagtl", "diagtr": "diagtr",
            "diagbl": "diagbl", "diagbr": "diagbr",
            "vertopen": "vertopen", "vertclose": "vertclose",
            "horzopen": "horzopen", "horzclose": "horzclose",
            "radial": "radial", "pixelize": "pixelize",
            "distance": "distance", "fadeblack": "fade",
            "fadewhite": "fadewhite",
            "hlslice": "hlslice", "hrslice": "hrslice",
            "vuslice": "vuslice", "vdslice": "vdslice",
            "shake": "fade",
            # ffmpeg 7.x 新增的斜向擦除 / 挤压 / 覆盖 / 揭示 / 风页。
            # 智能混搭模板会用到其中一部分，缺映射会被退化成 fade，
            # 又变回「看起来只有一种转场」。
            "wipetl": "wipetl", "wipetr": "wipetr",
            "wipebl": "wipebl", "wipebr": "wipebr",
            "squeezeh": "squeezeh", "squeezev": "squeezev",
            "coverleft": "coverleft", "coverright": "coverright",
            "coverup": "coverup", "coverdown": "coverdown",
            "revealleft": "revealleft", "revealright": "revealright",
            "revealup": "revealup", "revealdown": "revealdown",
            "fadegrays": "fadegrays", "fadefast": "fadefast",
            "fadeslow": "fadeslow", "zoomin": "zoomin",
            "hlwind": "hlwind", "hrwind": "hrwind",
            "vuwind": "vuwind", "vdwind": "vdwind",
        }

        for j in range(1, len(indices)):
            i = indices[j]
            entry = entries[i] if i < len(entries) else entries[-1]
            trans_dur = getattr(entry, "transition_duration", 0.5)
            trans_type = getattr(entry, "transition_type", "fade")
            xfade_type = xfade_map.get(trans_type, "fade")

            # xfade 要求转场时长 < 参与叠合的两段画面长度，否则输出退化（画面丢失）
            trans_dur = max(0.05, min(trans_dur, 0.9 * min(seg_dur[j], accum_dur)))

            # 转场起点 = 上一段（已含 d 秒尾部补偿）末尾 - 转场时长
            # => 起点正好是标称切点：新镜头在节拍点开始，转场只发生在
            #    节拍之后 d 秒内，不会把后面的切点整体提前。
            offset = accum_dur - trans_dur
            if offset < 0:
                offset = max(0.1, accum_dur * 0.5)

            # 音频交叉淡化时长不得 ≥ 任一被叠合的片段长度（acrossfade 会报错）
            fade_d = min(trans_dur, 0.9 * min(seg_dur[j], accum_audio))
            fade_d = max(0.05, fade_d)

            accum_dur = offset + seg_dur[j]
            accum_audio = accum_audio + seg_dur[j] - fade_d

            filter_parts.append(
                f"{current_label}[{j}:v]xfade=transition={xfade_type}:"
                f"duration={trans_dur}:offset={offset:.3f}[out{j}]"
            )
            filter_parts.append(
                f"{current_audio}[{j}:a]acrossfade=d={fade_d:.3f}:c1=tri:c2=tri[a{j}]"
            )
            current_label = f"[out{j}]"
            current_audio = f"[a{j}]"

        # 音频可能因转场误差比画面略长/略短：apad 补尾 + 输出 -t 精确对齐，避免
        # concat demuxer 按流各自拼接时音画逐段漂移
        filter_parts.append(f"{current_audio}apad[aout]")

        seg_video = self.output_dir / f"xfade_seg_{indices[0]}.mp4"
        cmd = ["ffmpeg", "-y"]
        for i in indices:
            cmd.extend(["-i", scaled_files[i]])
        cmd.extend([
            "-filter_complex", ";".join(filter_parts),
            "-map", current_label,
            "-map", "[aout]",
            "-t", f"{accum_dur:.3f}",
            *encode_args(None, "quality"),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            str(seg_video)
        ])

        result = run_ffmpeg(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        if result.returncode != 0:
            print(f"  xfade segment 失败，回退 concat")
            return self._render_concat([scaled_files[i] for i in indices])
        return str(seg_video)

    def _get_duration(self, video_path: str) -> float:
        """获取**视频流**时长（不是容器时长）

        容器 `format=duration` 是所有流时长的**最大值**。clip 的音频被 `apad`
        补齐到卡点时长后，音频往往比画面长（实测单段画面差 0.03~1.84s），
        这时再拿 format 时长当"画面长度"去算 xfade 的 `offset`，就会算出
        超过真实画面的 offset → xfade 越界、成片视频流被截断。
        所以这里取 `v:0` 的 stream duration，取不到再退回 format。
        """
        cmd = [
            ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            value = float((result.stdout or "").strip())
            if value > 0:
                return value
        except ValueError:
            pass

        # 退回容器时长
        cmd = [
            ffprobe_bin(), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            return float(result.stdout.strip())
        except ValueError:
            print(f"  [警告] 无法获取时长: {video_path}，使用默认值 2.0s")
            return 2.0

    @staticmethod
    def _has_audio(path: str) -> bool:
        """探测输入是否含音轨。

        用途：决定要不要补一条静音轨。**所有 clip 的流结构必须一致**，
        否则 concat demuxer 的 `-c copy` 会因为某段缺音轨而错位/失败。
        """
        cmd = [
            ffprobe_bin(), "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return bool((result.stdout or "").strip())

    @staticmethod
    def _atempo_chain(speed: float) -> str:
        """把任意倍速拆成多级 atempo（单级只支持 [0.5, 2.0]，超出必须串联）"""
        speed = max(0.1, float(speed))
        parts: List[str] = []
        s = speed
        while s > 2.0:
            parts.append("atempo=2.0")
            s /= 2.0
        while s < 0.5:
            parts.append("atempo=0.5")
            s /= 0.5
        parts.append(f"atempo={s:.6f}")
        return ",".join(parts)

    # 画布高度档位（16:9）。旧版本无条件压到 720p，1080p/4K 素材白丢一半以上像素。
    _CANVAS_LADDER = (480, 720, 1080, 1440, 2160)

    def _probe_dimensions(self, path: str) -> tuple:
        """读取视频分辨率，失败返回 (0, 0)。"""
        cmd = [
            ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
            w, h = r.stdout.strip().split(",")[:2]
            return int(w), int(h)
        except (ValueError, IndexError, OSError):
            return 0, 0

    def _resolve_canvas(self, paths: List[str],
                        width: Optional[int] = None,
                        height: Optional[int] = None) -> tuple:
        """决定输出画布尺寸。

        取所有素材短边的**中位数**作为目标高度（封顶 2160），再对齐到常见档位。
        用中位数而不是最大值：一两个 4K 素材不会把整条片子顶到 4K（编码慢、体积大），
        一两个低清素材也不会把整条片子拉到 480p。
        """
        if width and height and width > 1 and height > 1:
            return int(width) // 2 * 2, int(height) // 2 * 2

        sizes = [(w, h) for w, h in (self._probe_dimensions(p) for p in paths) if w and h]
        if not sizes:
            return 1280, 720

        shorts = sorted(min(w, h) for w, h in sizes)
        median = shorts[len(shorts) // 2]
        fitted = [hh for hh in self._CANVAS_LADDER if hh <= median]
        target = fitted[-1] if fitted else max(2, median // 2 * 2)
        target = min(target, 2160)
        return max(2, int(round(target * 16 / 9)) // 2 * 2), int(target)

    def _prepare_clip(self, input_path: str, output_path: str, duration: float,
                      speed: float = 1.0, width: int = 1280, height: int = 720,
                      source_in: float = 0.0, source_out: float = 0.0,
                      effect: str = "none"):
        """截取镜头内部子区间 + 变速 + 统一分辨率（一次 FFmpeg 调用，保留音轨）。

        与旧实现的区别：
        - 明确从 ``source_in`` 开始取 ``duration * speed`` 秒，而不是永远从镜头
          文件 0s 开始。这样长镜头可以只取中间最精彩的一段，同一素材每次
          也能取到不同画面。
        - 画面/声音长度严格等于 ``duration``，不足时克隆末帧，避免 xfade 越界。
        """
        has_audio = self._has_audio(input_path)
        total_src = self._get_duration(input_path)
        in_point = max(0.0, float(source_in or 0.0))
        if source_out and float(source_out) > in_point:
            avail = max(0.0, min(total_src - in_point,
                                float(source_out) - in_point))
        else:
            avail = max(0.0, total_src - in_point)

        speed = max(0.1, float(speed or 1.0))
        needed = max(0.1, float(duration) * speed)
        take = max(0.1, min(avail, needed))
        pad = float(duration) - take / speed
        # 有原音但画面需要补帧时，用 anullsrc+amix 补静音。
        # 不要用 apad：实测 atrim + apad 在部分源上会让 ffmpeg 永久等待。
        pad_audio = has_audio and pad > 0.02

        def _build(cpu_fallback: bool) -> list:
            cmd = ["ffmpeg", "-y", "-nostdin"]
            if not cpu_fallback:
                cmd.extend(decode_args())
            if in_point > 0.001:
                cmd.extend(["-ss", f"{in_point:.3f}"])
            cmd.extend(["-i", input_path])
            if (not has_audio) or pad_audio:
                cmd.extend(["-f", "lavfi", "-i",
                            "anullsrc=channel_layout=stereo:sample_rate=44100"])

            # flags=lanczos：默认的双线性缩放会把 1080p/4K 素材缩得发软，
            # 换成 lanczos 后细节保留明显更好（代价是切片编码慢一点点）
            scale_filter = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1"
            )
            # 用 trim 滤镜截取窗口，而不是在 -i 前加 -t。
            # 实测输入侧的 -t 会让 tpad 看不到 EOF，尾部补帧失效，
            # 每段短 0.1s 左右，13 段累计就少了将近 1 秒。
            if abs(speed - 1.0) > 1e-6:
                vf = (f"trim=duration={take:.3f},"
                      f"setpts={1.0 / speed:.6f}*(PTS-STARTPTS),"
                      f"fps=30,{scale_filter}")
            else:
                vf = (f"trim=duration={take:.3f},setpts=PTS-STARTPTS,"
                      f"fps=30,{scale_filter}")
            if pad > 0.02:
                vf += f",tpad=stop_mode=clone:stop_duration={pad + 0.10:.3f}"

            # 剪辑效果：推镜 / 白闪
            fx = (effect or "none").lower()
            if fx in {"punch", "punch_flash"}:
                frames = max(4, int(float(duration) * 30))
                vf += (
                    f",zoompan=z='min(1+0.10*on/{frames},1.10)':"
                    f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                    f"d=1:s={width}x{height}:fps=30"
                )
            if fx in {"flash", "punch_flash"}:
                # 从白色淡入：强拍切点上 2~3 帧闪白，比硬切更有冲击力
                vf += ",fade=t=in:st=0:d=0.10:color=white"

            cmd.extend(["-vf", vf, "-map", "0:v:0"])
            if has_audio:
                if pad_audio:
                    fc = (f"[0:a]atrim=duration={take:.3f},"
                          f"asetpts=PTS-STARTPTS")
                    if abs(speed - 1.0) > 1e-6:
                        fc += f",{self._atempo_chain(speed)}"
                    fc += "[a0];"
                    fc += (f"[1:a]atrim=duration={float(duration):.3f},"
                           f"asetpts=PTS-STARTPTS[a1];")
                    fc += ("[a0][a1]amix=inputs=2:duration=longest:"
                           "normalize=0[aout]")
                    cmd.extend(["-filter_complex", fc, "-map", "[aout]"])
                else:
                    cmd.extend(["-map", "0:a:0"])
                    af = (f"atrim=duration={take:.3f},"
                          f"asetpts=PTS-STARTPTS")
                    if abs(speed - 1.0) > 1e-6:
                        af += f",{self._atempo_chain(speed)}"
                    cmd.extend(["-af", af])
            else:
                cmd.extend(["-map", "1:a:0"])

            cmd.extend(["-t", f"{float(duration):.3f}"])
            if cpu_fallback:
                cmd.extend(["-c:v", "libx264", "-crf", str(quality_crf()),
                            "-preset", "fast", "-pix_fmt", "yuv420p"])
            else:
                # 中间产物用低 CRF（默认 18）保存：成片要经过切片→转场→调色/
                # 防抖/字幕多代编码，中间态一旦用 23 就会越压越糊。
                cmd.extend(encode_args(None, "balanced"))
            cmd.extend(["-c:a", "aac", "-b:a", "128k",
                        "-ar", "44100", "-ac", "2", output_path])
            return cmd

        last_err = None
        # GPU 编码偶发超时/驱动卡住时，自动用 CPU 再试一次，避免留下
        # 只有几十字节的坏 clip 把后续 concat/xfade 整条流程带崩。
        for cpu_fallback in (False, True):
            try:
                run_ffmpeg(_build(cpu_fallback), capture_output=True,
                           check=True, timeout=45)
                return
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                last_err = exc
                Path(output_path).unlink(missing_ok=True)
                if not cpu_fallback:
                    print(f"  [警告] GPU 编码失败/超时，回退 CPU 重试: "
                          f"{Path(input_path).name}")
        raise RuntimeError(f"clip 准备失败: {input_path} ({last_err})")

    def _trim_video(self, input_path: str, output_path: str, duration: float):
        """截取视频到指定时长"""
        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-i", input_path,
            "-t", str(duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            output_path
        ]
        run_ffmpeg(cmd, capture_output=True, check=True)

    def _normalize_bgm(self, input_path: str, output_path: str,
                       target_lufs: float = -14.0, start: float = 0.0,
                       duration: Optional[float] = None, loop: bool = False,
                       fade_out: str = "auto"):
        """
        BGM 音量归一化（两遍 loudnorm）
        目标：-14 LUFS（流媒体标准响度）

        start > 0 时从该时间点开始截取 BGM（用于裁掉前奏，把第一个节拍
        对齐到成片 0s；比"开头补黑场"更符合"无黑场"要求）。
        duration 给定时只归一化这么长；loop=True 时循环 BGM 填满成片。
        """
        start = max(0.0, float(start or 0.0))
        duration = float(duration) if duration and float(duration) > 0 else None

        if loop:
            input_args = ["-stream_loop", "-1", "-i", input_path]
            trim_args = []
            if start > 0.001:
                trim_args.extend(["-ss", f"{start:.3f}"])
            if duration:
                trim_args.extend(["-t", f"{duration:.3f}"])
        else:
            input_args = (["-ss", f"{start:.3f}"] if start > 0.001 else []) \
                + ["-i", input_path]
            trim_args = (["-t", f"{duration:.3f}"] if duration else [])

        # 第一遍：分析
        cmd_analyze = [
            "ffmpeg", "-y", "-nostdin", *input_args, *trim_args,
            "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-",
        ]
        result = subprocess.run(cmd_analyze, capture_output=True, text=True, encoding="utf-8", errors="replace")

        # 解析分析结果
        import json as _json
        stderr = result.stderr or ""
        measured_i = -14.0
        measured_tp = -1.5
        measured_lra = 11.0
        measured_thresh = -24.0
        offset = 0.0

        try:
            # 从 stderr 中提取 JSON 块
            json_start = stderr.rfind('{')
            json_end = stderr.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                stats = _json.loads(stderr[json_start:json_end])
                measured_i = _safe_float(stats.get("input_i"), -14.0)
                measured_tp = _safe_float(stats.get("input_tp"), -1.5)
                measured_lra = _safe_float(stats.get("input_lra"), 11.0)
                measured_thresh = _safe_float(stats.get("input_thresh"), -24.0)
                offset = _safe_float(stats.get("target_offset"), 0.0)
        except (ValueError, KeyError, _json.JSONDecodeError):
            pass

        # 静音/极低电平输入兜底：double-pass 需要有限且有效的测量值，
        # 若判定为静音（≤ -70 LUFS）则降级为单遍 loudnorm。
        if measured_i <= -70.0:
            af = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
        else:
            af = (
                f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
                f":measured_I={measured_i}"
                f":measured_TP={measured_tp}"
                f":measured_LRA={measured_lra}"
                f":measured_thresh={measured_thresh}"
                f":offset={offset}"
                f":linear=true"
            )

        # 第二遍：应用归一化；开头 30ms 淡入防爆音，片尾渐弱避免音乐被硬切。
        # 淡出做在 BGM 这一轨上（不是整条混音），对白/音效/旁白不受影响。
        fade = tail_fade_seconds(duration or 0.0, fade_out)
        tail = ""
        if fade > 0.05 and duration:
            st = max(0.0, float(duration) - fade)
            tail = f",afade=t=out:st={st:.3f}:d={fade:.3f}"
        cmd_normalize = [
            "ffmpeg", "-y", "-nostdin", *input_args, *trim_args,
            "-af", af + ",afade=t=in:st=0:d=0.03" + tail,
            "-ar", "44100",
            output_path,
        ]
        proc = subprocess.run(cmd_normalize, capture_output=True)
        if proc.returncode != 0:
            # 最终兜底：仅重采样到 44.1kHz，不做响度归一化，保证流程不中断
            fallback = ["ffmpeg", "-y", "-nostdin", *input_args, *trim_args,
                        "-ar", "44100",
                        "-af", "volume=1.0,afade=t=in:st=0:d=0.03" + tail,
                        output_path]
            subprocess.run(fallback, capture_output=True, check=True)

    def _adjust_speed(self, input_path: str, output_path: str, speed: float):
        """调整视频速度"""
        pts = 1.0 / speed
        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-i", input_path,
            "-vf", f"setpts={pts}*PTS",
            "-an",
            output_path
        ]
        run_ffmpeg(cmd, capture_output=True, check=True)
