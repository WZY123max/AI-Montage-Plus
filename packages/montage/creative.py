"""节拍驱动的动态混剪规划器。

旧版按“每个镜头 1~4 拍”顺序排，60 秒只有十几刀，观感像“截一段 + 配乐”。
这里改成真正的混剪节奏：

- 用 BGM 的真实节拍时间当切点，不再用中位拍间隔估算；
- 强拍/高能量段用 1~2 拍短切，弱拍/低能量段用 3~4 拍长切；
- 每个镜头提前拆成多个**互不重叠**的"子窗口"，同一素材可以取不同入点；
- 默认**每个镜头只用一次**（max_shot_uses=1），素材不够才逐级放宽，
  并把"重复了多少秒"记进 stats，由 pipeline 明确告警；
- 高能段随机加推镜（punch）和白闪（flash，只做少量点缀）；
- **转场模板真正生效**：按节拍强弱从模板效果池里轮换 8~10 种 xfade，
  其余切点保留硬切；时长/密度/白闪比例可由高级设置调整；
- 按 BGM 能量曲线选镜头运动量：高潮用高运动镜头，铺垫用平稳镜头。

返回的 `TimelineEntry` 多了 `effect` 字段，渲染器负责实际效果。
"""

from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from packages.core_types.models import Beat, Shot, TimelineEntry


@dataclass
class _Window:
    """镜头内部的一个候选子窗口。"""

    shot_id: int
    path: str
    source_in: float
    source_out: float
    motion: float
    score: float

    @property
    def length(self) -> float:
        return max(0.0, self.source_out - self.source_in)


# ---------------------------------------------------------------------------
# 转场效果池
# ---------------------------------------------------------------------------
# 都是 ffmpeg 7.x 的 xfade 实名（renderer._xfade_segment 里有完整映射）。
# 旧版只产出 cut / fade / dissolve / wipeleft，而且 90% 是硬切，高能段靠
# 镜头开头 0.10s 白闪撑节奏 —— 观感就是「转场只有快闪一种」。
DEFAULT_TRANSITION_SECONDS = 0.40   # 用户确认的「明显但不拖沓」默认时长
DEFAULT_TRANSITION_DENSITY = 0.60   # 约 6 成切点用转场，4 成保留硬切

SMART_MIX_STRONG = (
    "fadewhite", "wipeleft", "wiperight", "slideleft", "slideright",
    "zoomin", "pixelize", "diagtl", "wipeup", "circleopen",
)
SMART_MIX_WEAK = (
    "fade", "dissolve", "wipeup", "wipedown", "smoothleft",
    "smoothright", "circleopen", "radial", "smoothup", "vertopen",
)

# 模板里的「虚拟效果名」→ 真实 xfade。fadeblack/circlecrop 等会造黑场的
# 一律换成安全效果；flash 交给 fadewhite，不要再映射成 fadeblack。
_VIRTUAL_TRANSITIONS = {
    "flash": "fadewhite", "zoom": "zoomin", "blur": "hblur",
    "motion_blur": "hblur", "shake": "wiperight",
    "fadeblack": "fade", "fadewhite": "fadewhite",
    "wipe": "wipeleft", "smooth": "smoothleft",
    "circlecrop": "circleopen", "rectcrop": "radial",
}

_FLASH_RATES = {
    "auto": 0.06, "light": 0.10, "heavy": 0.22,
    "off": 0.0, "none": 0.0, "no": 0.0, "0": 0.0,
}


def _transition_pools(pattern: Optional[dict]) -> tuple[List[str], List[str]]:
    """从转场模板里取出强拍/弱拍可用的 xfade 效果池。

    ``auto`` / ``smart_mix`` 用内置的智能混搭池；``hard_cut`` 返回空池
    （全硬切）；其余模板沿用自带的 strong_beats/weak_beats，并把虚拟名
    换成真正能渲染的 xfade 名称。
    """
    pid = str((pattern or {}).get("id") or "auto").strip().lower()
    if pid == "hard_cut":
        return [], []
    if pid in ("auto", "smart_mix", ""):
        return list(SMART_MIX_STRONG), list(SMART_MIX_WEAK)
    if not pattern:
        return list(SMART_MIX_STRONG), list(SMART_MIX_WEAK)

    def _clean(names) -> List[str]:
        out = []
        for raw in names or []:
            key = str(raw).strip().lower()
            if key in ("cut", "none", ""):
                continue
            real = _VIRTUAL_TRANSITIONS.get(key, key)
            if real not in out:
                out.append(real)
        return out

    strong = _clean(pattern.get("strong_beats"))
    weak = _clean(pattern.get("weak_beats"))
    return strong or list(SMART_MIX_STRONG), weak or list(SMART_MIX_WEAK)


