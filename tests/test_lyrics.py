"""歌词字幕链路的离线回归测试。

不依赖音频/模型：whisper 识别结果用假数据注入，重点验证
- LRC / 纯文本 / 元数据 / 多时间戳的解析；
- 歌词文本 + 识别时间轴的序列对齐（识别有错字也要能对上）；
- 识别为空时按人声段 + 字数分配，不会整段空白；
- 收口后不重叠、不越界、不出现 0 长度段。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packages.subtitle_engine.src.lyrics import (  # noqa: E402
    AlignReport, _finalize, _spread_lines, build_lyric_segments, parse_lyrics,
    LyricLine,
)
from packages.subtitle_engine.src.caption_burner import CaptionSegment  # noqa: E402


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def test_parse_lrc_basic():
    text = """[ti:九万字]
[ar:黄诗扶]
[00:00.00]有谁不是 少年热诚
[00:04.30]孑然一身 爱一个人
[00:08.15]望尽了毕生温柔眼神
"""
    lines = parse_lyrics(text)
    assert len(lines) == 3, lines
    assert lines[0].text == "有谁不是 少年热诚"
    assert abs(lines[1].start - 4.30) < 1e-6
    assert abs(lines[2].start - 8.15) < 1e-6


def test_parse_lrc_offset_and_multistamp():
    text = """[offset:+300]
[00:01.00][00:05.00]重复的一句
[00:10.5]后半句
"""
    lines = parse_lyrics(text)
    # offset +300ms 应用到所有时间；多时间戳展开成两行
    assert len(lines) == 3, [(l.start, l.text) for l in lines]
    assert abs(lines[0].start - 1.3) < 1e-6
    assert abs(lines[1].start - 5.3) < 1e-6
    assert abs(lines[2].start - 10.8) < 1e-6


def test_parse_plain_skips_marks_and_blank():
    text = """[ti:某歌]
[Chorus]
有谁不是 少年热诚

孑然一身 爱一个人

（副歌）
望尽了毕生温柔眼神
"""
    lines = parse_lyrics(text)
    assert [l.text for l in lines] == [
        "有谁不是 少年热诚", "孑然一身 爱一个人", "望尽了毕生温柔眼神"]
    assert all(l.start is None for l in lines)


# ---------------------------------------------------------------------------
# 对齐：假识别结果（故意带错字）
# ---------------------------------------------------------------------------

def _fake_asr():
    """模拟 whisper 的词级输出：时间对，但有谐音错字、漏字。"""
    segs = []
    t = 1.0
    for text, dur in [
        ("有谁不是少年熱成", 2.4),   # 热诚 -> 熱成
        ("孑然一身爱一个人", 2.6),
        ("望尽了毕生温柔眼神", 3.0),
        ("写得出最刻薄的字文", 2.8),
    ]:
        step = dur / len(text)
        words = [{"word": ch, "start": t + i * step, "end": t + (i + 1) * step}
                 for i, ch in enumerate(text)]
        segs.append({"start": t, "end": t + dur, "text": text, "words": words})
        t += dur + 0.5
    return segs


def test_align_uses_lyric_text_and_asr_time():
    text = "有谁不是 少年热诚\n孑然一身 爱一个人\n望尽了毕生温柔眼神\n写得出最刻薄的字文"
    segs, report = build_lyric_segments(
        "不存在.mp4", text, duration=14.0,
        transcribe_fn=lambda size: _fake_asr(),
    )
    assert report.method == "align", report.summary()
    assert report.matched >= 3, report.matched
    # 文字必须是歌词原文（不是识别出来的错字）
    assert [s.text for s in segs] == text.split("\n")
    # 时间轴应贴着识别结果
    assert abs(segs[0].start - 1.0) < 0.35, segs[0].start
    assert abs(segs[1].start - 3.9) < 0.45, segs[1].start
    # 不重叠、递增
    for a, b in zip(segs, segs[1:]):
        assert a.start < b.start
        assert a.end <= b.start + 1e-6, (a.end, b.start)


def test_align_falls_back_to_spread_when_asr_garbage():
    """识别内容与歌词完全无关（纯音乐幻觉）→ 走 spread，不硬套时间。"""
    garbage = [{"start": 0.0, "end": 10.0, "text": " thanks for watching ",
                "words": [{"word": "thanks", "start": 0.0, "end": 1.0},
                          {"word": "for", "start": 1.0, "end": 2.0}]}]
    text = "\n".join(["第一句歌词啊啊啊", "第二句歌词啊啊啊", "第三句歌词啊啊啊"])
    segs, report = build_lyric_segments(
        "不存在.mp4", text, duration=30.0,
        transcribe_fn=lambda size: garbage,
    )
    assert report.method in ("spread", "uniform"), report.summary()
    assert len(segs) == 3
    for a, b in zip(segs, segs[1:]):
        assert a.end <= b.start + 1e-6


def test_spread_fills_voice_regions_not_silence():
    """人声段 5~25s，前面 5 秒是前奏：歌词不能被塞进前奏。"""
    lines = [LyricLine(f"第{i}句歌词内容") for i in range(6)]
    spans = _spread_lines(lines, [(5.0, 25.0)], duration=30.0)
    assert spans[0][0] >= 4.9, spans
    assert spans[-1][1] <= 25.2, spans
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert e1 <= s2 + 1e-6


def test_lrc_segments_end_before_next_line():
    text = "[00:00.00]第一句\n[00:05.00]第二句\n[00:09.00]第三句"
    segs, report = build_lyric_segments(
        "不存在.mp4", text, duration=20.0, offset=0.0,
        transcribe_fn=lambda size: [],
    )
    assert report.method == "lrc"
    assert segs[0].end <= segs[1].start
    assert segs[-1].end > segs[-1].start


def test_finalize_no_overlap_no_zero_length():
    segs = [
        CaptionSegment(1.0, 5.0, "a"),
        CaptionSegment(4.0, 4.2, "b"),   # 与上一句重叠且过短
        CaptionSegment(30.0, 40.0, "c"),  # 超出时长
    ]
    out = _finalize(segs, duration=12.0)
    assert out[0].end <= out[1].start + 1e-6
    for s in out:
        assert s.end > s.start
        assert s.end <= 12.0


def test_report_summary_text():
    r = AlignReport(method="align", matched=7, total=8, similarity=0.71)
    assert "7/8" in r.summary() and "71%" in r.summary()
    r2 = AlignReport(method="spread", total=5, notes=["按 3 段有声区间分配"])
    assert "人声段" in r2.summary()


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
