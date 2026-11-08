# -*- coding: utf-8 -*-
"""歌词时间轴「铺满演唱跨度 / 跳过未演唱歌词」的回归测试。

真实事故：60s 成片实际唱到 56.4s，但 spread 按 0.30s/字 估算把
17 句歌词收口到 42.9s，末尾 17 秒完全没有字幕；同时用户贴了整首歌
的歌词，开头 3 句念白在成片里根本没唱，align 却把它们塞进 0 秒
闪一下，并把整条时间轴挤偏。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "subtitle_engine"))
sys.path.insert(0, str(ROOT))

from src.lyrics import (  # noqa: E402
    _spread_lines, build_lyric_segments, parse_lyrics,
)

TEXT17 = ("飘泊的雪 摇曳回风\n诗意灵魂 更迭情人\n总惯用轻浮的茂盛 掩抹深沉\n"
          "有谁不是 少年热诚\n孑然一身 爱一个人\n望尽了毕生温柔眼神\n"
          "写得出最刻薄的字文\n以讥诮这庸尘\n却不忍 斥你毫分\n"
          "我也算万种风情 实非良人\n谁能有幸 错付终身\n最先动情的人\n"
          "剥去利刃 沦为人臣\n我爱你苍凉双眼 明月星辰\n不远万里 叩入心门\n"
          "一个孤僻的唇\n摘获了你首肯 献上一吻")


def test_spread_uses_full_vocal_span():
    """人声跨度 56s > 字数估算 42s 时，末句必须落到 50s 之后。"""
    lines = parse_lyrics(TEXT17)
    spans = _spread_lines(lines, [(0.0, 56.36)], 60.0)
    assert len(spans) == 17
    assert spans[-1][1] >= 50.0, spans[-1]
    assert spans[0][0] < 1.0


def test_spread_still_trims_long_instrumental_tail():
    """真的带 90s 长尾奏时仍要收口，不能把歌词摊满整轨。"""
    lines = parse_lyrics(TEXT17)
    spans = _spread_lines(lines, [(0.0, 90.0)], 90.0)
    assert spans[-1][1] < 70.0, spans[-1]


def _fake_word_asr(texts, step=0.35):
    """把若干句歌词摊成词级识别结果，每字一个 word。"""
    segs, t = [], 0.0
    for text in texts:
        words = []
        for ch in text:
            if ch.isspace():
                continue
            words.append({"start": round(t, 2), "end": round(t + step, 2), "word": ch})
            t += step
        segs.append({"start": words[0]["start"], "end": words[-1]["end"],
                     "text": text, "words": words})
    return segs


def test_align_skips_unsung_head_lines():
    """用户贴整首歌歌词、成片只唱后 14 句时，开头 3 句不该生成字幕。"""
    lines = TEXT17.splitlines()
    sung = ["".join(s.split()) for s in lines[3:]]
    asr = _fake_word_asr(sung)
    segs, report = build_lyric_segments(
        "nonexistent-media.mp4", TEXT17, duration=52.0,
        transcribe_fn=lambda m: asr, repeat="off")
    assert report.method == "align", report.summary()
    assert report.unsung == 3, report.summary()
    assert len(segs) == 14, [(s.start, s.text) for s in segs]
    assert segs[0].text == "有谁不是 少年热诚"
    assert "跳过 3 句" in report.summary()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("  ok  ", fn.__name__)
        except Exception as e:
            failed += 1
            print("  FAIL", fn.__name__, type(e).__name__, e)
    print(f"{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
