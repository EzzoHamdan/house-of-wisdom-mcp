"""Tests for how the server presents itself to an MCP orchestrator (v0.7.0).

These lock in the *interface* contract rather than any runtime behavior. The
server was functionally correct long before it was discoverable: the `consult`
description opened by redirecting the caller to a different tool, `context` was
a required field the caller had to author before it could call at all, and no
server-level `instructions` were sent — so a client that lazily loads tool
schemas saw nothing but the bare tool names.

Each test below pins one of those fixes so a future edit cannot quietly undo it.
"""

from __future__ import annotations

import asyncio

import pytest

from ai_council.config import AICouncilConfig, ModelConfig, Provider
from ai_council.models import ModelManager
from ai_council.main import (
    SERVER_INSTRUCTIONS,
    TOOL_CONSULT,
    TOOL_LIST_MODELS,
    AICouncilServer,
)


def _server() -> AICouncilServer:
    config = AICouncilConfig(models=[
        ModelConfig(name="A", provider=Provider.CUSTOM, model_id="a", base_url="http://x", api_key="k"),
        ModelConfig(name="B", provider=Provider.CUSTOM, model_id="b", base_url="http://x", api_key="k"),
    ])
    return AICouncilServer(config=config)


def _tools() -> dict:
    """Invoke the registered list_tools handler and index the result by name."""
    server = _server()
    handler = server.server.request_handlers
    # The lowlevel Server stores handlers by request type; reach the list_tools
    # closure through the registered handler rather than re-declaring the list.
    import mcp.types as types

    async def _run():
        result = await handler[types.ListToolsRequest](types.ListToolsRequest(method="tools/list"))
        return result.root.tools

    return {t.name: t for t in asyncio.run(_run())}


# --- Server-level instructions ----------------------------------------------

def test_server_instructions_are_sent_and_name_the_tool():
    """The only text guaranteed to reach the orchestrator must stand alone."""
    assert SERVER_INSTRUCTIONS.strip()
    # It has to name the tool, because a deferred-tool client may show nothing else.
    assert f"`{TOOL_CONSULT}`" in SERVER_INSTRUCTIONS
    assert TOOL_LIST_MODELS in SERVER_INSTRUCTIONS
    # And it has to say what the server actually does, unprompted.
    assert "independent" in SERVER_INSTRUCTIONS.lower()


def test_instructions_reach_the_initialize_handshake():
    server = _server()
    opts = server.server.create_initialization_options()
    assert opts.instructions == SERVER_INSTRUCTIONS


# --- The consult description -------------------------------------------------

def test_consult_description_does_not_redirect_to_another_tool():
    """It used to open with 'use the sequentialthinking MCP instead'.

    Naming a specific third-party server is doubly bad: it may not be installed,
    and a redirect in the first sentence is read before any reason to stay.
    """
    desc = _tools()[TOOL_CONSULT].description.lower()
    assert "sequentialthinking" not in desc
    assert "instead" not in desc


def test_consult_description_leads_with_positive_triggers():
    desc = _tools()[TOOL_CONSULT].description
    assert "USE IT WHEN:" in desc
    # The old text spent ~40% of its length on `agentic` migration trivia; that
    # belongs in the field schema, not in the blurb that decides whether to call.
    assert "BACKWARD COMPAT" not in desc
    assert "Equivalent to the old" not in desc


def test_consult_description_states_the_cheap_path():
    """High activation cost is why a tool gets skipped. Say it's cheap to start."""
    desc = _tools()[TOOL_CONSULT].description
    assert "SIMPLEST CALL" in desc


# --- Argument surface --------------------------------------------------------

def test_only_question_is_required():
    schema = _tools()[TOOL_CONSULT].inputSchema
    assert schema["required"] == ["question"]
    # context must still be offered, just not demanded.
    assert "context" in schema["properties"]


def test_consult_runs_without_context():
    """A call passing `question` alone must validate."""
    server = _server()
    server._validate_input("", "Is this design sound?")  # must not raise


def test_empty_question_still_rejected():
    server = _server()
    with pytest.raises(ValueError, match="Question cannot be empty"):
        server._validate_input("some context", "   ")


def _prompt_sent_for(context: str, monkeypatch) -> str:
    """Run call_model against a stubbed completion and return the user prompt."""
    cfg = AICouncilConfig(models=[
        ModelConfig(name="A", provider=Provider.CUSTOM, model_id="a",
                    base_url="http://x/v1", api_key="k"),
        ModelConfig(name="B", provider=Provider.CUSTOM, model_id="b",
                    base_url="http://x/v1", api_key="k"),
    ])
    mgr = ModelManager(config=cfg)
    seen = {}

    class _Msg:
        content = "an analysis"

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    async def fake_create_completion(client, model_config, messages, **kwargs):
        seen["prompt"] = messages[-1]["content"]
        return _Resp()

    monkeypatch.setattr(mgr, "_create_completion", fake_create_completion)
    result = asyncio.run(mgr.call_model(cfg.models[0], context, "Is this design sound?"))
    assert result.ok, result.text
    return seen["prompt"]


@pytest.mark.parametrize("context", ["", "   "])
def test_blank_context_contributes_no_header(context, monkeypatch):
    """A blank context must not emit a dangling 'Context:' with nothing under it."""
    prompt = _prompt_sent_for(context, monkeypatch)
    assert "Context:" not in prompt
    assert prompt.startswith("Question: Is this design sound?")


def test_real_context_still_included(monkeypatch):
    prompt = _prompt_sent_for("the auth module uses JWT", monkeypatch)
    assert prompt.startswith("Context: the auth module uses JWT")
    assert "Question: Is this design sound?" in prompt
