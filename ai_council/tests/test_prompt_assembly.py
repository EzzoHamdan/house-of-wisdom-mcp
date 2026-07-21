"""Tests for consultant system-prompt assembly (mode vs scope separation)."""

from __future__ import annotations

from ai_council.models import build_consultant_system_prompt
from ai_council.synthesis import MODE_PROMPT_SUFFIX, CouncilMode

STRICT_CAGE = "Do NOT read, list, or glob paths"


def test_scholar_guidance_not_wrapped_in_strict_cage():
    """SCHOLAR mode with no caller scope must not emit the strict cage (B2).

    Previously the SCHOLAR suffix ("scope is a starting point, not a cage") was
    concatenated into scope_hint, so it got wrapped in "SCOPE (strict): … Do
    NOT read outside it" — telling the model to roam and not roam at once.
    """
    guidance = MODE_PROMPT_SUFFIX[CouncilMode.SCHOLAR]
    prompt = build_consultant_system_prompt(64, scope_hint=None, mode_guidance=guidance)
    assert STRICT_CAGE not in prompt
    assert "STARTING POINT" in prompt  # the scholar guidance is still present


def test_caller_scope_still_caged():
    """A real caller scope_hint IS wrapped in the strict cage."""
    prompt = build_consultant_system_prompt(
        8, scope_hint="Only read main.py", mode_guidance=None
    )
    assert "SCOPE (strict):" in prompt
    assert STRICT_CAGE in prompt
    assert "Only read main.py" in prompt


def test_translator_scope_and_guidance_coexist():
    """TRANSLATOR: caller scope is caged, mode guidance sits outside it."""
    guidance = MODE_PROMPT_SUFFIX[CouncilMode.TRANSLATOR]
    prompt = build_consultant_system_prompt(
        8, scope_hint="Start with config.py", mode_guidance=guidance
    )
    assert "SCOPE (strict):" in prompt
    assert "Start with config.py" in prompt
    assert "MODE: TRANSLATOR" in prompt
    # The mode guidance must appear AFTER the strict scope block, not inside it.
    assert prompt.index("SCOPE (strict):") < prompt.index("MODE: TRANSLATOR")


def test_no_scope_no_guidance_has_no_cage():
    prompt = build_consultant_system_prompt(8, scope_hint=None, mode_guidance=None)
    assert "SCOPE (strict):" not in prompt
    assert STRICT_CAGE not in prompt
