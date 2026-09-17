#!/usr/bin/env python3
"""End-to-end OpenAI SDK walkthrough against flowjet-server.

Covers the user-facing Responses lifecycle with the official ``openai`` package:

* ``models.list``
* ``responses.create`` (string + message-list input)
* ``responses.stream`` / ``stream=True``
* ``responses.retrieve`` / ``responses.delete``
* FlowJet ``extra_body`` projections (report / progress / developer)
* Expected error paths (unknown model, missing response)

Start the server first::

    make run

Then::

    make examples-sdk
    # or: uv run python examples/e2e_openai_sdk.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _client import event_extra, make_client, output_text, section
from openai import NotFoundError


def main() -> None:
    client = make_client()
    print("OpenAI SDK → flowjet-server")
    print(f"base_url={client.base_url}")

    # ------------------------------------------------------------------ models
    section("GET /v1/models  (client.models.list)")
    models = client.models.list()
    ids = [m.id for m in models.data]
    print("models:", ", ".join(ids) or "(none)")
    if not ids:
        raise SystemExit("No models configured; set FLOWJET_MODELS on the server.")
    model = ids[0]

    # ---------------------------------------------------------- create (text)
    section("POST /v1/responses  (non-stream, string input)")
    created = client.responses.create(
        model=model,
        input="End-to-end create: say hello from FlowJet.",
    )
    print(f"id={created.id} status={created.status}")
    print("text:", output_text(created))

    # --------------------------------------------------- create (list input)
    section("POST /v1/responses  (message-list input)")
    listed = client.responses.create(
        model=model,
        input=[
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "List-input ping."}],
            }
        ],
    )
    print(f"id={listed.id} text={output_text(listed)!r}")

    # --------------------------------------------------------------- stream
    section("POST /v1/responses  (stream via responses.stream)")
    deltas: list[str] = []
    with client.responses.stream(
        model=model,
        input="Stream a short hello.",
    ) as stream:
        for event in stream:
            if event.type == "response.output_text.delta":
                print(event.delta, end="", flush=True)
                deltas.append(event.delta)
        final = stream.get_final_response()
    print()
    print(f"id={final.id} status={final.status} joined={''.join(deltas)!r}")

    # ------------------------------------------------- stream=True shortcut
    section("POST /v1/responses  (stream=True iterator)")
    types: list[str] = []
    for event in client.responses.create(
        model=model,
        input="stream=True path",
        stream=True,
    ):
        types.append(event.type)
    print("event types:", ", ".join(dict.fromkeys(types)))

    # -------------------------------------------------------- retrieve/delete
    section("GET/DELETE /v1/responses/{id}")
    fetched = client.responses.retrieve(created.id)
    print(f"retrieve ok id={fetched.id} text={output_text(fetched)!r}")

    raw = client.responses.with_raw_response.delete(created.id)
    print(f"delete HTTP {raw.http_response.status_code}: {raw.http_response.json()}")
    try:
        client.responses.retrieve(created.id)
        raise SystemExit("expected NotFoundError after delete")
    except NotFoundError as exc:
        print(f"retrieve after delete → {type(exc).__name__} ({exc.status_code})")

    # ------------------------------------------------------ flowjet report
    section("flowjet.projection=report  (default / explicit)")
    report = client.responses.create(
        model=model,
        input="Report projection only returns final text.",
        extra_body={"flowjet": {"projection": "report", "session": "fj-e2e-report"}},
    )
    print(f"status={report.status} text={output_text(report)!r}")

    # ---------------------------------------------------- flowjet progress
    section("flowjet.projection=progress  (SSE + extra_body)")
    with client.responses.stream(
        model=model,
        input="Progress projection demo.",
        extra_body={
            "flowjet": {
                "projection": "progress",
                "session": "fj-e2e-progress",
                "metadata": {"suite": "e2e_openai_sdk"},
            }
        },
    ) as stream:
        for event in stream:
            if event.type == "response.flowjet.progress":
                print(f"[progress] {event_extra(event, 'stage')}: {event_extra(event, 'message')}")
            elif event.type == "response.output_text.delta":
                print(event.delta, end="", flush=True)
        progress_final = stream.get_final_response()
    print()
    print(f"final={output_text(progress_final)!r}")

    # --------------------------------------------------- flowjet developer
    section("flowjet.projection=developer  (tool summaries, no args)")
    with client.responses.stream(
        model=model,
        input="Developer projection demo.",
        # emit_tools is honoured only by the fake backend; a real agent decides
        # for itself whether the prompt warrants a tool call.
        extra_body={
            "flowjet": {
                "projection": "developer",
                "metadata": {"emit_tools": True, "suite": "e2e_openai_sdk"},
            }
        },
    ) as stream:
        saw_tool = False
        for event in stream:
            if event.type == "response.flowjet.tool.started":
                saw_tool = True
                print(f"[tool started] {event_extra(event, 'tool')}")
            elif event.type == "response.flowjet.tool.completed":
                print(
                    f"[tool done] {event_extra(event, 'tool')} "
                    f"ok={event_extra(event, 'ok')} "
                    f"duration_ms={event_extra(event, 'duration_ms')}"
                )
            elif event.type == "response.output_text.delta":
                print(event.delta, end="", flush=True)
        dev_final = stream.get_final_response()
    print()
    if not saw_tool:
        print("[no tool calls] a real agent only emits these when the prompt needs a tool")
    print(f"final={output_text(dev_final)!r}")

    # ------------------------------------------------------- flowjet ask mode
    # Full ask vs agent walkthrough: examples/e2e_ask_agent_modes.py
    #   (make examples-modes)
    section("flowjet.interaction_mode=ask  (hard read-only nano)")
    ask = client.responses.create(
        model=model,
        input="Ask mode: briefly describe what you can inspect without editing files.",
        extra_body={
            "flowjet": {
                "projection": "report",
                "session": "fj-e2e-ask",
                "interaction_mode": "ask",
            }
        },
    )
    print(f"status={ask.status} text={output_text(ask)!r}")
    print("(see make examples-modes for ask + agent + pin conflict)")

    # ----------------------------------------------------------- error path
    section("Error: unknown model → NotFoundError")
    try:
        client.responses.create(model="does-not-exist", input="x")
        raise SystemExit("expected NotFoundError for unknown model")
    except NotFoundError as exc:
        print(f"{type(exc).__name__}: {exc.message}")

    section("Done")
    print("All OpenAI SDK end-to-end steps completed.")


if __name__ == "__main__":
    main()
