"""歌词优先的字幕：解析歌词 → 定位时间轴 → 生成字幕段。

为什么单独做一套
----------------
Whisper 擅长「说话」，不擅长「唱」。旋律、拖腔、混响、伴奏一叠，同一个模型
可能这一段认出七八成、下一段整句空白；认出来的字还常常是谐音错字。
歌词场景根本不需要猜内容——**词是已知的**，唯一缺的是时间。

所以这里反过来做：文本用歌词（100% 正确），whisper 只负责给时间。

三级定位（可靠性从高到低）
--------------------------
1. ``lrc``     歌词自带时间戳（LRC 文件 / mp3 内嵌带时间歌词）→ 直接可用；
               若成片裁过开头，用能量包络互相关自动求偏移（见 ``estimate_shift``）。
2. ``align``   纯文本歌词 + whisper 词级时间戳 → 字符流序列匹配，
               时间取自识别结果，文字全部替换回歌词原文。识别错字不影响。
3. ``spread``  识别为空/匹配率太低 → 按字数为权重，把人声段总时长分配给各行，
               保证每句都落在真正有声音的位置，而不是均分到整条时间轴。

对外接口
--------
    parse_lyrics(text)                      -> [LyricLine]
    extract_lyrics_from_media(path)         -> (text, 来源说明)
    build_lyric_segments(media, text, ...)  -> (segments, report)
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .caption_burner import CaptionSegment

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class LyricLine:
    """一行歌词。start/end 为 None 表示「无自带时间戳」。"""

    text: str
    start: Optional[float] = None
    end: Optional[float] = None


@dataclass
class AlignReport:
    """对齐结果诊断，最终会出现在任务提示里，方便判断为什么不准。"""

    method: str = "none"          # lrc / align / spread / uniform
    matched: int = 0              # align 模式下成功定位的行数
    total: int = 0
    similarity: float = 0.0       # 歌词与识别文本的整体相似度
    shift: float = 0.0            # lrc 模式下自动求出的时间偏移
    notes: List[str] = field(default_factory=list)
    repeats: List[dict] = field(default_factory=list)  # 自动检出的重复演唱段
    repeat_added: int = 0         # 因重复段自动补出的歌词句数
    unsung: int = 0               # 成片音轨里没唱、已跳过不显示的歌词行数
    vocal_end: float = 0.0        # 检测到的人声结束时刻
    dj_method: str = ""           # off / direct / restored / ref_warp
    dj_ratio: float = 1.0         # DJ 版相对原版的播放速率
    dj_detail: str = ""           # DJ 适配说明

    def summary(self) -> str:
        pct = f"{self.matched}/{self.total}" if self.total else "0"
        if self.method == "lrc":
            s = f"歌词自带时间轴，直接使用"
            if abs(self.shift) >= 0.15:
                s += f"（自动校正偏移 {self.shift:+.2f}s）"
        elif self.method == "align":
            s = f"歌词文本 + 识别时间轴对齐，定位 {pct} 行"
            if self.similarity:
                s += f"（识别相似度 {self.similarity:.0%}）"
        elif self.method == "spread":
            s = f"识别不可用，按人声段 + 字数分配时间（{self.total} 行）"
        else:
            s = "按时长均分（兜底）"
        if self.repeat_added:
            s += f"；自动补全 {len(self.repeats)} 遍重复段（+{self.repeat_added} 句）"
        if self.unsung:
            s += f"；跳过 {self.unsung} 句成片里未演唱的歌词"
        if self.dj_detail:
            s += f"；DJ 适配：{self.dj_detail}"
        if self.notes:
            s += "；" + "；".join(self.notes)
        return s


# ---------------------------------------------------------------------------
# LRC / 纯文本解析
# ---------------------------------------------------------------------------

# [00:12.34] / [00:12.3] / [00:12] / [1:02.50] / [00:12:34]（少数播放器用冒号分隔毫秒）
_TS = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# 元数据标签：[ti:歌名] [ar:歌手] [offset:+200]
_META = re.compile(r"^\[(ti|ar|al|by|offset|length|re|ve):")
# 结构标记：[Chorus] （副歌）/（前奏）这类不是歌词正文
_SECTION = re.compile(r"^[\[\(【（](?:chorus|verse|bridge|intro|outro|hook|pre-?chorus|"
                      r"副歌|主歌|前奏|间奏|尾奏|间奏|桥段|合唱)[^\]\)】）]*[\]\)】）]\s*$",
                      re.IGNORECASE)


def _norm_key(text: str) -> str:
    """归一化成可比较的字符流：只保留中日韩文字、字母、数字，统一小写。

    标点、空格、全角符号全部丢掉——whisper 输出的标点和歌词几乎不可能一致，
    留着只会拉低匹配率。
    """
    out = []
    for ch in text or "":
        if ch.isalnum():
            out.append(ch.lower())
        elif "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af":
            out.append(ch)
    return "".join(out)


def _parse_ts(minute: str, second: str, frac: Optional[str]) -> float:
    t = int(minute) * 60 + int(second)
    if frac:
        # 2 位是厘秒、3 位是毫秒，1 位是 0.1 秒
        t += int(frac) / (10 ** len(frac))
    return t


def parse_lyrics(text: str) -> List[LyricLine]:
    """解析歌词文本，兼容 LRC 与纯文本两种写法。

    - 带 ``[mm:ss.xx]`` 的行提取时间戳；一行多个时间戳会展开成多行（LRC 复制句的常见写法）。
    - ``[ti:]``/``[ar:]``/``[offset:]`` 等元数据忽略；``offset`` 会整体应用。
    - ``[Chorus]``/（副歌）这类结构标记跳过。
    - 空行、纯标点行跳过。
    """
    raw_lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    offset_ms = 0.0
    timed: List[LyricLine] = []
    plain: List[str] = []

    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue
        meta = _META.match(line)
        if meta:
            if meta.group(1) == "offset":
                m = re.search(r"([+-]?\d+)", line)
                if m:
                    offset_ms = float(m.group(1))
            continue
        if _SECTION.match(line):
            continue

        stamps = _TS.findall(line)
        body = _TS.sub("", line).strip()
        # 去掉行首残留的标记（如 [00:12.3][00:15.0]）
        body = re.sub(r"^[\s\-–—]*", "", body)
        if not body or not _norm_key(body):
            continue
        if stamps:
            for mn, sec, frac in stamps:
                timed.append(LyricLine(body, _parse_ts(mn, sec, frac)))
        else:
            plain.append(body)

    if timed:
        shift = offset_ms / 1000.0
        for ln in timed:
            ln.start = max(0.0, ln.start + shift)
        timed.sort(key=lambda x: x.start)
        # LRC 只有起点，终点留给后续统一收口（下一行起点 - 间隙）
        return timed
    return [LyricLine(t) for t in plain]


def extract_lyrics_from_media(path: str) -> Tuple[Optional[str], str]:
    """从媒体文件「拉取」歌词：内嵌标签优先，其次同名 .lrc/.txt 旁挂文件。

    返回 ``(歌词文本, 来源说明)``；都没有则 ``(None, "")``。

    mp3/flac 的 USLT 歌词块 ffmpeg 会映射成 ``lyrics`` / ``unsyncedlyrics`` 标签，
    这里不引入 mutagen，直接用 ffprobe 读 tags，少一个依赖。
    """
    p = Path(path)
    if not p.exists():
        return None, ""

    # 1) 旁挂文件：同名 .lrc / .txt（很多人下载 BGM 时歌词就在旁边）
    for suffix in (".lrc", ".LRC", ".txt"):
        cand = p.with_suffix(suffix)
        if cand.exists() and cand.stat().st_size > 0:
            try:
                text = cand.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                try:
                    text = cand.read_text(encoding="gbk")
                except UnicodeDecodeError:
                    continue
            if _norm_key(text.strip()):
                return text, f"{cand.name}"

    # 2) 内嵌标签
    try:
        proc = subprocess.run(
            [_bin("ffprobe"), "-v", "error", "-show_entries", "format_tags",
             "-of", "json", str(p)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        tags = (json.loads(proc.stdout or "{}").get("format") or {}).get("tags") or {}
    except Exception:
        tags = {}

    lowered = {str(k).lower(): str(v) for k, v in tags.items()}
    for key in ("lyrics", "unsyncedlyrics", "syncedlyrics", "uslt", "歌词"):
        val = (lowered.get(key) or "").strip()
        if val and _norm_key(val):
            return val, f"音频内嵌歌词标签（{key}）"
    return None, ""


# ---------------------------------------------------------------------------
# 音频分析：能量包络、人声段
# ---------------------------------------------------------------------------


def _bin(name: str) -> str:
    """找 ffmpeg/ffprobe：优先用竞品 package 里已解析好的绝对路径。

    subtitle_engine 不 import montage（避免耦合），但 ffmpeg 常常不在系统 PATH
    （本项目就是靠「开始.bat」注入的），所以这里做一次容错查找。
    """
    import os

    env = os.environ.get(f"{name.upper()}_BIN")
    if env and Path(env).exists():
        return env
    try:
        from packages.montage import media as _media  # type: ignore

        p = (_media.ffmpeg_bin if name == "ffmpeg" else _media.ffprobe_bin)()
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    return name


def _load_mono(path: str, sr: int = 16000):
    """读音频（视频也能读）。librosa 读不了 m4a 时先用 ffmpeg 转 wav。"""
    import numpy as np

    try:
        import librosa

        y, _ = librosa.load(path, sr=sr, mono=True)
        return y, sr
    except Exception:
        pass

    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="lyric_audio_")
    import os

    os.close(fd)
    try:
        _ffmpeg(["-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                 "-ar", str(sr), "-f", "wav", tmp])
        import librosa

        y, _ = librosa.load(tmp, sr=sr, mono=True)
        return y, sr
    finally:
        Path(tmp).unlink(missing_ok=True)


def _ffmpeg(args: Sequence[str]) -> None:
    subprocess.run([_bin("ffmpeg"), *args], capture_output=True, check=False)


def _rms_envelope(y, sr: int, hop: int = 160):
    """10ms 步长的 RMS 包络（与 bgm_offset.py 同一套参数，便于互相关）。"""
    import numpy as np

    if len(y) == 0:
        return np.zeros(1, dtype=np.float32)
    n = len(y) // hop
    trimmed = y[: n * hop].reshape(n, hop).astype(np.float32)
    env = np.sqrt((trimmed ** 2).mean(axis=1) + 1e-12)
    return env


def detect_vocal_regions(y, sr: int, hop: int = 160) -> List[Tuple[float, float]]:
    """粗粒度「有声音」区间（人声 + 伴奏都算）。

    不做严格人声分离：歌词只要求「别把字塞进静音里」，用能量自适应阈值已经够。
    阈值取 中位数 + 0.45*(P90-中位数)，短于 0.35s 的缝隙合并，短于 0.5s 的段丢弃。
    """
    import numpy as np

    env = _rms_envelope(y, sr, hop)
    if len(env) < 5:
        return [(0.0, len(y) / sr)]

    med = float(np.median(env))
    p90 = float(np.percentile(env, 90))
    thresh = med + 0.45 * max(p90 - med, 1e-6)

    frame = hop / sr
    active = env > thresh

    regions: List[List[float]] = []
    i = 0
    n = len(active)
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        while j < n and active[j]:
            j += 1
        regions.append([i * frame, j * frame])
        i = j

    if not regions:
        return [(0.0, len(y) / sr)]

    # 合并小缝隙
    merged: List[List[float]] = [regions[0][:]]
    for s, e in regions[1:]:
        if s - merged[-1][1] <= 0.35:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    # 丢掉碎段
    merged = [r for r in merged if r[1] - r[0] >= 0.5] or merged
    return [(float(s), float(e)) for s, e in merged]


def estimate_shift(envelope, line_starts: Sequence[float], sr: int = 16000,
                   hop: int = 160, search: float = 30.0,
                   duration: Optional[float] = None) -> float:
    """估计「歌词时间轴」相对「音频」的整体偏移，单位秒。

    场景：歌词 LRC 是原曲时间，成片把 BGM 前面裁掉了一段（或从副歌开始），
    直接套用会整体错位。做法是把每句歌词的起点当成一个「应当起音」的位置，
    在 ±search 秒内滑窗，看哪个偏移让这些位置落在音频能量上升沿上最多。

    **只在证据足够强时才采纳**：保守返回 0 比"把本来对的 LRC 拽歪"好得多，
    所以要求最优偏移的得分显著高于 0 偏移（1.5 倍以上），且不能让大部分行
    落到音频范围外。

    返回应叠加到歌词时间上的偏移量（成片时间 = 歌词时间 + shift）。
    """
    import numpy as np

    env = np.asarray(envelope, dtype=np.float32)
    if len(env) < 10 or not line_starts:
        return 0.0

    # 上升沿（能量增大处），归一化后作为「起音强度」
    diff = np.diff(env, prepend=env[0])
    onset = np.clip(diff, 0, None)
    onset = onset / (onset.max() + 1e-9)

    frame = hop / sr
    total = duration or len(env) * frame
    # 每句起点允许 ±0.35s 的宽容，用高斯核抹一下
    sigma = max(1, int(0.35 / frame))
    kernel = np.exp(-0.5 * (np.arange(-3 * sigma, 3 * sigma + 1) / sigma) ** 2)
    kernel /= kernel.sum()
    onset = np.convolve(onset, kernel, mode="same")

    def score_at(sh: int) -> float:
        vals = []
        for t in line_starts:
            idx = int(round(t / frame)) + sh
            if 0 <= idx < len(onset):
                vals.append(float(onset[idx]))
        if len(vals) < max(1, int(0.9 * len(line_starts))):
            return -1.0
        return sum(vals) / len(vals)

    max_shift = int(search / frame)
    base = score_at(0)
    best_shift, best_score = 0, base
    for sh in range(-max_shift, max_shift + 1):
        sc = score_at(sh)
        if sc > best_score:
            best_score, best_shift = sc, sh

    # 证据不足就认为 LRC 本来是对的
    if base < 0 or best_score < base * 1.5 or best_score <= 0.02:
        return 0.0

    shift = best_shift * frame
    # 偏移后首句被推到开头之前，说明是误判
    if min(line_starts) + shift < -0.5:
        return 0.0
    return shift


# ---------------------------------------------------------------------------
# 重复演唱段：给「一遍歌词」，自动补出第二遍 / 第三遍
# ---------------------------------------------------------------------------
#
# 现实里用户手上的歌词往往只写一遍（副歌重复时尤其如此），但歌里那段旋律
# 会唱两遍、三遍。要求「每句歌词都和音频一一对上」本来就不现实，正确做法是：
# 先定位第一遍，再用音频本身找出「后面哪几段和它长得一样」，把时间轴整体
# 平移复制过去。文字始终来自用户给的歌词，不靠识别去猜重复段唱了什么。
#
# 做法：逐帧音色特征（对数 mel，列归一化）→ 每句歌词当模板，用 FFT 互相关
# 在整条音轨上找相似片段 → 若多句歌词都指向同一个时间偏移 Δ，就判定存在
# 一个重复段。多行同时命中同一个 Δ 是很强的证据，单句偶发相似不会误判。


def _music_feature_matrix(y, sr: int):
    """逐帧音色特征 (D, N) 与每帧秒数。列已 L2 归一化，可直接做余弦点积。"""
    import numpy as np
    import librosa
    from scipy.ndimage import uniform_filter1d

    hop = 512
    n_fft = 2048
    mel = librosa.feature.melspectrogram(
        y=np.asarray(y, dtype=np.float32), sr=sr, n_fft=n_fft,
        hop_length=hop, n_mels=32, fmax=min(8000.0, sr / 2.0),
    )
    feat = np.log1p(mel * 10.0).astype(np.float32)
    # 时间平滑，削弱帧级抖动；再抹掉整体响度差异，只留"音色/编曲长什么样"
    feat = uniform_filter1d(feat, size=5, axis=1)
    feat /= (np.linalg.norm(feat, axis=0, keepdims=True) + 1e-8)
    return feat, hop / float(sr)


def _template_scores(feat, tmpl):
    """模板在整条特征序列上每个起点处的「平均逐帧余弦相似度」。

    feat/tmpl 的每帧都已归一化，sum_d(feat[d,t+k] * tmpl[d,k]) 就是两帧余弦；
    对每个 d 做一次 FFT 互相关再相加，等价于把所有起点的滑动点积一次算完。
    """
    import numpy as np
    from scipy.signal import fftconvolve

    length = tmpl.shape[1]
    scores = np.zeros(feat.shape[1] - length + 1, dtype=np.float32)
    for d in range(feat.shape[0]):
        scores += fftconvolve(feat[d], tmpl[d][::-1], mode="valid")
    return scores / max(1, length)


def detect_repeat_offsets(y, sr: int, segments,
                          min_similarity: float = 0.72,
                          min_support_ratio: float = 0.34,
                          tolerance: float = 0.45,
                          max_groups: int = 4):
    """找出音频里「把第一遍歌词再唱一遍」的时间偏移。

    Args:
        y, sr: 单声道音频
        segments: 已定位好的第一遍歌词字幕段（CaptionSegment）
        min_similarity: 单句模板匹配的最低余弦相似度
        min_support_ratio: 一个偏移至少要有多少比例的歌词行同时命中
        tolerance: 偏移聚类容差（秒）
        max_groups: 最多返回几个重复段

    Returns:
        [{"delta": 秒, "lines": [行号...], "support": 行数, "score": 平均相似度}]
    """
    import numpy as np
    from scipy.signal import find_peaks

    segs = [s for s in segments if s.text.strip()]
    if len(segs) < 2:
        return []
    feat, frame = _music_feature_matrix(y, sr)
    total_frames = feat.shape[1]
    if total_frames < 40:
        return []

    first_start = min(float(s.start) for s in segs)
    last_start = max(float(s.start) for s in segs)
    first_end = max(float(s.end) for s in segs)
    span = max(0.5, last_start - first_start)
    # 偏移太短基本是同一句内部的自相似，不是「又唱了一遍」。
    # DJ 版常有 4/8 拍鼓点循环与副歌旋律再现，0.45*span 会把它们误判成
    # 又唱了一遍；真正的第二遍偏移至少接近第一遍的整段长度。
    min_delta = max(1.5, 0.70 * span)
    guard = max(2, int(0.6 / frame))

    candidates = []  # (delta, line_index, score)
    for idx, seg in enumerate(segs):
        a = int(round(float(seg.start) / frame))
        b = int(round(float(seg.end) / frame))
        if a < 0 or b <= a + 5 or b > total_frames:
            continue
        tmpl = feat[:, a:b]
        scores = _template_scores(feat, tmpl)
        # 屏蔽掉模板自身及紧邻区域
        scores[:min(len(scores), a + max(guard, (b - a) // 2))] = -1.0
        peaks, _ = find_peaks(scores, height=min_similarity,
                              distance=max(2, int(1.0 / frame)))
        if not len(peaks):
            continue
        # 每句只保留最像的几个候选，避免一句贡献一堆噪声峰
        order = np.argsort(scores[peaks])[::-1][:4]
        for k in order:
            p = int(peaks[k])
            delta = (p - a) * frame
            if delta >= min_delta:
                candidates.append((float(delta), idx, float(scores[p])))

    if not candidates:
        return []

    # 同一个重复段里，各句的 Δ 应当几乎相同 → 按 Δ 聚类
    candidates.sort(key=lambda c: c[0])
    clusters: List[List[Tuple[float, int, float]]] = []
    for item in candidates:
        if clusters and item[0] - clusters[-1][-1][0] <= tolerance:
            clusters[-1].append(item)
        else:
            clusters.append([item])

    need = max(2, int(len(segs) * min_support_ratio))
    groups = []
    for cl in clusters:
        best: Dict[int, float] = {}
        for delta, idx, score in cl:
            if score > best.get(idx, 0.0):
                best[idx] = score
        if len(best) < need:
            continue
        delta = sum(d * s for d, _, s in cl) / max(1e-8, sum(s for _, _, s in cl))
        # 关键判据：复制后**最靠前**的一句也必须落在第一遍最后一句之后。
        # 歌里常有「副歌旋律再现」这类局部相似，那种 Δ 会让复制段插进
        # 第一遍中间，属于误判；真正的第二遍/第三遍一定整体排在后面。
        # （只重复副歌时，命中行本身就是靠后的几句，同样满足这个条件。）
        # 用第一遍「最后一句的结束」而不是最后一句的起点做门槛：
        # last_start+0.5 仍可能与第一遍尾句重叠，补出来就是两句歌词打架。
        shifted_min = min(float(segs[i].start) + delta for i in best)
        if shifted_min < first_end + 0.3:
            continue
        lines = sorted(best)
        # 连续区间里偶尔漏掉一两句，补上，免得重复段字幕断断续续
        if len(lines) >= 3:
            lo, hi = lines[0], lines[-1]
            missing = [i for i in range(lo, hi + 1) if i not in best]
            if len(missing) <= 2:
                lines = sorted(set(lines) | set(missing))
        groups.append({
            "delta": float(delta),
            "lines": lines,
            "support": len(best),
            "score": float(sum(best.values()) / len(best)),
        })

    groups.sort(key=lambda g: (g["support"], g["score"]), reverse=True)
    # 去掉彼此太近的段（同一遍被切成两半）
    picked = []
    for g in groups:
        if all(abs(g["delta"] - q["delta"]) > 0.8 * span for q in picked):
            picked.append(g)
        if len(picked) >= max_groups:
            break
    return picked


def expand_lyric_repeats(segments, groups, duration: float):
    """按检测出的偏移复制歌词句，返回 (扩展后的段列表, 新增句数)。"""
    if not groups or not segments:
        return list(segments), 0
    last_start = max(float(s.start) for s in segments)
    existing_starts = [float(s.start) for s in segments]
    out = list(segments)
    added = 0
    for g in sorted(groups, key=lambda x: x["delta"]):
        delta = float(g["delta"])
        for i in g["lines"]:
            if i >= len(segments):
                continue
            src = segments[i]
            ns = float(src.start) + delta
            ne = float(src.end) + delta
            # 不允许和第一遍打架、也不许越过片尾
            if ns < last_start - 0.15 or ns >= duration - 0.15:
                continue
            # 这个位置本来就有歌词（LRC 里第二遍已写好）→ 别再叠一句
            if any(abs(es - ns) < 0.6 for es in existing_starts):
                continue
            ne = min(ne, float(duration))
            if ne - ns < 0.3:
                continue
            out.append(CaptionSegment(start=ns, end=ne, text=src.text))
            added += 1
    out.sort(key=lambda s: s.start)
    return out, added


def _extend_tail_to_vocals(segments, report: AlignReport, media_path: str,
                           duration: float):
    """歌词时间轴明显早于人声结束时，把末句延到人声收尾。

    whisper 词级转写常漏掉尾音/拖腔：实测 align 出的最后一句 53.9s 就收，
    而人声唱到 56.4s，末尾突然空三秒。这里只延长最后一句，不动前面的锚点，
    保证「定位靠识别、收尾靠能量」互补而不是互相覆盖。
    """
    if not segments or not Path(str(media_path)).exists():
        return segments
    try:
        y, sr = _load_mono(media_path)
        regions = detect_vocal_regions(y, sr)
        if not regions:
            return segments
        vocal_end = min(float(duration), max(float(e) for _, e in regions))
    except Exception:
        return segments
    report.vocal_end = round(vocal_end, 2)
    last = segments[-1]
    if float(last.end) >= float(duration) - 0.8:
        return segments
    # 人声一直唱到接近片尾时，末句一路留到渐弱结束（片尾 BGM 渐弱里
    # 突然空 3 秒没字幕，观感就像「字幕没做完」）。确实是长尾奏的歌，
    # 仍然只延到人声收尾，不硬挂字幕。
    if vocal_end >= float(duration) - 5.0:
        target_end = float(duration) - 0.25
    else:
        target_end = vocal_end
    new_end = min(target_end, float(last.end) + 6.0)
    if new_end - float(last.end) < 0.8:
        return segments
    last.end = new_end
    report.notes.append(f"末句延到 {new_end:.1f}s 贴合人声收尾")
    return segments


def _finish(base, report: AlignReport, media_path: str,
            duration: float, repeat, max_repeats: int):
    """统一收尾：重复段补全 + 尾部人声校准。

    顺序很重要：只有「没有补出重复段」时才延长末句。已经补出第二遍/第三遍
    说明时间轴自然延伸到了后段，再改末句会打乱 expand 的收口与顺序约束。
    """
    out = _apply_repeats(base, report, media_path, duration, repeat, max_repeats)
    if len(out) == len(base):
        _extend_tail_to_vocals(out, report, media_path, duration)
    return out


def _apply_repeats(segments, report: AlignReport, media_path: str,
                   duration: float, repeat, max_repeats: int):
    """统一的重复段后处理：三条定位路径（lrc/align/spread）都走这里。"""
    key = str(repeat or "auto").strip().lower()
    if key in ("off", "none", "no", "0", "false") or max_repeats <= 0:
        return segments
    cap = max_repeats
    if key not in ("auto", "on", "yes", "true"):
        try:
            # 数字 = 最多补几遍（"1" 就是只补一遍，不能当成 auto 的别名）
            cap = max(1, min(8, int(float(key))))
        except (TypeError, ValueError):
            pass
    if not segments or len(segments) < 2:
        return segments
    # 没有真实音频（离线单测/纯文本预估）就没法检测，静默跳过
    if not Path(str(media_path)).exists():
        return segments
    # 歌词时间轴本来已铺满整条音轨（例如 LRC 里第二遍副歌是写好了的），
    # 再复制只会叠出重复字幕 —— 这种情况不要动
    span = max(float(s.start) for s in segments) - min(float(s.start) for s in segments)
    if duration > 0 and span > 0.75 * float(duration):
        return segments

    try:
        y, sr = _load_mono(media_path)
        groups = detect_repeat_offsets(y, sr, segments, max_groups=cap)
        if not groups:
            return segments
        expanded, added = expand_lyric_repeats(segments, groups, duration)
        if not added:
            return segments
        report.repeats = [
            {"delta": round(g["delta"], 2), "support": g["support"],
             "score": round(g["score"], 3)}
            for g in groups
        ]
        report.repeat_added = added
        return _finalize(expanded, duration)
    except Exception as e:  # 检测失败不能让整条字幕挂掉
        report.notes.append(f"重复段检测跳过（{type(e).__name__}）")
        return segments


# ---------------------------------------------------------------------------
# 对齐：歌词字符流 ↔ 识别词级时间轴
# ---------------------------------------------------------------------------


def _trim_asr_to_one_verse(raw_segments: Sequence[Dict],
                           lines: Sequence[LyricLine]) -> List[Dict]:
    """识别把同一段唱词念了好几遍时，只留第一遍。

    whisper 拿到歌词 prompt 后，常会把第二遍副歌也一起吐出来；拿这种「三遍
    长度的识别流」去做 DTW，一遍歌词会被摊到整首歌上，每句都拖得又长又偏。
    按歌词总字数把识别流截到第一遍（精确到 word），后面交给重复段检测去补。
    """
    lyric_chars = sum(len(_norm_key(ln.text)) for ln in lines)
    if lyric_chars < 8 or not raw_segments:
        return list(raw_segments)
    asr_chars = sum(len(_norm_key(str(s.get("text") or ""))) for s in raw_segments)
    if asr_chars <= lyric_chars * 1.45:
        return list(raw_segments)

    limit = int(lyric_chars * 1.12)
    kept: List[Dict] = []
    acc = 0
    for src in raw_segments:
        if acc >= limit:
            break
        seg = dict(src)
        words = src.get("words") or []
        if words:
            take = 0
            for w in words:
                n = len(_norm_key(str(w.get("word") or w.get("text") or "")))
                if acc + n > limit:
                    break
                acc += n
                take += 1
            seg["words"] = words[:max(1, take)]
            seg["text"] = "".join(str(w.get("word") or w.get("text") or "")
                                  for w in seg["words"])
            seg["end"] = float(seg["words"][-1].get("end", src.get("end", 0.0)))
        else:
            norm = _norm_key(str(src.get("text") or ""))
            seg["text"] = norm[:max(1, limit - acc)]
            acc += len(seg["text"])
        kept.append(seg)
    return kept


def _asr_char_stream(raw_segments: Sequence[Dict]) -> Tuple[str, List[Tuple[float, float]]]:
    """把 whisper 段落摊平成「字符流 + 每字时间」。

    中文 whisper 的 word 有时是单字、有时是整个词；这里按字符均分 word 的
    [start, end]，保证字符流长度与时间列表长度一致。
    """
    chars: List[str] = []
    times: List[Tuple[float, float]] = []
    for seg in raw_segments:
        words = seg.get("words") or []
        if not words:
            text = seg.get("text") or ""
            norm = _norm_key(text)
            if not norm:
                continue
            s, e = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
            step = (e - s) / max(1, len(norm))
            for k, ch in enumerate(norm):
                chars.append(ch)
                times.append((s + k * step, s + (k + 1) * step))
            continue
        for w in words:
            norm = _norm_key(str(w.get("word") or w.get("text") or ""))
            if not norm:
                continue
            ws, we = float(w.get("start", 0.0)), float(w.get("end", 0.0))
            step = (we - ws) / max(1, len(norm))
            for k, ch in enumerate(norm):
                chars.append(ch)
                times.append((ws + k * step, ws + (k + 1) * step))
    return "".join(chars), times


def _dtw_align(lyric_stream: str, asr_stream: str) -> Tuple[List[int], float]:
    """动态时间规整：把歌词字符流单调映射到识别字符流上。

    为什么不用精确子串匹配：whisper 认唱词会出现大量谐音错字
    （「付终身」→「付之身」），精确匹配整行直接对不上，一行都锚不住。
    但唱词的**字数与节奏**基本是对的，DTW 强制单调对齐后照样能拿到
    "这一行大约从第几秒唱到第几秒"。

    Returns:
        (每个歌词字符对应的识别字符索引, 命中率 0~1)
    """
    n, m = len(lyric_stream), len(asr_stream)
    if n == 0 or m == 0:
        return [0] * n, 0.0

    INF = float("inf")
    # C 累积代价，P 回溯指针（0=对角 1=上 2=左）
    C = [[INF] * (m + 1) for _ in range(n + 1)]
    P = [[0] * (m + 1) for _ in range(n + 1)]
    C[0][0] = 0.0
    for i in range(1, n + 1):
        C[i][0] = C[i - 1][0] + 1.0
        P[i][0] = 2
    for j in range(1, m + 1):
        C[0][j] = C[0][j - 1] + 1.0
        P[0][j] = 1

    for i in range(1, n + 1):
        li = lyric_stream[i - 1]
        Ci, Ci1, Pi = C[i], C[i - 1], P[i]
        for j in range(1, m + 1):
            sub = 0.0 if li == asr_stream[j - 1] else 1.0
            diag = Ci1[j - 1] + sub
            up = Ci1[j] + 1.0     # 歌词字多出来（漏识别）
            left = Ci[j - 1] + 1.0  # 识别多出来的字（多识别）
            if diag <= up and diag <= left:
                Ci[j], Pi[j] = diag, 0
            elif up <= left:
                Ci[j], Pi[j] = up, 1
            else:
                Ci[j], Pi[j] = left, 2

    # 回溯
    mapping = [0] * n
    i, j = n, m
    while i > 0 and j > 0:
        mapping[i - 1] = j - 1
        p = P[i][j]
        if p == 0:
            i, j = i - 1, j - 1
        elif p == 1:
            i -= 1
        else:
            j -= 1

    hit = 1.0 - C[n][m] / max(n, m)
    return mapping, max(0.0, min(1.0, hit))


def _align_lines(lines: Sequence[LyricLine], raw_segments: Sequence[Dict],
                 duration: float) -> Tuple[List[Optional[Tuple[float, float]]], AlignReport]:
    """用序列匹配把每行歌词映射到识别时间轴上。

    两级策略：
        1) 精确匹配块（SequenceMatcher）——对上了就最准，直接用；
        2) 覆盖不足时退到 DTW 模糊对齐——容忍谐音错字，保证每行都有时间。

    返回每行的 (start, end) 或 None（没对上）。
    """
    asr_stream, asr_times = _asr_char_stream(raw_segments)
    report = AlignReport(method="align", total=len(lines))

    if not asr_stream or not asr_times:
        return [None] * len(lines), report

    # 歌词侧字符流，同时记录每行在流里的 [a, b) 区间
    lyric_chars: List[str] = []
    spans: List[Tuple[int, int]] = []
    for ln in lines:
        k = _norm_key(ln.text)
        start = len(lyric_chars)
        lyric_chars.extend(k)
        spans.append((start, len(lyric_chars)))
    lyric_stream = "".join(lyric_chars)

    sm = SequenceMatcher(a=lyric_stream, b=asr_stream, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
    report.similarity = sm.ratio()

    exact: List[Optional[Tuple[float, float]]] = []
    matched = 0
    for (a0, a1) in spans:
        best = None
        for b in blocks:
            ov0, ov1 = max(a0, b.a), min(a1, b.a + b.size)
            if ov1 - ov0 < 1:
                continue
            # 只认「覆盖本行足够多字符」的块，避免相邻行的残字把起点拉偏
            if best is None or (ov1 - ov0) > best[0]:
                best = (ov1 - ov0, b.b + (ov0 - b.a), b.b + (ov1 - b.a), b.size)
        if best is None:
            exact.append(None)
            continue
        _, i0, i1, _blk = best
        i0 = max(0, min(i0, len(asr_times) - 1))
        i1 = max(i0 + 1, min(i1, len(asr_times)))
        exact.append((asr_times[i0][0], asr_times[i1 - 1][1]))
        matched += 1

    # 精确匹配够了就直接用（最准）
    if matched >= len(lines) * 0.6:
        report.matched = matched
        return exact, report

    # ---- 覆盖不足：DTW 模糊对齐 ----
    mapping, hit = _dtw_align(lyric_stream, asr_stream)
    report.similarity = max(report.similarity, hit)

    fuzzy: List[Optional[Tuple[float, float]]] = []
    for (a0, a1) in spans:
        if a1 <= a0:
            fuzzy.append(None)
            continue
        js = [mapping[k] for k in range(a0, a1)]
        i0 = max(0, min(min(js), len(asr_times) - 1))
        i1 = max(i0 + 1, min(max(js) + 1, len(asr_times)))
        fuzzy.append((asr_times[i0][0], asr_times[i1 - 1][1]))

    # 命中率太低说明识别内容跟歌词没关系（纯音乐/配错歌），别硬套时间
    if hit >= 0.45:
        report.matched = len(lines)
        report.notes.append(f"模糊对齐（字符命中 {hit:.0%}）")
        return fuzzy, report

    report.matched = matched
    return exact, report


def _fill_gaps(slots: List[Optional[Tuple[float, float]]], duration: float,
               lines: Sequence[LyricLine]) -> List[Tuple[float, float]]:
    """把没对上的行补上：夹在两个已知锚点之间就按字数插值，两端按段落节奏外推。"""
    n = len(slots)
    out: List[Optional[Tuple[float, float]]] = list(slots)

    # 已知锚点序列 [(行号, 起点, 终点)]
    anchors = [(i, s, e) for i, v in enumerate(out) if v for s, e in [v]]
    if not anchors:
        return [(0.0, duration)] * n

    for i in range(n):
        if out[i] is not None:
            continue
        prev = next((a for a in reversed(anchors) if a[0] < i), None)
        nxt = next((a for a in anchors if a[0] > i), None)

        if prev and nxt:
            gap = max(0.05, nxt[1] - prev[2])
            gap_lines = nxt[0] - prev[0]
            weights = [max(1, len(_norm_key(lines[k].text))) for k in range(prev[0], nxt[0])]
            total_w = sum(weights) or 1
            before = sum(weights[: i - prev[0]])
            after = before + weights[i - prev[0]]
            s = prev[2] + gap * before / total_w
            e = prev[2] + gap * after / total_w
            out[i] = (s, e)
        elif prev and not nxt:
            # 尾部：按本行字数估时长
            dur = max(1.2, len(_norm_key(lines[i].text)) * 0.28)
            s = prev[2] + 0.25
            out[i] = (s, min(duration, s + dur))
        elif nxt and not prev:
            dur = max(1.2, len(_norm_key(lines[i].text)) * 0.28)
            e = max(0.3, nxt[1] - 0.25)
            out[i] = (max(0.0, e - dur), e)
        else:
            out[i] = (0.0, duration)
    return [v for v in out]  # type: ignore[list-item]


def _rescue_tail_spans(filled, lines, tail: int, duration: float,
                       media_path: str, report: "AlignReport"):
    """把识别漏掉的**结尾几句**按人声跨度补回时间轴。

    whisper 对片尾拖腔 / 渐弱段经常一句都吐不出来，旧逻辑把「结尾连续
    ≥2 行未匹配」判成「这几句压根没唱」直接丢掉 —— 成片后半段字幕就
    整段消失（实测 49.5~53.5s 空档）。只要尾部音轨还有可听内容，就在
    [最后锚点, 人声结束] 之间按字数重新铺这些行；确实是纯尾奏（区间
    太短或几乎静音）返回 0，由调用方沿用原来的跳过逻辑。
    """
    n = len(filled)
    if tail <= 0 or tail >= n:
        return filled, 0
    anchor = max(0.0, float(filled[tail - 1][1]))
    try:
        y, sr = _load_mono(media_path)
        regions = detect_vocal_regions(y, sr)
        vocal_end = min(float(duration), max(
            [float(e) for _, e in regions] or [float(duration)]))
        env = _rms_envelope(y, sr)
        a = int(max(0.0, anchor) * sr / 160)
        b = int(min(float(duration), vocal_end + 0.5) * sr / 160)
        tail_energy = float(env[a:b].mean()) if b > a else 0.0
    except Exception:
        vocal_end, tail_energy = float(duration), 1.0

    end = min(float(duration), max(vocal_end, anchor + 1.0))
    if end - anchor < 1.0 or tail_energy < 0.004:
        return filled, 0

    weights = [max(1.0, float(len(_norm_key(lines[i].text))) or 1.0)
               for i in range(tail, n)]
    total = sum(weights)
    out = list(filled)
    acc = 0.0
    for k, i in enumerate(range(tail, n)):
        s = anchor + (end - anchor) * acc / total
        acc += weights[k]
        e = anchor + (end - anchor) * acc / total
        out[i] = (s, min(float(duration), e))
    report.notes.append(f"尾部 {n - tail} 句按人声跨度补齐到 {end:.1f}s")
    return out, n - tail


def _est_speech_seconds(text: str) -> float:
    """粗估一句歌词唱完要多久：中文约 0.30s/字，拉丁字母约 0.11s/字符。

    用来给"没有时间戳的歌词"定总长度，避免把 16 句歌词硬铺满整首 90 秒的歌
    （实际只唱了 60 秒，剩下 30 秒是间奏）。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff"
              or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalnum())
    return cjk * 0.30 + latin * 0.11


