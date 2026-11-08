"""DJ / remix 版歌词对齐的回归测试（全部用合成音频，不依赖外部素材）。"""
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.subtitle_engine.src import dj  # noqa: E402
from packages.subtitle_engine.src.lyrics import build_lyric_segments  # noqa: E402

SR = 16000


def _pulse(dur=9.0, period=0.5, sr=SR, freq=440.0):
    """一串间隔固定的短音，模拟人声起音；自带一点低频「鼓」干扰。"""
    y = np.zeros(int(sr * dur), dtype=np.float32)
    n = int(sr * 0.09)
    env = np.exp(-np.linspace(0, 6, n)).astype(np.float32)
    tone = (np.sin(2 * np.pi * freq * np.arange(n) / sr) * env).astype(np.float32)
    for t in np.arange(0.2, dur - 0.2, period):
        i = int(t * sr)
        y[i:i + n] += tone
    # 低频鼓点（每 0.25s 一下），验证 onset 特征不被它带偏
    for t in np.arange(0.0, dur, 0.25):
        i = int(t * sr)
        m = min(int(sr * 0.04), len(y) - i)
        y[i:i + m] += (np.sin(2 * np.pi * 60 * np.arange(m) / sr)
                       * np.exp(-np.linspace(0, 8, m)) * 0.5).astype(np.float32)
    return y


def _speedup(y, r):
    n = max(1, int(len(y) / r))
    return np.interp(np.linspace(0, len(y) - 1, n),
                     np.arange(len(y)), y).astype(np.float32)


def _wav(name, y, sr=SR):
    p = Path(tempfile.gettempdir()) / f"montage_dj_test_{name}.wav"
    sf.write(str(p), y, sr)
    return p, y


def test_re_sub_dj_strips_markers():
    assert dj.re_sub_dj("九万里(DJ 1.25x)") == "九万里"
    assert "DJ" not in dj.re_sub_dj("九万里_DJ重混版")
    assert dj.re_sub_dj("孤身摇 (Nightcore)") == "孤身摇"
    assert dj.re_sub_dj("一点点 加快版") == "一点点"


def test_ratio_estimation_speedup_and_slowdown():
    orig, _ = _wav("ratio_orig", _pulse())
    for r in (1.25, 0.85):
        fast, _ = _wav(f"ratio_{r}", _speedup(_pulse(), r))
        got, off, conf = dj.estimate_tempo_ratio(str(orig), str(fast))
        assert abs(got - r) <= 0.03, (r, got, conf)
        assert conf >= 0.15, (r, conf)
        assert off >= 0.0
    print("  变速比 1.25× / 0.85× 估计正确")


def test_ratio_identical_audio_is_unity():
    """同一首歌比对自己：必须是 1.00×，不能凭空造出变速。"""
    a, _ = _wav("same_ref", _pulse(dur=10.0))
    r, off, conf = dj.estimate_tempo_ratio(str(a), str(a))
    assert abs(r - 1.0) <= 0.02, (r, conf)
    assert conf >= 0.15, conf
    assert off <= 0.2, off
    print(f"  同源音频 1.00×（置信 {conf:.2f}）")


def test_warp_map_matches_linear_for_pure_speedup():
    orig, _ = _wav("warp_orig", _pulse(dur=12.0))
    fast, _ = _wav("warp_fast", _speedup(_pulse(dur=12.0), 1.25))
    r, off, conf = dj.estimate_tempo_ratio(str(orig), str(fast))
    assert abs(r - 1.25) <= 0.03
    w = dj.build_warp_map(str(orig), str(fast), r, off)
    assert len(w) >= 3
    prof = dj.DJProfile(ratio=r, offset=off, warp=w)
    for t in (0.0, 2.0, 5.0, 9.0, 12.0):
        assert abs(prof.to_dj(t) - t / r) <= 0.6, (t, prof.to_dj(t), t / r)
    # 映射必须单调
    ys = [prof.to_dj(t) for t in np.linspace(0, 12, 40)]
    assert all(b >= a - 1e-6 for a, b in zip(ys, ys[1:]))
    print("  warp 与全局线性一致且单调")