def _energy_lookup(curve: Optional[Sequence[Dict[str, float]]]):
    """把能量曲线变成 t -> 0..1 的插值函数。"""
    if not curve:
        return lambda _t: 0.5
    pts = sorted(
        (float(p.get("time", 0.0)), float(p.get("energy", 0.0)))
        for p in curve if p is not None
    )
    if not pts:
        return lambda _t: 0.5
    times = [p[0] for p in pts]
    vals = [p[1] for p in pts]

    def at(t: float) -> float:
        if t <= times[0]:
            return vals[0]
        if t >= times[-1]:
            return vals[-1]
        i = bisect.bisect_right(times, t) - 1
        t0, t1 = times[i], times[i + 1]
        v0, v1 = vals[i], vals[i + 1]
        if t1 <= t0:
            return v1
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)

    return at


def _build_windows(shots: Sequence[Shot], beat_interval: float,
                   rng: random.Random, stretch: float = 1.0) -> List[_Window]:
    """把每个镜头拆成 1~2 秒的候选子窗口。

    **窗口之间严格不重叠**（stride == chunk）。旧实现 stride = 0.65 * chunk，
    同一镜头的相邻窗口有 35% 画面重叠；两个窗口分别被选中时，成片里就是同一段
    画面播了两遍 —— 用户感知到的"30 秒重复"有一半来自这里。
    现在同一镜头内多个窗口是**互不重叠的不同内容**，复用同一镜头也不会看到
    重复帧（只是同一个场景的不同时间段）。

    入点不再做随机抖动：抖动会破坏"不重叠"这个前提。随机性来自
    "选哪个窗口"以及渲染时在窗口内随机取子区间。

    Args:
        stretch: 素材不足时整体拉长每刀的倍率（1.0 = 标准动态节奏）。
            窗口长度同步放大，保证长切时仍能 1:1 播放、不会变成慢动作。
    """
    chunk = max(1.15, 2.5 * beat_interval * max(1.0, float(stretch)))
    windows: List[_Window] = []

    for shot in shots:
        avail = float(getattr(shot, "duration", 0.0) or 0.0)
        path = getattr(shot, "file_path", "") or ""
        if avail < 0.5 or not path:
            continue
        motion = float(getattr(shot, "motion_score", 0.0) or 0.0)
        score = float(getattr(shot, "highlight_score", 0.0) or 0.0)

        if avail <= chunk + 0.05:
            windows.append(_Window(
                shot_id=int(shot.shot_id), path=path,
                source_in=0.0, source_out=avail,
                motion=motion, score=score,
            ))
            continue

        pos = 0.0
        while pos + chunk <= avail + 1e-6:
            windows.append(_Window(
                shot_id=int(shot.shot_id), path=path,
                source_in=pos, source_out=pos + chunk,
                motion=motion, score=score,
            ))
            pos += chunk  # 不重叠：步进 = 窗口长度

    rng.shuffle(windows)
    # 动态混剪不要用纯黑标题卡/静止字幕卡：运动幅度接近 0 的窗口
    # 会让成片一开头就是黑屏。但不能把这个过滤做得太狠 —— 滤掉一半以上
    # 窗口就等于变相减少素材，反而逼出重复。只剩不到六成时才保留原样。
    moving = [w for w in windows if w.motion >= 0.08]
    if len(moving) >= max(1, int(len(windows) * 0.6)):
        return moving
    return windows


