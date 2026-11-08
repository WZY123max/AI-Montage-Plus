"""缓存管家：把 var/ 目录的总占用压在上限以内，别让系统盘被中间文件撑爆。

为什么需要它
------------
剪一个 11 分钟 4K 素材会在 var/ 里留下这些中间产物：

* ``uploads/``  每次上传都整份复制一次（同一素材传 8 次就是 8 × 2G）
* ``cache/trim``  按分析上限裁出来的片段（约 1.2G）
* ``cache/clean`` 去黑场 + 缩放到工作分辨率（4K 时约 3.3G）
* ``cache/shots_*`` 镜头切片（142 个，约 7G）

实测跑十来次任务就攒到 50G。这些全是可再生的中间文件，成片在
``output/`` 里、任务表在 ``cache/tasks.db`` 里，都不该被清掉。

策略
----
1. **上限**：``var/`` 总大小超过 ``MONTAGE_CACHE_LIMIT_GB``（默认 20G），
   或者系统盘剩余低于 ``MONTAGE_CACHE_MIN_FREE_GB``（默认 12G）时触发清理。
2. **LRU**：按文件最后修改时间从旧到新删，删到达标为止。
3. **保护**：正在运行/排队任务引用的素材、最近 20 分钟动过的文件（正在写）、
   ``tasks.db``、以及 ``output/`` 里的成片，一律不删。
4. **只删明确的文件**：逐个 unlink，不递归删目录、不碰 var/ 之外的路径。

可用环境变量覆盖：``MONTAGE_CACHE_LIMIT_GB`` / ``MONTAGE_CACHE_MIN_FREE_GB`` /
``MONTAGE_CACHE_PROTECT_MINUTES`` / ``MONTAGE_CACHE_UPLOAD_KEEP_HOURS`` /
``MONTAGE_CACHE_KEEP_OUTPUT``（0 才允许到期清理成片）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

GB = 1024 ** 3

# 这些文件是任务表/配置，任何时候都不能删
_ALWAYS_KEEP_NAMES = {"tasks.db", "tasks.db-wal", "tasks.db-shm"}
# 上传文件名形如 "<32位uuid>_原名" / "bgm_<uuid>_原名"
_UPLOAD_PREFIX_RE = re.compile(r"^(?:bgm_|narration_)?[0-9a-f]{32}_")


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def limits() -> Dict[str, float]:
    """当前生效的清理阈值（供前端展示）。"""
    return {
        "limit_gb": _env_float("MONTAGE_CACHE_LIMIT_GB", 20.0),
        "min_free_gb": _env_float("MONTAGE_CACHE_MIN_FREE_GB", 12.0),
        "protect_minutes": _env_float("MONTAGE_CACHE_PROTECT_MINUTES", 20.0),
        "stale_minutes": _env_float("MONTAGE_CACHE_STALE_MINUTES", 1.0),
        "purge_recycle": _env_float("MONTAGE_CACHE_PURGE_RECYCLE", 1.0),
        "upload_keep_hours": _env_float("MONTAGE_CACHE_UPLOAD_KEEP_HOURS", 48.0),
        "output_keep_days": _env_float("MONTAGE_CACHE_OUTPUT_KEEP_DAYS", 30.0),
        "keep_output": _env_float("MONTAGE_CACHE_KEEP_OUTPUT", 1.0),
    }


def _iter_files(root: Path) -> Iterable[Path]:
    """递归列出 root 下的普通文件（不跟随符号链接、不进 .backup）。"""
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".backup")]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                if p.is_file():
                    yield p
            except OSError:
                continue


def _dir_size(root: Path) -> Tuple[int, int]:
    total, count = 0, 0
    for p in _iter_files(root):
        try:
            total += p.stat().st_size
            count += 1
        except OSError:
            continue
    return total, count


def usage(var_dir: Optional[Path] = None) -> Dict:
    """统计 var/ 占用与磁盘剩余。"""
    var_dir = Path(var_dir).resolve() if var_dir else _default_var_dir()
    parts = {}
    for key in ("cache", "uploads", "output"):
        size, count = _dir_size(var_dir / key)
        parts[key] = {"bytes": size, "gb": round(size / GB, 2), "files": count}
    total = sum(v["bytes"] for v in parts.values())
    try:
        free = shutil.disk_usage(var_dir).free
    except OSError:
        free = 0
    cfg = limits()
    return {
        "var_dir": str(var_dir),
        "parts": parts,
        "total_bytes": total,
        "total_gb": round(total / GB, 2),
        "disk_free_gb": round(free / GB, 2),
        "limit_gb": cfg["limit_gb"],
        "min_free_gb": cfg["min_free_gb"],
        "over_limit": total > cfg["limit_gb"] * GB,
        "low_disk": free < cfg["min_free_gb"] * GB,
        "keep_output": bool(cfg["keep_output"]),
    }


def _default_var_dir() -> Path:
    from .paths import VAR_DIR

    return Path(VAR_DIR).resolve()


def protected_paths(var_dir: Optional[Path] = None) -> Set[Path]:
    """运行中/排队任务引用的文件 + 最近刚写过的文件 + 任务表。"""
    var_dir = Path(var_dir).resolve() if var_dir else _default_var_dir()
    cfg = limits()
    out: Set[Path] = set()

    # 1) tasks.db / wal / shm
    for name in _ALWAYS_KEEP_NAMES:
        out.add((var_dir / "cache" / name).resolve())

    # 2) 非终态任务引用的所有路径（video_paths / bgm_path / ...）
    #    僵尸任务已经没有进程在读了，不能让它继续保护一整批素材
    stale = _stale_ids(var_dir)
    db = var_dir / "cache" / "tasks.db"
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
                status_col = "status" if "status" in cols else None
                rows = (conn.execute("SELECT id, payload, status FROM tasks")
                        if status_col else conn.execute("SELECT id, payload FROM tasks"))
                for row in rows:
                    tid = row[0]
                    payload, status = (row[1], row[2]) if status_col else (row[1], "done")
                    if tid in stale or (status or "").lower() in _TERMINAL:
                        continue
                    try:
                        p = json.loads(payload or "{}")
                    except (ValueError, TypeError):
                        continue
                    for value in _walk_strings(p):
                        if not value or len(value) > 500:
                            continue
                        try:
                            resolved = Path(value).resolve()
                        except OSError:
                            continue
                        if resolved.is_file():
                            out.add(resolved)
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            pass

    # 3) 最近 N 分钟改过的文件（ffmpeg 正在写，删了会失败或打断任务）
    cutoff = time.time() - cfg["protect_minutes"] * 60
    # 一个 4K 任务可能跑一两个小时，它早期生成的 trim/clean/镜头切片
    # mtime 早就超出"最近 N 分钟"了，但仍在被读。所以只要有任务在跑，
    # 就把保护起点提前到这一批任务的最早开始时间。
    since = _active_task_since(var_dir, stale)
    if since:
        cutoff = min(cutoff, since - 120)
    for p in _iter_files(var_dir / "cache"):
        try:
            if p.stat().st_mtime > cutoff:
                out.add(p.resolve())
        except OSError:
            continue
    for p in _iter_files(var_dir / "uploads"):
        try:
            if p.stat().st_mtime > cutoff:
                out.add(p.resolve())
        except OSError:
            continue
    return out


def _active_task_since(var_dir: Path, stale: Optional[Set[str]] = None) -> float:
    """真正活跃任务里最早的 started_at/created_at；没有则返回 0。"""
    db = var_dir / "cache" / "tasks.db"
    if not db.exists():
        return 0.0
    stale = stale if stale is not None else _stale_ids(var_dir)
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            vals = []
            for row in conn.execute(
                    "SELECT id, status, started_at, created_at FROM tasks"):
                tid, status = row[0], row[1]
                if tid in stale or (status or "").lower() in _TERMINAL:
                    continue
                for v in row[2:]:
                    if isinstance(v, (int, float)) and v > 0:
                        vals.append(float(v))
            return min(vals) if vals else 0.0
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return 0.0


# ---------------------------------------------------------------- 僵尸任务
# WebUI 的 worker 是 daemon 线程：进程被关掉/崩溃时它来不及收敛状态，
# 任务表会永远留一条 status='running'。只看 status 的话，自动清理、手动清理、
# 后台守护会一起被这条僵尸记录挡死——这正是"明明没有任务却清理不了"的原因。
_TERMINAL = ("done", "error", "failed", "canceled", "cancelled", "exhausted")
_WORKER_MARKERS = ("--webui", "uvicorn", "start_webui", "webui/src/app", "webui\\src\\app")
_PROC_CACHE = {"ts": 0.0, "value": True}


def _worker_process_alive() -> bool:
    """是否存在能执行任务的 WebUI/worker 进程（排除自己与清理守护脚本）。

    检测本身失败时保守返回 True：宁可这次不清理，也不能误删正在渲染的文件。
    """
    me = os.getpid()
    if os.name == "nt":
        script = ('Get-CimInstance Win32_Process -Filter "Name=\'python.exe\'" | '
                  'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }')
        try:
            raw = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=12).stdout or b""
        except (OSError, subprocess.SubprocessError):
            return True
        lines = raw.decode("utf-8", errors="replace").splitlines()
    else:
        lines = []
        try:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", errors="replace")
                lines.append(f"{pid}\t{cmd}")
        except OSError:
            return True

    for line in lines:
        pid_s, _, cmd = line.strip().partition("\t")
        if pid_s.isdigit() and int(pid_s) == me:
            continue
        low = cmd.lower()
        if any(marker in low for marker in _WORKER_MARKERS):
            return True
    return False


def _render_process_alive() -> bool:
    """是否有 ffmpeg/ffprobe 正在跑（长渲染阶段任务心跳可能很久不更新）。"""
    if os.name == "nt":
        try:
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                raw = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                    capture_output=True, timeout=10).stdout or b""
                if name.encode() in raw.lower():
                    return True
            return False
        except (OSError, subprocess.SubprocessError):
            return True
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode("utf-8", errors="replace").lower()
            if "ffmpeg" in cmd or "ffprobe" in cmd:
                return True
        return False
    except OSError:
        return True


def _processes_alive() -> bool:
    """带 5 秒缓存，避免每次查询都去枚举进程。"""
    now = time.time()
    if now - _PROC_CACHE["ts"] < 5.0:
        return _PROC_CACHE["value"]
    try:
        value = _worker_process_alive() or _render_process_alive()
    except Exception:  # 探测异常：保守当作仍有任务
        value = True
    _PROC_CACHE.update(ts=now, value=value)
    return value


def stale_tasks(var_dir: Optional[Path] = None) -> List[Dict]:
    """列出「进程已不在、心跳也停了」的僵尸任务。

    判定：非终态 + 没有 worker/ffmpeg 进程 + updated_at 超过 stale_minutes。
    三者都满足才算僵尸；任何一条不满足都按"任务还在跑"处理。
    """
    var_dir = Path(var_dir).resolve() if var_dir else _default_var_dir()
    db = var_dir / "cache" / "tasks.db"
    if not db.exists() or _processes_alive():
        return []
    cfg = limits()
    cutoff = time.time() - cfg["stale_minutes"] * 60
    out: List[Dict] = []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}

            def _col(name: str, fallback: str = "") -> str:
                if name in cols:
                    return name
                if fallback and fallback in cols:
                    return f"{fallback} AS {name}"
                return f"NULL AS {name}"

            query = ("SELECT id, status, "
                     + ", ".join((_col("updated_at", "created_at"),
                                  _col("created_at", "started_at"),
                                  _col("progress"), _col("message")))
                     + " FROM tasks")
            rows = conn.execute(query).fetchall()
            for tid, status, updated, created, progress, message in rows:
                if (status or "").lower() in _TERMINAL:
                    continue
                beat = float(updated or created or 0)
                if beat > cutoff:
                    continue
                out.append({"id": tid, "status": status, "progress": progress,
                            "message": message or "",
                            "idle_minutes": round((time.time() - beat) / 60, 1),
                            "reason": "WebUI/worker 进程已退出，任务未正常结束"})
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return []
    return out


def _stale_ids(var_dir: Path) -> Set[str]:
    return {t["id"] for t in stale_tasks(var_dir)}


def reap_stale_tasks(var_dir: Optional[Path] = None,
                     dry_run: bool = False) -> Dict:
    """把僵尸任务归档成 error（写清原因），让 UI 不再显示"运行中"。

    不做这一步的话，重启 WebUI 时 ``resume_stale`` 会把同一个卡死任务再捞起来，
    然后又卡死、又挡住清理，形成死循环。
    """
    var_dir = Path(var_dir).resolve() if var_dir else _default_var_dir()
    items = stale_tasks(var_dir)
    if dry_run or not items:
        return {"action": "reap_dry_run" if dry_run else "noop",
                "count": len(items), "tasks": items}
    db = var_dir / "cache" / "tasks.db"
    now = time.time()
    done = 0
    try:
        conn = sqlite3.connect(str(db), timeout=5)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
            sets = ["status='error'"]
            base_params: List = []
            if "error" in cols:
                sets.append("error=?")
                base_params.append("")
            if "finished_at" in cols:
                sets.append("finished_at=?")
                base_params.append(now)
            if "updated_at" in cols:
                sets.append("updated_at=?")
                base_params.append(now)
            sql = ("UPDATE tasks SET " + ", ".join(sets)
                   + " WHERE id=? AND status NOT IN ('done','error','failed',"
                     "'canceled','cancelled','exhausted')")
            for t in items:
                params = list(base_params)
                if "error" in cols:
                    params[0] = (f"任务进程已退出，未正常结束（原状态 {t['status']}，"
                                 f"停摆 {t['idle_minutes']} 分钟）；如需重跑请重新提交")
                cur = conn.execute(sql, (*params, t["id"]))
                done += cur.rowcount
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        return {"action": "reap_error", "count": 0, "error": str(e), "tasks": items}
    _PROC_CACHE["ts"] = 0.0  # 让下次判定立即重算
    return {"action": "reap", "count": done, "tasks": items}


def has_active_tasks(var_dir: Optional[Path] = None) -> bool:
    """是否还有**真正在跑**的任务（僵尸记录不算）。

    有就绝不自动清中间缓存，免得删掉正在读的文件。
    """
    var_dir = Path(var_dir).resolve() if var_dir else _default_var_dir()
    db = var_dir / "cache" / "tasks.db"
    if not db.exists():
        return False
    stale = _stale_ids(var_dir)
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            rows = conn.execute("SELECT id, status FROM tasks").fetchall()
            return any((s or "").lower() not in _TERMINAL and tid not in stale
                       for tid, s in rows)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def _walk_strings(obj) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


def _collect_candidates(var_dir: Path, protect: Set[Path],
                        include_output: bool) -> List[Tuple[Path, int, float]]:
    """可清理文件清单：(路径, 大小, mtime)，按时间从旧到新排。"""
    cfg = limits()
    now = time.time()
    upload_deadline = cfg["upload_keep_hours"] * 3600
    output_deadline = cfg["output_keep_days"] * 86400
    items: List[Tuple[Path, int, float]] = []

    roots = [var_dir / "cache", var_dir / "uploads"]
    if include_output:
        roots.append(var_dir / "output")

    for root in roots:
        for p in _iter_files(root):
            try:
                rp = p.resolve()
                if rp in protect or p.name in _ALWAYS_KEEP_NAMES:
                    continue
                st = p.stat()
            except OSError:
                continue
            age = now - st.st_mtime
            # 上传素材给一个"宽限期"：刚传完还没跑任务，别急着删
            if root.name == "uploads" and age < upload_deadline:
                # 但空间告急时宽限期失效，仍列入候选（放在队尾，最后才删）
                pass
            if root.name == "output" and age < output_deadline:
                continue
            items.append((p, st.st_size, st.st_mtime))

    # 分三轮删：先按时间清可再生的中间缓存，再考虑上传素材，最后才是成片。
    # 上传素材虽然也能重新传，但用户可能还要用同一素材再剪一次，别一上来就删光。
    def _rank(p: Path) -> int:
        try:
            top = p.relative_to(var_dir).parts[0]
        except (ValueError, IndexError):
            top = ""
        return {"cache": 0, "uploads": 1, "output": 2}.get(top, 0)

    items.sort(key=lambda it: (_rank(it[0]), it[2]))
    return items


def cleanup(var_dir: Optional[Path] = None, dry_run: bool = False,
            force: bool = False, allow_while_running: bool = False) -> Dict:
    """执行一次清理。返回可序列化的报告（dry_run 时只列清单不删）。

    force=True 时无视上限，压到 ``limit_gb`` 的 60%（给手动"立即清理"用）。
    默认只要有任务在跑就一律跳过：缓存命中的老文件 mtime 可能很旧，
    光靠"最近修改过"保护不住它们，最稳的是等这一轮渲染真正结束。
    """
    var_dir = Path(var_dir).resolve() if var_dir else _default_var_dir()
    cfg = limits()
    before = usage(var_dir)
    # 先把进程已退出的僵尸 running 归档：否则它们会永远挡住清理，
    # 还会让 WebUI 重启时反复续跑同一个卡死任务。
    reaped = reap_stale_tasks(var_dir, dry_run=dry_run)
    if has_active_tasks(var_dir) and not allow_while_running and not dry_run:
        return {"action": "noop",
                "reason": "有任务正在运行，等它跑完再清理（避免删掉正在使用的中间文件）",
                "reaped": reaped,
                "before": before, "freed_bytes": 0, "deleted": [],
                "dry_run": dry_run}
    limit_bytes = cfg["limit_gb"] * GB * (0.6 if force else 1.0)
    free_bytes = before["disk_free_gb"] * GB

    need = max(0, before["total_bytes"] - int(limit_bytes))
    if free_bytes < cfg["min_free_gb"] * GB:
        need = max(need, int(cfg["min_free_gb"] * GB - free_bytes))
    if need <= 0 and not force:
        # 即使 var/ 已达标，回收站里可能还压着上一轮删掉的缓存
        recycle = (purge_recycled_var(var_dir, dry_run=dry_run)
                   if cfg["purge_recycle"] else {"count": 0, "freed_gb": 0.0})
        return {"action": "noop", "reason": "未超上限，磁盘也够用",
                "reaped": reaped, "recycle": recycle,
                "before": before, "freed_bytes": 0, "deleted": [],
                "dry_run": dry_run}

    protect = protected_paths(var_dir)
    include_output = not bool(cfg["keep_output"])
    candidates = _collect_candidates(var_dir, protect, include_output)

    deleted: List[Dict] = []
    skipped_protected = len(protect)
    freed = 0
    for p, size, mtime in candidates:
        if freed >= need:
            break
        if not dry_run:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                # Windows 上被占用 / 只读：跳过，不影响其它文件
                continue
        freed += size
        deleted.append({"path": str(p), "gb": round(size / GB, 3),
                        "age_hours": round((time.time() - mtime) / 3600, 1)})

    # 删除可能被系统转成「移到回收站」：把本项目来源的条目永久清掉，
    # 否则 var/ 看着空了，C 盘空间却一点没还回来。
    recycle = (purge_recycled_var(var_dir, dry_run=dry_run)
               if cfg["purge_recycle"] else {"count": 0, "freed_gb": 0.0})

    after = usage(var_dir) if not dry_run else before
    return {
        "action": "dry_run" if dry_run else "cleanup",
        "before": {"total_gb": before["total_gb"],
                   "disk_free_gb": before["disk_free_gb"]},
        "after": {"total_gb": after["total_gb"],
                  "disk_free_gb": after["disk_free_gb"]},
        "freed_bytes": freed,
        "freed_gb": round(freed / GB, 2),
        "deleted": deleted,
        "deleted_count": len(deleted),
        "candidates": len(candidates),
        "skipped_protected": skipped_protected,
        "reaped": reaped,
        "recycle": recycle,
        "dry_run": dry_run,
    }


def dedupe_uploads(var_dir: Optional[Path] = None,
                   dry_run: bool = False) -> Dict:
    """删掉 uploads/ 里内容重复的副本，只留每份素材最新的一份。

    历史上传文件名是 ``<uuid>_原名``，同名同大小基本就是同一份素材。
    正式判断用「大小 + 头尾各 1MB 哈希」，避免只靠文件名误删。
    """
    import hashlib

    var_dir = Path(var_dir).resolve() if var_dir else _default_var_dir()
    upload_dir = var_dir / "uploads"
    protect = protected_paths(var_dir)
    groups: Dict[Tuple[str, int, str], List[Tuple[Path, float]]] = {}

    for p in _iter_files(upload_dir):
        try:
            if p.resolve() in protect:
                continue
            st = p.stat()
            sig = _content_signature(p, st.st_size, hashlib)
            key = (_UPLOAD_PREFIX_RE.sub("", p.name), st.st_size, sig)
            groups.setdefault(key, []).append((p, st.st_mtime))
        except OSError:
            continue

    deleted, kept, freed = [], 0, 0
    for key, entries in groups.items():
        if len(entries) < 2:
            kept += 1
            continue
        entries.sort(key=lambda e: e[1], reverse=True)  # 最新的留着
        for p, _ in entries[1:]:
            try:
                size = p.stat().st_size
                if not dry_run:
                    p.unlink(missing_ok=True)
                freed += size
                deleted.append({"path": str(p), "gb": round(size / GB, 3)})
            except OSError:
                continue
        kept += 1

    return {"action": "dedupe_dry_run" if dry_run else "dedupe",
            "groups": len(groups), "kept": kept,
            "deleted": deleted, "deleted_count": len(deleted),
            "freed_gb": round(freed / GB, 2), "dry_run": dry_run}


def _content_signature(path: Path, size: int, hashlib) -> str:
    """大小 + 头尾各 1MB 的 sha1，足够区分不同素材且很快。"""
    h = hashlib.sha1(str(size).encode())
    chunk = 1024 * 1024
    try:
        with open(path, "rb") as fh:
            h.update(fh.read(chunk))
            if size > chunk * 2:
                fh.seek(-chunk, os.SEEK_END)
                h.update(fh.read(chunk))
    except OSError:
        return "unreadable"
    return h.hexdigest()[:16]


# ------------------------------------------------------------ 回收站二次清理
# Windows 上有些环境会把删除「保护性」地转成移到回收站：var/ 看起来清空了，
# 但磁盘空间一点没还回来。所以清理完还要把**本项目 var/cache、var/uploads
# 来源**的回收站条目永久删掉。只认能从 $I 元数据解析出原路径的条目，
# 其它任何人的回收站内容一律不碰；output/ 成片也不在此列。

def _parse_recycle_meta(i_file: Path) -> Optional[Tuple[int, str]]:
    """解析 Windows 回收站 $I 文件 -> (原大小, 原路径)。"""
    try:
        raw = i_file.read_bytes()
        if len(raw) < 28:
            return None
        version, size, _ft = struct.unpack_from("<QQQ", raw, 0)
        name_len = struct.unpack_from("<I", raw, 24)[0]
        if version == 2:
            path = raw[28:28 + name_len * 2].decode("utf-16-le", errors="replace")
        else:
            path = raw[24:].decode("utf-16-le", errors="replace")
        return size, path.rstrip("\x00").split("\x00")[0]
    except (OSError, struct.error, UnicodeError, ValueError):
        return None


def recycle_bin_entries(var_dir: Optional[Path] = None,
                        recycle_root: Optional[Path] = None) -> List[Dict]:
    """列出回收站里来自本项目 var/cache、var/uploads 的条目（只读）。"""
    if os.name != "nt" and recycle_root is None:
        return []
    var = Path(var_dir).resolve() if var_dir else _default_var_dir()
    rb = (Path(recycle_root) if recycle_root
          else Path(var.drive + "\\") / "$Recycle.Bin")
    if not rb.exists():
        return []
    prefix = str(var).lower().rstrip("\\") + "\\"
    out: List[Dict] = []
    for i_file in rb.glob("*/$I*"):
        info = _parse_recycle_meta(i_file)
        if not info:
            continue
        size, origin = info
        low = origin.lower()
        if not low.startswith(prefix):
            continue
        rel = origin[len(prefix):]
        top = rel.split("\\", 1)[0].lower()
        if top not in ("cache", "uploads"):
            continue  # output/ 成片与 var 下其它内容绝不碰
        r_file = i_file.with_name("$R" + i_file.name[2:])
        out.append({"meta": i_file, "data": r_file, "size": size,
                    "origin": origin, "exists": r_file.exists()})
    return out


def purge_recycled_var(var_dir: Optional[Path] = None,
                       dry_run: bool = False,
                       recycle_root: Optional[Path] = None) -> Dict:
    """永久清除回收站里本项目缓存产生的条目，把磁盘空间真正还回来。"""
    entries = recycle_bin_entries(var_dir, recycle_root=recycle_root)
    freed, failed = 0, 0
    deleted = []
    for e in entries:
        if dry_run:
            freed += e["size"]
            deleted.append({"origin": e["origin"], "gb": round(e["size"] / GB, 4)})
            continue
        try:
            r = e["data"]
            if r.is_dir():
                shutil.rmtree(r)
            else:
                r.unlink(missing_ok=True)
            e["meta"].unlink(missing_ok=True)
            # 删不掉就如实计数，别虚报释放量
            if r.exists() or e["meta"].exists():
                failed += 1
                continue
            freed += e["size"]
            deleted.append({"origin": e["origin"], "gb": round(e["size"] / GB, 4)})
        except OSError:
            failed += 1
    return {"action": "purge_recycle_dry_run" if dry_run else "purge_recycle",
            "count": len(deleted), "freed_gb": round(freed / GB, 2),
            "failed": failed, "deleted": deleted, "dry_run": dry_run}


# ---------------------------------------------------------------- 自动触发
_gc_lock = threading.Lock()
_gc_running = False


def schedule_cleanup(var_dir: Optional[Path] = None, force: bool = False) -> bool:
    """后台触发一次清理；同一时刻只跑一个，不阻塞剪辑线程。"""
    global _gc_running
    with _gc_lock:
        if _gc_running:
            return False
        _gc_running = True

    def _job():
        global _gc_running
        try:
            report = cleanup(var_dir, force=force)
            if report.get("deleted_count"):
                print(f"[缓存管家] 清理 {report['deleted_count']} 个文件，"
                      f"释放 {report['freed_gb']:.2f} GB")
        except Exception as e:  # 清理失败绝不能影响主流程
            print(f"[缓存管家] 清理跳过：{type(e).__name__}: {e}")
        finally:
            with _gc_lock:
                _gc_running = False

    threading.Thread(target=_job, name="cache-gc", daemon=True).start()
    return True