def test_map_time_extrapolation_is_safe():
    pts = [(0.0, 0.0), (10.0, 8.0), (20.0, 16.0)]
    assert abs(dj._map_time(5.0, pts) - 4.0) < 1e-6
    assert dj._map_time(30.0, pts) > 16.0
    assert dj._map_time(-5.0, pts) < 0.0
    assert dj._map_time(5.0, []) == 5.0


def test_enhance_vocals_output_shape():
    src, y = _wav("enh_src", _pulse(dur=3.0))
    out = dj.enhance_vocals(str(src))
    info = sf.info(out)
    assert info.samplerate == SR
    assert info.channels == 1
    assert abs(info.duration - 3.0) < 0.1
    yy, _ = sf.read(out, dtype="float32")
    assert float(np.max(np.abs(yy))) > 0.05, "提纯后不该变成静音"
    print(f"  人声提纯输出 {info.duration:.2f}s / 16k mono")


def test_restore_speed_changes_length():
    src, y = _wav("rest_src", _pulse(dur=4.0))
    voc = dj.enhance_vocals(str(src))
    out = dj.restore_speed(voc, 1.25)
    assert out != voc
    info = sf.info(out)
    assert abs(info.duration - 4.0 * 1.25) < 0.2, info.duration
    print(f"  还原后时长 {info.duration:.2f}s（原 4.00s）")


def test_find_reference_skips_dj_versions():
    d = Path(tempfile.mkdtemp(prefix="montage_dj_ref_"))
    (d / "测试曲.mp3").write_bytes(b"x" * 100)
    (d / "测试曲(DJ 1.25x).mp3").write_bytes(b"x" * 100)
    target = d / "测试曲_DJ重混版.mp3"
    target.write_bytes(b"x" * 100)
    got = dj.find_reference(str(target))
    assert Path(got).name == "测试曲.mp3", got
    print("  自动找原版会跳过其它 DJ 版本")


def test_prepare_dj_off_and_enhance_modes():
    src, _ = _wav("prep_src", _pulse(dur=2.0))
    off = dj.prepare_dj(str(src), mode="off")
    assert off.method == "off" and not off.enabled
    enhance = dj.prepare_dj(str(src), mode="enhance")
    assert enhance.method == "direct"
    assert enhance.ref_path == ""
    assert enhance.ratio == 1.0
    assert enhance.enhanced_path
    print("  off / enhance 两种模式行为正确")


def test_build_lyric_segments_lrc_auto_warps():
    """给一遍 LRC + 一个变速版音频，自动把时间轴映射过去。"""
    orig, _ = _wav("e2e_orig", _pulse(dur=12.0))
    fast, _ = _wav("e2e_fast", _speedup(_pulse(dur=12.0), 1.20))
    lrc = "\n".join(f"[00:{t:05.2f}]第{i}句歌词" for i, t in
                    enumerate([1.0, 2.4, 4.0, 5.6, 7.2, 8.8], 1))
    segs, rep = build_lyric_segments(str(fast), lrc, dj="auto",
                                     dj_ref=str(orig), repeat="off")
    assert len(segs) == 6, len(segs)
    assert rep.method == "lrc"
    assert rep.dj_method == "ref_warp", rep.summary()
    assert abs(rep.dj_ratio - 1.20) <= 0.03, rep.dj_ratio
    for i, s in enumerate(segs[:4]):
        ideal = [1.0, 2.4, 4.0, 5.6][i] / 1.20
        assert abs(float(s.start) - ideal) <= 0.35, (i, s.start, ideal)
    print(f"  LRC 自动 warp：ratio={rep.dj_ratio:.2f}，前 4 句误差 <0.35s")


def test_dj_off_keeps_original_timeline():
    src, _ = _wav("off_src", _pulse(dur=12.0))
    lrc = "[00:01.00]第一句\n[00:02.40]第二句\n[00:04.00]第三句"
    segs, rep = build_lyric_segments(str(src), lrc, dj="off",
                                     offset=0.0, repeat="off")
    assert rep.dj_method == ""
    assert [round(float(s.start), 2) for s in segs] == [1.0, 2.4, 4.0]
    print("  关闭 DJ 适配时行为与旧版完全一致")


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
