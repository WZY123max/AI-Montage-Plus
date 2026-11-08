"""
字幕压制模块 - 将字幕烧录到视频中

支持多种字幕风格：
    通用：tiktok / youtube / minimal / karaoke / bold / cinematic / bilingual
    中文风味：jianghu（江湖竖排）/ ink（水墨隶书）/ handwrite（手写）/ epic（片名）
    特效：neon（霓虹发光）/ extrude（立体投影）/ variety（综艺字幕条）
支持 SRT/VTT/Whisper JSON 格式
"""

import copy
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional


def _run_ffmpeg_with_vf(cmd: List[str], filter_chain: str) -> None:
    """执行带 ``-vf`` 的 ffmpeg 命令。

    字幕句数多时（例如自动补出重复段后有 20~40 句），drawtext 滤镜串会
    超过 Windows 32767 字符的命令行上限，报 ``WinError 206 文件名或扩展名太长``。
    这时把滤镜图写进临时文件，用 ``-filter_script:v`` 传文件名；只传文件名并
    设置 cwd，避开 Windows 盘符 ``C:`` 在滤镜脚本路径里被误解析的老问题。
    """
    if len(filter_chain) <= 6000:
        subprocess.run(cmd, capture_output=True, check=True)
        return
    fd, script = tempfile.mkstemp(suffix=".ff", prefix="montage_vf_")
    os.close(fd)
    try:
        Path(script).write_text(filter_chain, encoding="utf-8")
        patched = list(cmd)
        idx = patched.index("-vf")
        patched[idx] = "-filter_script:v"
        patched[idx + 1] = Path(script).name
        subprocess.run(patched, capture_output=True, check=True,
                       cwd=str(Path(script).parent))
    finally:
        Path(script).unlink(missing_ok=True)


class CaptionStyle(str, Enum):
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"
    MINIMAL = "minimal"
    KARAOKE = "karaoke"
    BOLD = "bold"
    CINEMATIC = "cinematic"
    BILINGUAL = "bilingual"
    # 中文风味
    JIANGHU = "jianghu"          # 江湖风：竖排 + 暗金 + 行楷
    INK = "ink"                  # 水墨古风：隶书 + 米白
    HANDWRITE = "handwrite"      # 手写便签
    EPIC = "epic"                # 片名/史诗：米金 + 硬投影
    # 特效
    NEON = "neon"                # 霓虹发光
    EXTRUDE = "extrude"          # 立体投影
    VARIETY = "variety"          # 综艺字幕条（黄底黑字）


@dataclass
class CaptionSegment:
    start: float
    end: float
    text: str
    words: Optional[List[Dict[str, Any]]] = None
    # 双语字幕的译文行（原文在 text，译文在 text2）
    text2: Optional[str] = None


@dataclass
class StyleConfig:
    fontsize: int = 48
    fontcolor: str = "white"
    fontfile: Optional[str] = None
    borderw: int = 2
    bordercolor: str = "black"
    shadowcolor: str = "black@0.5"
    shadowx: int = 2
    shadowy: int = 2
    box: bool = False
    boxcolor: str = "black@0.6"
    boxborderw: int = 10
    x_expr: str = "(w-text_w)/2"
    y_expr: str = "h-100"
    line_spacing: int = 10
    # --- 版式与特效扩展 ---
    layout: str = "horizontal"          # horizontal 横排 / vertical 竖排（从右往左单列）
    font_hints: Optional[List[str]] = None  # 字体文件名候选（按优先级，找不到回退中文字体）
    glow: bool = False                  # 外发光：先画一层粗描边的同色字
    glow_color: str = "white@0.35"
    glow_width: int = 12                # 发光层额外加粗的像素
    extrude: int = 0                    # >0：先画一层深色偏移字，做立体投影
    extrude_color: str = "black@0.85"


