"""歌词尾部空档回归测试。

场景：whisper 只识别出前 12 句，结尾 5 句一句都没吐出来（片尾拖腔/
渐弱段常见）。旧逻辑把「结尾连续 ≥2 行未匹配」判成未演唱直接丢掉，
成片后半段字幕整段消失；现在要按人声跨度补回，并把末句留到渐弱结束。
"""

import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packages.subtitle_engine.src.lyrics import build_lyric_segments  # noqa: E402

DURATION = 20.0
LYRICS = "\n".join(f"第{i}句歌词内容测试" for i in range(1, 18))


def _make_wav(path: Path):
    """20 秒持续有声、最后 2 秒渐弱的模拟人声（不依赖任何真实素材）。"""
    sr = 16000
    t = np.linspace(0, DURATION, int(sr * DURATION), endpoint=False)
    rng = np.random.default_rng(7)
    base = (0.12 * np.sin(2 * np.pi * 220 * t)
            + 0.08 * np.sin(2 * np.pi * 330 * t))
    mod = 0.55 + 0.45 * np.sin(2 * np.pi * 2.1 * t)
    y = base * mod + 0.015 * rng.standard_normal(len(t))
    fade = np.ones_like(y)
    n = int(2 * sr)
    fade[-n:] = np.linspace(1.0, 0.18, n)
    y = np.clip(y * fade, -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype("<i2").tobytes())


def _fake_asr(_model_size):
    """只识别前 12 句，后 5 句完全缺失。"""
    segs = []
    for i, line in enumerate(LYRICS.splitlines()[:12]):
        s = 0.3 + i * 1.02
        segs.append({"text": line, "start": s, "end": s + 0.88})
    return segs


def test_tail_lines_are_rescued_and_last_line_reaches_outro():
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "vocal.wav"
        _make_wav(wav)
        segs, rep = build_lyric_segments(
            str(wav), LYRICS, duration=DURATION,
            transcribe_fn=_fake_asr, dj="off", repeat="off",
        )

    assert rep.method == "align", rep.summary()
    assert len(segs) == 17, f"结尾 5 句被丢了：{len(segs)} 句 / {rep.summary()}"
    assert rep.unsung == 0, rep.summary()
    assert any("尾部" in n for n in rep.notes), rep.notes
    # 后半段不能出现 3 秒以上的字幕空档
    gaps = [b.start - a.end for a, b in zip(segs, segs[1:])]
    assert max(gaps) < 3.0, f"仍有字幕空档 {max(gaps):.2f}s"
    assert segs[-1].end >= DURATION - 1.0, \
        f"末句没留到片尾渐弱结束：{segs[-1].end:.2f}s"
    tail = segs[12:]
    assert all(a.start < b.start for a, b in zip(tail, tail[1:]))


if __name__ == "__main__":
    test_tail_lines_are_rescued_and_last_line_reaches_outro()
    print("PASS test_tail_lines_are_rescued_and_last_line_reaches_outro")
