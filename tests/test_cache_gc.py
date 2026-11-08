"""缓存管家单测：不依赖真实 50G 数据，全部在临时目录里跑。"""

import io
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packages.montage import cache_gc as gc


def _make_var(tmp: Path, active: bool = False, idle_minutes: float = 0.0) -> Path:
    (tmp / "cache").mkdir()
    (tmp / "uploads").mkdir()
    (tmp / "output").mkdir()
    now = time.time()
    # 3 个旧缓存 + 1 个刚写过的（应被保护）
    for i, age_h in enumerate((72, 48, 24)):
        p = tmp / "cache" / f"old_{i}.mp4"
        p.write_bytes(b"x" * 1024)
        t = now - age_h * 3600
        import os
        os.utime(p, (t, t))
    fresh = tmp / "cache" / "fresh.tmp"
    fresh.write_bytes(b"y" * 512)
    (tmp / "output" / "final.mp4").write_bytes(b"z" * 2048)

    db = tmp / "cache" / "tasks.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, payload TEXT, "
                 "created_at REAL, started_at REAL, updated_at REAL, "
                 "progress INTEGER, message TEXT, error TEXT, finished_at REAL)")
    status = "running" if active else "done"
    beat = now - idle_minutes * 60
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("t1", status, "{}", now - 3600, now - 3600, beat,
                  34, "已截取素材", "", None))
    conn.commit()
    conn.close()
    return tmp


def _set_limit(monkeypatch, gb: float):
    import os
    monkeypatch.setenv("MONTAGE_CACHE_LIMIT_GB", str(gb))
    monkeypatch.setenv("MONTAGE_CACHE_MIN_FREE_GB", "0")
    monkeypatch.setenv("MONTAGE_CACHE_PROTECT_MINUTES", "20")
    monkeypatch.setenv("MONTAGE_CACHE_UPLOAD_KEEP_HOURS", "0")
    monkeypatch.setenv("MONTAGE_CACHE_KEEP_OUTPUT", "1")
    monkeypatch.setenv("MONTAGE_CACHE_STALE_MINUTES", "1")
    # 单测绝不能碰到真实回收站
    monkeypatch.setenv("MONTAGE_CACHE_PURGE_RECYCLE", "0")


def test_usage_counts_all_parts(tmp_path, monkeypatch):
    var = _make_var(tmp_path)
    _set_limit(monkeypatch, 1)
    u = gc.usage(var)
    assert set(u["parts"]) == {"cache", "uploads", "output"}
    # 3 个旧缓存 + 1 个刚写的 + tasks.db
    assert u["parts"]["cache"]["files"] == 5
    assert u["total_bytes"] >= 1024 * 3 + 512 + 2048


def test_cleanup_dry_run_never_deletes(tmp_path, monkeypatch):
    var = _make_var(tmp_path)
    _set_limit(monkeypatch, 0.000001)
    before = sorted(str(p) for p in var.rglob("*") if p.is_file())
    r = gc.cleanup(var, dry_run=True, force=True)
    after = sorted(str(p) for p in var.rglob("*") if p.is_file())
    assert r["action"] == "dry_run" and r["deleted_count"] > 0
    assert before == after, "dry_run 不该动任何文件"


def test_cleanup_keeps_fresh_and_output(tmp_path, monkeypatch):
    var = _make_var(tmp_path)
    _set_limit(monkeypatch, 0.000001)
    r = gc.cleanup(var, force=True)
    assert r["deleted_count"] >= 1
    assert (var / "cache" / "fresh.tmp").exists(), "刚写过的文件不能被删"
    assert (var / "output" / "final.mp4").exists(), "成片不能被删"
    assert (var / "cache" / "tasks.db").exists(), "任务表不能被删"
    deleted = {Path(d["path"]).name for d in r["deleted"]}
    assert deleted <= {"old_0.mp4", "old_1.mp4", "old_2.mp4"}


def test_active_task_blocks_cleanup(tmp_path, monkeypatch):
    """worker 进程确实活着时，running 任务必须挡住清理。"""
    var = _make_var(tmp_path, active=True)
    _set_limit(monkeypatch, 0.000001)
    orig = gc._processes_alive
    gc._processes_alive = lambda: True
    try:
        assert gc.has_active_tasks(var) is True
        r = gc.cleanup(var, force=True)
        assert r["action"] == "noop"
        assert "任务正在运行" in r["reason"]
        assert (var / "cache" / "old_0.mp4").exists()
    finally:
        gc._processes_alive = orig


def test_stale_running_task_no_longer_blocks(tmp_path, monkeypatch):
    """进程已退出、心跳也停了的僵尸 running：不该再挡住清理，且要被归档。"""
    var = _make_var(tmp_path, active=True, idle_minutes=9)
    _set_limit(monkeypatch, 0.000001)
    orig = gc._processes_alive
    gc._processes_alive = lambda: False
    try:
        assert len(gc.stale_tasks(var)) == 1
        assert gc.has_active_tasks(var) is False
        r = gc.cleanup(var, force=True)
        assert r["action"] == "cleanup", r
        assert r["deleted_count"] >= 1
        assert r["reaped"]["count"] == 1
        assert not (var / "cache" / "old_0.mp4").exists()
        conn = sqlite3.connect(var / "cache" / "tasks.db")
        status = conn.execute("SELECT status FROM tasks WHERE id='t1'").fetchone()[0]
        conn.close()
        assert status == "error", "僵尸任务应归档成 error，避免重启后无限续跑"
    finally:
        gc._processes_alive = orig


