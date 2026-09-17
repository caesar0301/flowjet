"""OpenAI Python SDK compatibility integration tests.

These hit a real uvicorn process so the official ``openai`` client exercises
the same HTTP/SSE path production SDKs use.
"""

from __future__ import annotations

import pytest
from openai import AuthenticationError, NotFoundError, OpenAI
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


def test_sdk_lists_models(openai_client: OpenAI) -> None:
    page = openai_client.models.list()
    ids = {m.id for m in page.data}
    assert "default" in ids
    assert "researcher" in ids
    assert page.object == "list"


def test_sdk_create_non_stream_string_input(openai_client: OpenAI) -> None:
    response = openai_client.responses.create(
        model="default",
        input="hello from sdk",
    )
    assert isinstance(response, Response)
    assert response.id.startswith("resp_")
    assert response.object == "response"
    assert response.status == "completed"
    assert response.model == "default"
    assert _output_text(response).startswith("Echo: hello from sdk")
    assert response.parallel_tool_calls is True
    assert response.tools == []
    assert response.usage is not None
    assert response.usage.total_tokens >= 0


def test_sdk_create_with_message_list_input(openai_client: OpenAI) -> None:
    response = openai_client.responses.create(
        model="researcher",
        input=[
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "list input"}],
            }
        ],
    )
    assert response.status == "completed"
    assert "list input" in _output_text(response)


def test_sdk_stream_report_projection(openai_client: OpenAI) -> None:
    stream = openai_client.responses.create(
        model="default",
        input="stream please",
        stream=True,
    )
    types: list[str] = []
    deltas: list[str] = []
    completed: Response | None = None
    for event in stream:
        et = getattr(event, "type", None)
        if isinstance(et, str):
            types.append(et)
        if et == "response.output_text.delta":
            deltas.append(getattr(event, "delta", "") or "")
        if et == "response.completed":
            completed = getattr(event, "response", None)

    assert "response.created" in types
    assert "response.in_progress" in types
    assert "response.output_text.delta" in types
    assert "response.output_text.done" in types
    assert "response.completed" in types
    assert "".join(deltas).startswith("Echo: stream please")
    assert completed is not None
    assert completed.status == "completed"
    assert _output_text(completed).startswith("Echo: stream please")


def test_sdk_stream_context_manager(openai_client: OpenAI) -> None:
    text_parts: list[str] = []
    with openai_client.responses.stream(
        model="default",
        input="context manager stream",
    ) as stream:
        for event in stream:
            if event.type == "response.output_text.delta":
                text_parts.append(event.delta)
        final = stream.get_final_response()

    assert "".join(text_parts).startswith("Echo: context manager stream")
    assert final.status == "completed"
    assert _output_text(final).startswith("Echo: context manager stream")


def test_sdk_retrieve_and_delete(openai_client: OpenAI) -> None:
    created = openai_client.responses.create(model="default", input="persist me")
    fetched = openai_client.responses.retrieve(created.id)
    assert fetched.id == created.id
    assert _output_text(fetched) == _output_text(created)

    # openai>=2 delete() is typed to return None; inspect raw body for contract.
    raw = openai_client.responses.with_raw_response.delete(created.id)
    assert raw.http_response.status_code == 200
    body = raw.http_response.json()
    assert body["id"] == created.id
    assert body["deleted"] is True

    with pytest.raises(NotFoundError) as exc:
        openai_client.responses.retrieve(created.id)
    assert exc.value.status_code == 404


def test_sdk_delete_twice_is_not_found(openai_client: OpenAI) -> None:
    created = openai_client.responses.create(model="default", input="to delete")
    assert openai_client.responses.delete(created.id) is None
    with pytest.raises(NotFoundError):
        openai_client.responses.delete(created.id)


def test_sdk_unknown_model_raises_not_found(openai_client: OpenAI) -> None:
    with pytest.raises(NotFoundError) as exc:
        openai_client.responses.create(model="does-not-exist", input="x")
    assert exc.value.status_code == 404
    body = exc.value.response.json()
    assert body["error"]["code"] == "model_not_found"


def test_sdk_ignores_unknown_request_fields(openai_client: OpenAI) -> None:
    # Forward-compat: extra OpenAI fields must not 400 (Pydantic extra=ignore).
    response = openai_client.responses.create(
        model="default",
        input="with temperature",
        temperature=0.2,
        top_p=0.9,
        store=True,
    )
    assert response.status == "completed"


def test_sdk_flowjet_extra_body_report(openai_client: OpenAI) -> None:
    response = openai_client.responses.create(
        model="default",
        input="namespaced options",
        extra_body={
            "flowjet": {
                "projection": "report",
                "session": "fj-sdk-session-1",
                "metadata": {"suite": "openai_sdk"},
            }
        },
    )
    assert response.status == "completed"
    assert "namespaced options" in _output_text(response)


def test_sdk_stream_with_progress_extra_body(openai_client: OpenAI) -> None:
    """Stock SDK must still complete when FlowJet progress events are present."""
    types: list[str] = []
    with openai_client.responses.stream(
        model="default",
        input="progress via extra_body",
        extra_body={"flowjet": {"projection": "progress"}},
    ) as stream:
        for event in stream:
            types.append(event.type)
        final = stream.get_final_response()

    assert "response.completed" in types
    # FlowJet extension events are best-effort for typed SDK unions.
    assert final.status == "completed"
    assert "progress via extra_body" in _output_text(final)


def test_sdk_stream_with_developer_tools(openai_client: OpenAI) -> None:
    types: list[str] = []
    with openai_client.responses.stream(
        model="default",
        input="developer tools",
        extra_body={
            "flowjet": {
                "projection": "developer",
                "metadata": {"emit_tools": True},
            }
        },
    ) as stream:
        for event in stream:
            types.append(event.type)
        final = stream.get_final_response()

    assert "response.output_text.delta" in types
    assert "response.completed" in types
    assert final.status == "completed"


def test_sdk_bearer_auth_required(authed_live_server: tuple[object, str]) -> None:
    server, api_key = authed_live_server
    base_url = server.base_url  # type: ignore[attr-defined]

    bad = OpenAI(api_key="wrong", base_url=base_url)
    with pytest.raises(AuthenticationError) as exc:
        bad.models.list()
    assert exc.value.status_code == 401

    good = OpenAI(api_key=api_key, base_url=base_url)
    page = good.models.list()
    assert any(m.id == "default" for m in page.data)

    response = good.responses.create(model="default", input="authed")
    assert response.status == "completed"


def test_sdk_with_raw_response_headers(openai_client: OpenAI) -> None:
    raw = openai_client.responses.with_raw_response.create(
        model="default",
        input="raw response path",
    )
    assert raw.http_response.status_code == 200
    parsed = raw.parse()
    assert parsed.status == "completed"
    assert "raw response path" in _output_text(parsed)
