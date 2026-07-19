import os
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from .models import ModelConfig, ModelManager
from .logger import AICouncilLogger
from .tools import ToolRegistry, filter_schemas


class CouncilMode(str, Enum):
    """The three modes of the House of Wisdom, named for medieval roles.

    SCRIBE     = one-shot from given context (fast, no tools)
    TRANSLATOR = bounded, scope-caged investigation (default deep mode)
    SCHOLAR    = liberated free inquiry (high tool budget, scope as suggestion)
    """
    SCRIBE = "scribe"
    TRANSLATOR = "translator"
    SCHOLAR = "scholar"

    @classmethod
    def from_agentic(cls, agentic: Optional[bool], tools_enabled: bool) -> "CouncilMode":
        """Back-compat: agentic=False -> SCRIBE, agentic=True -> TRANSLATOR."""
        if agentic is False:
            return cls.SCRIBE
        if agentic is True:
            return cls.TRANSLATOR
        return cls.TRANSLATOR if tools_enabled else cls.SCRIBE


# Per-mode system-prompt suffixes. The base consultant prompt lives in
# models.py::call_models_parallel_agentic; these append mode-specific
# guidance about how strictly to adhere to scope and budget.
MODE_PROMPT_SUFFIX = {
    CouncilMode.TRANSLATOR: (
        "\n\nMODE: TRANSLATOR. You are working through specific source material "
        "the way a medieval translator would carefully render a known text. "
        "STAY WITHIN THE SCOPE given above — do not wander to other files. "
        "Treat your tool budget as a hard limit; plan before you read, then "
        "answer. If you cannot verify something within budget, say so."
    ),
    CouncilMode.SCHOLAR: (
        "\n\nMODE: SCHOLAR. You are a free inquirer in the House of Wisdom — "
        "explore broadly, follow leads, aim for thoroughness. The scope above "
        "is a STARTING POINT, not a cage; you may follow a relevant thread "
        "elsewhere in the workspace if it materially advances the answer. "
        "Your tool budget is generous but finite; use it well. Prioritize "
        "depth and grounding over speed."
    ),
}


class ResponseSynthesizer:
    """Collects independent perspectives from each consultant model.

    v0.4.0: there is no synthesizer. When agentic tools are enabled, every
    consultant runs its own read-only tool loop against the workspace and
    returns its complete, self-contained analysis. When agentic tools are
    disabled, every consultant answers plain-text in parallel (legacy
    v0.2.x behavior, but without the synthesis step).

    The class retains the name ResponseSynthesizer for backward-compat with
    v0.3.0 call sites; its responsibility is now perspective collection.
    """

    def __init__(self, model_manager: ModelManager, logger: Optional[AICouncilLogger] = None):
        self.model_manager = model_manager
        self.logger = logger or AICouncilLogger()

    def _label_for(self, model: ModelConfig, anonymous: bool) -> str:
        """Return the label used for this model's perspective."""
        return model.code_name if anonymous else model.name

    async def collect_perspectives(
        self,
        context: str,
        question: str,
        models: List[ModelConfig],
        workspace_root_override: Optional[str] = None,
        agentic_override: Optional[bool] = None,
        scope_hint: Optional[str] = None,
        mode: Any = None,
    ) -> Tuple[List[Dict[str, Any]], List[ModelConfig]]:
        """Collect one independent perspective per consultant model.

        v0.4.0: there is no synthesizer. Each consultant returns its own
        complete analysis.

        v0.4.1: agentic_override + scope_hint + dynamic concurrency.

        v0.4.3: three named modes (House of Wisdom roles):
        - SCRIBE      — one-shot from context, no tools (~10s). The fast path.
        - TRANSLATOR  — bounded, scope-caged tool loop (default deep mode).
        - SCHOLAR      — liberated budget, scope as suggestion not cage.

        `mode` (optional) takes precedence over `agentic_override` when passed.
        If neither is passed, falls back to synthesizer_tools.enabled: true ->
        TRANSLATOR, false -> SCRIBE. SCHOLAR must be requested explicitly.

        Args:
            workspace_root_override: per-call sandbox root, takes precedence
                over the config default. Falls back to process cwd if both
                are None.

        Returns:
            Tuple of (perspectives, models_run) where perspectives is a list
            of dicts: [{"label": str, "model_name": str, "code_name": str,
            "analysis": str, "status": "ok"|"error", "mode": str}].
        """
        if not models:
            raise ValueError("No models provided")

        tools_cfg = self.model_manager.config.synthesizer_tools
        anonymous = self.model_manager.config.anonymous_perspectives

        # Normalize the mode arg: accept CouncilMode, str, or None.
        if isinstance(mode, str):
            try:
                mode = CouncilMode(mode.strip().lower())
            except ValueError:
                self.logger.warning(
                    f"Unknown mode '{mode}', falling back to agentic_override/config"
                )
                mode = None
        # Resolve the effective mode: explicit mode arg > agentic_override
        # boolean > config default.
        if mode is None:
            mode = CouncilMode.from_agentic(agentic_override, tools_cfg.enabled)
        ws_root = workspace_root_override or tools_cfg.workspace_root or os.getcwd()

        # Mode -> budget + scope-cage semantics
        agentic = mode != CouncilMode.SCRIBE
        if mode == CouncilMode.SCHOLAR:
            max_iter = tools_cfg.scholar_max_tool_iterations
        else:
            max_iter = tools_cfg.max_tool_iterations

        self.logger.info(f"Collecting perspectives from {len(models)} consultants", {
            "mode": mode.value,
            "agentic": agentic,
            "workspace_root": ws_root if agentic else None,
            "anonymous": anonymous,
            "scope_hint": bool(scope_hint),
            "max_concurrent": self.model_manager.config.max_concurrent_consultants,
            "max_iterations": max_iter,
        })

        if agentic:
            # Append the mode-specific guidance to the scope_hint so the
            # consultant system prompt in models.py picks it up.
            mode_suffix = MODE_PROMPT_SUFFIX.get(mode, "")
            effective_scope = (scope_hint or "") + mode_suffix
            try:
                schemas = filter_schemas(tools_cfg.allowed_tools)
                registries = [
                    ToolRegistry(
                        workspace_root=ws_root,
                        allowed_tools=tools_cfg.allowed_tools,
                        logger=self.logger,
                    )
                    for _ in models
                ]
                analyses = await self.model_manager.call_models_parallel_agentic(
                    models, context, question,
                    tool_schemas=schemas,
                    tool_registries=registries,
                    max_iterations=max_iter,
                    scope_hint=effective_scope or None,
                )
            except Exception as e:
                self.logger.error(
                    f"Agentic perspective collection failed, falling back to "
                    f"plain parallel calls: {e}"
                )
                analyses = await self.model_manager.call_models_parallel(
                    models, context, question
                )
        else:
            analyses = await self.model_manager.call_models_parallel(
                models, context, question
            )

        perspectives: List[Dict[str, Any]] = []
        for model, analysis in zip(models, analyses):
            is_error = (
                analysis.startswith("Error from")
                or analysis.startswith("Error for model")
                or analysis.startswith("Timeout error")
                or analysis.startswith("Error during agentic")
                or analysis.startswith("Synthesis timed out")
            )
            perspectives.append({
                "label": self._label_for(model, anonymous),
                "model_name": model.name,
                "code_name": model.code_name,
                "analysis": analysis,
                "status": "error" if is_error else "ok",
                "mode": mode.value,
            })

        return perspectives, models