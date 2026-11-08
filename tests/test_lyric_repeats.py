# -*- coding: utf-8 -*-
"""重复演唱段自动补齐 + 片尾音乐渐弱的离线回归测试。

不依赖真实 BGM / 模型：合成「同一段旋律唱三遍」的音频，验证只给一遍歌词时
系统能自动把第二遍、第三遍的时间轴补出来；再验证 BGM 归一化那一遍的尾部
淡出确实生效、且能被关掉。
"""
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packages.subtitle_engine.src.lyrics import (  # noqa: E402
    build_lyric_segments, detect_repeat_offsets, expand_lyric_repeats,
)
from packages.subtitle_engine.src.caption_burner import CaptionSegment  # noqa: E402
from packages.montage.renderer import VideoRenderer, tail_fade_seconds  # noqa: E402

SR = 16000
LRC = "[00:00.00]第一句\n[00:02.00]第二句\n[00:04.00]第三句\n[00:06.00]第四句"
TEXTS = ["第一句", "第二句", "第三句", "第四句"]


def _chord(freqs, seconds=2.0):
    t = np.arange(int(SR * seconds)) / SR
    env = np.minimum(1.0, t / 0.05) * np.minimum(1.0, (seconds - t) / 0.08)
    wave = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)
    return (0.35 * env * wave).astype(np.float32)


def _verse():
    """4 句、每句 2 秒、和弦各不相同，共 8 秒。"""
    return np.concatenate([
        _chord((220, 330, 440)),
        _chord((260, 390, 520)),
        _chord((300, 450, 600)),
        _chord((350, 520, 700)),
    ])


def _three_verses():
    gap = np.zeros(int(SR * 0.5), dtype=np.float32)
    # 尾部留 2s 余量：否则第三遍最后一句的匹配峰正好落在特征序列边界，
    # find_peaks 找不到边界峰，会少补一句（真实成片结尾也常有尾奏）
    tail = np.zeros(int(SR * 2.0), dtype=np.float32)
    return np.concatenate([_verse(), gap, _verse(), gap, _verse(), tail])


def _write_wav(path, y):
    import soundfile as sf
    sf.write(str(path), np.asarray(y, dtype=np.float32), SR)


def _tmp_wav(y):
    fd, p = tempfile.mkstemp(suffix=".wav", prefix="lyric_repeat_")
    os.close(fd)
    _write_wav(p, y)
    return p


def _first_verse_segments():
    return [CaptionSegment(i * 2.0, i * 2.0 + 1.94, t)
            for i, t in enumerate(TEXTS)]


# ---------------------------------------------------------------------------
# 重复段检测
# ---------------------------------------------------------------------------

def test_detect_two_repeats():
    y = _three_verses()
    groups = detect_repeat_offsets(y, SR, _first_verse_segments())
    assert len(groups) == 2, groups
    deltas = sorted(g["delta"] for g in groups)
    assert abs(deltas[0] - 8.5) < 0.6, deltas
    assert abs(deltas[1] - 17.0) < 0.6, deltas
    assert all(g["support"] >= 3 for g in groups), groups


def test_expand_repeats_keeps_order_and_text():
    groups = detect_repeat_offsets(_three_verses(), SR, _first_verse_segments())
    out, added = expand_lyric_repeats(_first_verse_segments(), groups, 25.0)
    assert added == 8, (added, len(out))
    assert len(out) == 12
    assert [s.text for s in out] == TEXTS * 3
    for a, b in zip(out, out[1:]):
        assert a.start < b.start, (a, b)
        assert a.end <= b.start + 1e-6, (a, b)


def test_build_auto_expands_to_three_verses():
    p = _tmp_wav(_three_verses())
    try:
        segs, report = build_lyric_segments(p, LRC, offset=0.0, repeat="auto")
        assert report.method == "lrc", report.summary()
        assert len(segs) == 12, [(s.start, s.text) for s in segs]
        assert report.repeat_added == 8, report.summary()
        assert len(report.repeats) == 2, report.repeats
        assert "重复段" in report.summary()
        for a, b in zip(segs, segs[1:]):
            assert a.end <= b.start + 1e-6, (a, b)
    finally:
        Path(p).unlink(missing_ok=True)


