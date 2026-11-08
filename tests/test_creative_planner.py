"""动态混剪规划器回归测试。

覆盖：
- 60 秒目标能排出 40+ 刀，而不是旧版的十几刀；
- 总时长贴近目标；
- 子窗口不越界；
- 静态黑标题卡不会被当成开场镜头；
- 高能段会产生推镜/白闪。
"""

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packages.core_types.models import Beat, Shot  # noqa: E402
from packages.montage.creative import plan_dynamic  # noqa: E402


def _shots():
    # 第 0 个是黑标题卡：运动≈0，不能被选为开场
    durations = [2.5, 4.6, 2.4, 2.3, 1.7, 2.2, 3.1, 0.6, 1.1,
                 1.8, 2.0, 0.9, 1.3, 1.1, 2.9, 10.5, 3.7, 27.4]
    shots = []
    start = 0.0
    for i, dur in enumerate(durations):
        motion = 0.003 if i == 0 else 1.0 + (i % 7) * 0.9
        shots.append(Shot(
            shot_id=i, start_time=start, end_time=start + dur,
            duration=dur, file_path=f"shot_{i}.mp4",
            source_video="source.mp4",
            highlight_score=0.30 if i == 0 else 0.45 + 0.03 * (i % 8),
            motion_score=motion,
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


def _many_shots(count=70, dur=6.0):
    """素材充足的情况：够多的独立镜头，用来验证"刀数够多且不重复"。"""
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


def _energy(seconds=60):
    # 前段铺垫、后段高能，模拟真实 BGM 的能量曲线
    return [
        {"time": i * 0.5, "energy": min(1.0, 0.25 + 0.55 * (i / 120))}
        for i in range(int(seconds * 2) + 2)
    ]


def test_60s_has_many_cuts_and_hits_target():
    # 素材充足（70 个镜头）→ 节奏仍是密集混剪
    timeline, stats = plan_dynamic(
        _many_shots(), _beats(), 60, rng=random.Random(7),
        energy_curve=_energy(),
    )
    total = sum(e.duration for e in timeline)
    assert 35 <= len(timeline) <= 85, len(timeline)
    assert 58.5 <= total <= 61.0, total
    assert stats["cuts"] == len(timeline) - 1
    assert stats["effects"]["punch"] >= 1
    assert stats["effects"]["flash"] >= 1


def test_no_shot_is_reused_when_supply_is_enough():
    """素材够时必须"每个镜头只用一次"，不能靠重播同一段填时长。"""
    timeline, stats = plan_dynamic(
        _many_shots(70), _beats(), 60, rng=random.Random(5),
        energy_curve=_energy(),
    )
    ids = [e.shot_id for e in timeline]
    assert len(ids) == len(set(ids)), f"有镜头被重复使用：{len(ids)} 段 / {len(set(ids))} 个镜头"
    assert stats["repeat_seconds"] == 0.0


def test_short_supply_slows_down_instead_of_repeating():
    """素材不够时应该"每刀更长、刀数更少"，而不是把同一段画面反复播。

    18 个镜头撑 60 秒：旧实现会硬排 40+ 刀 → 每个镜头平均播 2~3 次
    （用户实测 60 秒里约 30 秒在重复）；新实现会把节奏拉长。
    """
    timeline, stats = plan_dynamic(
        _shots(), _beats(), 60, rng=random.Random(7),
        energy_curve=_energy(),
    )
    total = sum(e.duration for e in timeline)
    assert 58.5 <= total <= 61.0, total
    assert stats["stretch"] > 1.0
    # 这组素材里只有 11 个镜头长到能撑住一刀（另有 3 个 1 秒以下的短镜头、
    # 1 个黑标题卡），11 × 最长 4.7s ≈ 52s < 60s，物理上必然有几刀复用。
    # 要求是"明显变少"：旧实现这里约 34s（57%），现在应压到 20% 以内。
    assert stats["repeat_seconds"] <= total * 0.20, stats["repeat_seconds"]


def test_windows_of_same_shot_do_not_overlap():
    """同一镜头的多个入选区间不能在源上重叠（重叠 = 成片里同样画面播两遍）"""
    timeline, _ = plan_dynamic(
        _many_shots(70), _beats(), 60, rng=random.Random(9),
        energy_curve=_energy(),
    )
    by_shot = {}
    for e in timeline:
        by_shot.setdefault(e.shot_id, []).append((e.source_in, e.source_out))
    for sid, ivs in by_shot.items():
        ivs.sort()
        for a, b in zip(ivs, ivs[1:]):
            assert a[1] <= b[0] + 1e-6, f"shot {sid} 的区间重叠：{a} vs {b}"


def test_static_title_card_not_used_as_opener():
    timeline, _ = plan_dynamic(
        _shots(), _beats(), 60, rng=random.Random(11),
        energy_curve=_energy(),
    )
    assert timeline
    assert timeline[0].shot_id != 0, "黑标题卡被选成开场"


def test_source_windows_stay_inside_shot():
    shots = _shots()
    durations = {s.shot_id: s.duration for s in shots}
    timeline, _ = plan_dynamic(
        shots, _beats(), 60, rng=random.Random(3),
        energy_curve=_energy(),
    )
    for entry in timeline:
        assert entry.source_in >= -1e-6
        assert entry.source_out <= durations[entry.shot_id] + 1e-6
        assert entry.source_out > entry.source_in


def test_calm_style_is_not_forced_dynamic():
    # calm 不走这个规划器；这里只验证规划器本身可被调用而不报错
    timeline, stats = plan_dynamic(
        _shots(), _beats(30), 20, style="calm",
        rng=random.Random(2), energy_curve=_energy(30),
    )
    assert timeline
    assert stats["actual"] > 0


def test_cut_hints_pull_shot_boundaries():
    """歌词换句点会吸附切点：给了 hints 后，边界应明显更贴近 3.0 / 6.0 秒。"""
    shots = _shots()
    beats = _beats()

    def dist_to_hints(tl, hints):
        ends = [e.end_time for e in tl[:-1]]
        return sum(min(abs(t - h) for h in hints) for t in ends) / max(1, len(ends))

    hints = [3.0, 6.0, 9.0]
    tl_plain, _ = plan_dynamic(shots, beats, 12, rng=random.Random(7),
                               energy_curve=_energy())
    tl_hint, stats = plan_dynamic(shots, beats, 12, rng=random.Random(7),
                                  energy_curve=_energy(), cut_hints=hints)
    assert tl_hint, "带 hints 也要能排出时间线"
    assert stats["hint_snaps"] > 0, "一个切点都没吸附上"
    assert dist_to_hints(tl_hint, hints) <= dist_to_hints(tl_plain, hints) + 1e-6
