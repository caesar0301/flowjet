"""List LangGraph threads from the sqlite checkpointer."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

_PREVIEW_LIMIT = 72
_SLUG_HASH_LEN = 12
_UNSAFE_SLUG_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class ConcurrentSessionError(RuntimeError):
    """Raised when another ``fj`` process holds the lock for the same thread."""


def _soothe_home() -> Path:
    """Resolve soothe home without importing soothe-nano."""
    env = os.environ.get("SOOTHE_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".soothe"


def resolve_workdir(start: Path | None = None) -> Path:
    """Return the project root for ``start``: nearest ancestor with ``.git``, else ``start``.

    ``.git`` may be a directory (normal clone) or a file (worktree/submodule).
    """
    base = (start or Path.cwd()).expanduser()
    try:
        base = base.resolve()
    except OSError:
        base = base.absolute()
    for candidate in (base, *base.parents):
        if (candidate / ".git").exists():
            return candidate
    return base


def workdir_slug(path: Path) -> str:
    """Readable, collision-free file name for a workdir (``name-<hash>``)."""
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:_SLUG_HASH_LEN]
    name = _UNSAFE_SLUG_CHARS.sub("-", path.name).strip("-") or "root"
    return f"{name}-{digest}"


def active_thread_path(workdir: Path | None = None) -> Path:
    """Return this workdir's active-thread pointer under ``~/.soothe/data/active_threads/``."""
    root = resolve_workdir(workdir)
    return _soothe_home() / "data" / "active_threads" / workdir_slug(root)


def thread_lock_path(thread_id: str) -> Path:
    """Return per-thread lock file under ``~/.soothe/data/locks/``."""
    safe = thread_id.strip().replace("/", "_")
    return _soothe_home() / "data" / "locks" / f"{safe}.lock"


def read_active_thread_id(
    path: Path | None = None,
    *,
    workdir: Path | None = None,
) -> str | None:
    """Return the thread id pinned for ``workdir`` (default: current project), if any."""
    file = path or active_thread_path(workdir)
    try:
        text = file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def write_active_thread_id(
    thread_id: str,
    path: Path | None = None,
    *,
    workdir: Path | None = None,
) -> None:
    """Pin ``thread_id`` as the active conversation for ``workdir``'s later queries."""
    file = path or active_thread_path(workdir)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(thread_id.strip() + "\n", encoding="utf-8")


def new_thread_id() -> str:
    """Allocate a fresh ``<uuid>`` thread id."""
    return str(uuid.uuid4())


def _lock_holder_pid(lock_path: Path) -> int | None:
    try:
        text = lock_path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return None
    if not text:
        return None
    try:
        return int(text[0].strip())
    except ValueError:
        return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


@contextlib.contextmanager
def hold_thread_lock(thread_id: str, *, blocking: bool = False) -> Iterator[None]:
    """Exclusive lock for one thread for the duration of an ``fj`` query.

    Different threads may run concurrently; two CLIs on the same thread are
    refused. Non-blocking by default: if another live ``fj`` holds the lock,
    raises :class:`ConcurrentSessionError`.

    Uses ``fcntl.flock`` (POSIX). The kernel releases the lock if the holder
    exits, so stale locks after crashes are not sticky.
    """
    import fcntl

    tid = thread_id.strip()
    if not tid:
        raise ValueError("thread_id must be non-empty")
    lock_path = thread_lock_path(tid)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            holder = _lock_holder_pid(lock_path)
            if holder is not None and _pid_is_alive(holder):
                msg = (
                    f"another fj process is already running on thread {tid} "
                    f"(pid {holder}); wait for it to finish"
                )
            else:
                msg = (
                    f"another fj process is already running on thread {tid}; wait for it to finish"
                )
            raise ConcurrentSessionError(msg) from exc
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass(frozen=True)
class ThreadInfo:
    """One persisted conversation thread."""

    thread_id: str
    updated_at: str | None = None
    preview: str | None = None


def simplify_timestamp(ts: str | None) -> str | None:
    """Truncate an ISO timestamp to second precision (``YYYY-MM-DD HH:MM:SS``)."""
    if not ts or not ts.strip():
        return None
    text = ts.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        if "T" in text:
            date, _, rest = text.partition("T")
            time_part = rest.split("+", 1)[0].split("-", 1)[0].split(".", 1)[0]
            if len(time_part) >= 8:
                return f"{date} {time_part[:8]}"
        return text[:19]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return " ".join(p.strip() for p in parts if p and p.strip())
    return str(content).strip() if content is not None else ""


def _is_human_message(msg: Any) -> bool:
    msg_type = getattr(msg, "type", None)
    if msg_type == "human":
        return True
    name = type(msg).__name__
    return name in {"HumanMessage", "HumanMessageChunk"}


def preview_user_request(payload: Any, *, limit: int = _PREVIEW_LIMIT) -> str | None:
    """Extract a one-line preview of the first human message from a writes payload."""
    messages = payload if isinstance(payload, list) else [payload]
    for msg in messages:
        if not _is_human_message(msg):
            continue
        text = _content_text(getattr(msg, "content", None))
        if not text:
            continue
        text = " ".join(text.split())
        if len(text) > limit:
            return text[: limit - 1] + "…"
        return text
    return None


