#!/usr/bin/env python3
"""End-to-end raw HTTP coverage of the flowjet-server public API.

Uses ``httpx`` (no OpenAI SDK) so you can see the wire contract:

* ``GET  /health``
* ``GET  /v1/models``
* ``POST /v1/responses`` (JSON + SSE)
* ``GET  /v1/responses/{id}``
* ``DELETE /v1/responses/{id}``
* FlowJet ``flowjet`` request namespace + ``response.flowjet.*`` SSE events
* Auth / not-found error shapes

Start the server first::

    make run

Then::

    make examples-http
    # or: uv run python examples/e2e_http_api.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _client import (
    make_http,
    output_text_from_dict,
    parse_sse,
    require_completed,
    section,
)


def main() -> None:
    http = make_http()
    print("Raw HTTP → flowjet-server")
    print(f"root={http.base_url}")

    # ---------------------------------------------------------------- health
    section("GET /health")
    health = http.get("/health")
    health.raise_for_status()
    print(health.json())

    # ---------------------------------------------------------------- models
    section("GET /v1/models")
    models = http.get("/v1/models")
    models.raise_for_status()
    body = models.json()
    ids = [m["id"] for m in body.get("data", [])]
    print("models:", ", ".join(ids))
    if not ids:
        raise SystemExit("No models configured; set FLOWJET_MODELS on the server.")
    model = ids[0]

    # -------------------------------------------------------- create JSON
    section("POST /v1/responses  (stream=false)")
    created = http.post(
        "/v1/responses",
        json={"model": model, "input": "Raw HTTP create.", "stream": False},
    )
    created.raise_for_status()
    created_body = created.json()
    rid = created_body["id"]
    print(f"id={rid} status={created_body['status']}")
    print("text:", output_text_from_dict(created_body))

    # -------------------------------------------------------------- retrieve
    section(f"GET /v1/responses/{rid}")
    got = http.get(f"/v1/responses/{rid}")
    got.raise_for_status()
    print(f"status={got.json()['status']} text={output_text_from_dict(got.json())!r}")

    # ---------------------------------------------------------- SSE report
    section("POST /v1/responses  (SSE, report projection)")
    stream = http.post(
        "/v1/responses",
        json={
            "model": model,
            "input": "Raw SSE report.",
            "stream": True,
            "flowjet": {"projection": "report"},
        },
    )
    stream.raise_for_status()
    assert "text/event-stream" in stream.headers.get("content-type", "")
    events = parse_sse(stream.text)
    types = [e.get("type") for e in events]
    print("events:", ", ".join(t for t in types if t))
    require_completed(events, "report projection")
    assert "response.created" in types
    assert "response.output_text.delta" in types
    completed = next(e for e in events if e.get("type") == "response.completed")
    print("completed text:", output_text_from_dict(completed["response"]))

    # -------------------------------------------------------- SSE progress
    section("POST /v1/responses  (SSE, progress projection)")
    progress = http.post(
        "/v1/responses",
        json={
            "model": model,
            "input": "Raw SSE progress.",
            "stream": True,
            "flowjet": {
                "projection": "progress",
                "session": "fj-e2e-http-progress",
                "metadata": {"suite": "e2e_http_api"},
            },
        },
    )
    progress.raise_for_status()
    pev = parse_sse(progress.text)
    ptypes = [e.get("type") for e in pev]
    print("events:", ", ".join(t for t in ptypes if t))
    require_completed(pev, "progress projection")
    for e in pev:
        if e.get("type") == "response.flowjet.progress":
            print(f"[progress] {e.get('stage')}: {e.get('message')}")
    assert "response.flowjet.progress" in ptypes

    # ------------------------------------------------------- SSE developer
    section("POST /v1/responses  (SSE, developer projection)")
    developer = http.post(
        "/v1/responses",
        json={
            "model": model,
            "input": "Raw SSE developer.",
            "stream": True,
            # emit_tools is honoured only by the fake backend; a real agent
            # decides for itself whether the prompt warrants a tool call.
            "flowjet": {
                "projection": "developer",
                "metadata": {"emit_tools": True, "suite": "e2e_http_api"},
            },
        },
    )
    developer.raise_for_status()
    dev = parse_sse(developer.text)
    dtypes = [e.get("type") for e in dev]
    print("events:", ", ".join(t for t in dtypes if t))
    require_completed(dev, "developer projection")
    for e in dev:
        if e.get("type") == "response.flowjet.tool.started":
            print(f"[tool started] {e.get('tool')}")
        elif e.get("type") == "response.flowjet.tool.completed":
            print(
                f"[tool done] {e.get('tool')} ok={e.get('ok')} duration_ms={e.get('duration_ms')}"
            )
        # Whatever the backend did, tool arguments must never reach the wire.
        assert "arguments" not in e
    if "response.flowjet.tool.started" not in dtypes:
        print("[no tool calls] a real agent only emits these when the prompt needs a tool")

    # ---------------------------------------------------------------- delete
    section(f"DELETE /v1/responses/{rid}")
    deleted = http.delete(f"/v1/responses/{rid}")
    deleted.raise_for_status()
    print(deleted.json())
    missing = http.get(f"/v1/responses/{rid}")
    print(f"GET after delete → HTTP {missing.status_code}")
    print(missing.json())
    assert missing.status_code == 404

    # --------------------------------------------------------- error shapes
    section("Error shapes")
    bad_model = http.post("/v1/responses", json={"model": "nope", "input": "x"})
    print(f"unknown model → HTTP {bad_model.status_code}: {bad_model.json()}")
    assert bad_model.status_code == 404
    assert bad_model.json()["error"]["code"] == "model_not_found"

    bad_id = http.get("/v1/responses/resp_does_not_exist")
    print(f"missing id → HTTP {bad_id.status_code}: {bad_id.json()}")
    assert bad_id.status_code == 404

    section("Done")
    print("All raw HTTP end-to-end steps completed.")


if __name__ == "__main__":
    main()
