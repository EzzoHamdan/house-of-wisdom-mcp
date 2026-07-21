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
from pydantic import BaseModel

from . import __version__
from .models import ModelManager, ConfigValidationError
from .synthesis import ResponseSynthesizer
from .logger import AICouncilLogger
from .config import load_config


# Response Models
class ConsensusInfo(BaseModel):
    """Information about model consensus."""
    models_queried: int
    models_succeeded: int
    models_failed: int


class Perspective(BaseModel):
    """One consultant's independent analysis (v0.4.0+)."""
    label: str
    model_name: str
    code_name: str
    analysis: str
    status: str  # "ok" | "error"
    mode: str = "translator"  # "scribe" | "translator" | "scholar" (v0.4.3+)


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
        
        self.server = Server("house-of-wisdom")
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
                        "CHOOSING A THINKING TOOL (read first):\n"
                        "- Use the `sequentialthinking` MCP instead when the puzzle needs YOUR OWN focused reasoning "
                        "(decomposing, planning, catching your own mid-reasoning errors). One mind, instant, free.\n"
                        "- Use THIS tool (consult) when you want 3 DIVERSE model families weighing in on the same "
                        "question, not just you. Pick a MODE based on how complex and open the question is:\n"
                        "    * SCRIBE (mode=\"scribe\") — one-shot from given context, NO tool calls, ~10s. Use when "
                        "you have already pre-fed the relevant file/code contents into `context`, or when the "
                        "question is a judgment call that doesn't need codebase verification. Equivalent to the "
                        "old agentic=false.\n"
                        "    * TRANSLATOR (mode=\"translator\") — bounded, scope-caged tool loop. Each "
                        "consultant investigates within the scope_hint under a tight tool budget (the server's "
                        "configured max_tool_iterations), then answers. Use for hard problems where the answer "
                        "must be grounded in specific known files. Equivalent to the old agentic=true. ~30-60s.\n"
                        "    * SCHOLAR (mode=\"scholar\") — liberated free inquiry. Generous tool budget (the "
                        "server's configured scholar_max_tool_iterations), scope_hint treated as a starting point "
                        "not a cage, consultants may follow relevant threads elsewhere in the workspace. Use for "
                        "genuinely open questions where the right files to read aren't known in advance. Slowest; "
                        "bounded by parallel_timeout.\n"
                        "Decision axis: sequentialthinking = focus; SCRIBE = diversity on a settled question; "
                        "TRANSLATOR = diversity + grounding in known material; SCHOLAR = diversity + free inquiry "
                        "for open questions. Do NOT fire consult reflexively on every prompt — only when a "
                        "second/third model family seeing the problem would actually change the outcome.\n\n"
                        "DEFAULT MODE: if you omit `mode` (and `agentic`), the server decides from its config — "
                        "TRANSLATOR when the agentic tool loop is enabled, otherwise SCRIBE (the built-in "
                        "default). Pass `mode` explicitly when you care which one runs.\n\n"
                        "BACKWARD COMPAT: the old `agentic` boolean still works (false→SCRIBE, true→TRANSLATOR) "
                        "but `mode` takes precedence when both are passed.\n\n"
                        "RETURN SHAPE: a list of independent perspectives (no synthesizer), each tagged with the "
                        "mode it ran in. You (the orchestrator) read all perspectives and weigh them yourself — "
                        "do not treat any single one as ground truth."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "context": {
                                "type": "string",
                                "description": "Important background information and context. For SCRIBE mode, paste the file/code contents here (up to 200k chars). For TRANSLATOR/SCHOLAR, brief context is enough — the consultants will read files themselves."
                            },
                            "question": {
                                "type": "string",
                                "description": "The specific, detailed question you want answered."
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["scribe", "translator", "scholar"],
                                "description": (
                                    "SCRIBE = one-shot from context, no tools, ~10s. "
                                    "TRANSLATOR = bounded, scope-caged tool loop. "
                                    "SCHOLAR = liberated free inquiry, generous tool budget, scope as suggestion. "
                                    "If omitted, the server picks TRANSLATOR when its agentic tool loop is enabled, "
                                    "else SCRIBE. Takes precedence over `agentic` if both are passed."
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
                        "required": ["context", "question"]
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
        """Validate input parameters."""
        if not context or not context.strip():
            raise ValueError("Context cannot be empty")
        
        if not question or not question.strip():
            raise ValueError("Question cannot be empty")
        
        # Length validation — generous context limit to support pre-feeding
        # large file contents (v0.4.1 pre-feed pattern).
        if len(context) > 200000:
            raise ValueError("Context too long (max 200,000 characters)")
        
        if len(question) > 10000:
            raise ValueError("Question too long (max 10,000 characters)")
    
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
        result = SuccessResponse(
            data=SuccessData(
                perspectives=[Perspective(**p) for p in perspectives],
                consensus=ConsensusInfo(
                    models_queried=len(models),
                    models_succeeded=ok_count,
                    models_failed=len(models) - ok_count
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
                    )
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