STYLE_CONFIGS: Dict[CaptionStyle, StyleConfig] = {
    CaptionStyle.TIKTOK: StyleConfig(
        fontsize=64, fontcolor="white",
        borderw=4, bordercolor="black",
        shadowx=0, shadowy=0, box=False,
        x_expr="(w-text_w)/2",
        y_expr="(h-text_h)*0.75",
    ),
    CaptionStyle.YOUTUBE: StyleConfig(
        fontsize=42, fontcolor="white",
        borderw=2, bordercolor="black",
        shadowx=2, shadowy=2,
        box=True, boxcolor="black@0.7", boxborderw=8,
        x_expr="(w-text_w)/2",
        y_expr="h-80",
    ),
    CaptionStyle.MINIMAL: StyleConfig(
        fontsize=36, fontcolor="white",
        borderw=1, bordercolor="black@0.5",
        shadowx=1, shadowy=1, box=False,
        x_expr="(w-text_w)/2",
        y_expr="h-60",
    ),
    CaptionStyle.KARAOKE: StyleConfig(
        fontsize=56, fontcolor="yellow",
        borderw=3, bordercolor="black",
        shadowx=0, shadowy=0, box=False,
        x_expr="(w-text_w)/2",
        y_expr="(h-text_h)/2",
    ),
    CaptionStyle.BOLD: StyleConfig(
        fontsize=72, fontcolor="white",
        borderw=5, bordercolor="black",
        shadowx=3, shadowy=3, box=False,
        x_expr="(w-text_w)/2",
        y_expr="(h-text_h)*0.6",
    ),
    CaptionStyle.CINEMATIC: StyleConfig(
        fontsize=38, fontcolor="white@0.9",
        borderw=0, box=True,
        boxcolor="black@0.4", boxborderw=12,
        shadowx=0, shadowy=0,
        x_expr="(w-text_w)/2",
        y_expr="h*0.85",
    ),
    # 双语：原/译文上下两行，实际压制走 ASS（见 build_bilingual_ass），
    # 这里的 fontsize 仅作为 ASS 版式的基准字号参考。
    CaptionStyle.BILINGUAL: StyleConfig(
        fontsize=48, fontcolor="white",
        borderw=2, bordercolor="black",
        shadowx=0, shadowy=0, box=False,
        x_expr="(w-text_w)/2",
        y_expr="h-100",
    ),
    # ---------------- 中文风味 ----------------
    # 江湖风：右侧竖排、暗金行楷、厚墨描边，武侠/古装配乐常用
    CaptionStyle.JIANGHU: StyleConfig(
        layout="vertical",
        font_hints=["stxingka.ttf", "simkai.ttf"],
        fontsize=58, fontcolor="0xE8C86A",
        borderw=6, bordercolor="black",
        shadowx=3, shadowy=3, shadowcolor="black@0.7",
    ),
    # 水墨古风：隶书、米白、贴底，适合国风/纪录片
    CaptionStyle.INK: StyleConfig(
        font_hints=["simli.ttf", "simkai.ttf"],
        fontsize=48, fontcolor="0xF3EDE1",
        borderw=2, bordercolor="0x120F0C@0.9",
        shadowx=2, shadowy=4, shadowcolor="black@0.55",
        y_expr="h-120",
    ),
    # 手写便签：方正舒体，压在上方
    CaptionStyle.HANDWRITE: StyleConfig(
        font_hints=["fzstk.ttf", "simkai.ttf"],
        fontsize=52, fontcolor="0xFFF6E5",
        borderw=2, bordercolor="0x4A2C17",
        shadowx=3, shadowy=3, shadowcolor="black@0.6",
        y_expr="h*0.12",
    ),
    # 片名/史诗：华文中宋 + 米金 + 硬投影，居中
    CaptionStyle.EPIC: StyleConfig(
        font_hints=["stzhongs.ttf", "simli.ttf"],
        fontsize=62, fontcolor="0xF7E7C4",
        borderw=0, bordercolor="black",
        shadowx=0, shadowy=6, shadowcolor="black@0.85",
        y_expr="(h-text_h)*0.5",
    ),
    # ---------------- 特效 ----------------
    # 霓虹：青色外发光 + 冷白字芯
    CaptionStyle.NEON: StyleConfig(
        fontsize=56, fontcolor="0xEAFBFF",
        borderw=2, bordercolor="0x0B4E5A",
        glow=True, glow_color="0x39F3FF@0.5", glow_width=14,
        shadowx=0, shadowy=0,
        y_expr="(h-text_h)*0.68",
    ),
    # 立体：白字 + 红色偏移投影，复古标语感
    CaptionStyle.EXTRUDE: StyleConfig(
        fontsize=60, fontcolor="white",
        borderw=2, bordercolor="black",
        extrude=7, extrude_color="0xC0392B",
        shadowx=0, shadowy=0,
        y_expr="(h-text_h)*0.72",
    ),
    # 综艺字幕条：亮黄色块 + 黑字
    CaptionStyle.VARIETY: StyleConfig(
        fontsize=52, fontcolor="black",
        borderw=0, bordercolor="black",
        shadowx=0, shadowy=0,
        box=True, boxcolor="0xFFD400@0.92", boxborderw=18,
        y_expr="(h-text_h)*0.78",
    ),
}


# ---------------------------------------------------------------------------
# 字体解析
# ---------------------------------------------------------------------------
# 各平台常见中文字体候选（按优先级）。
# 不指定字体时 ffmpeg 的默认字体不含中文字形，中文会渲染成「豆腐块」，
# 因此这里自动挑一个可用的中文字体文件。
_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",            # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",          # 黑体
    r"C:\Windows\Fonts\simsun.ttc",          # 宋体
    r"C:\Windows\Fonts\Deng.ttf",            # 等线
    "/System/Library/Fonts/PingFang.ttc",    # macOS 苹方
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]

_FFMPEG_MONO_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\consola.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]

_font_cache: Dict[str, Optional[str]] = {}


def find_font(kind: str = "cjk") -> Optional[str]:
    """查找可用的字体文件。

    Args:
        kind: "cjk" 中文字体，"mono" 等宽字体

    Returns:
        字体文件路径；找不到返回 None（此时按 ffmpeg 默认字体处理）
    """
    if kind in _font_cache:
        return _font_cache[kind]

    env_key = "MONTAGE_FONT" if kind == "cjk" else "MONTAGE_FONT_MONO"
    candidates = []
    if os.environ.get(env_key):
        candidates.append(os.environ[env_key])
    candidates += _CJK_FONT_CANDIDATES if kind == "cjk" else _FFMPEG_MONO_FONT_CANDIDATES

    found = None
    for p in candidates:
        if p and Path(p).exists():
            found = p
            break
    if found is None and kind == "cjk":
        print("  [警告] 未找到中文字体，字幕中文可能显示为方块；"
              "可通过环境变量 MONTAGE_FONT 指定字体文件")
    _font_cache[kind] = found
    return found


def escape_font_path(path: str) -> str:
    """转义并引用 ffmpeg 滤镜中的字体路径。

    必须用单引号包裹，否则 ffmpeg 滤镜解析器会把盘符的 ``:`` 当作选项分隔符
    （Windows 下 ``fontfile=C:/...`` 会直接报 filter 解析失败）。
    """
    p = str(path).replace("\\", "/").replace(":", "\\:")
    return f"'{p}'"


# 各平台字体目录（按风格挑字体时用）
_FONT_DIRS = [
    r"C:\Windows\Fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    "/usr/share/fonts",
    "/usr/share/fonts/truetype",
]

_font_file_index: Optional[Dict[str, str]] = None


def _font_index() -> Dict[str, str]:
    """建立 字体文件名(小写) -> 绝对路径 的索引（只扫一层目录，结果缓存）。"""
    global _font_file_index
    if _font_file_index is not None:
        return _font_file_index
    index: Dict[str, str] = {}
    for d in _FONT_DIRS:
        p = Path(d)
        if not p.is_dir():
            continue
        try:
            for f in p.iterdir():
                if f.is_file():
                    index.setdefault(f.name.lower(), str(f))
        except OSError:
            continue
    _font_file_index = index
    return index


def resolve_style_font(hints: Optional[List[str]] = None) -> Optional[str]:
    """按样式期望的字体文件名挑选字体，找不到就回退通用中文字体。

    hints 里可以写文件名（如 ``stxingka.ttf``）或绝对路径。名字匹配不区分大小写，
    因为 Windows 字体目录里 ``STXINGKA.TTF`` 与 ``stxingka.ttf`` 混用。
    """
    if not hints:
        return find_font("cjk")
    index = _font_index()
    for hint in hints:
        raw = str(hint)
        if Path(raw).exists():
            return raw
        key = raw.replace("\\", "/").split("/")[-1].lower()
        if key in index:
            return index[key]
    return find_font("cjk")


