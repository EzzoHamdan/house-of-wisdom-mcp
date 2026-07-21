#!/usr/bin/env python3

import asyncio
import time
import sys
from typing import Any, Dict, List, Optional, Union

from mcp.server.models import InitializationOptions
from mcp.server import NotificationOptions, Server
from mcp.types import (
    Tool
)
import mcp.types as types
from pydantic import BaseModel, Field

from . import __version__
from .models import ModelManager, ConfigValidationError
from .synthesis import ResponseSynthesizer
from .logger import AICouncilLogger
from .config import load_config


# Response Models
class ConsensusInfo(BaseModel):
    """Dispatch tally for the call. Despite the name it measures nothing about
    agreement — see the README. v0.7.0 adds the batch's aggregate cost."""
    models_queried: int
    models_succeeded: int
    models_failed: int
    # Wall-clock, not the sum of per-consultant durations: consultants run in
    # parallel, so summing them would report a number nobody actually waited.
    wall_clock_s: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    # None when no participating model carries pricing in config — a local
    # Ollama roster has no dollar cost, and 0.0 would read as "measured as free"
    # rather than "not priced".
    total_cost_usd: Optional[float] = None


class PerspectiveTelemetry(BaseModel):
    """What one consultant spent, and what it actually looked at (v0.7.0).

    `files_read` is the load-bearing field: it is the difference between a
    perspective grounded in the codebase and one that merely sounds grounded.
    Two consultants disagreeing matters far less when only one of them opened
    the file in question.
    """
    duration_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    api_calls: int = 0
    cost_usd: Optional[float] = None
    tool_rounds_used: int = 0
    tool_rounds_budget: Optional[int] = None
    files_read: List[str] = Field(default_factory=list)
    paths_listed: List[str] = Field(default_factory=list)
    tool_calls: Dict[str, int] = Field(default_factory=dict)


class Perspective(BaseModel):
    """One consultant's independent analysis (v0.4.0+)."""
    label: str
    model_name: str
    code_name: str
    analysis: str
    status: str  # "ok" | "error"
    mode: str = "translator"  # "scribe" | "translator" | "scholar" (v0.4.3+)
    telemetry: PerspectiveTelemetry = Field(default_factory=PerspectiveTelemetry)


class ModelInfo(BaseModel):
    """One configured consultant, as returned by list_models."""
    name: str
    model_id: str
    provider: str
    enabled: bool


class ModelsListData(BaseModel):
    """Data returned by list_models."""
    models: List[ModelInfo]
    max_models: int
    enabled_count: int


class SuccessData(BaseModel):
    """Data returned on successful AI Council processing.

    v0.4.0: there is no synthesizer. Each consultant returns its own complete,
    independent analysis. The orchestrator (the MCP client) weighs the
    perspectives itself.
    """
    perspectives: List[Perspective]
    consensus: ConsensusInfo


class ErrorInfo(BaseModel):
    """Error information structure."""
    code: str
    message: str
    type: str
    details: str


class SuccessResponse(BaseModel):
    """Successful response from AI Council."""
    status: str = "success"
    data: Union[SuccessData, ModelsListData]


class ErrorResponse(BaseModel):
    """Error response from AI Council."""
    status: str = "error"
    error: ErrorInfo
    data: Optional[Dict[str, Any]] = None


# Union type for all possible responses
AICouncilResponse = Union[SuccessResponse, ErrorResponse]


# Public tool names (v0.5.0+). The tools were renamed from "ai_council" and
# "ai_council_list_models" to the shorter names below. The legacy names are
# still accepted by the call handler as backward-compatible aliases (see
# AICouncilServer._canonical_tool) so pre-existing clients or scripts keep
# working, but only these canonical names are advertised in list_tools.
TOOL_CONSULT = "consult"
TOOL_LIST_MODELS = "list_models"

# Legacy name -> canonical name. Kept indefinitely; cheap and breaks nothing.
_TOOL_ALIASES = {
    "ai_council": TOOL_CONSULT,
    "ai_council_list_models": TOOL_LIST_MODELS,
}