def test_off_disables_expansion():
    p = _tmp_wav(_three_verses())
    try:
        segs, report = build_lyric_segments(p, LRC, offset=0.0, repeat="off")
        assert len(segs) == 4, len(segs)
        assert report.repeat_added == 0
    finally:
        Path(p).unlink(missing_ok=True)


def test_no_repeat_no_false_positive():
    """只有一遍 + 4 秒静音：不该凭空补出第二遍。"""
    p = _tmp_wav(np.concatenate([
        _verse(), np.zeros(int(SR * 4.0), dtype=np.float32)]))
    try:
        segs, report = build_lyric_segments(p, LRC, offset=0.0, repeat="auto")
        assert len(segs) == 4, [(s.start, s.text) for s in segs]
        assert report.repeat_added == 0
    finally:
        Path(p).unlink(missing_ok=True)


def test_full_timeline_not_double_expanded():
    """LRC 本来就写满了整条音轨（含重复段）→ 不再复制。"""
    p = _tmp_wav(_three_verses())
    try:
        full_lrc = ("[00:00.00]第一句\n[00:02.00]第二句\n[00:04.00]第三句\n"
                    "[00:06.00]第四句\n[00:08.50]第一句\n[00:10.50]第二句\n"
                    "[00:12.50]第三句\n[00:14.50]第四句\n[00:17.00]第一句\n"
                    "[00:19.00]第二句\n[00:21.00]第三句\n[00:23.00]第四句")
        segs, report = build_lyric_segments(p, full_lrc, offset=0.0, repeat="auto")
        assert len(segs) == 12, len(segs)
        assert report.repeat_added == 0, report.summary()
    finally:
        Path(p).unlink(missing_ok=True)


def test_lrc_second_verse_not_duplicated():
    """LRC 已写了前两遍时，只补没写的第三遍，不把已有句子再叠一遍。"""
    p = _tmp_wav(_three_verses())
    try:
        lrc = ("[00:00.00]第一句\n[00:02.00]第二句\n[00:04.00]第三句\n"
               "[00:06.00]第四句\n[00:08.50]第一句\n[00:10.50]第二句\n"
               "[00:12.50]第三句\n[00:14.50]第四句")
        segs, report = build_lyric_segments(p, lrc, offset=0.0, repeat="auto")
        assert len(segs) == 12, [(s.start, s.text) for s in segs]
        assert report.repeat_added == 4, report.summary()
    finally:
        Path(p).unlink(missing_ok=True)


def test_numeric_cap_limits_repeats():
    p = _tmp_wav(_three_verses())
    try:
        segs, report = build_lyric_segments(p, LRC, offset=0.0, repeat="1")
        assert len(segs) == 8, len(segs)
        assert report.repeat_added == 4
    finally:
        Path(p).unlink(missing_ok=True)


def test_align_trims_repeated_asr_to_first_verse():
    """识别把同一段唱词念了三遍 → 对齐只发生在第一遍，不被摊到整首歌。"""
    words = ["第一句歌词", "第二句歌词", "第三句歌词", "第四句歌词"]
    raw = []
    t = 0.0
    for _ in range(3):
        for txt in words:
            ws = [{"word": ch, "start": t + j * 0.3, "end": t + (j + 1) * 0.3}
                  for j, ch in enumerate(txt)]
            raw.append({"start": t, "end": t + 1.5, "text": txt, "words": ws})
            t += 2.0
    segs, report = build_lyric_segments(
        "不存在.mp4", "\n".join(words), duration=24.0,
        transcribe_fn=lambda size: raw,
    )
    assert report.method == "align", report.summary()
    assert len(segs) == 4, [(s.start, s.text) for s in segs]
    assert segs[-1].end < 12.0, [(s.start, s.end) for s in segs]
    assert "第一遍" in report.summary()