def _is_wide_char(ch: str) -> bool:
    """是否为全角字符（CJK / 全角标点），用于估算文本宽度"""
    return ord(ch) > 0x2E80


# ---------------------------------------------------------------------------
# 画幅 / 分辨率自适应
#
# 旧版把所有样式的字号写死成 36~72 像素（按 1080p 横屏调的），换到 4K 横屏
# 时 48px 只占画面高度的 2.2%，看起来就是"字幕太小"。这里统一按成片尺寸
# 缩放：以 1920x1080 横屏为 1.0 基准，横屏跟高度走、竖屏跟宽度走。
# ---------------------------------------------------------------------------

# 横屏字幕额外放大系数：让字幕占画面宽度的比例更接近竖屏短视频的观感。
# 想恢复"纯等比"把它改成 1.0，或在页面把「字幕大小档位」选成"偏小"。
LANDSCAPE_BOOST: float = 1.35

FIT_PRESETS: Dict[str, float] = {
    "auto": 1.0,
    "small": 0.85,
    "normal": 1.0,
    "large": 1.22,
    "xlarge": 1.45,
}

ASPECT_ALIASES: Dict[str, float] = {
    "16:9": 16 / 9, "16/9": 16 / 9, "横屏": 16 / 9,
    "9:16": 9 / 16, "9/16": 9 / 16, "竖屏": 9 / 16,
    "1:1": 1.0, "1/1": 1.0, "方形": 1.0,
    "4:5": 4 / 5, "4/5": 4 / 5,
    "4:3": 4 / 3, "4/3": 4 / 3,
    "3:4": 3 / 4, "3/4": 3 / 4,
    "21:9": 21 / 9, "21/9": 21 / 9,
}


def resolve_aspect(spec: Optional[str], width: int, height: int) -> float:
    """字幕排版使用的画幅 w/h。auto / 空 时用视频真实尺寸。"""
    key = (spec or "auto").strip().lower().replace("：", ":")
    if key in ASPECT_ALIASES:
        return ASPECT_ALIASES[key]
    return (width / height) if width > 0 and height > 0 else 16 / 9


def auto_fit_scale(width: int, height: int, aspect: Optional[str] = "auto",
                   fit: str = "auto") -> float:
    """计算字号缩放系数（1.0 = 1920x1080 横屏下的样式原始字号）。

    - 横屏/方屏：按高度缩放。4K 横屏 3840x2160 → 2.0，字号翻倍，
      这正好修掉用户反馈的"横屏字幕太小"。
    - 竖屏：按宽度缩放（短边决定一行能放多少字），再给一点视觉加成。
    - fit 是用户可选的"字幕大小"档位，作为整体倍率叠加上去。
    """
    if width <= 0 or height <= 0:
        width, height = 1920, 1080
    ratio = resolve_aspect(aspect, width, height)
    if ratio >= 1.0:
        # 横屏：按高度缩放，再给一档视觉加成。
        # 只按高度等比缩放的话，1080p 横屏仍是原来的 42px，在 1920 宽的画面里
        # 相对竖屏短视频显得特别小 —— 这正是用户反馈的"横屏字幕太小"。
        base = (height / 1080.0) * LANDSCAPE_BOOST
    else:
        # 竖屏以「宽度 1080」为基准，否则按高度算会把字撑到一行放不下
        base = (width / 1080.0) * 1.15
    preset = FIT_PRESETS.get((fit or "auto").strip().lower(), 1.0)
    return max(0.6, min(3.5, base * preset))


def _scale_px_expr(expr: str, scale: float) -> str:
    """把坐标表达式里的固定像素偏移按 scale 缩放。

    只动 `h-100` / `w-text_w-40` / 表达式末尾的 `±20` 这类像素量，
    `h*0.62`、`.../2` 里的比例与除法常量保持原样。
    """
    if not expr or abs(scale - 1.0) < 0.01:
        return expr

    def _minus(m):
        return f"{m.group(1)}-{int(round(int(m.group(2)) * scale))}"

    out = re.sub(r"([hw])-\s*(\d{2,})(?![\d.])", _minus, expr)

    def _tail(m):
        return f"{m.group(1)}{int(round(int(m.group(2)) * scale))}"

    # 负向回顾：h-200 / w-40 已在上一步缩放过，别再乘第二次
    out = re.sub(r"(?<![hw])([+\-])\s*(\d{2,})\s*$", _tail, out)
    return out


def apply_fit_config(config: StyleConfig, scale: float) -> StyleConfig:
    """把缩放系数应用到字号、描边、阴影、发光与位置表达式。"""
    if abs(scale - 1.0) < 0.01:
        return config
    config.fontsize = max(14, int(round(config.fontsize * scale)))
    for attr in ("borderw", "boxborderw", "shadowx", "shadowy",
                 "line_spacing", "glow_width", "extrude"):
        val = getattr(config, attr, 0)
        if isinstance(val, int) and val > 0:
            setattr(config, attr, max(1, int(round(val * scale))))
    config.y_expr = _scale_px_expr(config.y_expr, scale)
    config.x_expr = _scale_px_expr(config.x_expr, scale)
    return config


def estimate_text_width(text: str, fontsize: int) -> float:
    """粗略估算文本像素宽度。

    drawtext 无法在构建滤镜时测量真实字宽，这里按经验值估算：
    CJK/全角字符 ≈ 1 em，其余（ASCII 字母、数字、半角空格）≈ 0.5 em。
    只要基础行与高亮词使用同一套估算，二者的相对位置就一致。
    """
    return sum(fontsize * (1.0 if _is_wide_char(c) else 0.5) for c in text)


# 竖排时通常会去掉的行末标点（保留 ！？ 这类有情绪的）
_VERTICAL_TRAILING_PUNCT = "，。、；：,.;:"


def vertical_tokens(text: str) -> List[str]:
    """把一句字幕拆成竖排用的列元素。

    中文逐字拆；连续的拉丁字母/数字作为一个词保留（竖排里横着放），
    空白丢弃。首尾的普通标点会去掉，避免竖排第一格是个逗号。
    """
    s = (text or "").strip().strip(_VERTICAL_TRAILING_PUNCT)
    tokens: List[str] = []
    buf = ""
    for ch in s:
        if _is_wide_char(ch):
            if buf:
                tokens.append(buf)
                buf = ""
            if not ch.isspace():
                tokens.append(ch)
        elif ch.isspace():
            if buf:
                tokens.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        tokens.append(buf)
    return tokens