# Server-level instructions, sent once at initialize and surfaced by MCP clients
# as always-in-context text — unlike a tool description, which many clients load
# lazily (or never, if the tool list is deferred behind a search). This is the
# only text guaranteed to reach the orchestrator, so it must stand alone: say
# what the server does, name the tool, and give concrete reasons to reach for it.
# Keep it short — it costs context on every single turn.
SERVER_INSTRUCTIONS = (
    "House of Wisdom asks the same question to several different AI model families "
    "at once and returns every answer separately, unmerged — one complete, "
    "independent analysis per model. There is no synthesizer: you read the "
    "perspectives and decide.\n\n"
    "Use the `consult` tool when a second or third independent mind would "
    "plausibly change your answer:\n"
    "- The user asks for a second opinion, a sanity check, or a review of your reasoning.\n"
    "- A design, architecture, or trade-off decision is genuinely contested.\n"
    "- You have been circling the same bug or explanation without converging.\n"
    "- A conclusion is high-stakes or hard to reverse and deserves independent verification.\n"
    "- You want blind spots surfaced — a different model family often catches what one mind misses.\n\n"
    "The simplest valid call passes `question` alone; every other argument is "
    "optional and the server picks sane defaults. Use `list_models` first when the "
    "user should choose which consultants answer.\n\n"
    "Each call spends several model API calls and takes tens of seconds, so it is "
    "worth reaching for deliberately rather than on every turn."
)