def _pick_window(pool: List[_Window], duration: float, energy: float,
                 recent: Sequence[int], rng: random.Random,
                 uses: Optional[Dict[int, int]] = None,
                 cap: int = 1) -> tuple[Optional[_Window], int]:
    """按能量选窗口：高能优先高运动，低能优先平稳。

    选窗口分四档优先级（**先保证不重复，再谈能量匹配**）：
        1) 完全没用过、且不在最近几个镜头里的；
        2) 用过但没超过上限 cap、且不在最近列表里的；
        3) 用过但没超过上限 cap（允许"刚放完又出现"，只放宽时间距离）；
        4) 只要够长就行（素材真的不够，上限也会被突破）。

    返回 (窗口, 档位)。档位 4 表示这一刀不得不复用已经超额的镜头，
    调用方据此累加 cap，让后续镜头**均匀分摊**复用，而不是随机撞车。
    旧实现只排除 recent 里最后 3 个 shot_id，其余镜头可以被无限次使用，
    这是"同一段画面反复出现"的直接原因。
    """
    need = max(0.30, duration * 0.55)
    uses = uses or {}
    usable = [w for w in pool if w.length >= need]
    if not usable:
        return None, 0

    recent_set = set(recent)
    tiers = [
        [w for w in usable if uses.get(w.shot_id, 0) == 0 and w.shot_id not in recent_set],
        [w for w in usable
         if uses.get(w.shot_id, 0) < cap and w.shot_id not in recent_set],
        [w for w in usable if uses.get(w.shot_id, 0) < cap],
        usable,
    ]
    candidates: List[_Window] = []
    tier = 0
    for k, cand in enumerate(tiers, 1):
        if cand:
            candidates = cand
            tier = k
            break
    if not candidates:
        return None, 0

    candidates = sorted(candidates, key=lambda w: (w.motion, w.score))
    if energy >= 0.62:
        candidates = candidates[max(0, int(len(candidates) * 0.45)):]
    elif energy <= 0.38:
        candidates = candidates[:max(1, int(len(candidates) * 0.55))]

    chosen = rng.choice(candidates)
    pool.remove(chosen)
    return chosen, tier