def test_fresh_heartbeat_still_protected(tmp_path, monkeypatch):
    """心跳还热（刚更新过）的任务不能因为进程探测抖动被误判成僵尸。"""
    var = _make_var(tmp_path, active=True, idle_minutes=0.1)
    _set_limit(monkeypatch, 0.000001)
    orig = gc._processes_alive
    gc._processes_alive = lambda: False
    try:
        assert gc.stale_tasks(var) == []
        assert gc.has_active_tasks(var) is True
        r = gc.cleanup(var, force=True)
        assert r["action"] == "noop"
    finally:
        gc._processes_alive = orig


def test_recycle_purge_only_touches_var_cache_uploads(tmp_path, monkeypatch):
    """回收站二次清理：只永久删 cache/uploads 来源，成片与别人文件一律不动。"""
    import struct

    var = _make_var(tmp_path)
    rb = tmp_path / "rb" / "S-1-5-21-test"
    rb.mkdir(parents=True)
    origins = [
        str(var / "cache" / "old.mp4"),
        str(var / "uploads" / "clip.mp4"),
        str(var / "output" / "final.mp4"),          # 成片：绝不能删
        r"C:\Users\someone_else\Documents\a.mp4",    # 别人的：绝不能删
    ]
    for idx, origin in enumerate(origins, 1):
        name = (origin + "\x00").encode("utf-16-le")
        meta = struct.pack("<QQQI", 2, 1000 * idx, 0, len(origin) + 1) + name
        (rb / f"$I{idx:04d}.mp4").write_bytes(meta)
        (rb / f"$R{idx:04d}.mp4").write_bytes(b"data" * 20)

    entries = gc.recycle_bin_entries(var, recycle_root=tmp_path / "rb")
    assert len(entries) == 2, f"只该命中 cache/uploads 两条，实际 {len(entries)}"

    dry = gc.purge_recycled_var(var, dry_run=True, recycle_root=tmp_path / "rb")
    assert dry["count"] == 2 and (rb / "$R0003.mp4").exists()

    r = gc.purge_recycled_var(var, dry_run=False, recycle_root=tmp_path / "rb")
    assert r["count"] == 2 and r["failed"] == 0
    assert not (rb / "$R0001.mp4").exists() and not (rb / "$I0001.mp4").exists()
    assert not (rb / "$R0002.mp4").exists() and not (rb / "$I0002.mp4").exists()
    assert (rb / "$R0003.mp4").exists() and (rb / "$I0003.mp4").exists(), "成片条目必须留着"
    assert (rb / "$R0004.mp4").exists() and (rb / "$I0004.mp4").exists(), "别人的条目必须留着"


def test_dedupe_uploads_keeps_newest(tmp_path, monkeypatch):
    import os
    var = _make_var(tmp_path)
    names = ["a" * 32 + "_clip.mp4", "b" * 32 + "_clip.mp4", "c" * 32 + "_clip.mp4"]
    for i, name in enumerate(names):
        p = var / "uploads" / name
        p.write_bytes(b"same-content" * 100)
        t = time.time() - (3 - i) * 3600
        os.utime(p, (t, t))
    r = gc.dedupe_uploads(var, dry_run=False)
    assert r["deleted_count"] == 2, r
    left = [p.name for p in (var / "uploads").iterdir()]
    assert left == [names[-1]], f"应该只留最新的 {names[-1]}，实际 {left}"


def test_upload_dedup_saves_only_one_copy(tmp_path, monkeypatch):
    """同一份素材上传两次，磁盘上只应有一份。"""
    from packages.webui.src import app as webapp

    monkeypatch.setattr(webapp, "UPLOAD_DIR", tmp_path / "uploads")
    (tmp_path / "uploads").mkdir()

    def _upload(data: bytes, name: str = "clip.mp4"):
        return SimpleNamespace(filename=name, file=io.BytesIO(data))

    payload = b"hello world" * 1000
    p1 = Path(webapp._save_upload(_upload(payload)))
    p2 = Path(webapp._save_upload(_upload(payload)))
    assert p1 == p2, "同内容应复用同一个文件"
    assert len(list((tmp_path / "uploads").iterdir())) == 1
    assert p1.stat().st_size == len(payload)
    p3 = Path(webapp._save_upload(_upload(payload + b"different")))
    assert p3 != p1, "不同内容必须另存"


def _main():
    """没装 pytest 时手动跑：用临时目录 + 简易 monkeypatch。"""
    import tempfile

    class MP:
        def __init__(self):
            self.env = {}

        def setenv(self, k, v):
            import os
            self.env[k] = os.environ.get(k)
            os.environ[k] = v

        def setattr(self, obj, k, v):
            setattr(obj, k, v)

    fns = [(k, v) for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    bad = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as td:
            mp = MP()
            try:
                fn(Path(td), mp)
                print("ok  ", name)
            except Exception as e:
                bad += 1
                print("FAIL", name, type(e).__name__, e)
            finally:
                import os
                for k, old in mp.env.items():
                    if old is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = old
                # 还原被 monkeypatch 的 UPLOAD_DIR
                try:
                    from packages.montage.paths import UPLOAD_DIR
                    from packages.webui.src import app as webapp
                    webapp.UPLOAD_DIR = UPLOAD_DIR
                except Exception:
                    pass
    print(f"{len(fns) - bad}/{len(fns)} passed")
    return bad


if __name__ == "__main__":
    raise SystemExit(_main())