class AICouncilServer:
    """Main MCP server for AI Council."""
    
    def __init__(self, config=None):
        self.logger = AICouncilLogger()
        try:
            # Load config if not provided
            if config is None:
                config = load_config()
            
            self.config = config
            self.model_manager = ModelManager(config=config, logger=self.logger)
            self.synthesizer = ResponseSynthesizer(self.model_manager, logger=self.logger)
        except (ConfigValidationError, ValueError) as e:
            self.logger.error(f"Configuration validation failed: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Failed to initialize AI Council Server: {e}")
            raise
        
        self.server = Server(
            "house-of-wisdom",
            version=__version__,
            instructions=SERVER_INSTRUCTIONS,
        )
        self._setup_handlers()

    @staticmethod
    def _canonical_tool(name: str) -> Optional[str]:
        """Resolve a requested tool name to its canonical form, or None if unknown.

        Accepts both the current names (``consult``, ``list_models``) and the
        legacy names they replaced (``ai_council``, ``ai_council_list_models``),
        so clients registered before the v0.5.0 rename keep working.
        """
        if name in (TOOL_CONSULT, TOOL_LIST_MODELS):
            return name
        return _TOOL_ALIASES.get(name)

    def _setup_handlers(self):
        """Set up MCP server handlers."""
        
        @self.server.list_tools()
        async def handle_list_tools() -> List[Tool]:
            """List available tools."""
            return [
                Tool(
                    name=TOOL_LIST_MODELS,
                    description=(
                        "List the configured AI council consultants and whether each is enabled. "
                        "Returns one entry per model: name, model_id, provider, enabled. Call this "
                        "BEFORE consult if you want to show the user their current roster, let them "
                        "pick a subset, or check what's available. This tool does NOT ping endpoints "
                        "and does NOT verify API keys — it only reflects the server's loaded config."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                ),
                Tool(
                    name=TOOL_CONSULT,
                    description=(
                        "Ask the same question to several different AI model families at once and get back "
                        "every answer, unmerged — one complete, independent analysis per model.\n\n"
                        "USE IT WHEN:\n"
                        "- The user asks for a second opinion, a sanity check, or a review of your reasoning.\n"
                        "- A design, architecture, or trade-off decision is genuinely contested.\n"
                        "- You have been circling the same bug or explanation without converging.\n"
                        "- A conclusion is high-stakes or hard to reverse and deserves independent verification.\n"
                        "- You want blind spots surfaced — a different model family often catches what one "
                        "mind misses.\n\n"
                        "SIMPLEST CALL: pass `question` alone. Every other argument is optional and the server "
                        "picks sane defaults.\n\n"
                        "OPTIONAL DEPTH (`mode`) — omit it and the server chooses for you:\n"
                        "- scribe — answers from whatever you put in `context`; no file access; fastest.\n"
                        "- translator — each consultant reads the files you point it at, then answers.\n"
                        "- scholar — each consultant investigates the workspace freely; deepest, slowest.\n"
                        "translator and scholar need `workspace_root` (an absolute path).\n\n"
                        "RETURNS: one independent perspective per model, each tagged with the mode it ran in. "
                        "There is no synthesizer — read them all and weigh them yourself; do not treat any "
                        "single one as ground truth."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The specific, detailed question you want answered. This is the only required argument."
                            },
                            "context": {
                                "type": "string",
                                "description": "Optional background. A sentence or two is usually plenty. Only worth filling in at length for scribe mode, which cannot read files — there, paste the relevant code or docs (up to 200k chars)."
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["scribe", "translator", "scholar"],
                                "description": (
                                    "Optional. scribe = answers from `context` only, no file access, fastest. "
                                    "translator = bounded investigation within `scope_hint`. "
                                    "scholar = free investigation of the workspace, generous tool budget. "
                                    "If omitted, the server picks translator when its tool loop is enabled, else "
                                    "scribe. Takes precedence over `agentic` if both are passed."
                                )
                            },
                            "workspace_root": {
                                "type": "string",
                                "description": "Sandbox root for TRANSLATOR/SCHOLAR modes. Absolute path. Only used when mode is not scribe."
                            },
                            "agentic": {
                                "type": "boolean",
                                "description": "DEPRECATED — use `mode` instead. false=SCRIBE, true=TRANSLATOR. Kept for backward compat."
                            },
                            "scope_hint": {
                                "type": "string",
                                "description": "TRANSLATOR/SCHOLAR only. Natural-language scope, e.g. 'Start with A.md and B.md'. In TRANSLATOR mode this is a cage; in SCHOLAR mode it's a starting point."
                            },
                            "models": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Optional subset of consultant names to fire (e.g. [\"GLM\", \"Opus\"]). "
                                    "Names must match the `name` field in config. Unknown names are ignored. "
                                    "If none of the names match any enabled model, the call fails. "
                                    "Omit to use all enabled models (subject to max_models cap)."
                                )
                            }
                        },
                        "required": ["question"]
                    }
                )
            ]
        
        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
            """Handle tool calls."""
            # Normalize legacy tool names (pre-v0.5.0) to their canonical form
            # so clients registered before the rename keep working.
            canonical = self._canonical_tool(name)

            if canonical == TOOL_LIST_MODELS:
                try:
                    result = await self._process_list_models()
                    return [types.TextContent(type="text", text=result.model_dump_json(indent=2))]
                except Exception as e:
                    self.logger.error(f"Error in list_models tool call: {e}")
                    error_result = ErrorResponse(
                        error=ErrorInfo(
                            code="INTERNAL_ERROR",
                            message="Failed to list models",
                            type="system_error",
                            details=str(e)
                        )
                    )
                    return [types.TextContent(type="text", text=error_result.model_dump_json(indent=2))]

            if canonical != TOOL_CONSULT:
                error_result = ErrorResponse(
                    error=ErrorInfo(
                        code="UNKNOWN_TOOL",
                        message=f"Unknown tool: {name}",
                        type="user_input_error",
                        details=f"The tool '{name}' is not supported. Available tools: {TOOL_LIST_MODELS}, {TOOL_CONSULT}"
                    )
                )
                return [types.TextContent(type="text", text=error_result.model_dump_json(indent=2))]

            try:
                result = await self._process_ai_council(arguments)
                return [types.TextContent(type="text", text=result.model_dump_json(indent=2))]
            except Exception as e:
                self.logger.error(f"Error in tool call: {e}")
                error_result = ErrorResponse(
                    error=ErrorInfo(
                        code="INTERNAL_ERROR",
                        message="An unexpected error occurred during processing",
                        type="system_error",
                        details=str(e)
                    )
                )
                return [types.TextContent(type="text", text=error_result.model_dump_json(indent=2))]
    
    def _validate_input(self, context: str, question: str) -> None:
        """Validate input parameters.

        Only `question` is required. `context` was required until v0.7.0, but a
        mandatory field the caller must author before it can call at all is a
        real barrier to the tool ever being used — and it bought nothing, since
        translator/scholar consultants read the workspace themselves and scribe
        can still answer a judgment question from the wording alone.
        """
        if not question or not question.strip():
            raise ValueError("Question cannot be empty")

        # Length validation — generous context limit to support pre-feeding
        # large file contents (v0.4.1 pre-feed pattern).
        if len(context) > 200000:
            raise ValueError("Context too long (max 200,000 characters)")
        
        if len(question) > 10000:
            raise ValueError("Question too long (max 10,000 characters)")
    
    def _make_progress_cb(self):
        """Build a per-consultant progress reporter bound to the current request.

        A consult runs silently for tens of seconds, which reads as a hang to
        both the user and the calling agent — and a tool that looks hung is a
        tool that stops getting called. MCP clients opt in by sending a
        `progressToken` in the request `_meta`; when they do, each consultant
        that finishes emits a notification.

        Returns None when the client did not ask for progress, or when there is
        no active request context (direct calls in tests). The council runs
        identically either way — this is reporting, never control flow.
        """
        try:
            ctx = self.server.request_context
        except LookupError:
            return None

        token = getattr(ctx.meta, "progressToken", None) if ctx.meta else None
        if token is None:
            return None

        async def report(completed: int, total: int, model_name: str) -> None:
            await ctx.session.send_progress_notification(
                progress_token=token,
                progress=float(completed),
                total=float(total),
                message=f"{model_name} finished ({completed}/{total} consultants)",
                related_request_id=ctx.request_id,
            )

        return report

    async def _process_list_models(self) -> SuccessResponse:
        """Return the configured consultant roster with enabled flags."""
        all_models = self.model_manager.config.models
        infos = [
            ModelInfo(
                name=m.name,
                model_id=m.model_id,
                provider=m.provider.value,
                enabled=m.enabled,
            )
            for m in all_models
        ]
        enabled_count = sum(1 for m in all_models if m.enabled)
        data = ModelsListData(
            models=infos,
            max_models=self.model_manager.config.max_models,
            enabled_count=enabled_count,
        )
        return SuccessResponse(data=data)

    async def _process_ai_council(self, arguments: Dict[str, Any]) -> AICouncilResponse:
        """Process the AI council request."""
        start_time = time.time()
        
        # Validate arguments
        context = arguments.get("context", "")
        question = arguments.get("question", "")
        workspace_root = arguments.get("workspace_root") or None
        agentic_override = arguments.get("agentic")
        if isinstance(agentic_override, str):
            agentic_override = agentic_override.lower() in ("true", "1", "yes")
        scope_hint = arguments.get("scope_hint") or None
        requested_models = arguments.get("models")
        if isinstance(requested_models, str):
            requested_models = [requested_models]
        # v0.4.3: explicit mode arg takes precedence over the legacy agentic bool.
        mode_arg = arguments.get("mode")
        if isinstance(mode_arg, str) and mode_arg.strip():
            mode_arg = mode_arg.strip().lower()
        else:
            mode_arg = None
        
        # Validate input parameters
        try:
            self._validate_input(context, question)
        except ValueError as e:
            return ErrorResponse(
                error=ErrorInfo(
                    code="INVALID_INPUT",
                    message="Input validation failed",
                    type="user_input_error",
                    details=str(e)
                )
            )
        
        # Resolve which models fire. An explicit `models` subset resolves against
        # the FULL enabled list — a named, enabled model must be reachable
        # regardless of file order or max_models. The max_models cap applies only
        # to the default (no-subset) fan-out. Unknown names are ignored; if NONE
        # of the requested names match, fail. (Actual concurrency is bounded
        # separately by max_concurrent_consultants.)
        if requested_models:
            enabled_all = self.model_manager.get_enabled_models(limit=False)
            requested_set = {n.strip() for n in requested_models if n and n.strip()}
            models = [m for m in enabled_all if m.name in requested_set]
            if not models:
                return ErrorResponse(
                    error=ErrorInfo(
                        code="NO_MATCHING_MODELS",
                        message="None of the requested model names match any enabled model",
                        type="user_input_error",
                        details=(
                            f"Requested: {sorted(requested_set)}. "
                            f"Available enabled: {[m.name for m in enabled_all]}."
                        )
                    )
                )
        else:
            models = self.model_manager.get_enabled_models()
        if not models:
            return ErrorResponse(
                error=ErrorInfo(
                    code="NOT_ENOUGH_MODELS_ENABLED",
                    message="No models available to query",
                    type="configuration_error",
                    details="The fireable roster is empty (no enabled models within the max_models window)"
                )
            )
        
        self.logger.info("Starting AI Council process...", {
            "models": [{"name": m.name, "code_name": m.code_name} for m in models],
            "model_count": len(models)
        })
        
        # Make parallel calls to all models
        self.logger.info("Dispatching calls to all consultants in parallel")
        parallel_start = time.time()

        perspectives = await self.synthesizer.collect_perspectives(
            context, question, models,
            workspace_root_override=workspace_root,
            agentic_override=agentic_override,
            scope_hint=scope_hint,
            mode=mode_arg,
            progress_cb=self._make_progress_cb(),
        )
        parallel_duration = time.time() - parallel_start

        ok_count = sum(1 for p in perspectives if p["status"] == "ok")
        self.logger.info(f"All consultant perspectives collected in {parallel_duration:.2f}s", {
            "parallel_duration": parallel_duration,
            "ok": ok_count,
            "error": len(perspectives) - ok_count,
            "analysis_lengths": [len(p["analysis"]) for p in perspectives],
        })

        # Check if we have any valid perspectives
        if ok_count == 0:
            return ErrorResponse(
                error=ErrorInfo(
                    code="ALL_MODELS_FAILED",
                    message="All consultants failed to provide valid analyses",
                    type="service_error",
                    details=f"Attempted to call {len(models)} models but all failed or timed out"
                ),
                data={
                    "attempted_models": len(models),
                    "failed_responses": len(perspectives)
                }
            )

        if ok_count < len(perspectives):
            self.logger.warning(f"Only {ok_count} out of {len(perspectives)} consultants provided valid analyses")

        # Prepare result — v0.4.0: no synthesizer, return all perspectives
        total_duration = time.time() - start_time
        built = [Perspective(**p) for p in perspectives]

        # Roll the per-consultant telemetry up into the batch tally. Cost stays
        # None unless at least one participating model was priced, so an
        # unpriced roster reports "not measured" rather than a confident $0.00.
        priced = [p.telemetry.cost_usd for p in built if p.telemetry.cost_usd is not None]
        result = SuccessResponse(
            data=SuccessData(
                perspectives=built,
                consensus=ConsensusInfo(
                    models_queried=len(models),
                    models_succeeded=ok_count,
                    models_failed=len(models) - ok_count,
                    wall_clock_s=round(parallel_duration, 3),
                    total_tokens_in=sum(p.telemetry.tokens_in for p in built),
                    total_tokens_out=sum(p.telemetry.tokens_out for p in built),
                    total_cost_usd=round(sum(priced), 6) if priced else None,
                )
            )
        )

        self.logger.info("Process completed successfully", {
            "total_duration": total_duration
        })

        return result
    
    async def run(self):
        """Run the MCP server."""
        # MCP server setup
        from mcp.server.stdio import stdio_server
        
        self.logger.info("Starting AI Council MCP Server on stdio")
        
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="house-of-wisdom",
                    server_version=__version__,
                    capabilities=self.server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={}
                    ),
                    # Sent once at initialize; clients surface this as
                    # always-in-context text. Without it the orchestrator may
                    # only ever see the bare tool NAMES.
                    instructions=SERVER_INSTRUCTIONS,
                )
            )