def _spread_lines(lines: Sequence[LyricLine], regions: Sequence[Tuple[float, float]],
                  duration: float) -> List[Tuple[float, float]]:
    """没人声段/识别失败时的兜底：在「有声跨度」内按字数权重分配各行时间。

    早期实现按"所有有声段的累计时长"分配，遇到带伴奏的歌曲就崩：能量阈值
    只挑出零碎的高能段，累计 12 秒却要装 16 句，结果全部挤在开头 12 秒里。
    现在改成按**跨度**（第一段起点 → 最后一段终点）分配，再用字数估算的总时长
    收口，避免 60 秒的歌词被摊到 90 秒的歌里；每个起点还会吸附到最近的有声段
    边界，防止字幕悬在完全静音的位置。
    """
    if not lines:
        return []
    regions = [(max(0.0, s), min(duration, e)) for s, e in regions if e > s]
    if not regions:
        step = duration / max(1, len(lines))
        return [(i * step, min(duration, (i + 1) * step)) for i in range(len(lines))]

    span_start = regions[0][0]
    span_end = regions[-1][1]
    # 按字数估算"歌词实际唱多久"，和有声跨度取小值：间奏/尾奏不该被歌词占满
    est = sum(_est_speech_seconds(ln.text) for ln in lines)
    est = max(est, len(lines) * 1.2)
    # 0.30s/字是快歌估值，古风慢歌拖腔常到 0.40s/字以上。以前只要
    # 有声跨度 > est 就砍到 est，实测把 56s 的演唱压成 42s，末尾 14s 全空。
    # 现在只有明显带长间奏/尾奏（>1.6 倍）才收口，且留 1.35 倍余量。
    if span_end - span_start > est * 1.6:
        span_end = span_start + est * 1.35
    # 有声跨度比行数还短（极短音频/碎段）→ 直接用整段时间
    if span_end - span_start < max(2.0, len(lines) * 0.4):
        span_start, span_end = 0.0, max(duration, span_end)

    weights = [max(1.0, float(len(_norm_key(ln.text))) or 1.0) for ln in lines]
    total_w = sum(weights)

    edges: List[float] = [span_start]
    acc = 0.0
    for w in weights[:-1]:
        acc += w
        edges.append(span_start + (span_end - span_start) * acc / total_w)
    edges.append(span_end)

    def snap(t: float) -> float:
        for rs, re_ in regions:
            if rs <= t <= re_:
                return t
        best_d, best_t = None, t
        for rs, re_ in regions:
            for cand in (rs, re_):
                d = abs(t - cand)
                if best_d is None or d < best_d:
                    best_d, best_t = d, cand
        return best_t if (best_d is not None and best_d <= 1.5) else t

    out: List[Tuple[float, float]] = []
    for i in range(len(lines)):
        s = snap(edges[i])
        e = edges[i + 1]
        if s >= e - 0.3:      # 吸附把起点推到下一句后面了 → 回退
            s = edges[i]
        if e - s < 0.35:
            e = min(span_end, s + 0.35)
        out.append((max(0.0, s), e))
    return out