def parse_srt(srt_path: Path) -> List[CaptionSegment]:
    segments = []
    content = srt_path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(\d+)\s*\n"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n"
        r"(.*?)(?=\n\n|\n*$)",
        re.DOTALL,
    )
    for match in pattern.finditer(content):
        start = int(match.group(2)) * 3600 + int(match.group(3)) * 60 + int(match.group(4)) + int(match.group(5)) / 1000
        end = int(match.group(6)) * 3600 + int(match.group(7)) * 60 + int(match.group(8)) + int(match.group(9)) / 1000
        text = match.group(10).strip().replace("\n", " ")
        segments.append(CaptionSegment(start=start, end=end, text=text))
    return segments


def parse_vtt(vtt_path: Path) -> List[CaptionSegment]:
    segments = []
    content = vtt_path.read_text(encoding="utf-8")
    lines = content.split("\n")
    i = 0
    while i < len(lines) and "-->" not in lines[i]:
        i += 1
    pattern = re.compile(r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})")
    while i < len(lines):
        match = pattern.match(lines[i].strip())
        if match:
            start = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + int(match.group(4)) / 1000
            end = int(match.group(5)) * 3600 + int(match.group(6)) * 60 + int(match.group(7)) + int(match.group(8)) / 1000
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i].strip())
                i += 1
            segments.append(CaptionSegment(start=start, end=end, text=" ".join(text_lines)))
        i += 1
    return segments


def parse_whisper_json(json_path: Path) -> List[CaptionSegment]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    segments = []
    for seg in data.get("segments", []):
        segments.append(CaptionSegment(
            start=seg["start"], end=seg["end"],
            text=seg["text"].strip(),
            words=seg.get("words"),
        ))
    return segments


def load_captions(caption_path: str) -> List[CaptionSegment]:
    path = Path(caption_path)
    suffix = path.suffix.lower()
    if suffix == ".srt":
        return parse_srt(path)
    elif suffix == ".vtt":
        return parse_vtt(path)
    elif suffix == ".json":
        return parse_whisper_json(path)
    else:
        raise ValueError(f"不支持的字幕格式: {suffix}")


