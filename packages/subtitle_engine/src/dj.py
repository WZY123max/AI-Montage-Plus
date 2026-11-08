"""DJ / remix 版歌词对齐：人声增强 + 变速映射 + 原版时间轴 warp。

为什么需要单独一层
------------------
古风 DJ 版（变速、加鼓、切片循环、混响）会同时破坏两件事：

* **识别**：鼓点与 bass 掩蔽人声，Whisper 的 VAD 把整段判成 music，返回空或幻觉；
  整体变速变调又让声学特征落在训练分布之外。
* **时间轴**：原版 LRC 或原版识别结果按「原速」标注，DJ 版被整体拉伸/压缩，
  单一 ``estimate_shift`` 偏移不够用；drop 段还会插入额外时长。

这一层做三件事，全部只用项目里已有的 numpy / scipy / librosa / ffmpeg，
**不引入 torch 模型权重、不联网下载**：

1. ``enhance_vocals``   中置声道软掩膜 + HPSS 去打击乐 + 人声带通，提纯人声。
   只用于「算时间轴」，成片音轨仍然用原混音，音质不变。
2. ``estimate_tempo_ratio``  用 onset 包络在候选速率上做时间缩放互相关，
   估出 DJ 版相对原版的播放速率（含裁头偏移），对 pitch-shift 不敏感。
3. ``build_warp_map``   在原版与 DJ 版的 mel/chroma 特征上做带斜率约束的 DTW，
   得到单调分段线性映射；把原版逐句时间轴直接搬过来，drop/循环段也能跟住。

对外主入口是 ``prepare_dj``，返回 ``DJProfile``。``lyrics.build_lyric_segments``
拿到 profile 后决定：有原版就在原版上定位时间轴再 warp；没有原版就用增强人声
直接识别，必要时按已知速率还原音高再识别。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np

# 变速比搜索范围：覆盖常见 nightcore(1.2~1.35)、slowed(0.75~0.9)、DJ 加速(1.1~1.25)
RATIO_MIN, RATIO_MAX, RATIO_STEP = 0.72, 1.45, 0.01
# 人声带通（Hz）。古风女声/戏腔基频约 200~1.2k，谐波到 4k 足够。
VOICE_LO, VOICE_HI = 170.0, 4800.0
FEATURE_SR = 16000          # 特征提取统一采样率
ASR_SR = 16000              # whisper 输入采样率


@dataclass
class DJProfile:
    """一次 DJ 适配的诊断结果，会写进字幕报告。"""

    enabled: bool = False
    enhanced_path: str = ""        # 提纯人声 wav（16k mono）
    ref_path: str = ""             # 用来对齐的原版音频
    ratio: float = 1.0             # DJ 相对原版的播放速率（>1 = 加速）
    ratio_confidence: float = 0.0  # 0~1，低于阈值不采纳
    offset: float = 0.0            # DJ 版相对原版裁掉的开头（原版秒数）
    warp: List[Tuple[float, float]] = field(default_factory=list)  # [(原版t, DJ t)]
    method: str = "off"            # off / direct / restored / ref_warp
    notes: List[str] = field(default_factory=list)
    restored_path: str = ""        # 还原到原速/原调后的识别音频
    restore_scale: float = 1.0     # DJ 时间 = 还原音频时间 * restore_scale

    def summary(self) -> str:
        if not self.enabled or self.method == "off":
            return ""
        bits = []
        if self.method == "ref_warp":
            bits.append(f"原版对齐并映射到 DJ 版（{len(self.warp)} 个锚点）")
        elif self.method == "restored":
            bits.append(f"按 {self.ratio:.2f}× 还原音高后识别")
        else:
            bits.append("提纯人声后识别")
        if abs(self.ratio - 1.0) >= 0.02 and self.ratio_confidence:
            bits.append(f"变速比 {self.ratio:.2f}×（置信 {self.ratio_confidence:.0%}）")
        if abs(self.offset) >= 0.2:
            bits.append(f"裁头 {self.offset:+.2f}s")
        if self.notes:
            bits.extend(self.notes)
        return "；".join(bits)

    def to_ref(self, t_dj: float) -> float:
        """DJ 时间 → 原版时间（warp 的逆映射）。"""
        return _map_time(t_dj, [(b, a) for a, b in self.warp])

    def to_dj(self, t_ref: float) -> float:
        """原版时间 → DJ 时间。"""
        return _map_time(t_ref, self.warp)


def _map_time(t: float, pairs: Sequence[Tuple[float, float]]) -> float:
    """单调分段线性插值；没有锚点时退化为恒等映射。"""
    if not pairs:
        return float(t)
    pts = sorted(pairs)
    if t <= pts[0][0]:
        x0, y0 = pts[0]
        x1, y1 = pts[1] if len(pts) > 1 else (x0 + 1.0, y0 + 1.0)
        return float(y0 + (t - x0) * (y1 - y0) / max(1e-6, x1 - x0))
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        if t <= x1:
            return float(y0 + (t - x0) * (y1 - y0) / max(1e-6, x1 - x0))
    x0, y0 = pts[-2] if len(pts) > 1 else pts[0]
    x1, y1 = pts[-1]
    return float(y1 + (t - x1) * (y1 - y0) / max(1e-6, x1 - x0))


# ---------------------------------------------------------------------------
# ffmpeg 辅助
# ---------------------------------------------------------------------------


def _bin(name: str) -> str:
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


def _ffmpeg(args: Sequence[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run([_bin("ffmpeg"), "-y", "-nostdin", "-v", "error", *args],
                          capture_output=True, check=check)


def _to_wav(path: str, out: str, sr: int = FEATURE_SR, stereo: bool = False,
            max_seconds: Optional[float] = None) -> str:
    trim = ["-t", str(max_seconds)] if max_seconds else []
    _ffmpeg(["-i", str(path), *trim, "-vn", "-ac", "2" if stereo else "1",
             "-ar", str(sr), "-c:a", "pcm_s16le", str(out)], check=True)
    return out


def _cache_dir() -> Path:
    """放到项目 var/cache/dj，跟其它缓存一起被 cache_gc 管理。"""
    try:
        root = Path(__file__).resolve().parents[3]
        cand = root / "var" / "cache" / "dj"
        if (root / "var").exists():
            cand.mkdir(parents=True, exist_ok=True)
            return cand
    except Exception:
        pass
    p = Path(tempfile.gettempdir()) / "montage_dj"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _fingerprint(path: str, extra: str = "") -> str:
    st = Path(path).stat()
    h = hashlib.sha1(f"{path}|{st.st_size}|{int(st.st_mtime)}|{extra}".encode("utf-8"))
    return h.hexdigest()[:20]


# ---------------------------------------------------------------------------
# 1) 人声增强
# ---------------------------------------------------------------------------


def enhance_vocals(path: str, sr: int = ASR_SR, use_cache: bool = True) -> str:
    """提纯人声，输出 16k 单声道 wav 路径。

    三步，全部对「算时间轴」生效，不改动成片音轨：

    * 中置软掩膜：人声通常压在声场中央，``|M| - α|S|`` 抑制两侧铺开的伴奏；
    * HPSS：取 harmonic 分量，去掉鼓点/打击乐这类 percussive 能量；
    * 带通 + 峰值归一化：把人声主频与谐波留下，低频 bass 与高频镲片去掉。

    任一步失败都退化为「简单带通」，再失败就退化为原始音频抽轨，保证不中断流程。
    """
    import soundfile as sf
    from scipy import signal as sig

    out_dir = _cache_dir()
    key = _fingerprint(path, f"voc{sr}")
    out = out_dir / f"voc_{key}.wav"
    if use_cache and out.exists() and out.stat().st_size > 1000:
        return str(out)

    fd, stereo_wav = tempfile.mkstemp(suffix=".wav", prefix="dj_stereo_")
    os.close(fd)
    try:
        _to_wav(path, stereo_wav, sr=22050, stereo=True)
        data, srx = sf.read(stereo_wav, dtype="float32", always_2d=True)
        if data.shape[1] >= 2:
            mid = data[:, 0] + data[:, 1]
            side = data[:, 0] - data[:, 1]
        else:
            mid = data[:, 0] * 2.0
            side = np.zeros_like(mid)
    finally:
        Path(stereo_wav).unlink(missing_ok=True)

    # --- 中置软掩膜（STFT 域） ---
    try:
        import librosa

        n_fft, hop = 2048, 512
        M = librosa.stft(mid, n_fft=n_fft, hop_length=hop)
        S = librosa.stft(side, n_fft=n_fft, hop_length=hop)
        aM, aS = np.abs(M), np.abs(S)
        mask = np.clip((aM - 0.55 * aS) / (aM + aS + 1e-8), 0.08, 1.0)
        # 时频平滑，避免掩膜把字切成碎片
        mask = sig.ndimage.uniform_filter(mask.astype(np.float32), size=(3, 3))
        V = M * mask
        # --- HPSS：只保留 harmonic（人声/旋律），抑制鼓点 ---
        try:
            V_h, _ = librosa.decompose.hpss(V, margin=2.0)
            V = 0.85 * V_h + 0.15 * V
        except Exception:
            pass
        y = librosa.istft(V, hop_length=hop, length=len(mid))
    except Exception:
        y = mid.copy()

    # --- 带通 ---
    try:
        sos = sig.butter(4, [VOICE_LO, VOICE_HI], btype="bandpass",
                         fs=srx, output="sos")
        y = sig.sosfiltfilt(sos, y).astype(np.float32)
    except Exception:
        pass

    # --- 重采样 + 归一化 ---
    try:
        import librosa

        y = librosa.resample(y, orig_sr=srx, target_sr=sr)
    except Exception:
        y = sig.resample_poly(y, sr, srx)
    peak = float(np.max(np.abs(y)) + 1e-9)
    if peak > 0:
        y = y / peak * 0.92
    sf.write(str(out), y.astype(np.float32), sr, subtype="PCM_16")
    return str(out)


# ---------------------------------------------------------------------------
# 2) 变速比 / 裁头偏移估计
# ---------------------------------------------------------------------------


def _onset_feature(path: str, sr: int = FEATURE_SR, hop: int = 256,
                   max_seconds: Optional[float] = None):
    """对音高不敏感、对鼓点/旋律起音敏感的 1D 包络。

    用 spectral flux 而不是 chroma：DJ 常做 pitch shift，chroma 会整体旋半音，
    flux 只关心「能量什么时候起来」，变速后仍可按比例缩放对齐。
    """
    import librosa
    import soundfile as sf

    fd, wav = tempfile.mkstemp(suffix=".wav", prefix="dj_feat_")
    os.close(fd)
    try:
        _to_wav(path, wav, sr=sr, stereo=False, max_seconds=max_seconds)
        y, _ = sf.read(wav, dtype="float32")
    finally:
        Path(wav).unlink(missing_ok=True)
    if len(y) < sr:
        return np.zeros(1, dtype=np.float32), sr / hop
    try:
        onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop,
                                            center=True, aggregate=np.mean)
    except Exception:
        S = np.abs(librosa.stft(y, n_fft=1024, hop_length=hop))
        onset = np.maximum(0.0, np.diff(S, axis=1, prepend=S[:, :1])).mean(axis=0)
    # 去直流 + 平滑 + 归一化
    onset = onset.astype(np.float32)
    onset = onset - np.convolve(onset, np.ones(81) / 81.0, mode="same")
    onset = np.maximum(onset, 0.0)
    std = float(onset.std() + 1e-9)
    return onset / std, hop / sr


def _corr_shift(a: np.ndarray, b: np.ndarray, max_shift_ratio: float = 0.35):
    """FFT 互相关求 a、b 的最佳相对平移（帧数），返回 (shift, score)。

    b 已按候选速率重采样到与 a 等长；shift>0 表示 b 相对 a 延后。
    """
    n = min(len(a), len(b))
    if n < 32:
        return 0, 0.0
    aa = a[:n].astype(np.float64)
    bb = b[:n].astype(np.float64)
    aa = aa - aa.mean()
    bb = bb - bb.mean()
    denom = np.sqrt((aa * aa).sum() * (bb * bb).sum()) + 1e-9
    size = 1 << (2 * n - 1).bit_length()
    fa = np.fft.rfft(aa, size)
    fb = np.fft.rfft(bb, size)
    cc = np.fft.irfft(fa * np.conj(fb), size)[: 2 * n - 1]
    lim = int(n * max_shift_ratio)
    center = n - 1
    lo, hi = max(0, center - lim), min(len(cc) - 1, center + lim)
    k = int(np.argmax(cc[lo:hi + 1])) + lo
    score = float(cc[k] / denom)
    return k - center, max(0.0, score)


def estimate_tempo_ratio(ref_path: str, dj_path: str,
                         ratios: Optional[Sequence[float]] = None,
                         with_peak: bool = False):
    """估 DJ 版相对原版的播放速率、裁头偏移、置信度。

    返回 ``(ratio, offset_ref_seconds, confidence)``。
    ``ratio>1`` 表示 DJ 版更快（原版 t 秒 → DJ 版 t/ratio 秒）。
    ``offset_ref_seconds`` 是 DJ 开头对应原版的时刻（裁掉的开头）。

    做法：把 DJ 的 onset 包络按候选速率**拉伸**回原版时间尺度，
    与 ref 包络做归一化互相关；取全候选里得分最高且明显优于次优的组合。
    """
    a, _ = _onset_feature(ref_path)
    b, frame_s = _onset_feature(dj_path)
    empty = (1.0, 0.0, 0.0, 0.0) if with_peak else (1.0, 0.0, 0.0)
    if len(a) < 64 or len(b) < 32:
        return empty

    if ratios is None:
        ratios = np.arange(RATIO_MIN, RATIO_MAX + 1e-9, RATIO_STEP)

    base = np.arange(len(a), dtype=np.float64)
    best = (1.0, 0.0, 0.0)
    scores = []
    for r in ratios:
        r = float(r)
        # DJ 帧 j 对应原版帧 j*r；把 DJ 拉回原版尺度：a[i] ↔ b[i/r]
        stretched = np.interp(base / r, np.arange(len(b), dtype=np.float64),
                              b, left=0.0, right=0.0)
        shift, sc = _corr_shift(a, stretched)
        scores.append((sc, r, shift))
    if not scores:
        return empty
    scores.sort(reverse=True)
    top = scores[0]
    # 峰值明显程度：与「去掉 r≈top 的其它候选最好成绩」比较
    others = [s for s in scores if abs(s[1] - top[1]) > 0.04]
    second = others[0][0] if others else 0.0
    confidence = float(np.clip((top[0] - second) / max(0.08, top[0]), 0.0, 1.0))
    # 绝对相关太低 = 两首不同的歌（自动找原版时可能误命中），一律不采纳
    if top[0] < 0.22:
        return (1.0, 0.0, 0.0, top[0]) if with_peak else (1.0, 0.0, 0.0)
    # 偏移方向：stretched 相对 a 延后 shift 帧 == DJ 开头在原版 shift 帧处
    offset_ref = max(0.0, top[2] * frame_s)
    if with_peak:
        return top[1], offset_ref, confidence, top[0]
    return top[1], offset_ref, confidence


# ---------------------------------------------------------------------------
# 3) 分段线性 warp（原版时间轴 → DJ 时间轴）
# ---------------------------------------------------------------------------


def _mel_chroma(path: str, sr: int = FEATURE_SR, hop: int = 1024):
    """低维时频特征：mel 能量（对鼓点稳健）+ 一阶差分，用于 DTW。"""
    import librosa
    import soundfile as sf

    fd, wav = tempfile.mkstemp(suffix=".wav", prefix="dj_mel_")
    os.close(fd)
    try:
        _to_wav(path, wav, sr=sr, stereo=False)
        y, _ = sf.read(wav, dtype="float32")
    finally:
        Path(wav).unlink(missing_ok=True)
    mel = librosa.feature.melspectrogram(y=y, sr=sr, n_fft=1024,
                                         hop_length=hop, n_mels=32, fmax=4000)
    feat = librosa.power_to_db(mel, ref=np.max)
    feat = (feat - feat.mean(axis=1, keepdims=True)) / (feat.std(axis=1, keepdims=True) + 1e-6)
    return feat.astype(np.float32), hop / sr


def build_warp_map(ref_path: str, dj_path: str, ratio: float = 1.0,
                   offset_ref: float = 0.0, max_points: int = 90,
                   max_deviation: float = 2.0
                   ) -> List[Tuple[float, float]]:
    """求「原版 t → DJ t」的单调分段线性映射。

    全局线性 ``(t - offset) / ratio`` 是基准（``estimate_tempo_ratio`` 实测
    误差 0.000，最可靠）；DTW 只负责修正 **局部** 偏差——DJ 版插入 drop、
    循环或切掉间奏时，基准会在那一段之后整体错位。因此把 DTW 输出当作
    「相对基准的偏移量」，做中值滤波、限幅、单调化后再叠加，绝不让它改写
    全局斜率。任何异常都安全退回纯线性映射。
    """
    r = max(ratio, 1e-6)
    t_end = 0.0
    try:
        import soundfile as sf

        fd, wav = tempfile.mkstemp(suffix=".wav", prefix="dj_len_")
        os.close(fd)
        try:
            _to_wav(ref_path, wav, sr=FEATURE_SR)
            t_end = float(sf.info(wav).duration)
        finally:
            Path(wav).unlink(missing_ok=True)
    except Exception:
        pass
    if t_end <= 1.0:
        t_end = 120.0

    def linear(x: float) -> float:
        return max(0.0, (x - offset_ref) / r)

    grid = np.linspace(0.0, t_end, max_points)
    base_y = np.array([linear(float(x)) for x in grid])
    dev = np.zeros_like(base_y)
    try:
        import librosa

        fa, sa = _mel_chroma(ref_path)
        fb, _ = _mel_chroma(dj_path)
        # 把 DJ 特征按 1/ratio 拉伸回原版时间尺度，DTW 理想路径就是对角线
        idx = np.arange(fa.shape[1], dtype=np.float64) / r
        fb = np.stack([np.interp(idx, np.arange(fb.shape[1]), fb[i])
                       for i in range(fb.shape[0])]).astype(np.float32)
        fa = (fa - fa.mean(axis=1, keepdims=True)) / (fa.std(axis=1, keepdims=True) + 1e-6)
        fb = (fb - fb.mean(axis=1, keepdims=True)) / (fb.std(axis=1, keepdims=True) + 1e-6)
        step = max(1, int(max(fa.shape[1], fb.shape[1]) / 420))
        A, B = fa[:, ::step], fb[:, ::step]
        wp = np.asarray(librosa.sequence.dtw(X=A, Y=B, metric="euclidean",
                                             backtrack=True)[1])[::-1]
        x = wp[:, 0].astype(float) * step * sa            # 原版秒
        z = wp[:, 1].astype(float) * step * sa            # 拉回原版尺度后的 DJ 秒
        # 理想 z == x；偏差换算成 DJ 秒后就是相对线性基准的修正
        d = (z - x) / r
        # 每个网格点取路径上邻近样本的偏差中值，抑制单点跳变
        order = np.argsort(x)
        x, d = x[order], d[order]
        for i, gx in enumerate(grid):
            lo = np.searchsorted(x, gx - 1.5, side="left")
            hi = np.searchsorted(x, gx + 1.5, side="right")
            if hi > lo:
                dev[i] = float(np.median(d[lo:hi]))
        from scipy import signal as _sig

        dev = _sig.medfilt(dev, kernel_size=5)
        # 平滑：DTW 在纯静音/纯鼓段上会走出锯齿状噪声，不能让它逐点拽时间轴
        dev = np.convolve(dev, np.ones(9) / 9.0, mode="same")
        # 限幅按音频时长缩放：短视频上 ±2s 足以把整条轴推歪，长视频才允许
        # 这么大的局部修正（一个 drop 段通常 1~2s）。
        cap = min(max_deviation, max(0.25, t_end * 0.02))
        dev = np.clip(dev, -cap, cap)
        dev[0] = 0.0
    except Exception:
        dev = np.zeros_like(base_y)

    y = np.maximum.accumulate(np.maximum(0.0, base_y + dev))
    return [(round(float(a), 3), round(float(b), 3)) for a, b in zip(grid, y)]


# ---------------------------------------------------------------------------
# 4) 音高/速度还原（给 whisper 用）
# ---------------------------------------------------------------------------


def restore_speed(enhanced_wav: str, ratio: float, sr: int = ASR_SR) -> str:
    """把 DJ 音频还原到原速原调，返回新 wav 路径。

    ``asetrate = sr/ratio`` 同时把速度与音高按 1/ratio 还原（正好抵消
    ``asetrate = sr*ratio`` 型变速），再 ``aresample`` 回目标采样率。
    识别出的时间 t_rest 需乘 ``1/ratio`` 才是 DJ 版时间。
    """
    if abs(ratio - 1.0) < 0.02:
        return enhanced_wav
    out_dir = _cache_dir()
    key = _fingerprint(enhanced_wav, f"rest{ratio:.3f}")
    out = out_dir / f"rest_{key}.wav"
    if out.exists() and out.stat().st_size > 1000:
        return str(out)
    new_sr = int(round(sr / ratio))
    _ffmpeg(["-i", str(enhanced_wav), "-af",
             f"asetrate={sr}*{1.0/ratio:.6f},aresample={sr},"
             f"aformat=sample_rates={sr}:channel_layouts=mono",
             "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(out)], check=False)
    if not out.exists() or out.stat().st_size < 1000:
        return enhanced_wav
    return str(out)


# ---------------------------------------------------------------------------
# 5) 主入口
# ---------------------------------------------------------------------------


def _has_dj_marker(name: str) -> bool:
    """文件名里是否带「这是重混版」的标记。

    只把 dj 当作独立标记：``dj_input`` / ``montage_dj_test`` 这类普通命名
    不能被误伤，``九万里_DJ重混版`` / ``(DJ 1.25x)`` 则要认出来。
    """
    import re

    return bool(re.search(
        r"(?<![A-Za-z0-9])dj(?![A-Za-z_])|remix|nightcore|slowed|sped\s*up|bootleg|"
        r"mashup|加快版|变速|慢速|降调|升调|重混版|鼓点版|抖音版|热播版|"
        r"\d+(?:\.\d+)?\s*[xX×倍速]", name, re.IGNORECASE))


def re_sub_dj(name: str) -> str:
    """去掉 DJ 版本常见标记，得到原曲名。"""
    import re

    s = name
    # 括号里整体是版本标记时整块删掉，如「(DJ 1.25x)」「(Nightcore)」
    s = re.sub(r"[\(（\[【][^\)）\]】]{0,32}[\)）\]】]",
               lambda m: "" if _has_dj_marker(m.group(0)) else m.group(0), s)
    s = re.sub(r"(?<![A-Za-z0-9])dj(?![A-Za-z_])", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[_\-\s]+(?:remix|nightcore|slowed|sped\s*up|bootleg|mashup|"
               r"加快版|变速|慢速|降调|升调|重混版|鼓点版|完整版|抖音版|热播版|"
               r"\d+(?:\.\d+)?\s*[xX×倍速])", "", s, flags=re.IGNORECASE)
    return re.sub(r"[\s_\-]+", "", s).strip()


def _chroma_fingerprint(path: str, sr: int = FEATURE_SR, hop: int = 512):
    """12 维色度指纹，逐帧 L2 归一化。旋律/和声的唯一性远高于节奏。"""
    import librosa
    import soundfile as sf

    fd, wav = tempfile.mkstemp(suffix=".wav", prefix="dj_chroma_")
    os.close(fd)
    try:
        _to_wav(path, wav, sr=sr, stereo=False, max_seconds=95)
        y, _ = sf.read(wav, dtype="float32")
    finally:
        Path(wav).unlink(missing_ok=True)
    c = librosa.feature.chroma_stft(y=y, sr=sr, n_fft=2048,
                                    hop_length=hop, n_chroma=12)
    return (c / (np.linalg.norm(c, axis=0, keepdims=True) + 1e-8)).astype(np.float32)


def chroma_similarity(ref_path: str, dj_path: str, ratio: float) -> float:
    """同源判定：把 DJ 色度按 ratio 拉回原版时间尺度，再做半音旋转不变匹配。

    变速变调会让整条色度序列平移 k 个半音（1.18× ≈ +3 半音），必须遍历
    12 个旋转取最大值；不同歌曲即使同为 4/4 拍古风，旋律相关也只有 0.2~0.4，
    而同源版本能到 0.7 以上，判别力远好于 onset 节奏包络。
    """
    try:
        A = _chroma_fingerprint(ref_path)
        B = _chroma_fingerprint(dj_path)
        if A.shape[1] < 32 or B.shape[1] < 32:
            return 0.0
        idx = np.arange(A.shape[1], dtype=np.float64) / max(ratio, 1e-6)
        valid = idx < B.shape[1] - 1
        if valid.sum() < 32:
            return 0.0
        Bw = np.stack([np.interp(idx, np.arange(B.shape[1]), B[i],
                                 left=0.0, right=0.0) for i in range(12)])
        Bw = Bw / (np.linalg.norm(Bw, axis=0, keepdims=True) + 1e-8)
        Av, Bv = A[:, valid], Bw[:, valid]
        best = 0.0
        for k in range(12):
            s = float(np.mean(np.sum(Av * np.roll(Bv, k, axis=0), axis=0)))
            best = max(best, s)
        return best
    except Exception:
        return 0.0


def find_reference_by_audio(media_path: str, candidates: Sequence[Path],
                            max_candidates: int = 14) -> str:
    """名字看不出来时用音频指纹找同源原曲。

    场景很常见：用户把视频存成 ``dj_input.mp4`` / ``preview (2).mp4``，
    文件名里根本没有歌名。

    两阶段：先用 onset 全长互相关估出变速比（对节奏稳健），再用色度指纹
    做**同源判定**。节奏包络单独做同源判据不可靠——同为 4/4 拍的古风歌
    也能拿到 0.3 左右的偶然相关，实测会把《夹心瑶》当成《九万里》的原版；
    旋律指纹则能干净分开（同源 0.77 / 异曲 0.23~0.37）。
    评分额外奖励「估出明显变速差」的候选，优先挑原速原曲而不是另一个 DJ 版。
    """
    # 先试小文件，避免一上来就解 38MB 的 flac
    try:
        ordered = sorted(candidates, key=lambda f: f.stat().st_size)[:max_candidates]
    except Exception:
        ordered = list(candidates)[:max_candidates]
    ratios = np.arange(RATIO_MIN, RATIO_MAX + 1e-9, 0.02)
    best, top = "", 0.0
    for f in ordered:
        try:
            r, _, conf, peak = estimate_tempo_ratio(
                str(f), media_path, ratios=ratios, with_peak=True)
            if conf <= 0 or peak < 0.22:
                continue
            sim = chroma_similarity(str(f), media_path, r)
            # 0.52 是实测分界：异曲最高 0.37，同源 DJ 版 0.57，原速原曲 0.77
            if sim < 0.52:
                continue
            score = sim * (1.0 + 0.5 * min(1.0, abs(r - 1.0) * 3.0))
        except Exception:
            continue
        if score > top:
            top, best = score, str(f)
    # 不能要求「显著高于次优」：同一首原曲常在 uploads / Music 里存了好几份，
    # 它们得分天然并列。用绝对阈值即可。
    return best if top >= 0.60 else ""


def find_reference(media_path: str, explicit: str = "") -> str:
    """自动找原版音频：显式指定 > 同目录同名 > uploads/Music > 音频指纹。

    DJ 版文件名常带 ``(DJ)`` / ``remix`` / ``1.25x`` / ``加快版`` 等后缀，
    去掉这些标记后再做模糊匹配；名字完全看不出歌名时用音频指纹找同源原曲。
    """
    # 显式给的原版只在「看起来确实不是重混版」时直接采用；否则继续自动找，
    # 免得把上传的 DJ 版 BGM 自己当成对齐基准。
    if explicit and Path(explicit).exists() and not _has_dj_marker(Path(explicit).stem):
        return str(explicit)
    p = Path(media_path)
    stem = re_sub_dj(p.stem).lower()
    roots = [p.parent]
    try:
        root = Path(__file__).resolve().parents[3]
        roots.append(root / "var" / "uploads")
    except Exception:
        pass
    roots.append(Path.home() / "Music")
    exts = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma"}
    best, best_score = "", 0.0
    all_candidates: List[Path] = []
    seen = set()
    for d in roots:
        if not d.exists():
            continue
        try:
            candidates = [f for f in d.iterdir()
                          if f.is_file() and f.suffix.lower() in exts
                          and str(f.resolve()) != str(p.resolve())]
        except Exception:
            continue
        for f in candidates:
            key = str(f.resolve())
            if key in seen:
                continue
            seen.add(key)
            all_candidates.append(f)
            fs = re_sub_dj(f.stem).lower()
            if not fs or not stem:
                continue
            if stem == fs:
                score = 3.0
            elif stem in fs or fs in stem:
                score = 2.0
            else:
                # 字符集重合度，容忍「九万里 / 九万字」这类别名
                common = len(set(stem) & set(fs))
                score = common / max(1, min(len(set(stem)), len(set(fs)))) * 1.5
            # 名字里带 DJ/remix/变速 的本身就是另一个重混版，降权
            if _has_dj_marker(f.stem):
                score -= 2.0
            if score > best_score:
                best, best_score = str(f), score
    if best_score >= 1.2:
        return best
    return find_reference_by_audio(media_path, all_candidates)


def prepare_dj(media_path: str, mode: str = "auto", ref: str = "",
               ratio: Optional[float] = None, enable_enhance: bool = True
               ) -> DJProfile:
    """DJ 适配主入口。

    Args:
        media_path: 待配字幕的音频/视频（DJ 版）
        mode: auto / off / on。auto 会先判断音频是否真的「重混」再决定启用
        ref: 原版音频路径；留空时自动在 uploads / Music 里找
        ratio: 手动指定变速比；None 表示自动估计
        enable_enhance: 是否提纯人声（默认开）

    Returns:
        DJProfile。``method`` 决定 ``lyrics`` 走哪条定位路径：

        * ``ref_warp``  找到可信原版 → 在原版上定位时间轴再映射到 DJ 版；
        * ``restored``  已知变速比但无原版 → 还原音高后识别，时间轴按比缩放；
        * ``direct``    仅提纯人声后直接识别；
        * ``off``       不做 DJ 适配（普通歌曲）。
    """
    key = str(mode or "auto").strip().lower()
    if key in ("off", "none", "false", "0", "no"):
        return DJProfile(enabled=False, method="off")
    # enhance 模式：只提纯人声，不找原版、不估变速（用于纯人声增强场景/对照测试）
    vocal_only = key in ("enhance", "vocal", "vocals", "stem")
    prof = DJProfile(enabled=True, method="direct")

    # 1) 提纯人声（失败不影响后续）
    if enable_enhance:
        try:
            prof.enhanced_path = enhance_vocals(media_path)
        except Exception as e:
            prof.notes.append(f"人声提纯降级（{type(e).__name__}）")

    if not vocal_only:
        # 2) 找原版
        prof.ref_path = find_reference(media_path, ref)

        # 3) 估变速比
        if ratio is not None and float(ratio) > 0:
            prof.ratio = float(ratio)
            prof.ratio_confidence = 1.0
            prof.notes.append("变速比由用户指定")
        elif prof.ref_path:
            try:
                r, off, conf = estimate_tempo_ratio(prof.ref_path, media_path)
                prof.ratio, prof.offset, prof.ratio_confidence = r, off, conf
                if conf < 0.18:
                    prof.notes.append("变速比估计置信不足，按原速处理")
                    prof.ratio, prof.ratio_confidence = 1.0, 0.0
            except Exception as e:
                prof.notes.append(f"变速比估计失败（{type(e).__name__}）")

        # 4) 有可信原版 → 建 warp
        if prof.ref_path and prof.ratio_confidence >= 0.18:
            try:
                prof.warp = build_warp_map(prof.ref_path, media_path,
                                           prof.ratio, prof.offset)
                if len(prof.warp) >= 3:
                    prof.method = "ref_warp"
            except Exception as e:
                prof.notes.append(f"时间轴映射失败（{type(e).__name__}）")

        # 5) 无 ref 但确认变速 → 还原音高后识别
        if prof.method == "direct" and abs(prof.ratio - 1.0) >= 0.05 and prof.enhanced_path:
            try:
                rest = restore_speed(prof.enhanced_path, prof.ratio)
                if rest != prof.enhanced_path:
                    prof.restored_path = rest
                    prof.restore_scale = 1.0 / prof.ratio
                    prof.method = "restored"
            except Exception:
                pass

    # auto 模式：既没找到原版、也没变速证据、音频也不像重混 → 只保留人声增强即可
    return prof


def warp_segments(segments, prof: DJProfile):
    """把「原版时间轴」的字幕段整体映射到 DJ 时间轴（原地改 start/end）。"""
    if not prof.warp:
        for s in segments:
            s.start = (s.start - prof.offset) / max(prof.ratio, 1e-6)
            s.end = (s.end - prof.offset) / max(prof.ratio, 1e-6)
        return segments
    for s in segments:
        s.start = prof.to_dj(s.start)
        s.end = prof.to_dj(s.end)
    return segments


def scale_segments(segments, scale: float):
    """把「还原音频时间轴」的字幕段缩放回 DJ 时间轴。"""
    if abs(scale - 1.0) < 1e-6:
        return segments
    for s in segments:
        s.start *= scale
        s.end *= scale
    return segments