def _activity_sort_key(item: tuple[str, str | None, str]) -> tuple[bool, str, str]:
    """Sort key for ``reverse=True``: newest ``updated_at``, then latest checkpoint id."""
    _tid, updated_at, latest_cid = item
    return (updated_at is not None, updated_at or "", latest_cid)


async def _load_thread_activity(
    checkpointer: Any,
) -> list[tuple[str, str | None, str]]:
    """Return ``(thread_id, updated_at, latest_checkpoint_id)`` for every thread.

    ``updated_at`` comes from the latest checkpoint's ``ts`` (last activity), not
    the thread's first checkpoint (creation).
    """
    query = """
        SELECT c.thread_id, t.latest, c.type, c.checkpoint
        FROM checkpoints c
        INNER JOIN (
            SELECT thread_id, MAX(checkpoint_id) AS latest
            FROM checkpoints
            WHERE checkpoint_ns = ''
            GROUP BY thread_id
        ) t
          ON c.thread_id = t.thread_id
         AND c.checkpoint_id = t.latest
         AND c.checkpoint_ns = ''
    """
    rows: list[tuple[str, str | None, str]] = []
    async with checkpointer.conn.execute(query) as cur:
        for thread_id, latest, type_, blob in await cur.fetchall():
            updated_at: str | None = None
            try:
                checkpoint = checkpointer.serde.loads_typed((type_, blob))
                if isinstance(checkpoint, dict):
                    updated_at = simplify_timestamp(checkpoint.get("ts"))
            except Exception:
                updated_at = None
            rows.append((str(thread_id), updated_at, str(latest)))
    rows.sort(key=_activity_sort_key, reverse=True)
    return rows


async def resolve_thread_id(
    checkpointer: Any,
    *,
    explicit: str | None = None,
    follow: bool = False,
    workdir: Path | None = None,
) -> str:
    """Choose the thread for a query and pin it as active for this workdir.

    Priority: ``-t`` explicit id, else ``-f``/``--follow`` (the id pinned for this
    workdir, else a new id), else a new id. Follow never crosses projects: a
    thread pinned in another workdir is only reachable with an explicit ``-t``.
    """
    if explicit:
        tid = explicit.strip()
    elif follow:
        tid = read_active_thread_id(workdir=workdir) or new_thread_id()
    else:
        tid = new_thread_id()
    write_active_thread_id(tid, workdir=workdir)
    return tid


async def _load_previews(checkpointer: Any, thread_ids: list[str]) -> dict[str, str | None]:
    """Map thread_id → latest human-message preview (most recent activity)."""
    if not thread_ids:
        return {}
    placeholders = ",".join("?" for _ in thread_ids)
    query = f"""
        SELECT w.thread_id, w.checkpoint_id, w.idx, w.type, w.value
        FROM writes w
        WHERE w.channel = 'messages'
          AND w.checkpoint_ns = ''
          AND w.thread_id IN ({placeholders})
        ORDER BY w.checkpoint_id DESC, w.idx DESC
    """
    out: dict[str, str | None] = {}
    async with checkpointer.conn.execute(query, thread_ids) as cur:
        rows = await cur.fetchall()
    for thread_id, _cid, _idx, type_, value in rows:
        tid = str(thread_id)
        if tid in out:
            continue
        try:
            payload = checkpointer.serde.loads_typed((type_, value))
            preview = preview_user_request(payload)
        except Exception:
            continue
        if preview:
            out[tid] = preview
    for tid in thread_ids:
        out.setdefault(tid, None)
    return out


DEFAULT_LIST_LIMIT = 20


async def list_threads(checkpointer: Any, *, limit: int = DEFAULT_LIST_LIMIT) -> list[ThreadInfo]:
    """Return threads ordered by latest activity time (newest first).

    Activity is the latest checkpoint ``ts``, not thread creation. ``limit`` caps
    how many rows are returned (default ``20``). Use a non-positive value to
    return all threads.
    """
    if checkpointer is None:
        return []

    activity = await _load_thread_activity(checkpointer)
    if limit > 0:
        activity = activity[:limit]

    ordered = [tid for tid, _ts, _cid in activity]
    updated = {tid: ts for tid, ts, _cid in activity}
    previews = await _load_previews(checkpointer, ordered)
    return [
        ThreadInfo(
            thread_id=tid,
            updated_at=updated.get(tid),
            preview=previews.get(tid),
        )
        for tid in ordered
    ]


def format_thread_list(threads: list[ThreadInfo]) -> str:
    """Format threads: ``tid  timestamp  preview`` (newest first)."""
    if not threads:
        return ""
    tid_width = max(len(t.thread_id) for t in threads)
    ts_width = max((len(t.updated_at) for t in threads if t.updated_at), default=0)
    lines: list[str] = []
    for t in threads:
        parts = [t.thread_id.ljust(tid_width)]
        if ts_width:
            parts.append((t.updated_at or "").ljust(ts_width))
        elif t.updated_at:
            parts.append(t.updated_at)
        if t.preview:
            parts.append(t.preview)
        lines.append("  ".join(parts).rstrip())
    return "\n".join(lines) + "\n"


def write_thread_list(threads: list[ThreadInfo], out: TextIO) -> None:
    out.write(format_thread_list(threads))
