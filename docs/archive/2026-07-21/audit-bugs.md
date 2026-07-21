# PLAN — Bug audit: issues that need resolving

> Status: active
> Created: 2026-07-21 · Last updated: 2026-07-21
> Owns: defects found in a full-source audit of `ai_council/` (v0.6.1, commit `c9be2e0`) · Does not own: improvements that change design rather than fix wrong behavior — those live in [audit-enhancements.md](audit-enhancements.md)
> Done when: every B-item below is fixed with a regression test, or explicitly rejected with a note here
> Read when: picking up fix work on this repo, or verifying whether a symptom is already known
> Verify with: `uv run pytest -q` (baseline: 62 passed) + the repro snippets inline per bug
> Reflects code as of 2026-07-21, commit `c9be2e0`, Python 3.13.13

Every item was verified against the source on the date above; items marked **repro-verified**
were additionally reproduced by running the code. Severity is impact-based: **high** = wrong or
broken behavior on a mainline path, **medium** = wrong behavior on a reachable edge, **low** =
misleading but contained.

## Summary

| ID | Severity | Area | One line |
| --- | --- | --- | --- |
| [B1](#b1) | High |