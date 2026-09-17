"""Unit tests for thread listing."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from flowjet.cli.threads import (
    ThreadInfo,
    format_thread_list,
    list_threads,
    preview_user_request,
    simplify_timestamp,
)


def test_simplify_timestamp_to_seconds() -> None:
    assert simplify_timestamp("2026-07-21T08:57:45.236018+00:00") == "2026-07-21 08:57:45"
    assert simplify_timestamp("2026-07-21T08:57:45Z") == "2026-07-21 08:57:45"
    assert simplify_timestamp(None) is None


def test_preview_user_request_first_human() -> None:
    preview = preview_user_request(
        [HumanMessage(content="hello world"), AIMessage(content="hi")],
        limit=20,
    )
    assert preview == "hello world"


def test_preview_user_request_truncates() -> None:
    preview = preview_user_request([HumanMessage(content="x" * 100)], limit=10)
    assert preview is not None
    assert len(preview) == 10
    assert preview.endswith("…")


def test_format_thread_list_empty() -> None:
    assert format_thread_list([]) == ""


def test_format_thread_list_columns() -> None:
    text = format_thread_list(
        [
            ThreadInfo("fj-short", "2026-07-21 10:00:00", "hello"),
            ThreadInfo("fj-longer-id", "2026-07-20 09:00:00", None),
        ]
    )
    lines = text.strip().splitlines()
    assert lines[0].startswith("fj-short")
    assert "2026-07-21 10:00:00" in lines[0]
    assert lines[0].endswith("hello")
    assert "fj-longer-id" in lines[1]
    assert "2026-07-20 09:00:00" in lines[1]


class _FakeConn:
    """Minimal async sqlite-like connection for thread listing tests."""

    def __init__(self, activity_rows: list[tuple], preview_rows: list[tuple]) -> None:
        self._activity_rows = activity_rows
        self._preview_rows = preview_rows
        self._query = ""

    def execute(self, query: str, params: tuple[object, ...] = ()):
        self._query = query
        self._params = params
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def fetchall(self):
        q = self._query
        if "c.checkpoint" in q and "MAX(checkpoint_id)" in q:
            return self._activity_rows
        if "channel = 'messages'" in q or 'channel = "messages"' in q:
            return self._preview_rows
        if "w.channel" in q:
            return self._preview_rows
        return []


class _FakeSerde:
    def __init__(self, blobs: dict[bytes, object]) -> None:
        self._blobs = blobs

    def loads_typed(self, data: tuple[str, bytes]) -> object:
        _type, blob = data
        return self._blobs[blob]


class _FakeCp:
    def __init__(self, conn: _FakeConn, serde: _FakeSerde) -> None:
        self.conn = conn
        self.serde = serde


@pytest.mark.asyncio
async def test_resolve_thread_id_priority(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import threads as threads_mod

    path = tmp_path / "active"
    monkeypatch.setattr(threads_mod, "active_thread_path", lambda *_a, **_k: path)

    # Pinned active is ignored unless --follow.
    threads_mod.write_active_thread_id("fj-pinned")
    default_id = await threads_mod.resolve_thread_id(object())
    assert default_id
    assert default_id != "fj-pinned"

    # --follow uses pinned active.
    threads_mod.write_active_thread_id("fj-pinned")
    assert await threads_mod.resolve_thread_id(object(), follow=True) == "fj-pinned"

    # Explicit -t wins.
    assert await threads_mod.resolve_thread_id(object(), explicit="fj-explicit") == "fj-explicit"
    assert threads_mod.read_active_thread_id() == "fj-explicit"

    # No pin for this workdir: follow starts a fresh thread instead of borrowing
    # the newest thread from another project.
    path.unlink()
    fresh = await threads_mod.resolve_thread_id(object(), follow=True)
    assert fresh not in {"fj-pinned", "fj-explicit"}
    assert threads_mod.read_active_thread_id() == fresh


def test_active_thread_roundtrip(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import threads as threads_mod

    path = tmp_path / "active"
    monkeypatch.setattr(threads_mod, "active_thread_path", lambda *_a, **_k: path)
    assert threads_mod.read_active_thread_id() is None
    threads_mod.write_active_thread_id("fj-abc")
    assert threads_mod.read_active_thread_id() == "fj-abc"


def test_resolve_workdir_finds_git_root(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.threads import resolve_workdir

    repo = tmp_path / "repo"
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()

    assert resolve_workdir(nested) == repo.resolve()
    assert resolve_workdir(repo) == repo.resolve()

    # Worktrees / submodules store ``.git`` as a file.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    assert resolve_workdir(worktree) == worktree.resolve()


def test_resolve_workdir_without_repo_uses_dir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli.threads import resolve_workdir

    plain = tmp_path / "plain"
    plain.mkdir()
    assert resolve_workdir(plain) == plain.resolve()


def test_active_thread_is_scoped_per_workdir(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import threads as threads_mod

    monkeypatch.setattr(threads_mod, "_soothe_home", lambda: tmp_path / "home")
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()

    assert threads_mod.active_thread_path(one) != threads_mod.active_thread_path(two)

    threads_mod.write_active_thread_id("fj-one", workdir=one)
    assert threads_mod.read_active_thread_id(workdir=one) == "fj-one"
    assert threads_mod.read_active_thread_id(workdir=two) is None

    threads_mod.write_active_thread_id("fj-two", workdir=two)
    assert threads_mod.read_active_thread_id(workdir=one) == "fj-one"
    assert threads_mod.read_active_thread_id(workdir=two) == "fj-two"


@pytest.mark.asyncio
async def test_resolve_thread_id_follow_ignores_other_workdir(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import threads as threads_mod

    monkeypatch.setattr(threads_mod, "_soothe_home", lambda: tmp_path / "home")
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()

    first = await threads_mod.resolve_thread_id(object(), follow=True, workdir=one)
    assert await threads_mod.resolve_thread_id(object(), follow=True, workdir=one) == first

    other = await threads_mod.resolve_thread_id(object(), follow=True, workdir=two)
    assert other != first
    assert threads_mod.read_active_thread_id(workdir=one) == first


def test_hold_thread_lock_blocks_same_thread(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flowjet.cli import threads as threads_mod

    monkeypatch.setattr(threads_mod, "_soothe_home", lambda: tmp_path)

    with threads_mod.hold_thread_lock("fj-a"):
        with pytest.raises(threads_mod.ConcurrentSessionError, match="thread fj-a"):
            with threads_mod.hold_thread_lock("fj-a"):
                pass

    # Different threads may run concurrently.
    with threads_mod.hold_thread_lock("fj-a"):
        with threads_mod.hold_thread_lock("fj-b"):
            pass

    # Released — second acquire on same thread succeeds.
    lock = threads_mod.thread_lock_path("fj-a")
    with threads_mod.hold_thread_lock("fj-a"):
        assert lock.read_text(encoding="utf-8").strip().isdigit()


@pytest.mark.asyncio
async def test_list_threads_none_checkpointer() -> None:
    assert await list_threads(None) == []


@pytest.mark.asyncio
async def test_list_threads_orders_by_activity_ts_not_checkpoint_id() -> None:
    conn = _FakeConn(
        activity_rows=[
            ("fj-stale", "cp-z-late-id", "msgpack", b"stale"),
            ("fj-active", "cp-a-early-id", "msgpack", b"active"),
        ],
        preview_rows=[
            # Newest-first scan: latest human for each thread.
            ("fj-active", "cp-2", 0, "msgpack", b"active-latest"),
            ("fj-active", "cp-0", 0, "msgpack", b"active-first"),
            ("fj-stale", "cp-1", 0, "msgpack", b"stale-msg"),
        ],
    )
    serde = _FakeSerde(
        {
            b"stale": {"ts": "2026-07-20T12:00:00.999999+00:00"},
            b"active": {"ts": "2026-07-21T12:00:00.123456+00:00"},
            b"active-latest": [HumanMessage(content="follow-up question")],
            b"active-first": [HumanMessage(content="original question")],
            b"stale-msg": [HumanMessage(content="older question")],
        }
    )

    threads = await list_threads(_FakeCp(conn, serde))
    assert [t.thread_id for t in threads] == ["fj-active", "fj-stale"]
    assert threads[0].updated_at == "2026-07-21 12:00:00"
    assert threads[0].preview == "follow-up question"
    assert threads[1].preview == "older question"

    limited = await list_threads(_FakeCp(conn, serde), limit=1)
    assert [t.thread_id for t in limited] == ["fj-active"]


@pytest.mark.asyncio
async def test_list_threads_preview_skips_ai_only_writes() -> None:
    conn = _FakeConn(
        activity_rows=[("fj-1", "cp-9", "msgpack", b"ck")],
        preview_rows=[
            ("fj-1", "cp-9", 0, "msgpack", b"ai-only"),
            ("fj-1", "cp-8", 0, "msgpack", b"human"),
        ],
    )
    serde = _FakeSerde(
        {
            b"ck": {"ts": "2026-07-21T12:00:00+00:00"},
            b"ai-only": [AIMessage(content="thinking")],
            b"human": [HumanMessage(content="real question")],
        }
    )
    threads = await list_threads(_FakeCp(conn, serde))
    assert threads[0].preview == "real question"