# ---------------------------------------------------------------------------
# 片尾渐弱
# ---------------------------------------------------------------------------

def test_tail_fade_seconds():
    assert tail_fade_seconds(60.0, "auto") == 4.2
    assert abs(tail_fade_seconds(30.0, "auto") - 2.1) < 1e-6
    assert tail_fade_seconds(10.0, "auto") == 2.0
    assert tail_fade_seconds(120.0, "auto") == 4.5
    assert tail_fade_seconds(60.0, "off") == 0.0
    assert tail_fade_seconds(60.0, "3") == 3.0
    assert tail_fade_seconds(60.0, "999") == 27.0   # 不超过时长 45%
    assert tail_fade_seconds(0.0, "auto") == 0.0


def test_normalize_bgm_tail_fade_is_audible():
    """真跑一遍 ffmpeg：淡出开启时片尾能量应显著低于中段，关闭时则不该衰减。"""
    import soundfile as sf
    import subprocess

    t = np.arange(int(SR * 10.0)) / SR
    tone = (0.3 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    src = _tmp_wav(tone)
    out_on = str(Path(tempfile.gettempdir()) / "fade_on.wav")
    out_off = str(Path(tempfile.gettempdir()) / "fade_off.wav")
    renderer = VideoRenderer(output_dir=tempfile.gettempdir())
    try:
        renderer._normalize_bgm(src, out_on, duration=10.0, fade_out="3")
        renderer._normalize_bgm(src, out_off, duration=10.0, fade_out="off")

        def rms(path, a, b):
            data, rate = sf.read(path)
            seg = data[int(a * rate):int(b * rate)]
            return float(np.sqrt(np.mean(np.asarray(seg, dtype=np.float64) ** 2)))

        mid_on, tail_on = rms(out_on, 5.0, 6.0), rms(out_on, 9.6, 10.0)
        mid_off, tail_off = rms(out_off, 5.0, 6.0), rms(out_off, 9.6, 10.0)
        assert mid_on > 1e-4, (mid_on, tail_on)
        assert tail_on < mid_on * 0.35, (mid_on, tail_on)
        assert tail_off > mid_off * 0.6, (mid_off, tail_off)
    finally:
        for p in (src, out_on, out_off):
            Path(p).unlink(missing_ok=True)


def test_burn_pipeline_passes_repeat_through():
    """参数从 WebUI 一路传到歌词引擎，不能中途丢掉。"""
    from packages.subtitle_engine.src.burn_pipeline import _lyric_segments
    p = _tmp_wav(_three_verses())
    try:
        segs, rep = _lyric_segments(p, LRC, "base", "zh", 0.0, repeat="auto")
        assert len(segs) == 12, len(segs)
        assert rep.repeat_added == 8
        segs2, rep2 = _lyric_segments(p, LRC, "base", "zh", 0.0, repeat="off")
        assert len(segs2) == 4 and rep2.repeat_added == 0
    finally:
        Path(p).unlink(missing_ok=True)


def test_wiring_source_no_typo():
    """静态核对各层参数名，防止改了一头忘了另一头。"""
    def read(rel):
        return (ROOT / rel).read_text(encoding="utf-8")

    app = read("packages/webui/src/app.py")
    pipe = read("packages/montage/pipeline.py")
    rend = read("packages/montage/renderer.py")
    burn = read("packages/subtitle_engine/src/burn_pipeline.py")
    lyrics = read("packages/subtitle_engine/src/lyrics.py")
    assert "bgm_fade_out=bgm_fade_out" in app
    assert '"bgm_fade_out"' in app and '"lyric_repeat"' in app
    assert "bgm_fade_out: str" in pipe and "bgm_fade_out=bgm_fade_out" in pipe
    assert "fade_out=bgm_fade_out" in rend and "fade_out: str" in rend
    assert "lyric_repeat=lyric_repeat" in app
    assert "repeat=lyric_repeat" in burn
    assert "_apply_repeats(base, report, media_path, duration" in lyrics


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