def escape_ffmpeg_text(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
    text = text.replace(":", "\\:")
    text = text.replace("%", "\\%")
    return text


# ---------------------------------------------------------------------------
# 双语字幕：ASS（libass）生成与压制
# ---------------------------------------------------------------------------
# 字体文件 -> 字体族名（ASS 用族名而非文件路径）
_FONT_FAMILY_BY_FILE = {
    "msyh.ttc": "Microsoft YaHei",
    "msyhbd.ttc": "Microsoft YaHei",
    "msyhl.ttc": "Microsoft YaHei",
    "simhei.ttf": "SimHei",
    "simsun.ttc": "SimSun",
    "deng.ttf": "DengXian",
    "pingfang.ttc": "PingFang SC",
    "stheitimedium.ttc": "Heiti SC",
    "notosanscjk-regular.ttc": "Noto Sans CJK SC",
    "wqy-zenhei.ttc": "WenQuanYi Zen Hei",
    # 中文风味样式用到的字体（ASS 里写族名，libass 才能找到同一款字）
    "stxingka.ttf": "STXingkai",
    "simkai.ttf": "KaiTi",
    "simli.ttf": "LiSu",
    "fzstk.ttf": "FZShuTi",
    "stzhongs.ttf": "STZhongsong",
    "sthupo.ttf": "STHupo",
}


def font_family_from_file(font_path: Optional[str]) -> str:
    """由字体文件路径推断 ASS 字体族名；未知时回退 Microsoft YaHei。"""
    if not font_path:
        return "Microsoft YaHei"
    name = Path(font_path).name.lower()
    if name in _FONT_FAMILY_BY_FILE:
        return _FONT_FAMILY_BY_FILE[name]
    # 模糊匹配（覆盖如 msyhbd.ttc / NotoSansCJK-Regular.ttc 等变体）
    for key, fam in _FONT_FAMILY_BY_FILE.items():
        if key.split(".")[0] in name:
            return fam
    return "Microsoft YaHei"


def _ass_time(seconds: float) -> str:
    """秒 -> ASS 时间码 H:MM:SS.cc"""
    if seconds < 0:
        seconds = 0
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    """转义 ASS 文本：换行转 \\N，去掉花括号（避免被当作特效块）。"""
    t = (text or "").replace("\r", "")
    t = t.replace("{", "(").replace("}", ")")
    t = re.sub(r"\n+", r"\\N", t)
    return t.strip()


def _ass_color(rgb: str) -> str:
    """'RRGGBB' -> ASS &HAABBGGRR（不透明）。"""
    rgb = rgb.lstrip("#")
    r, g, b = int(rgb[0:2], 16), int(rgb[2:4], 16), int(rgb[4:6], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}"


# drawtext 的命名颜色（与 ffmpeg/libass 常用名一致）
_NAMED_COLORS = {
    "white": "FFFFFF", "black": "000000", "yellow": "FFFF00",
    "cyan": "00FFFF", "aqua": "00FFFF", "orange": "FFA500",
    "pink": "FFC0CB", "red": "FF0000", "green": "00FF00",
    "blue": "0000FF", "gray": "808080", "grey": "808080",
}

_HEX_CHARS = set("0123456789abcdefABCDEF")


def ass_color_from_spec(spec: Optional[str], default: str = "FFFFFF") -> str:
    """把字幕样式里的颜色写法转成 ASS 颜色。

    支持 drawtext 的几种写法：``white`` / ``0xE8C86A`` / ``#E8C86A@0.7``，
    ``@`` 后面是透明度（1=不透明），会折算成 ASS 的 alpha 字节。
    认不出来时回退 default，不让一个颜色写法把整条字幕链路搞崩。
    """
    s = str(spec or "").strip()
    alpha = 1.0
    if "@" in s:
        s, _, a = s.partition("@")
        try:
            alpha = float(a)
        except ValueError:
            alpha = 1.0
    low = s.strip().lower()
    if low.startswith("0x") or low.startswith("#"):
        hexs = low[2:] if low.startswith("0x") else low[1:]
    elif low in _NAMED_COLORS:
        hexs = _NAMED_COLORS[low]
    else:
        # 裸十六进制（如 "FFE066"）也认，否则样式里直接写的字色会被当成白色
        hexs = low if (len(low) == 6 and all(c in _HEX_CHARS for c in low)) else ""
    if not hexs:
        return _ass_color(default)
    if len(hexs) != 6 or any(c not in _HEX_CHARS for c in hexs):
        return _ass_color(default)

    rgb = _ass_color(hexs)
    alpha = max(0.0, min(1.0, alpha))
    if alpha >= 0.999:
        return rgb
    a_byte = int(round((1.0 - alpha) * 255))
    return f"&H{a_byte:02X}{rgb[4:]}"


def build_bilingual_ass(
    segments: List[CaptionSegment],
    out_path: Optional[str] = None,
    video_size: tuple = (1280, 720),
    target_lang: str = "en",
    font: Optional[str] = None,
    style_config: Optional[StyleConfig] = None,
) -> str:
    """把双语字幕段写成 ASS 文件（原文在上、译文在下）。

    Args:
        segments: CaptionSegment 列表；``text`` 为原文，``text2`` 为译文
        out_path: 输出 .ass 路径，默认写到临时文件
        video_size: (宽, 高)，用于排版与字号缩放
        target_lang: 目标语言（仅用于注释）
        font: 字体族名，默认由系统中文字体推断
        style_config: 所选字幕样式；给了就用它的字体/字色/描边，
            让双语也能出"江湖风金字""霓虹冷白"等效果，而不是永远一套白+黄。
            竖排样式（layout=vertical）在 ASS 里排不了竖排，降级为横排双行。

    Returns:
        生成的 .ass 文件路径
    """
    w, h = video_size
    w = w if w and w > 0 else 1280
    h = h if h and h > 0 else 720

    cfg = style_config
    if font is None:
        src_font = (cfg.fontfile if cfg and cfg.fontfile else None)
        font = font_family_from_file(src_font or find_font("cjk"))

    main_hex = "FFFFFF"
    sub_hex = "FFE066"
    outline_w = max(2, int(h * 0.004))
    sub_outline_w = max(2, int(h * 0.003))
    if cfg is not None:
        main_hex = str(cfg.fontcolor or main_hex)
        # 译文用金色；原文本身就是暖金色时，译文改用冷白以便区分
        if ass_color_from_spec(main_hex)[4:] in ("6AC8E8",):  # BGR of E8C86A
            sub_hex = "EAF6FF"
        outline_w = max(2, min(8, int(cfg.borderw or 2)))
        sub_outline_w = max(1, outline_w - 1)

    base_size = int(cfg.fontsize) if (cfg and cfg.fontsize) else int(h * 0.075)
    # 双行排版：原文略大、译文略小，整体不超过画面高度的 1/8
    main_size = max(20, min(int(h * 0.082), base_size))
    sub_size = max(16, int(main_size * 0.74))
    sub_mv = max(20, int(h * 0.062))
    main_mv = sub_mv + int(main_size * 1.55)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {w}\n"
        f"PlayResY: {h}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.601\n"
        f"; bilingual subtitles (target={target_lang})\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Main,{font},{main_size},{ass_color_from_spec(main_hex)},{ass_color_from_spec(main_hex)},"
        f"{_ass_color('000000')},&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},1,2,40,40,{main_mv},1\n"
        f"Style: Sub,{font},{sub_size},{ass_color_from_spec(sub_hex)},{ass_color_from_spec(sub_hex)},"
        f"{_ass_color('000000')},&H80000000,0,0,0,0,100,100,0,0,1,{sub_outline_w},1,2,40,40,{sub_mv},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events: List[str] = []
    for seg in segments:
        src = _escape_ass_text(seg.text)
        tr = _escape_ass_text(seg.text2 or "")
        # 翻译直通（译文 == 原文）时不要傻乎乎地重复一行
        if tr and src and tr.strip().lower() == src.strip().lower():
            tr = ""
        start, end = _ass_time(seg.start), _ass_time(seg.end)
        if src:
            events.append(f"Dialogue: 0,{start},{end},Main,,0,0,0,,{src}")
        if tr:
            events.append(f"Dialogue: 0,{start},{end},Sub,,0,0,0,,{tr}")

    content = header + "\n".join(events) + "\n"

    if out_path is None:
        import tempfile
        fd, out_path = tempfile.mkstemp(suffix=".ass", prefix="bilingual_")
        os.close(fd)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(content, encoding="utf-8-sig")
    return out_path


def burn_ass(video_path: str, ass_path: str, output_path: Optional[str] = None) -> str:
    """用 libass 把 ASS 字幕烧录进视频。"""
    video = Path(video_path)
    if not video.exists():
        raise FileNotFoundError(f"视频不存在: {video_path}")
    if not Path(ass_path).exists():
        raise FileNotFoundError(f"字幕不存在: {ass_path}")
    if output_path is None:
        output_path = str(video.parent / f"{video.stem}_subtitled.mp4")

    # ass 滤镜路径需转义 Windows 盘符冒号
    from packages.montage.media import encode_args

    ass_arg = escape_ffmpeg_path(str(ass_path))
    cmd = [
        "ffmpeg", "-y", "-i", str(video),
        "-vf", f"ass={ass_arg}",
        *encode_args(None, "quality"),
        "-c:a", "copy", output_path,
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return output_path


def escape_ffmpeg_path(path: str) -> str:
    """转义 ffmpeg 滤镜中引用的文件路径（单引号包裹 + 转义冒号/反斜杠）。"""
    p = str(path).replace("\\", "/")
    p = p.replace(":", "\\:")
    p = p.replace("'", "\\'")
    return f"'{p}'"


class CaptionBurner:
    def __init__(self, style: CaptionStyle = CaptionStyle.YOUTUBE,
                 custom_config: Optional[StyleConfig] = None,
                 aspect: str = "auto", fit: str = "auto"):
        self.style = style
        # 画幅 / 大小档位：由压制入口传入，_fit_config 里统一应用
        self.aspect = aspect or "auto"
        self.fit = fit or "auto"
        # 复制一份，避免修改模块级 STYLE_CONFIGS 单例
        self.config = copy.deepcopy(custom_config or STYLE_CONFIGS[style])
        # 未指定字体时按样式期望的字体挑一个，否则中文会变成方块
        if not self.config.fontfile:
            self.config.fontfile = resolve_style_font(self.config.font_hints)

    def _font_part(self, config: StyleConfig) -> List[str]:
        """drawtext 的 fontfile 参数（含路径转义）"""
        if not config.fontfile:
            return []
        return [f"fontfile={escape_font_path(config.fontfile)}"]

    @staticmethod
    def _shift(expr: str, dx: int = 0, dy: int = 0) -> str:
        """给坐标表达式加偏移（用于发光/立体层的错位绘制）。"""
        out = expr
        if dx:
            out = f"({out})+{dx}" if any(c in out for c in "+-*/()") else f"{out}+{dx}"
        if dy:
            out = f"({out})+{dy}" if any(c in out for c in "+-*/()") else f"{out}+{dy}"
        return out

    def _build_drawtext_filters(
        self, segment: CaptionSegment, config: StyleConfig,
        text_override: Optional[str] = None,
        x_override: Optional[str] = None,
        y_override: Optional[str] = None,
        fontsize_override: Optional[int] = None,
        color_override: Optional[str] = None,
        borderw_override: Optional[int] = None,
    ) -> List[str]:
        """生成一个字幕段的绘制滤镜。

        普通样式返回 1 条；开启发光/立体时返回 2~3 条（后画的盖在上面）。
        """
        raw_text = segment.text if text_override is None else text_override
        text = escape_ffmpeg_text(raw_text)
        fs = int(fontsize_override or config.fontsize)
        x = x_override or config.x_expr
        y = y_override or config.y_expr
        enable = f"enable='between(t,{segment.start:.3f},{segment.end:.3f})'"
        font_part = self._font_part(config)
        main_color = color_override or config.fontcolor
        bw = config.borderw if borderw_override is None else int(borderw_override)

        def layer(color: str, borderw: int, bordercolor: str,
                  dx: int = 0, dy: int = 0,
                  shadow: bool = False, boxed: bool = False) -> str:
            parts = [
                f"drawtext=text='{text}'",
                f"fontsize={fs}",
                f"fontcolor={color}",
                f"borderw={borderw}",
                f"bordercolor={bordercolor}",
                f"x={self._shift(x, dx)}",
                f"y={self._shift(y, dy)}",
                *font_part,
                enable,
            ]
            if shadow and (config.shadowx or config.shadowy):
                parts.append(f"shadowcolor={config.shadowcolor}")
                parts.append(f"shadowx={config.shadowx}")
                parts.append(f"shadowy={config.shadowy}")
            if boxed and config.box:
                parts.append("box=1")
                parts.append(f"boxcolor={config.boxcolor}")
                parts.append(f"boxborderw={config.boxborderw}")
            return ":".join(parts)

        filters: List[str] = []
        # 1) 立体投影层（最底）
        if config.extrude and config.extrude > 0:
            e = int(config.extrude)
            filters.append(layer(config.extrude_color, bw, config.bordercolor, e, e))
        # 2) 发光层：粗描边 + 半透明同色
        if config.glow and color_override is None:
            filters.append(layer(
                config.glow_color,
                max(bw, 0) + int(config.glow_width),
                config.glow_color,
            ))
        # 3) 字芯（带描边/阴影/色块）
        filters.append(layer(
            main_color, bw, config.bordercolor,
            shadow=True, boxed=True,
        ))
        return filters

    def _bilingual_sub_color(self, config: StyleConfig) -> str:
        """译文颜色：默认暖金；原文本身就是暖金色时改用冷白，保证两行能区分。"""
        try:
            main = ass_color_from_spec(config.fontcolor)
        except Exception:
            main = ""
        if main == "&H006AC8E8":      # 0xE8C86A（江湖风暗金）的 BGR
            return "0xEAF6FF"
        return "0xFFE066"

    def _build_bilingual_filters(
        self, segment: CaptionSegment, config: StyleConfig,
        video_size: tuple = (1280, 720),
    ) -> List[str]:
        """双语：原文在上、译文在下（同一套 drawtext，字体/描边/发光都跟样式走）。

        不用 ASS 的原因：libass 在 Windows 上按**字体族名**找字，
        行楷/隶书/舒体这类中文字体经常解析失败而静默回退成雅黑，
        用户选了"江湖风"却看到普通黑体。drawtext 直接吃字体文件路径，稳定得多。
        """
        w, h = video_size
        w = w if w and w > 0 else 1280
        h = h if h and h > 0 else 720
        filters: List[str] = []
        tr = (segment.text2 or "").strip()
        # 翻译直通（译文=原文）时不要叠一行一样的字
        if tr and tr.lower() == (segment.text or "").strip().lower():
            tr = ""

        if config.layout == "vertical":
            # 竖排原文照旧在右侧成列；译文放底部居中横排小字
            filters.extend(self._build_vertical_filters(segment, config, video_size))
            if tr:
                sub_fs = max(16, int(config.fontsize * 0.60))
                filters.extend(self._build_drawtext_filters(
                    segment, config,
                    text_override=tr,
                    x_override="(w-text_w)/2",
                    y_override=f"h-{max(36, int(h * 0.07))}",
                    fontsize_override=sub_fs,
                    color_override=self._bilingual_sub_color(config),
                ))
            return filters

        fs = int(config.fontsize)
        sub_fs = max(16, int(fs * 0.72))
        gap = max(4, int(fs * 0.14))
        total = fs + gap + sub_fs
        # 把"两行"当成一个整体，以样式原本的单行位置为中心：
        # 贴底样式（youtube/cinematic）如果只把译文往下加，会被画面底边切掉。
        top = f"({config.y_expr})-{int(total * 0.5)}"
        main_y = top
        sub_y = f"{top}+{fs + gap}"
        filters.extend(self._build_drawtext_filters(
            segment, config, y_override=main_y))
        if tr:
            filters.extend(self._build_drawtext_filters(
                segment, config,
                text_override=tr,
                y_override=sub_y,
                fontsize_override=sub_fs,
                color_override=self._bilingual_sub_color(config),
            ))
        return filters


    def _build_vertical_filters(
        self, segment: CaptionSegment, config: StyleConfig,
        video_size: tuple = (1280, 720),
    ) -> List[str]:
        """竖排：逐字/逐词向下排一列，整体在右侧、垂直居中。"""
        w, h = video_size
        w = w if w and w > 0 else 1280
        h = h if h and h > 0 else 720
        tokens = vertical_tokens(segment.text)
        if not tokens:
            return []

        fs = int(config.fontsize)
        line_h = fs * 1.12
        limit = h * 0.78
        if line_h * len(tokens) > limit:
            fs = max(16, int(fs * limit / (line_h * len(tokens))))
            line_h = fs * 1.12
        total = line_h * len(tokens)
        top = max(int(h * 0.06), int((h - total) / 2))
        margin = max(12, int(w * 0.07))

        filters: List[str] = []
        for i, token in enumerate(tokens):
            filters.extend(self._build_drawtext_filters(
                segment, config,
                text_override=token,
                x_override=f"w-{margin}-text_w",
                y_override=str(int(top + i * line_h)),
                fontsize_override=fs,
            ))
        return filters

    def _build_karaoke_filters(self, segment: CaptionSegment, config: StyleConfig) -> List[str]:
        if not segment.words:
            return self._build_drawtext_filters(segment, config)

        words = [w for w in segment.words if str(w.get("word", "")).strip()]
        if not words:
            return self._build_drawtext_filters(segment, config)

        fs = config.fontsize

        # 逐词绘制：灰底词与黄色高亮词共用同一套坐标，彻底避免错位
        texts = [str(w.get("word", "")).strip() for w in words]
        widths = [estimate_text_width(t, fs) for t in texts]

        # 词间距：所有词都是单个全角字（中文逐字）时不插额外间距，
        # 否则中文会被撑开；拉丁词之间保留一个空格宽度。
        all_single_wide = all(len(t) == 1 and _is_wide_char(t) for t in texts)
        space_w = 0.0 if all_single_wide else fs * 0.35

        line_w = sum(widths) + space_w * max(0, len(words) - 1)
        center = f"(w-{line_w:.0f})/2"

        filters: List[str] = []
        cum = 0.0
        for i, word_data in enumerate(words):
            x = f"{center}+{cum:.0f}"
            common = [
                f"fontsize={fs}",
                f"borderw={config.borderw}",
                f"bordercolor={config.bordercolor}",
                f"x={x}",
                f"y={config.y_expr}",
            ]
            common.extend(self._font_part(config))

            # 基础词（整句时段内显示为灰色）
            base_parts = [
                f"drawtext=text='{escape_ffmpeg_text(texts[i])}'",
                "fontcolor='gray'",
                *common,
                f"enable='between(t,{segment.start:.3f},{segment.end:.3f})'",
            ]
            filters.append(":".join(base_parts))

            # 高亮词（该词自己的时间段内覆盖为高亮色）
            word_start = word_data.get("start", segment.start)
            word_end = word_data.get("end", segment.end)
            hi_parts = [
                f"drawtext=text='{escape_ffmpeg_text(texts[i])}'",
                f"fontcolor={config.fontcolor}",
                *common,
                f"enable='between(t,{word_start:.3f},{word_end:.3f})'",
            ]
            filters.append(":".join(hi_parts))

            cum += widths[i] + space_w
        return filters

    def _build_filter_chain(
        self, segments: List[CaptionSegment], config: Optional[StyleConfig] = None,
        video_size: tuple = (1280, 720), bilingual: bool = False,
    ) -> str:
        config = config or self.config
        filters: List[str] = []
        if bilingual:
            # 双语：原文 + 译文两行，竖排/发光/立体等样式同样生效
            for seg in segments:
                filters.extend(self._build_bilingual_filters(seg, config, video_size))
        elif config.layout == "vertical":
            for seg in segments:
                filters.extend(self._build_vertical_filters(seg, config, video_size))
        elif self.style == CaptionStyle.KARAOKE:
            for seg in segments:
                filters.extend(self._build_karaoke_filters(seg, config))
        else:
            for seg in segments:
                filters.extend(self._build_drawtext_filters(seg, config))
        return ",".join(filters)


    @staticmethod
    def _probe_width(video_path: str) -> int:
        """获取视频宽度（失败返回 0）"""
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width", "-of", "csv=p=0", video_path],
            capture_output=True, text=True,
        )
        try:
            return int(r.stdout.strip().split(",")[0])
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _probe_size(video_path: str) -> tuple:
        """获取视频 (宽, 高)，失败回退 (1280, 720)。"""
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path],
            capture_output=True, text=True,
        )
        try:
            w, h = r.stdout.strip().split(",")[:2]
            return int(w), int(h)
        except (ValueError, IndexError):
            return 1280, 720

    def _fit_config(self, segments: List[CaptionSegment], video_width: int,
                    video_height: int = 0) -> StyleConfig:
        """先按画幅/分辨率放大字号，再检查溢出并收窄，得到最终版式。

        横排看宽度；竖排看高度（一列能放多少个字）。
        """
        config = copy.deepcopy(self.config)
        scale = auto_fit_scale(video_width, video_height, self.aspect, self.fit)
        if abs(scale - 1.0) >= 0.01:
            old = config.fontsize
            config = apply_fit_config(config, scale)
            print(f"  字幕画幅自适应: {video_width}x{video_height} "
                  f"画幅={self.aspect} 档位={self.fit} 缩放={scale:.2f} "
                  f"字号 {old} -> {config.fontsize}")
        if config.layout == "vertical":
            if video_height <= 0:
                return config
            max_tokens = max((len(vertical_tokens(s.text)) for s in segments), default=0)
            if max_tokens:
                limit = video_height * 0.78
                need = max_tokens * config.fontsize * 1.12
                if need > limit:
                    old = config.fontsize
                    config.fontsize = max(16, int(old * limit / need))
                    print(f"  竖排字幕过长，字号 {old} -> {config.fontsize} 以避免溢出")
            return config
        if video_width <= 0:
            return config
        max_text = max((s.text for s in segments), key=len, default="")
        est = estimate_text_width(max_text, config.fontsize)
        limit = video_width * 0.9
        if est > limit and est > 0:
            scale = limit / est
            old = config.fontsize
            config.fontsize = max(16, int(old * scale))
            print(f"  字幕过长，字号 {old} -> {config.fontsize} 以避免溢出")
        return config

    def burn(
        self, video_path: str, caption_path: str,
        output_path: Optional[str] = None,
        codec: str = "libx264", crf: Optional[int] = None, preset: str = "medium",
    ) -> str:
        video = Path(video_path)
        if not video.exists():
            raise FileNotFoundError(f"视频不存在: {video_path}")
        if not Path(caption_path).exists():
            raise FileNotFoundError(f"字幕不存在: {caption_path}")
        if output_path is None:
            output_path = str(video.parent / f"{video.stem}_captioned.mp4")

        segments = load_captions(caption_path)
        if not segments:
            raise ValueError("没有字幕段")

        size = self._probe_size(video_path)
        config = self._fit_config(segments, size[0], size[1])
        filter_chain = self._build_filter_chain(segments, config, size)
        if crf is None:
            from packages.montage.media import quality_crf
            crf = quality_crf()
        out_abs = str(Path(output_path).resolve())
        cmd = [
            "ffmpeg", "-y", "-i", str(Path(video_path).resolve()),
            "-vf", filter_chain,
            "-c:v", codec, "-crf", str(int(crf)), "-preset", preset,
            "-c:a", "copy", out_abs,
        ]
        _run_ffmpeg_with_vf(cmd, filter_chain)
        return output_path

    def burn_segments(
        self, segments: List[CaptionSegment], video_path: str,
        output_path: Optional[str] = None,
        bilingual: Optional[bool] = None,
    ) -> str:
        """直接压制已加载的字幕段。

        双语（段落带 text2 / 样式是 BILINGUAL）走 drawtext 双行排版；
        其余风格走单行 drawtext。便于调用方先转录/翻译再压制，避免中间文件往返。

        Args:
            bilingual: 强制指定是否双语；None 时按"有没有译文"自动判断
        """
        video = Path(video_path)
        if not video.exists():
            raise FileNotFoundError(f"视频不存在: {video_path}")
        if not segments:
            raise ValueError("没有字幕段")
        if output_path is None:
            output_path = str(video.parent / f"{video.stem}_captioned.mp4")

        if bilingual is None:
            bilingual = self.style == CaptionStyle.BILINGUAL or any(
                (s.text2 or "").strip() for s in segments)

        size = self._probe_size(video_path)
        config = self._fit_config(segments, size[0], size[1])
        filter_chain = self._build_filter_chain(
            segments, config, size, bilingual=bool(bilingual))
        from packages.montage.media import encode_args
        cmd = [
            "ffmpeg", "-y", "-i", str(Path(video_path).resolve()),
            "-vf", filter_chain,
            *encode_args(None, "quality"),
            "-c:a", "copy", str(Path(output_path).resolve()),
        ]
        _run_ffmpeg_with_vf(cmd, filter_chain)
        return output_path


