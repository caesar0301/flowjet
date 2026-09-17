#!/usr/bin/env python3
"""Real end-to-end demo of FlowJet ``interaction_mode``: ask vs agent.

Requires a running flowjet-server backed by soothe-nano ≥ 1.2.23
(``DualModeCoreAgent``).

Covers:

* ``ask`` — hard read-only (inspect with ls/read/glob/grep; no writes/shell)
* ``agent`` — full coding agent (mutating tools when enabled in nano.yml)
* Streaming with ``projection=progress`` in both modes
* Session pin: flipping mode on the same ``session`` → HTTP 400
* Switching modes by allocating a **new** ``session``

Start the server first::

    make run

Then::

    make examples-modes
    # or: uv run python examples/e2e_ask_agent_modes.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _client import event_extra, make_client, output_text, section
from openai import APIStatusError, BadRequestError


def _session(prefix: str) -> str:
    return f"fj-{prefix}-{uuid.uuid4().hex[:8]}"


def _stream_mode(
    client,
    *,
    model: str,
    mode: str,
    session: str,
    prompt: str,
) -> str:
    """Run one streamed turn; print progress + text; return final output text."""
    print(f"session={session} interaction_mode={mode}")
    print(f"prompt: {prompt}")
    print("---")
    with client.responses.stream(
        model=model,
        input=prompt,
        extra_body={
            "flowjet": {
                "projection": "progress",
                "session": session,
                "interaction_mode": mode,
                "metadata": {"suite": "e2e_ask_agent_modes"},
            }
        },
    ) as stream:
        for event in stream:
            if event.type == "response.flowjet.progress":
                stage = event_extra(event, "stage")
                message = event_extra(event, "message")
                print(f"[progress] {stage}: {message}")
            elif event.type == "response.output_text.delta":
                print(event.delta, end="", flush=True)
            elif event.type == "response.failed":
                err = getattr(event, "response", None)
                print(f"\n[failed] {err}")
        final = stream.get_final_response()
    print()
    print(f"status={final.status} id={final.id}")
    text = output_text(final)
    print(f"answer: {text!r}" if len(text) < 400 else f"answer: {text[:400]!r}…")
    return text


def main() -> None:
    client = make_client()
    print("FlowJet ask vs agent modes (OpenAI SDK)")
    print(f"base_url={client.base_url}")

    models = client.models.list()
    ids = [m.id for m in models.data]
    if not ids:
        raise SystemExit("No models configured; set FLOWJET_MODELS on the server.")
    model = ids[0]
    print(f"model={model}")

    # ------------------------------------------------------------------ ask
    section("1) ask mode — hard read-only inspect")
    ask_session = _session("ask")
    ask_text = _stream_mode(
        client,
        model=model,
        mode="ask",
        session=ask_session,
        prompt=(
            "You are in Ask mode. Without editing any files, briefly list what "
            "read-only tools you can use and name one file you can see in the "
            "workspace (if any). Keep the answer under 8 sentences."
        ),
    )
    if not ask_text.strip():
        print("[warn] empty ask answer — check nano config / LLM credentials")

    # ---------------------------------------------------------------- agent
    section("2) agent mode — full agent (separate session)")
    agent_session = _session("agent")
    agent_text = _stream_mode(
        client,
        model=model,
        mode="agent",
        session=agent_session,
        prompt=(
            "You are in Agent mode. Briefly explain the difference between Ask "
            "and Agent for this workspace, then list top-level entries you can "
            "see (read is fine; do not create or modify files unless needed to "
            "answer). Keep the answer under 10 sentences."
        ),
    )
    if not agent_text.strip():
        print("[warn] empty agent answer — check nano config / LLM credentials")

    # ----------------------------------------------------------- same ask again
    section("3) ask mode — second turn on the same ask session (pin holds)")
    follow = client.responses.create(
        model=model,
        input="Still in ask mode: reply with exactly the word ASK_OK.",
        extra_body={
            "flowjet": {
                "projection": "report",
                "session": ask_session,
                "interaction_mode": "ask",
            }
        },
    )
    print(f"status={follow.status} text={output_text(follow)!r}")

    # -------------------------------------------------------- pin conflict
    section("4) mode pin conflict — flip ask→agent on same session → 400")
    try:
        client.responses.create(
            model=model,
            input="This should be rejected by the mode pin.",
            extra_body={
                "flowjet": {
                    "projection": "report",
                    "session": ask_session,
                    "interaction_mode": "agent",
                }
            },
        )
        raise SystemExit("expected interaction_mode_conflict when flipping mode")
    except (BadRequestError, APIStatusError) as exc:
        status = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        code = None
        if isinstance(body, dict):
            code = (body.get("error") or {}).get("code")
        print(f"got {type(exc).__name__} status={status} code={code}")
        if status not in (400, None) and code not in (
            "interaction_mode_conflict",
            None,
        ):
            # Some SDK versions surface message only; still treat 400 as success.
            if status != 400:
                raise
        print("pin conflict rejected as expected (use a new session to switch modes)")

    # ----------------------------------------------------- switch via new session
    section("5) switch modes — new session starts agent cleanly")
    switched = _session("agent-switch")
    again = client.responses.create(
        model=model,
        input="Agent session after switch: reply with exactly the word AGENT_OK.",
        extra_body={
            "flowjet": {
                "projection": "report",
                "session": switched,
                "interaction_mode": "agent",
            }
        },
    )
    print(f"session={switched} status={again.status} text={output_text(again)!r}")

    section("Done")
    print("Ask / agent mode walkthrough completed.")
    print(
        "Tip: keep interaction_mode stable per session; allocate a new "
        "flowjet.session when changing ask ↔ agent."
    )


if __name__ == "__main__":
    main()
