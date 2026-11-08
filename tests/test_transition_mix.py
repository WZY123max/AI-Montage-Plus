"""转场多样化回归测试。

覆盖这次修复的三个要点：
- 转场模板（auto / smart_mix / hard_cut）真的传进规划器并生效；
- 默认智能混搭能产出 6 种以上不同 xfade，且保留硬切、白闪只是点缀；
- 转场时长按高级设置走（默认 0.40s），不再被压到 0.18s 以内。
"""

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packages.core_types.models import Beat, Shot  # noqa: E402
from packages.montage.creative import plan_dynamic  # noqa: E402
from packages.montage_engine.src.transition_patterns import get_pattern  # noqa: E402


def _shots(count=80, dur=6.0):
    shots = []
    start = 0.0
    for i in range(count):
        shots.append(Shot(
            shot_id=i, start_time=start, end_time=start + dur,
            duration=dur, file_path=f"shot_{i}.mp4",
            source_video="source.mp4",
            highlight_score=0.45 + 0.03 * (i % 8),
            motion_score=1.0 + (i % 7) * 0.9,
        ))
        start += dur
    return shots


def _beats(seconds=70, interval=0.465):
    n = int(seconds / interval) + 2
    return [
        Beat(time=i * interval, strength=0.8,
             beat_type="strong" if i % 4 == 0 else "normal")
        for i in range(n)
    ]


def _plan(pattern=None, **kw):
    return plan_dynamic(
        _shots(), _beats(), 60.0, rng=random.Random(11),
        energy_curve=[{"time": 0.0, "energy": 0.5},
                      {"time": 30.0, "energy": 0.8},
                      {"time": 60.0, "energy": 0.5}],
        transition_pattern=pattern if pattern is not None else {},
        **kw,
    )


def test_auto_template_produces_many_transition_types():
    tl, st = _plan({"id": "auto", "transition_length": 0.40,
                    "transition_density": 0.60, "transition_flash": "auto"})
    assert st["cuts"] >= 35
    assert st["transition_types"] >= 6, st["transitions"]
    assert st["transition_cuts"] >= 18, st["transitions"]
    assert st["transitions"].get("cut", 0) >= 8, "硬切要保留，不能全是叠化"
    assert max(e.transition_duration for e in tl) <= 0.401
    assert any(abs(e.transition_duration - 0.40) < 0.001 for e in tl), \
        "默认 0.40s 转场必须真的出现在时间线上"


def test_smart_mix_template_and_custom_length():
    pattern = dict(get_pattern("smart_mix"),
                   transition_length=0.55, transition_density=0.80)
    tl, st = _plan(pattern)
    assert st["transition_types"] >= 6
    assert st["transition_cuts"] >= 28
    assert max(e.transition_duration for e in tl) <= 0.551
    assert any(abs(e.transition_duration - 0.55) < 0.001 for e in tl)


def test_hard_cut_template_keeps_all_cuts():
    tl, st = _plan({"id": "hard_cut", "transition_length": 0.40,
                    "transition_density": 1.0})
    assert all(e.transition_type == "cut" for e in tl)
    assert st["transition_cuts"] == 0


def test_no_consecutive_repeated_effect():
    tl, _ = _plan({"id": "auto"})
    seq = [e.transition_type for e in tl]
    effects = [t for t in seq if t != "cut"]
    assert all(a != b for a, b in zip(effects, effects[1:])), \
        "相邻两个转场不能是同一种效果"


def test_flash_is_only_an_accent():
    tl, st = _plan({"id": "auto", "transition_flash": "auto"})
    assert st["effects"]["flash"] <= max(3, len(tl) // 6), \
        "白闪不该再是唯一的转场观感"
    tl_off, st_off = _plan({"id": "auto", "transition_flash": "off"})
    assert st_off["effects"]["flash"] == 0


def test_timeline_duration_unchanged():
    tl, st = _plan({"id": "auto", "transition_length": 0.70,
                    "transition_density": 1.0})
    assert abs(st["actual"] - 60.0) < 0.6


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