def burn_bilingual(
    video_path: str, segments: List[CaptionSegment],
    style: str = "bilingual", output_path: Optional[str] = None,
    fontsize: Optional[int] = None, fontcolor: Optional[str] = None,
    position: str = "auto", aspect: str = "auto", fit: str = "auto",
) -> str:
    """压制双语字幕：原文在上、译文在下。

    样式决定字体/描边/发光（江湖风是竖排原文 + 底部译文），
    position 只对横排样式生效（双语整体上下移动）。
    """
    try:
        caption_style = CaptionStyle((style or "bilingual").lower())
    except ValueError:
        caption_style = CaptionStyle.BILINGUAL
    config = copy.deepcopy(STYLE_CONFIGS[caption_style])
    if fontsize and int(fontsize) > 0:
        config.fontsize = int(fontsize)
    if fontcolor:
        config.fontcolor = str(fontcolor)

    pos = (position or "auto").lower()
    if config.layout == "vertical":
        pos = "auto"
    elif pos in ("top", "上", "顶部"):
        config.y_expr = "h*0.12"
    elif pos in ("middle", "center", "中", "居中"):
        config.y_expr = "(h-text_h)/2 - 20"
    elif pos in ("bottom", "下", "底部"):
        config.y_expr = "h-190"
    elif pos in ("lyric", "歌词", "safe"):
        # 歌词专用：抬到底部硬字幕带上面（素材自带字幕/水印时用）
        config.y_expr = "h*0.62"

    burner = CaptionBurner(style=caption_style, custom_config=config,
                           aspect=aspect, fit=fit)
    return burner.burn_segments(segments, video_path, output_path, bilingual=True)



