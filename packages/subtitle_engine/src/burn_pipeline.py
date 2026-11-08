"""字幕一站式处理：转录音频 + 压制字幕。

字幕有两种产出模式（``mode``）：
    - ``mono``     单语：只显示识别出来的原文，用所选样式（tiktok/江湖风/霓虹…）压字；
    - ``bilingual`` 双语：原文在上、译文在下，走 ASS 排版，可继承所选样式的字体/字色；
    - ``auto``     有翻译目标就是双语，没有就是单语（向后兼容旧调用）。

文字从哪来（``subtitle_source``）
    - ``asr``    语音识别（whisper）。适合对白/口播。
    - ``lyrics`` 歌词优先：文本 100% 用歌词，whisper 只负责给时间轴。
                 唱词场景必须走这条——whisper 对唱腔的识别又慢又容易错，
                 而且经常整段空白；歌词是已知的，没必要让它去猜。
    - ``auto``   给了歌词就用歌词，没给就语音识别。

karaoke 等风格需要**词级时间戳**，而 SRT 格式不承载词级信息，
因此这里根据风格自动选择中间格式：
    - 普通风格 -> SRT
    - 词级风格（karaoke） -> Whisper JSON（含 words 字段）

CLI（pipeline.py）与 WebUI（packages/webui）共用本函数，避免逻辑重复。
"""

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .transcriber import Transcriber
from .caption_burner import (
    STYLE_CONFIGS, CaptionStyle, burn_bilingual, burn_captions, load_captions,
)
from .translator import translate_segments


# 需要词级时间戳的字幕风格（与 CaptionBurner._build_karaoke_filters 对应）
WORD_LEVEL_STYLES = {"karaoke"}

# 双语字幕风格
BILINGUAL_STYLE = "bilingual"

# 中文按字符判断用的区间（CJK 统一表意文字 + 扩展 A）
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))

# 最近一次字幕生成的诊断（哪个来源、对齐质量如何），由 WebUI 读出来展示
LAST_REPORT: Dict[str, Any] = {}


def get_last_report() -> Dict[str, Any]:
    return dict(LAST_REPORT)


def _set_report(**kw: Any) -> None:
    LAST_REPORT.clear()
    LAST_REPORT.update(kw)


def _temp_path(suffix: str) -> str:
    """字幕中间文件（srt/json）放系统临时目录，不往成片目录里丢。"""
    fd, p = tempfile.mkstemp(suffix=suffix, prefix="montage_sub_")
    os.close(fd)
    return p


def _looks_chinese(segments, sample: int = 12) -> bool:
    """从识别结果里粗略判断是不是中文（Whisper JSON 不带 language 字段）。"""
    text = " ".join((s.text or "") for s in segments[:sample])
    if not text.strip():
        return False
    cjk = sum(1 for ch in text if any(a <= ord(ch) <= b for a, b in _CJK_RANGES))
    return cjk >= max(3, len([c for c in text if not c.isspace()]) * 0.25)


def _default_target(segments, language: Optional[str]) -> str:
    """没指定翻译目标时的默认值：中文 -> 英文，其它 -> 中文。"""
    if language:
        lang = language.lower()
        return "en" if lang.startswith("zh") else "zh"
    return "en" if _looks_chinese(segments) else "zh"


def _same_lang(a: str, b: str) -> bool:
    a, b = (a or "").lower(), (b or "").lower()
    if not a or not b:
        return False
    return a.split("-")[0] == b.split("-")[0]


def _style_config(style: str, fontsize: Optional[int] = None,
                  fontcolor: Optional[str] = None):
    """按样式名取一份可安全修改的配置（含字号/颜色覆盖）。"""
    try:
        caption_style = CaptionStyle((style or "").lower())
    except ValueError:
        caption_style = CaptionStyle.YOUTUBE
    cfg = copy.deepcopy(STYLE_CONFIGS[caption_style])
    if fontsize and int(fontsize) > 0:
        cfg.fontsize = int(fontsize)
    if fontcolor:
        cfg.fontcolor = str(fontcolor)
    return caption_style, cfg