def main():
    """Main entry point."""
    import argparse
    
    # Simple argument parsing for basic options
    parser = argparse.ArgumentParser(description="AI Council MCP Server")
    # add api keys
    parser.add_argument("--openai-api-key", help="OpenAI API key")
    parser.add_argument("--openrouter-api-key", help="OpenRouter API key")
    parser.add_argument("--max-models", type=int, help="Maximum number of models to use")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Log level")
    parser.add_argument("--log-format", choices=["text", "json"], help="Log rendering format")
    parser.add_argument("--config", help="Path to config file")
    parser.add_argument("--parallel-timeout", type=int, help="Timeout for parallel calls")
    
    args = parser.parse_args()
    
    async def async_main():
        try:
            # Create config with command line overrides
            config_kwargs = {}
            if args.openai_api_key:
                config_kwargs['openai_api_key'] = args.openai_api_key
            if args.openrouter_api_key:
                config_kwargs['openrouter_api_key'] = args.openrouter_api_key
            if args.max_models:
                config_kwargs['max_models'] = args.max_models
            if args.log_level:
                config_kwargs['log_level'] = args.log_level
            if args.log_format:
                config_kwargs['log_format'] = args.log_format
            if args.parallel_timeout:
                config_kwargs['parallel_timeout'] = args.parallel_timeout
            
            config = load_config(config_file=args.config, **config_kwargs)
            
            server = AICouncilServer(config=config)
            await server.run()
        except (ConfigValidationError, ValueError) as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Failed to start AI Council server: {e}", file=sys.stderr)
            sys.exit(1)
    
    asyncio.run(async_main())


if __name__ == "__main__":
    main() 