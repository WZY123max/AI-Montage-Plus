"""
语音转字幕模块 - 使用 Whisper 自动生成字幕

支持 openai-whisper 和 faster-whisper 两种后端。
输出 SRT/VTT/JSON 格式字幕文件。

为什么不是一句 ``model.transcribe()`` 就完事
--------------------------------------------
实测「有时候能识别、有时候整段空白」有三个确定原因：

1. **默认参数把唱词当成了非语音**。``no_speech_threshold`` 默认 0.6，
   唱歌带混响/拖腔时人声概率常被判到线下，整段直接丢弃。
2. **上下文污染**。``condition_on_previous_text=True`` 时，前一段如果识别失败
   （输出空或幻觉），后面会被带着一起空掉 —— 表现就是"前半段有字，后半段没有"。
3. **模型规格不对**。base 对内容简单的对白够用，对唱词/口音/背景音乐就吃力。

所以 ``transcribe_robust`` 做了三件事：更宽松的非语音判定 + 关掉上下文依赖 +
结果为空时自动升一档模型重试（只在失败时升级，正常情况不多花时间）。
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# 已加载的 whisper 模型缓存：同一进程里反复渲染任务不必重复加载（base 也要 1~2s）
_MODEL_CACHE: Dict[str, Any] = {}

# 模型升档顺序：识别为空时往后试，最多试 3 档，避免一条 60s 视频跑十几分钟
_MODEL_ORDER = ["tiny", "base", "small", "medium", "large-v3", "large"]

# whisper 常见幻觉片段（纯音乐/无人声时它会"编"出这些）
_HALLUCINATION_PATTERNS = [
    r"字幕由.{0,12}(提供|制作|翻译)",
    r"(请)?(订阅|关注)(我们|频道|本频道)",
    r"Thanks? for watching",
    r"Subtitles? by",
    r"Amara\.org",
    r"www\.[a-z0-9\-]+\.(com|cn|org)",
    r"明镜与点点",
]


def _ffmpeg_bin() -> str:
    """ffmpeg 可能不在系统 PATH（本项目由「开始.bat」注入），做一次容错查找。"""
    import os
    import shutil

    found = shutil.which("ffmpeg")
    if found:
        return found
    env = os.environ.get("FFMPEG_BIN")
    if env and Path(env).exists():
        return env
    try:
        from packages.montage.media import ffmpeg_bin  # type: ignore

        p = ffmpeg_bin()
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    return "ffmpeg"


def extract_audio(video_path: str, audio_path: str, sample_rate: int = 16000) -> str:
    """从视频中提取音频"""
    cmd = [
        _ffmpeg_bin(), "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le",
        "-ar", str(sample_rate), "-ac", "1",
        audio_path,
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return audio_path


def transcribe_whisper(
    audio_path: str,
    model_size: str = "base",
    language: Optional[str] = None,
    prompt: Optional[str] = None,
    music: bool = False,
) -> Dict[str, Any]:
    """使用 openai-whisper 转录音频（宽松参数，适合对白/唱词）。"""
    try:
        import whisper
    except ImportError:
        raise ImportError("请安装 openai-whisper: pip install openai-whisper")

    model = _get_model(model_size)
    kwargs: Dict[str, Any] = {
        "language": language,
        "word_timestamps": True,
        "verbose": False,
        # 关掉上下文依赖：否则前一段识别失败会把后面的段落一起带空
        "condition_on_previous_text": False,
        # 放宽"非语音"判定：唱歌、带混响的人声不容易被判成人声
        "no_speech_threshold": 0.85 if music else 0.7,
        # 关掉置信度过滤：它会整段丢弃低置信结果（宁可留错字也不要空白）
        "logprob_threshold": None,
        # 温度回退：0 失败时依次升温重试，whisper 内部会自动选压缩率合格的那次
        "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        "beam_size": 5,
    }
    if prompt:
        # initial_prompt 让模型"预先知道要听什么词"，对歌词谐音错字改善明显
        kwargs["initial_prompt"] = prompt
    return model.transcribe(audio_path, **kwargs)


def _get_model(model_size: str):
    """按规格取模型实例（同进程缓存，避免每段视频都重新加载）。"""
    key = (model_size or "base").strip()
    if key not in _MODEL_CACHE:
        import whisper

        _MODEL_CACHE[key] = whisper.load_model(key)
    return _MODEL_CACHE[key]


def _usable_text(segments: List[Dict[str, Any]]) -> str:
    """拼接可用文本（去掉纯标点/空白）。"""
    return "".join(
        re.sub(r"[\s\W_]+", "", str(s.get("text") or ""), flags=re.UNICODE)
        for s in segments
    )


def _drop_hallucinations(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """清掉 whisper 在纯音乐/无人声时"编"出来的字幕。

    典型症状：整段 20 秒只吐一句「字幕由 XXX 提供」、同一句重复五六遍。
    这类结果如果原样压进画面，比没有字幕更难看。
    """
    out: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for seg in segments:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        hit = any(re.search(p, text, re.IGNORECASE) for p in _HALLUCINATION_PATTERNS)
        if hit:
            continue
        # 超长单段（>18s）且字数极少 → 几乎都是幻觉式循环
        dur = float(seg.get("end", 0)) - float(seg.get("start", 0))
        if dur > 18 and len(text) < 12:
            continue
        # 同一句反复出现超过 3 次，只留前两次
        key = re.sub(r"\s+", "", text)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 3:
            continue
        out.append(seg)
    return out


def _escalation_chain(model_size: str) -> List[str]:
    """当前模型 + 向上最多两档；识别失败时逐档重试。

    medium 是「唱词可用的最低档」，再往上 large 体积太大、CPU 跑不动，
    所以从 medium 起步时失败就往小模型退，保证至少能出东西。
    """
    size = (model_size or "base").strip()
    if size not in _MODEL_ORDER:
        return [size]
    i = _MODEL_ORDER.index(size)
    if size == "medium":
        return ["medium", "small", "base"]
    return _MODEL_ORDER[i:i + 3]


def transcribe_robust(
    input_path: str,
    model_size: str = "base",
    language: Optional[str] = None,
    prompt: Optional[str] = None,
    music: bool = False,
) -> Dict[str, Any]:
    """带降级重试的语音识别（字幕业务的统一入口）。

    流程：抽 16k 单声道 → 用指定模型识别 → 结果为空则升一档模型重试（最多 3 档）
    → 清幻觉 → 返回段落和诊断信息。

    Args:
        input_path: 视频或音频
        model_size: 起始模型规格
        language: 语言代码，None 自动检测
        prompt: initial_prompt（歌词场景传前几句歌词，显著减少谐音错字）
        music: 唱词场景（放宽非语音判定）

    Returns:
        dict:
            segments   识别段落（含 words）
            text       纯文本
            model_used 实际生效的模型
            attempts   每档模型的尝试记录
            has_speech 是否识别到内容
    """
    path = Path(input_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {input_path}")

    # 视频统一先抽音频：避免 whisper 内部 ffmpeg 调用受 PATH 影响
    tmp_audio = None
    audio_path = str(path)
    if path.suffix.lower() in (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts"):
        fd, tmp_audio = tempfile.mkstemp(suffix=".wav", prefix="sub_asr_")
        import os

        os.close(fd)
        _ffmpeg_extract(str(path), tmp_audio)
        audio_path = tmp_audio

    attempts: List[Dict[str, Any]] = []
    best: Optional[Dict[str, Any]] = None
    best_text = ""
    try:
        for size in _escalation_chain(model_size):
            try:
                result = transcribe_whisper(
                    audio_path, size, language, prompt=prompt, music=music)
            except ImportError:
                raise
            except Exception as e:
                attempts.append({"model": size, "error": f"{type(e).__name__}: {e}"})
                continue

            segs = _drop_hallucinations(result.get("segments") or [])
            text = _usable_text(segs)
            attempts.append({"model": size, "chars": len(text), "segments": len(segs)})
            if len(text) > len(best_text):
                best, best_text = {"segments": segs, "text": text, "model": size}, text
            # 有实质内容就停（≥4 个字，避免被一个"啊"糊弄过去）
            if len(text) >= 4:
                break
    finally:
        if tmp_audio:
            Path(tmp_audio).unlink(missing_ok=True)

    if best is None:
        return {"segments": [], "text": "", "model_used": model_size,
                "attempts": attempts, "has_speech": False}

    return {
        "segments": best["segments"],
        "text": best["text"],
        "model_used": best["model"],
        "attempts": attempts,
        "has_speech": len(best_text) >= 4,
    }


def _ffmpeg_extract(src: str, dst: str) -> None:
    cmd = [_ffmpeg_bin(), "-y", "-v", "error", "-i", src, "-vn",
           "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", dst]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-400:])


def transcribe_faster_whisper(
    audio_path: str,
    model_size: str = "base",
    language: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """使用 faster-whisper 转录音频"""
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(model_size, device="cpu")
        segments, info = model.transcribe(
            audio_path, language=language, word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=False,
        )

        result_segments = []
        for seg in segments:
            words = []
            if seg.words:
                for w in seg.words:
                    words.append({"word": w.word, "start": w.start, "end": w.end})
            result_segments.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "words": words,
            })
        return result_segments
    except ImportError:
        raise ImportError("请安装 faster-whisper: pip install faster-whisper")


def segments_to_srt(segments: List[Dict[str, Any]]) -> str:
    """将转录段落转为 SRT 格式"""
    lines = []
    for i, seg in enumerate(segments, 1):
        start = _format_srt_time(seg["start"])
        end = _format_srt_time(seg["end"])
        text = seg["text"].strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def segments_to_vtt(segments: List[Dict[str, Any]]) -> str:
    """将转录段落转为 WebVTT 格式"""
    lines = ["WEBVTT\n"]
    for seg in segments:
        start = _format_vtt_time(seg["start"])
        end = _format_vtt_time(seg["end"])
        text = seg["text"].strip()
        lines.append(f"{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def segments_to_whisper_json(segments: List[Dict[str, Any]]) -> str:
    """将转录段落转为 Whisper JSON 格式"""
    return json.dumps({"segments": segments}, ensure_ascii=False, indent=2)


def _format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _format_vtt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


class Transcriber:
    """语音转字幕"""

    def __init__(self, backend: str = "whisper", model_size: str = "base"):
        self.backend = backend
        self.model_size = model_size
        # 最近一次识别的诊断信息（用了哪个模型、试了几档、有没有识别到内容）
        self.last_report: Dict[str, Any] = {}

    def transcribe(
        self,
        input_path: str,
        output_path: Optional[str] = None,
        output_format: str = "srt",
        language: Optional[str] = None,
        prompt: Optional[str] = None,
        music: bool = False,
        allow_empty: bool = False,
    ) -> str:
        """转录音频/视频并生成字幕文件

        Args:
            input_path: 输入音频或视频文件
            output_path: 输出字幕文件路径
            output_format: 输出格式 (srt/vtt/json)
            language: 语言代码 (如 zh/en/ja)，None 为自动检测
            prompt: initial_prompt；歌词场景传前几句歌词可显著减少谐音错字
            music: 唱词场景，放宽「非语音」判定
            allow_empty: 识别不到内容时是否照常写出空字幕（默认抛错，让上层给出解释）

        Returns:
            输出字幕文件路径
        """
        input_file = Path(input_path)

        if self.backend == "faster_whisper":
            if input_file.suffix.lower() in (".mp4", ".mkv", ".avi", ".mov", ".webm"):
                audio_path = str(input_file.parent / f"{input_file.stem}_audio.wav")
                extract_audio(input_path, audio_path)
                cleanup_audio = True
            else:
                audio_path = input_path
                cleanup_audio = False
            try:
                raw_segments = transcribe_faster_whisper(audio_path, self.model_size, language)
                self.last_report = {"segments": len(raw_segments), "model_used": self.model_size,
                                    "has_speech": bool(raw_segments)}
            finally:
                if cleanup_audio and Path(audio_path).exists():
                    Path(audio_path).unlink()
        else:
            report = transcribe_robust(
                str(input_path), model_size=self.model_size, language=language,
                prompt=prompt, music=music,
            )
            raw_segments = report["segments"]
            self.last_report = report
            if not raw_segments and not allow_empty:
                tried = "、".join(
                    f"{a.get('model')}({a.get('chars', 0)}字)" for a in report.get("attempts", [])
                ) or "无"
                raise ValueError(
                    "没有字幕段：音频里没识别到人声（试过模型：" + tried + "）。"
                    "如果是歌曲/纯音乐，请改用「歌词字幕」并填入歌词。"
                )

        # 转换格式
        if output_format == "srt":
            content = segments_to_srt(raw_segments)
            suffix = ".srt"
        elif output_format == "vtt":
            content = segments_to_vtt(raw_segments)
            suffix = ".vtt"
        elif output_format == "json":
            content = segments_to_whisper_json(raw_segments)
            suffix = ".json"
        else:
            raise ValueError(f"不支持的格式: {output_format}")

        if output_path is None:
            output_path = str(input_file.parent / f"{input_file.stem}_subtitles{suffix}")

        Path(output_path).write_text(content, encoding="utf-8")
        return output_path
