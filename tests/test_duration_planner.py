"""时长规划回归测试。

覆盖用户遇到的两个问题：
- 填了 60s / 600s，成片却只有 6~10s；
- 每次剪出来画面一样（现在从镜头内部随机取子区间）。
"""

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packages.core_types.models import Beat, Shot  # noqa: E402
from packages.montage.sync import BeatSyncEngine  # noqa: E402


def _beats(seconds: float, interval: float = 0.5):
    n = int(seconds / interval) + 2
    return [Beat(time=i * interval, strength=0.8,
                 beat_type="strong" if i % 4 == 0 else "normal")
            for i in range(n)]


def _shots(count: int, duration: float, score: float = 0.5):
    return [
        Shot(
            shot_id=i,
            start_time=i * duration,
            end_time=(i + 1) * duration,
            duration=duration,
            file_path=f"shot_{i}.mp4",
            source_video="source.mp4",
            highlight_score=score,
        )
        for i in range(count)
    ]


def test_target_60s_with_enough_footage():
    shots = _shots(21, 5.0)          # 105s 素材
    timeline = BeatSyncEngine().sync(
        shots, _beats(70), target_duration=60, allow_reuse=False,
        rng=random.Random(7),
    )
    total = sum(e.duration for e in timeline)
    assert 57 <= total <= 63, total
    assert len(timeline) >= 15
    for entry in timeline:
        assert 0 <= entry.source_in < entry.source_out <= 5.0001
        assert entry.source_out - entry.source_in >= 0.1


def test_target_600s_reuses_when_footage_short():
    shots = _shots(21, 5.0)          # 105s 素材
    timeline = BeatSyncEngine().sync(
        shots, _beats(700), target_duration=600, allow_reuse=True,
        rng=random.Random(11),
    )
    total = sum(e.duration for e in timeline)
    assert 570 <= total <= 630, total
    assert len({e.shot_id for e in timeline}) < len(timeline)


def test_target_long_with_enough_footage_no_reuse():
    shots = _shots(80, 8.0)          # 640s 素材
    timeline = BeatSyncEngine().sync(
        shots, _beats(700), target_duration=600, allow_reuse=True,
        rng=random.Random(3),
    )
    total = sum(e.duration for e in timeline)
    assert 570 <= total <= 630, total
    assert len({e.shot_id for e in timeline}) == len(timeline)


def test_source_windows_are_random_between_runs():
    shots = _shots(10, 12.0)
    a = BeatSyncEngine().sync(shots, _beats(40), target_duration=30,
                              rng=random.Random(1))
    b = BeatSyncEngine().sync(shots, _beats(40), target_duration=30,
                              rng=random.Random(2))
    assert [e.source_in for e in a] != [e.source_in for e in b]


def test_no_target_keeps_legacy_beat_path():
    shots = _shots(5, 4.0)
    timeline = BeatSyncEngine().sync(shots, _beats(20))
    assert timeline
    assert sum(e.duration for e in timeline) > 0