def _segments_to_json(segments, path: str) -> str:
    """把字幕段写成 whisper JSON（burn 流程统一从这里读，样式逻辑不用分叉）。"""
    data = {
        "segments": [
            {"start": round(float(s.start), 3), "end": round(float(s.end), 3),
             "text": s.text, "words": s.words}
            for s in segments
        ]
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _lyric_segments(video_path: str, lyrics: str, model_size: str,
                    language: Optional[str], offset: Optional[float],
                    repeat: str = "auto", dj: str = "auto",
                    dj_ref: str = "", dj_ratio: Optional[float] = None):
    """歌词 → 字幕段。返回 ``(segments, report)``。

    repeat: 用户只给一遍歌词时，自动把重复演唱的第二遍/第三遍也补出来。
    dj: DJ/remix 版适配；dj_ref 原版音频；dj_ratio 手动变速比。
    """
    from .lyrics import build_lyric_segments

    return build_lyric_segments(
        str(video_path), lyrics, offset=offset, language=language,
        model_size=model_size, repeat=repeat,
        dj=dj, dj_ref=dj_ref, dj_ratio=dj_ratio,
    )


def transcribe_and_burn(
    video_path: str,
    style: str = "youtube",
    output_path: Optional[str] = None,
    model_size: str = "base",
    backend: str = "whisper",
    language: Optional[str] = None,
    translate: Optional[str] = None,
    source_lang: str = "auto",
    translate_backend: str = "auto",
    fontsize: Optional[int] = None,
    fontcolor: Optional[str] = None,
    position: str = "auto",
    mode: str = "auto",
    lyrics: Optional[str] = None,
    lyrics_offset: Optional[float] = None,
    subtitle_source: str = "auto",
    lyric_repeat: str = "auto",
    dj_mode: str = "auto",
    dj_ratio: Optional[float] = None,
    dj_ref: str = "",
    aspect: str = "auto",
    fit: str = "auto",
) -> str:
    """对视频进行语音转录并压制字幕（单语 / 双语 / 歌词）。

    Args:
        video_path: 输入视频路径
        style: 字幕风格，可选 tiktok/youtube/minimal/karaoke/bold/cinematic/bilingual，
            以及中文风味 jianghu（竖排）/ink/handwrite/epic、
            特效 neon/extrude/variety
        output_path: 输出视频路径；默认在原文件旁生成 ``<stem>_subtitled.mp4``
        model_size: Whisper 模型规格（tiny/base/small/medium/large）
        backend: whisper 后端（whisper/faster_whisper）
        language: 转录语言代码（zh/en/ja...），None 为自动检测
        translate: 目标语言代码（如 "en"）；双语模式下的译文语言
        source_lang: 源语言代码（auto 为自动）
        translate_backend: 翻译后端（auto/llm/deep）
        mode: mono（单语）/ bilingual（双语）/ auto（有 translate 就是双语）
        lyrics: 歌词文本（LRC 或纯文本）。给了就走「歌词优先」，文字用歌词、
            识别只提供时间戳；识别不到内容时按人声段分配，不会整段空白。
        lyrics_offset: 歌词时间轴相对成片的偏移秒数；None 自动估计，0 表示不偏移
        lyric_repeat: 重复段处理。auto = 只给一遍歌词时自动检测音频里重复演唱的
            段落并补齐第二遍/第三遍；off = 不补；数字 = 最多补几遍
        dj_mode: 古风 DJ / remix 版适配。auto = 自动提纯人声、估变速比、找原版
            并把原版歌词时间轴映射到 DJ 版；off = 关闭；enhance = 只提纯人声
        dj_ratio: 手动指定 DJ 变速比（如 1.25），留空自动估计
        dj_ref: 原版音频路径，留空自动在 uploads / Music 里找同名原曲
        subtitle_source: auto（有歌词用歌词）/ asr（强制语音识别）/ lyrics（强制歌词）
        aspect: 字幕画幅（auto 跟随成片真实尺寸；也可强制 16:9 / 9:16 / 1:1 等），
            影响字号与位置的自动缩放
        fit: 字幕大小档位 auto/small/normal/large/xlarge

    Returns:
        带字幕的视频路径

    Raises:
        FileNotFoundError: 输入视频不存在
        ImportError: 未安装 whisper 依赖
    """
    video = Path(video_path)
    if not video.exists():
        raise FileNotFoundError(f"视频不存在: {video_path}")

    if output_path is None:
        output_path = str(video.with_name(f"{video.stem}_subtitled.mp4"))

    mode_key = (mode or "auto").strip().lower()
    style_key = (style or "youtube").strip().lower()
    source_key = (subtitle_source or "auto").strip().lower()

    # 双语判定：显式 mode > 选了"双语"样式 > 填了翻译目标
    if mode_key in ("bilingual", "dual", "双", "双语"):
        want_bilingual = True
    elif mode_key in ("mono", "single", "单", "单语"):
        want_bilingual = False
    else:
        want_bilingual = bool(translate) or style_key == BILINGUAL_STYLE

    want_lyrics = bool((lyrics or "").strip()) and source_key != "asr"

    # ---------------- 取字幕段：歌词优先 / 语音识别 ----------------
    json_path = _temp_path(".json")
    try:
        if want_lyrics:
            segments, ly_report = _lyric_segments(
                str(video), lyrics or "", model_size, language, lyrics_offset,
                repeat=lyric_repeat, dj=dj_mode, dj_ref=dj_ref,
                dj_ratio=dj_ratio)
            if segments:
                print(f"  [字幕] 歌词字幕：{ly_report.summary()}")
                # 歌词默认抬到画面 70% 高度：素材常自带底部对话字幕/水印，
                # 贴着底部压字会直接叠在一起。用户显式选了位置则尊重用户。
                if (position or "auto").lower() == "auto":
                    position = "lyric"
                _set_report(source="lyrics", detail=ly_report.summary(),
                            method=ly_report.method, matched=ly_report.matched,
                            total=ly_report.total, similarity=round(ly_report.similarity, 3),
                            repeats=ly_report.repeats,
                            repeat_added=ly_report.repeat_added,
                            dj_method=ly_report.dj_method,
                            dj_ratio=round(ly_report.dj_ratio, 3),
                            dj_detail=ly_report.dj_detail)
            else:
                # 歌词解析不出内容（空文本/全是标记）→ 退回识别，别让用户没字幕
                print("  [字幕] 歌词解析为空，改用语音识别")
                _set_report(source="asr", detail="歌词解析为空，已改用语音识别")
                segments = []
                want_lyrics = False
        else:
            segments = []

        if not segments and not want_lyrics:
            transcriber = Transcriber(backend=backend, model_size=model_size)
            transcriber.transcribe(str(video), output_path=json_path,
                                   output_format="json", language=language,
                                   prompt=None, music=False)
            segments = load_captions(json_path)
            rep = transcriber.last_report or {}
            used = rep.get("model_used") or model_size
            if rep.get("model_used") and rep["model_used"] != model_size:
                print(f"  [字幕] {model_size} 没识别出内容，已自动升到 {used}")
            _set_report(source="asr", detail=f"语音识别（{used}）")
            LAST_REPORT.update({"model_used": used,
                                "attempts": rep.get("attempts", [])})
        elif not segments:
            raise ValueError("没有字幕段：既没有可用的歌词，也没有启用语音识别")

        if not segments:
            raise ValueError(
                "没有字幕段：音频里没识别到人声。如果是歌曲/纯音乐，"
                "请改用「歌词字幕」并填入歌词文本（或放一个同名 .lrc）。"
            )

        _segments_to_json(segments, json_path)
    except Exception:
        Path(json_path).unlink(missing_ok=True)
        raise

    # ---------------- 单语：所选样式直接压字 ----------------
    try:
        if not want_bilingual:
            burn_captions(str(video), json_path, style=style,
                          output_path=output_path, fontsize=fontsize,
                          fontcolor=fontcolor, position=position,
                          aspect=aspect, fit=fit)
            return output_path

        # ---------------- 双语：原文 + 译文双行 ----------------
        target = (translate or "").strip() or _default_target(segments, language)
        if _same_lang(target, language or source_lang) or (
                not language and _same_lang(
                    target, "zh" if _looks_chinese(segments) else "en")):
            print(f"  [字幕] 目标语言 {target} 与源语言相同，按单语输出")
            burn_captions(str(video), json_path, style=style,
                          output_path=output_path, fontsize=fontsize,
                          fontcolor=fontcolor, position=position,
                          aspect=aspect, fit=fit)
            return output_path

        print(f"  翻译字幕 -> {target}（后端: {translate_backend}）...")
        bilingual = translate_segments(
            segments, target=target, source=source_lang,
            backend=translate_backend,
        )
        # 双语用 drawtext 渲染（与单语同一套字体解析），不用 ASS：
        # libass 只认字体族名，行楷/隶书/舒体在 Windows 上常常解析失败、
        # 静默回退成雅黑；drawtext 直接吃字体文件路径，样式效果才对得上。
        burn_bilingual(
            str(video), bilingual, style=style, output_path=output_path,
            fontsize=fontsize, fontcolor=fontcolor, position=position,
            aspect=aspect, fit=fit,
        )
        return output_path
    finally:
        Path(json_path).unlink(missing_ok=True)
