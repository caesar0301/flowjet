"""Concurrent load tests against a real uvicorn flowjet-server.

These spin up the live ASGI app (same ``LiveServer`` fixture used by the OpenAI
SDK suite) and fire many parallel requests through the official ``openai``
client to exercise the full HTTP/SSE stack under contention:

* many non-stream creates running at once
* many SSE streams drained concurrently
* mixed stream + retrieve + delete traffic against a shared run store
* unique-response-id invariant: no two parallel runs must ever share an id

They target real concurrency bugs (store races, response-id collisions, event
interleaving) rather than business logic, which is already covered elsewhere.
"""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from openai import OpenAI
from openai.types.responses import Response


def _output_text(response: Response) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text:
        return text
    parts: list[str] = []
    for item in response.output:
        if getattr(item, "type", None) != "message":
            continue
        for block in getattr(item, "content", []) or []:
            if getattr(block, "type", None) == "output_text":
                parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def test_concurrent_non_stream_creates_unique_ids(openai_client: OpenAI) -> None:
    """N parallel non-stream creates must each succeed with a distinct id."""
    n = 24
    results: list[tuple[int, Response]] = []
    errors: list[BaseException] = []

    def one(i: int) -> tuple[int, Response]:
        r = openai_client.responses.create(
            model="default",
            input=f"concurrent create {i}",
        )
        return i, r

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, i) for i in range(n)]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    assert not errors, f"{len(errors)} request(s) failed: {errors[0]!r}"
    assert len(results) == n

    responses = [r for _, r in results]
    assert all(r.status == "completed" for r in responses)

    ids = [r.id for r in responses]
    assert len(set(ids)) == n, f"response ids collided: {Counter(ids).most_common(3)}"

    for i, r in sorted(results, key=lambda x: x[0]):
        assert _output_text(r) == f"Echo: concurrent create {i}"


def test_concurrent_streams_each_complete(openai_client: OpenAI) -> None:
    """N parallel SSE streams must each emit a complete, well-formed lifecycle."""
    n = 16
    results: list[dict] = []
    errors: list[BaseException] = []

    def one(i: int) -> dict:
        deltas: list[str] = []
        types: list[str] = []
        completed: Response | None = None
        for event in openai_client.responses.create(
            model="default",
            input=f"stream {i}",
            stream=True,
        ):
            et = getattr(event, "type", None)
            if isinstance(et, str):
                types.append(et)
            if et == "response.output_text.delta":
                deltas.append(getattr(event, "delta", "") or "")
            if et == "response.completed":
                completed = getattr(event, "response", None)
        return {"i": i, "types": types, "deltas": deltas, "completed": completed}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, i) for i in range(n)]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    assert not errors, f"{len(errors)} stream(s) failed: {errors[0]!r}"
    assert len(results) == n

    for r in sorted(results, key=lambda x: x["i"]):
        assert "response.created" in r["types"]
        assert "response.output_text.delta" in r["types"]
        assert "response.completed" in r["types"]
        assert "".join(r["deltas"]) == f"Echo: stream {r['i']}"
        assert r["completed"] is not None
        assert r["completed"].status == "completed"


def test_concurrent_retrieve_after_create(openai_client: OpenAI) -> None:
    """A retrieve issued right after each create must return the stored body."""
    n = 16
    outcomes: list[bool] = []
    errors: list[BaseException] = []

    def one(i: int) -> bool:
        created = openai_client.responses.create(
            model="default",
            input=f"persist {i}",
        )
        fetched = openai_client.responses.retrieve(created.id)
        return fetched.id == created.id and _output_text(fetched) == _output_text(created)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, i) for i in range(n)]
        for fut in as_completed(futures):
            try:
                outcomes.append(fut.result())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    assert not errors, f"{len(errors)} retrieve(s) failed: {errors[0]!r}"
    assert all(outcomes)


def test_concurrent_delete_is_idempotent_per_id(openai_client: OpenAI) -> None:
    """Each create→delete pair must delete exactly once; a second delete 404s."""
    n = 12
    results: list[tuple] = []
    errors: list[BaseException] = []

    from openai import NotFoundError

    def one(i: int) -> tuple:
        created = openai_client.responses.create(model="default", input=f"del {i}")
        rid = created.id
        raw = openai_client.responses.with_raw_response.delete(rid)
        first_ok = raw.http_response.status_code == 200
        try:
            openai_client.responses.retrieve(rid)
            second_404 = False
        except NotFoundError as exc:
            second_404 = exc.status_code == 404
        return (i, first_ok, second_404)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, i) for i in range(n)]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    assert not errors, f"{len(errors)} delete(s) failed: {errors[0]!r}"
    assert len(results) == n
    assert all(first_ok and second_404 for _, first_ok, second_404 in results)


def test_concurrent_mixed_load_does_not_corrupt_store(
    live_server: object,
    parse_sse,
) -> None:
    """Mixed create/stream/retrieve/delete traffic must not corrupt the store.

    Uses raw httpx so the SSE wire path is exercised directly under load and we
    can assert on the exact event types returned per stream.
    """
    base = getattr(live_server, "root_url", "")
    n_creates = 12
    n_streams = 12
    errors: list[str] = []

    def do_create(i: int) -> None:
        r = httpx.post(
            f"{base}/v1/responses",
            json={"model": "default", "input": f"mixed create {i}"},
            headers={"Authorization": "Bearer local-test-key"},
            timeout=60.0,
        )
        if r.status_code != 200:
            errors.append(f"create {i}: {r.status_code}")
            return
        rid = r.json()["id"]
        g = httpx.get(
            f"{base}/v1/responses/{rid}",
            headers={"Authorization": "Bearer local-test-key"},
            timeout=60.0,
        )
        if g.status_code != 200 or g.json()["id"] != rid:
            errors.append(f"retrieve {i}: {g.status_code}")

    def do_stream(i: int) -> None:
        r = httpx.post(
            f"{base}/v1/responses",
            json={
                "model": "default",
                "input": f"mixed stream {i}",
                "stream": True,
            },
            headers={"Authorization": "Bearer local-test-key"},
            timeout=60.0,
        )
        if r.status_code != 200:
            errors.append(f"stream {i}: {r.status_code}")
            return
        events = parse_sse(r.text)
        types = [e.get("type") for e in events]
        if "response.completed" not in types:
            errors.append(f"stream {i}: no response.completed in {types}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(do_create, i) for i in range(n_creates)]
        futures += [pool.submit(do_stream, i) for i in range(n_streams)]
        for fut in as_completed(futures):
            fut.result()

    assert not errors, f"corruption detected: {errors[:3]}"


def test_concurrent_streams_use_unique_item_ids(openai_client: OpenAI) -> None:
    """Parallel streams must not share message item ids (per-run ProjectionEngine)."""
    n = 10
    item_ids: list[str] = []
    lock = threading.Lock()

    def one(i: int) -> None:
        local: list[str] = []
        for event in openai_client.responses.create(
            model="default",
            input=f"itemid {i}",
            stream=True,
        ):
            if getattr(event, "type", None) == "response.output_text.delta":
                local.append(getattr(event, "item_id", "") or "")
        assert local, f"stream {i} produced no deltas"
        with lock:
            item_ids.extend(local)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(one, i) for i in range(n)]
        for fut in as_completed(futures):
            fut.result()

    assert len(item_ids) > n
    assert len(set(item_ids)) == n, (
        f"expected {n} distinct item ids across runs, got {len(set(item_ids))}"
    )