def _finalize(segments: List[CaptionSegment], duration: float,
              min_dur: float = 0.55, gap: float = 0.04) -> List[CaptionSegment]:
    """统一收口：起点递增、不重叠、不越界、太短的补到最短时长。"""
    segs = [s for s in segments if s.text.strip()]
    for s in segs:
        s.start = max(0.0, min(float(s.start), max(0.0, duration - 0.2)))
        s.end = max(s.start + 0.25, min(float(s.end), duration))

    for i in range(len(segs) - 1):
        if segs[i].end > segs[i + 1].start - gap:
            segs[i].end = max(segs[i].start + 0.3, segs[i + 1].start - gap)
    if segs:
        segs[-1].end = min(segs[-1].end, duration)

    out = []
    for s in segs:
        if s.end - s.start < min_dur:
            s.end = min(duration, s.start + min_dur)
            if s.end - s.start < 0.2:
                continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def build_lyric_segments(
    media_path: str,
    text: str,
    offset: Optional[float] = None,
    language: Optional[str] = None,
    model_size: str = "base",
    duration: Optional[float] = None,
    transcribe_fn=None,
    repeat: str = "auto",
    max_repeats: int = 3,
    dj: str = "auto",
    dj_ref: str = "",
    dj_ratio: Optional[float] = None,
) -> Tuple[List[CaptionSegment], AlignReport]:
    """把歌词文本变成带时间轴的字幕段。

    Args:
        media_path: 成片（或 BGM）路径，用它自己的音轨定位时间
        text: 歌词文本，LRC 或纯文本
        offset: LRC 时间轴的已知偏移（成片裁掉的开头秒数）。
            None = 自动估计；0 = 不偏移。
        language: 传给 whisper 的语言（唱词通常是 zh/en/ja）
        model_size: whisper 模型规格
        duration: 已知时长（省一次探测）
        transcribe_fn: 注入的转写函数，签名 ``fn(model_size) -> List[dict]``，
            测试时可用假数据替代真识别
        repeat: 重复段处理。auto = 自动检测音频里再次演唱的段落并把这一遍
            歌词时间轴平移复制过去（第二遍、第三遍）；off = 不复制；
            也可直接给数字，表示最多补几遍。
        max_repeats: auto 模式下最多补几遍（默认 3）
        dj: DJ / remix 版适配。auto = 自动提纯人声、估变速比、找原版并把
            原版时间轴 warp 到 DJ 版；off = 完全关闭；on = 强制开启。
        dj_ref: 原版音频路径（可选）。留空会自动在 uploads / Music 里找同名原曲。
        dj_ratio: 手动指定变速比（如 1.25）；None = 自动估计。

    Returns:
        (CaptionSegment 列表, AlignReport)
    """
    lines = parse_lyrics(text)
    report = AlignReport(total=len(lines))
    if not lines:
        return [], report

    if duration is None:
        duration = _probe_duration(media_path)

    # ---------- 0) DJ / remix 版适配 ----------
    # 变速、加鼓、循环切片会让 Whisper 判成纯音乐、也会让原版时间轴整体拉伸。
    # 这里决定「用哪条音轨算时间」以及「算完后怎么换算回成片时间轴」。
    dj_prof = None
    asr_media = media_path
    analysis_media = media_path
    dj_scale = 1.0
    _dj_warp = _dj_scale_segs = None
    dj_key = str(dj or "auto").strip().lower()
    # 注入 transcribe_fn 是离线单测（没有真实音频可分析），跳过 DJ 层以保持确定性
    if dj_key not in ("off", "none", "no", "false", "0") and transcribe_fn is None:
        try:
            from .dj import (prepare_dj, warp_segments as _dj_warp,
                             scale_segments as _dj_scale_segs)

            dj_prof = prepare_dj(media_path, mode=dj, ref=dj_ref, ratio=dj_ratio)
            if dj_prof.enabled:
                report.dj_method = dj_prof.method
                report.dj_ratio = dj_prof.ratio
                report.dj_detail = dj_prof.summary()
                if dj_prof.method == "ref_warp":
                    # 原版音轨干净：识别/对齐都在原版上做，最后整体映射到 DJ 时间
                    asr_media = dj_prof.ref_path or media_path
                    analysis_media = dj_prof.enhanced_path or media_path
                elif dj_prof.method == "restored":
                    # 无原版但确认变速：还原音高后识别，时间按 1/ratio 缩回
                    asr_media = dj_prof.restored_path or media_path
                    analysis_media = dj_prof.enhanced_path or media_path
                    dj_scale = dj_prof.restore_scale
                else:
                    # 只提纯人声：时间轴本来就是成片时间，无需换算
                    asr_media = dj_prof.enhanced_path or media_path
                    analysis_media = dj_prof.enhanced_path or media_path
        except Exception as e:
            report.notes.append(f"DJ 适配跳过（{type(e).__name__}: {e}）")
            dj_prof = None
            asr_media = analysis_media = media_path
            dj_scale = 1.0

    # 定位阶段用「被分析音轨」自己的时长，映射回 DJ 时间轴后再按成片时长收口
    loc_duration = duration
    if dj_prof and dj_prof.enabled and asr_media != media_path:
        try:
            loc_duration = _probe_duration(asr_media) or duration
        except Exception:
            loc_duration = duration

    def _dj_time(segs):
        """把定位得到的时间轴换算到成片（DJ 版）时间轴。"""
        if dj_prof and dj_prof.method == "ref_warp" and _dj_warp is not None:
            _dj_warp(segs, dj_prof)
        elif dj_scale != 1.0 and _dj_scale_segs is not None:
            _dj_scale_segs(segs, dj_scale)
        return segs

    # ---------- 1) 已有时间戳：LRC 直接可用 ----------
    if all(ln.start is not None for ln in lines):
        starts = [float(ln.start) for ln in lines]
        # 已经找到可信原版时由 warp 负责时间轴换算，别再用能量偏移把 LRC 拽歪
        warped = bool(dj_prof and dj_prof.method == "ref_warp")
        if offset is None and any(abs(s) > 1e-6 for s in starts) and not warped:
            try:
                y, sr = _load_mono(analysis_media)
                env = _rms_envelope(y, sr)
                shift = estimate_shift(env, starts, sr=sr)
                # 偏移小于半句就不动，避免把本来正确的 LRC 拽歪
                offset = shift if abs(shift) >= 0.35 else 0.0
            except Exception:
                offset = 0.0
        offset = 0.0 if offset is None else float(offset)

        segs: List[CaptionSegment] = []
        for i, ln in enumerate(lines):
            s = max(0.0, float(ln.start) + offset)
            if ln.end is not None:
                e = float(ln.end) + offset
            elif i + 1 < len(lines):
                e = float(lines[i + 1].start) + offset - 0.06
            else:
                e = s + max(1.6, len(_norm_key(ln.text)) * 0.3)
            segs.append(CaptionSegment(start=s, end=e, text=ln.text))
        report.method = "lrc"
        report.shift = offset
        report.matched = len(segs)
        base = _finalize(_dj_time(_finalize(segs, loc_duration)), duration)
        return _finish(base, report, analysis_media, duration,
                       repeat, max_repeats), report

    # ---------- 2) 纯文本：识别拿时间，歌词拿文字 ----------
    raw_segments: List[Dict] = []
    # 唱词场景 base 基本不可用（实测只吐出前四句，后面整段丢弃），
    # 起步就用 medium；用户显式指定更大/更小的模型则尊重用户。
    asr_model = model_size if model_size not in (None, "", "base") else "medium"
    if transcribe_fn is not None:
        try:
            raw_segments = transcribe_fn(asr_model) or []
        except Exception as e:  # 识别挂了也要出字幕
            report.notes.append(f"语音识别失败（{type(e).__name__}）")
    else:
        try:
            from .transcriber import transcribe_robust

            # initial_prompt 给歌词全文：模型"预先知道要听什么词"，谐音错字大幅减少
            # （实测前 4~7 句能做到几乎全对）。超过 120 字会被截断，取前 120 字。
            lyric_prompt = " ".join(ln.text for ln in lines)[:120]
            result = transcribe_robust(
                asr_media, model_size=asr_model, language=language,
                prompt=lyric_prompt, music=True,
            )
            raw_segments = result.get("segments") or []
            if result.get("model_used") and result["model_used"] != asr_model:
                report.notes.append(f"识别模型自动调整到 {result['model_used']}")
        except Exception as e:
            report.notes.append(f"语音识别失败（{type(e).__name__}: {e}）")

    if raw_segments:
        before = len(raw_segments)
        raw_segments = _trim_asr_to_one_verse(raw_segments, lines)
        if len(raw_segments) < before:
            report.notes.append("识别含重复段，已只按第一遍对齐")
        slots, align_report = _align_lines(lines, raw_segments, loc_duration)
        report.similarity = align_report.similarity
        good_ratio = align_report.matched / max(1, len(lines))
        # 匹配率 60% 以上才值得信任；否则说明识别内容与歌词对不上（纯音乐/错版本）
        if good_ratio >= 0.6 and align_report.similarity >= 0.18:
            filled = _fill_gaps(slots, loc_duration, lines)
            # 开头连续多行没对上 = 成片裁掉了前奏/念白，仍然直接跳过
            # （_fill_gaps 会把它们塞进 0 秒闪一下并把整条时间轴挤偏）。
            head = 0
            while head < len(slots) and slots[head] is None:
                head += 1
            if head < 2:            # 单行漏识别不判为「未演唱」
                head = 0
            # 结尾连续没对上：whisper 常漏掉收尾拖腔/渐弱段，旧逻辑把
            # 这几行也判成「未演唱」整段丢掉，正是后半段字幕消失的原因。
            # 只要尾部音轨还有人声，就按人声跨度把它们铺回去。
            tail = len(slots)
            while tail > head and slots[tail - 1] is None:
                tail -= 1
            rescued = 0
            if tail < len(slots):
                filled, rescued = _rescue_tail_spans(
                    filled, lines, tail, loc_duration, analysis_media, report)
            skip_tail = (set(range(tail, len(slots)))
                         if not rescued and len(slots) - tail >= 2 else set())
            unsung = set(range(head)) | skip_tail
            segs = [CaptionSegment(start=s, end=e, text=ln.text)
                    for i, (ln, (s, e)) in enumerate(zip(lines, filled))
                    if i not in unsung]
            report.method = "align"
            report.matched = align_report.matched
            report.unsung = len(unsung)
            base = _finalize(_dj_time(_finalize(segs, loc_duration)), duration)
            return _finish(base, report, analysis_media, duration,
                           repeat, max_repeats), report
        report.notes.append(
            f"识别内容与歌词对不上（匹配 {align_report.matched}/{len(lines)} 行）")

    # ---------- 3) 兜底：按人声段 + 字数分配 ----------
    # 这里的时间轴直接来自提纯后的 DJ 音轨，本身就是成片时间，无需再换算。
    try:
        y, sr = _load_mono(analysis_media)
        regions = detect_vocal_regions(y, sr)
        spans = _spread_lines(lines, regions, duration)
        report.method = "spread"
        if regions:
            report.notes.append(f"按 {len(regions)} 段有声区间分配")
    except Exception:
        step = duration / max(1, len(lines))
        spans = [(i * step, min(duration, (i + 1) * step)) for i in range(len(lines))]
        report.method = "uniform"

    segs = [CaptionSegment(start=s, end=e, text=ln.text) for ln, (s, e) in zip(lines, spans)]
    base = _finalize(segs, duration)
    return _finish(base, report, analysis_media, duration,
                   repeat, max_repeats), report


def _probe_duration(path: str) -> float:
    try:
        proc = subprocess.run(
            [_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True,
        )
        return float((json.loads(proc.stdout or "{}").get("format") or {}).get("duration") or 0.0)
    except Exception:
        return 0.0
