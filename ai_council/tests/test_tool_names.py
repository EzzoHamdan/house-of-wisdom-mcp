"""Tests for tool-name resolution, including the v0.5.0 rename aliases.

The tools were renamed (``ai_council`` -> ``consult``,
``ai_council_list_models`` -> ``list_models``). The legacy names must keep
resolving so clients registered before the rename do not break.
"""

from __future__ import annotations

import pytest

from ai_council.main import AICouncilServer, TOOL_CONSULT, TOOL_LIST_MODELS


@pytest.mark.parametrize(
    "requested, expected",
    [
        # Canonical names resolve to themselves.
        ("consult", TOOL_CONSULT),
        ("list_models", TOOL_LIST_MODELS),
        # Legacy names still resolve (backward compatibility).
        ("ai_council", TOOL_CONSULT),
        ("ai_council_list_models", TOOL_LIST_MODELS),
    ],
)
def test_canonical_tool_resolves(requested: str, expected: str):
    assert AICouncilServer._canonical_tool(requested) == expected


@pytest.mark.parametrize("requested", ["", "nonsense", "council", "consult_models"])
def test_canonical_tool_rejects_unknown(requested: str):
    assert AICouncilServer._canonical_tool(requested) is None