def plan_dynamic(
    shots: Sequence[Shot],
    beats: Sequence[Beat],
    target_duration: float,
    style: str = "dynamic",
    rng: Optional[random.Random] = None,
    energy_curve: Optional[Sequence[Dict[str, float]]] = None,
    transition_pattern: Optional[dict] = None,
    max_shot_seconds: float = 2.8,
    max_shot_uses: int = 1,
    cut_hints: Optional[Sequence[float]] = None,
    transition_length: float = DEFAULT_TRANSITION_SECONDS,
    transition_density: float = DEFAULT_TRANSITION_DENSITY,
    transition_flash: str = "auto",
) -> tuple[List[TimelineEntry], Dict[str, Any]]:
    """规划一条 40~60 刀/分钟的卡点时间线。

    Args:
        max_shot_uses: 同一个镜头在成片里最多出现几次。默认 1（**每个镜头只用一次**），
            这是"不重复"最有效的约束：只要素材够，成片里每段画面都只出现一次。
            窗口池用尽时会自动放宽到 2、3……并如实记进 stats["repeat_seconds"]。
        cut_hints: 希望切点落在这几个时刻（秒），通常是歌词换句的位置。
            只影响"这一刀切几拍"的选择，不改变节拍网格，也不影响去重逻辑：
            在候选刀长里挑一个让结尾最靠近 hint 的，偏差超过 0.45s 就不吸附。
        transition_pattern: 转场模板 dict。可携带 ``transition_length`` /
            ``transition_density`` / ``transition_flash`` 覆盖下面三个参数，
            这样 WebUI 的高级设置不用改 pipeline 的函数签名。
        transition_length: 单个转场的目标秒数（默认 0.40s）。
        transition_density: 多少比例的切点用转场（默认 0.60，其余硬切）。
        transition_flash: 白闪点缀比例 auto/light/heavy/off。
    """
    rng = rng or random.Random()
    tp = dict(transition_pattern or {})
    try:
        trans_len = float(tp.get("transition_length", transition_length)
                          or DEFAULT_TRANSITION_SECONDS)
    except (TypeError, ValueError):
        trans_len = DEFAULT_TRANSITION_SECONDS
    trans_len = max(0.05, min(0.80, trans_len))
    try:
        trans_density = float(tp.get("transition_density", transition_density)
                              if tp.get("transition_density", transition_density) is not None
                              else DEFAULT_TRANSITION_DENSITY)
    except (TypeError, ValueError):
        trans_density = DEFAULT_TRANSITION_DENSITY
    trans_density = max(0.0, min(1.0, trans_density))
    flash_mode = str(tp.get("transition_flash", transition_flash) or "auto").lower()
    flash_rate = _FLASH_RATES.get(flash_mode, _FLASH_RATES["auto"])
    strong_pool, weak_pool = _transition_pools(tp)
    last_transition = ""
    target = max(2.0, float(target_duration or 0.0))
    hints = sorted(float(h) for h in (cut_hints or []) if float(h) > 0.3)
    hint_snaps = 0
    beats = [b for b in beats if float(getattr(b, "time", 0.0)) >= 0.0]
    if len(beats) < 2 or not shots:
        return [], {}

    intervals = [
        float(beats[i + 1].time) - float(beats[i].time)
        for i in range(len(beats) - 1)
    ]
    intervals = [x for x in intervals if x > 0.05]
    if not intervals:
        return [], {}
    beat_interval = sorted(intervals)[len(intervals) // 2]
    beat_interval = max(0.15, beat_interval)

    energy_at = _energy_lookup(energy_curve)

    # 先按标准节奏拆一遍窗口，用"真正能用的镜头数"（而不是输入镜头数）
    # 来估算素材供给：黑场/静止镜头会被过滤掉，短镜头也可能撑不了一整刀。
    probe_pool = _build_windows(shots, beat_interval, rng, stretch=1.0)
    if not probe_pool:
        return [], {}
    distinct = len({w.shot_id for w in probe_pool})

    # 素材供给先算一遍：镜头数不够时，与其把同一批镜头反复用（用户看到的就是
    # "重复三十秒"），不如把每刀拉长一点、刀数少一点。
    # 一"刀"平均约 2.2 拍（1~4 拍按能量混合），据此估算需要多少刀
    want_cuts = target / max(0.35, 2.2 * beat_interval)
    # 允许 2% 的复用余量（节拍取整会带来一点不可避免的抖动）
    target_cuts = distinct * 1.02
    stretch = 1.0
    if distinct and want_cuts > target_cuts:
        stretch = min(2.8, want_cuts / target_cuts)

    # 子窗口长度约 2.5 拍，保证 4 拍长切也能以 >=0.55x 放完
    chunk = max(1.15, 2.5 * beat_interval * stretch)
    # 每刀最多几拍：标准节奏 5 拍封顶；素材不足拉长节奏时同步放宽，
    # 否则"想长也长不了"，刀数压不下去，复用依旧。
    hard_max = 5 if stretch <= 1.05 else min(10, 5 + int(round(stretch * 2)))
    max_count = max(1, min(hard_max, int((chunk / 0.55) / beat_interval)))
    # 单镜头最长秒数同样跟着放宽（否则长切会被 2.8s 的硬顶截断）
    max_shot_seconds = float(max_shot_seconds) * min(1.7, max(1.0, stretch))
    pool = probe_pool if stretch <= 1.0001 else _build_windows(
        shots, beat_interval, rng, stretch=stretch)
    if not pool:
        return [], {}

    timeline: List[TimelineEntry] = []
    effects = {"punch": 0, "flash": 0}
    transitions = {"cut": 0, "fade": 0, "dissolve": 0, "wipeleft": 0}
    idx = 0
    # 最近用过的镜头（越大越不容易"刚放完又放"）：6 刀 ≈ 6~9 秒
    recent: List[int] = []
    cycles = 0
    cap = max(1, int(max_shot_uses))
    uses: Dict[int, int] = {}          # shot_id -> 已用次数
    used_shots = set()                 # 用于统计重复秒数
    repeat_seconds = 0.0               # 与前面出现过的镜头重复的时长
    relaxed = 0                        # 因素材不足被迫放宽上限的次数

    while idx < len(beats) - 1 and len(timeline) < 4000:
        start = float(beats[idx].time)
        if start >= target - 0.12:
            break
        energy = energy_at(start)

        # 能量越高切得越短；强拍再减一拍。
        # 60 秒大约 45~55 刀：比旧版十几刀明显更“混剪”，又不会碎到晕。
        if energy >= 0.68:
            choices = (1, 2, 2)
        elif energy >= 0.42:
            choices = (2, 3, 3)
        else:
            choices = (3, 4, 4)
        choices = [max(1, min(max_count, int(round(c * stretch)))) for c in choices]
        if getattr(beats[idx], "beat_type", "normal") == "strong":
            choices = [max(1, c - 1) for c in choices]

        count = rng.choice(choices)
        # 歌词换句吸附：在候选刀长里挑一个让这一刀结尾最接近歌词新句的位置。
        # 只改"切几拍"，节拍网格与去重逻辑都不动，所以最坏情况只是没吸附上。
        if hints:
            nxt = next((h for h in hints if h > start + 0.45), None)
            if nxt is not None:
                cand = sorted(set(choices))
                best = min(cand, key=lambda c: abs(
                    start + c * beat_interval - nxt))
                if abs(start + best * beat_interval - nxt) <= 0.45:
                    count = best
                    hint_snaps += 1
        end_idx = min(idx + count, len(beats) - 1)
        duration = float(beats[end_idx].time) - start
        if duration <= 0.12:
            idx += 1
            continue
        if duration > max_shot_seconds:
            count = max(1, int(max_shot_seconds / beat_interval))
            end_idx = min(idx + count, len(beats) - 1)
            duration = float(beats[end_idx].time) - start

        remaining = target - start
        # 素材紧张时按"剩余时间 / 剩余没用过的镜头"均摊每刀长度。
        # 光靠 stretch 放大平均长度不够：随机抽到短切（2 拍）会把镜头提前用光，
        # 后面就只能重复。这里保证"还没用过的镜头数"刚好撑完剩余时长。
        if stretch > 1.0 and remaining > 0:
            unused_left = max(1, distinct - len(used_shots))
            need_dur = remaining / unused_left
            if need_dur > duration + 1e-6:
                beats_needed = max(1, min(max_count, int(math.ceil(
                    need_dur / beat_interval - 1e-6))))
                alt_idx = min(idx + beats_needed, len(beats) - 1)
                alt_dur = float(beats[alt_idx].time) - start
                if 0 < alt_dur <= max_shot_seconds:
                    end_idx, duration = alt_idx, alt_dur

        if duration > remaining:
            duration = remaining
            if duration < 0.25:
                break

        window = None
        if not timeline:
            # 冷开场：第一刀用最高分/最高运动的镜头，别把黑标题卡放开头
            need = max(0.30, duration * 0.55)
            candidates = [w for w in pool if w.length >= need]
            if candidates:
                candidates.sort(
                    key=lambda w: (w.score + 0.015 * w.motion), reverse=True)
                window = rng.choice(candidates[:max(1, len(candidates) // 5)])
                pool.remove(window)
        if window is None:
            window, tier = _pick_window(
                pool, duration, energy, recent, rng, uses=uses, cap=cap)
            if tier >= 4:
                # 这一刀已经超出"每镜头最多用 cap 次"，且没有更好的候选：
                # 抬起上限，让后面的复用**均匀分摊到所有镜头**，
                # 而不是让个别镜头被反复抽中。
                relaxed += 1
                cap += 1
            if window is None:
                # 窗口池彻底干了：重新拆一批（同一镜头里不重叠的另一段时间）。
                if cycles >= 30:
                    break
                cycles += 1
                cap += 1
                pool = _build_windows(shots, beat_interval, rng, stretch=stretch)
                window, _tier = _pick_window(
                    pool, duration, energy, recent, rng, uses=uses, cap=cap)
                if window is None:
                    break

        wl = window.length
        if wl >= duration:
            speed = 1.0
            if energy >= 0.60 and rng.random() < 0.25:
                speed = 1.06
            elif energy <= 0.35 and rng.random() < 0.20:
                speed = 0.94
            take = duration * speed
            if take > wl:
                speed = wl / duration
                take = wl
            source_in = window.source_in + rng.uniform(0.0, max(0.0, wl - take))
            source_out = source_in + take
        else:
            speed = max(0.50, wl / duration)
            source_in = window.source_in
            source_out = window.source_out

        # 高能段：推镜为主，白闪只做少量点缀（多了就变成「只有快闪」）
        effect = "none"
        if energy >= 0.60:
            roll = rng.random()
            if roll < flash_rate and duration >= 0.45:
                effect = "flash"
            elif roll < flash_rate + 0.17:
                effect = "punch"
        elif energy >= 0.40 and rng.random() < 0.12:
            effect = "punch"
        if not timeline and effect == "none":
            # 开场第一刀轻微推镜，画面一进来就有运动感
            effect = "punch"

        # ---- 转场：模板生效 + 效果轮换 ----
        # 旧实现把 transition_pattern 收进来却从没用过，只按能量随机给
        # fade/dissolve/wipeleft 且时长 ≤0.18s，肉眼基本看不见。
        # 现在按强/弱拍从模板效果池里抽，且不与上一次重复。
        transition = "cut"
        trans_dur = 0.0
        if effect != "flash" and timeline and (strong_pool or weak_pool):
            if rng.random() < trans_density:
                beat_kind = getattr(beats[idx], "beat_type", "normal")
                # 注意别叫 pool：外层的 pool 是镜头窗口池，同名会把它覆盖掉，
                # 下一刀 _pick_window 就会拿到一串字符串。
                effect_pool = (strong_pool if beat_kind == "strong" else weak_pool) \
                    or strong_pool or weak_pool
                candidates = [t for t in effect_pool
                              if t != last_transition] or list(effect_pool)
                chosen = rng.choice(candidates)
                # 不能让转场吃掉半个镜头。旧的 0.9*beat_interval 会把
                # 0.55/0.70s 的长转场也压回 0.42s，等于高级设置白选；
                # 跨一两拍的转场本来就是正常的，只按镜头长度收口。
                d = min(trans_len, 0.45 * duration)
                if d >= 0.12:
                    transition, trans_dur = chosen, float(d)
                    last_transition = chosen

        entry = TimelineEntry(
            shot_id=int(window.shot_id),
            shot_path=window.path,
            start_time=start,
            end_time=start + duration,
            duration=duration,
            beat_time=start,
            speed_factor=speed,
            transition_type=transition,
            transition_duration=max(0.0, float(trans_dur)),
            source_in=float(source_in),
            source_out=float(source_out),
            effect=effect,
        )
        timeline.append(entry)
        if effect in effects:
            effects[effect] += 1
        transitions[transition] = transitions.get(transition, 0) + 1

        uses[window.shot_id] = uses.get(window.shot_id, 0) + 1
        if window.shot_id in used_shots:
            repeat_seconds += duration
        used_shots.add(window.shot_id)

        recent.append(window.shot_id)
        if len(recent) > 6:
            recent.pop(0)
        idx = end_idx if end_idx > idx else idx + 1

    actual = sum(e.duration for e in timeline)
    stats = {
        "target": target,
        "actual": actual,
        "shots": len(timeline),
        "unique_shots": len({e.shot_id for e in timeline}),
        "unique_windows": len({(e.shot_id, round(e.source_in, 2)) for e in timeline}),
        "cuts": max(0, len(timeline) - 1),
        "avg_shot": (actual / len(timeline)) if timeline else 0.0,
        "effects": effects,
        "transitions": transitions,
        "transition_types": len([k for k in transitions if k != "cut"]),
        "transition_cuts": sum(v for k, v in transitions.items() if k != "cut"),
        "transition_seconds": trans_len,
        "transition_density": trans_density,
        "flash_mode": flash_mode,
        "cycles": cycles,
        "cap": cap,
        "relaxed": relaxed,
        "stretch": stretch,
        "distinct_shots": distinct,
        "repeat_seconds": repeat_seconds,
        "cut_hints": len(hints),
        "hint_snaps": hint_snaps,
        "reused": max(0, len(timeline) - len({(e.shot_id, round(e.source_in, 2)) for e in timeline})),
        "short": actual < target * 0.95,
    }
    return timeline, stats