def burn_captions(
    video_path: str, caption_path: str,
    style: str = "youtube", output_path: Optional[str] = None,
    fontsize: Optional[int] = None, fontcolor: Optional[str] = None,
    position: str = "auto", aspect: str = "auto", fit: str = "auto",
) -> str:
    """压制字幕。

    fontsize/fontcolor/position 用于在预设样式上做微调（WebUI 的字幕高级选项）：
        position: top / middle / bottom / auto（auto 用样式自带位置）
    竖排样式（如 jianghu）忽略 position，始终在右侧垂直居中；
    字号过宽/竖排过高时会自动缩小，避免溢出画面。
    """
    try:
        caption_style = CaptionStyle(style.lower())
    except ValueError:
        caption_style = CaptionStyle.YOUTUBE

    config = copy.deepcopy(STYLE_CONFIGS[caption_style])
    if fontsize and int(fontsize) > 0:
        config.fontsize = int(fontsize)
    if fontcolor:
        config.fontcolor = str(fontcolor)
    pos = (position or "auto").lower()
    if config.layout == "vertical":
        pos = "auto"
    if pos in ("top", "上", "顶部"):
        config.y_expr = "h*0.12"
    elif pos in ("middle", "center", "中", "居中"):
        config.y_expr = "(h-text_h)/2"
    elif pos in ("bottom", "下", "底部"):
        config.y_expr = "h-100" if caption_style != CaptionStyle.CINEMATIC else "h*0.85"
    elif pos in ("lyric", "歌词", "safe"):
        # 歌词专用：抬到底部硬字幕带上面（素材自带字幕/水印时用）
        config.y_expr = "h*0.62"

    burner = CaptionBurner(style=caption_style, custom_config=config,
                           aspect=aspect, fit=fit)
    return burner.burn(video_path, caption_path, output_path)
