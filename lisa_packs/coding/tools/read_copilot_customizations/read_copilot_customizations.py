"""Read VS Code Copilot customization files from the local coding workspace.

Discovers and parses ``.github/{instructions,agents,prompts,skills}`` and the
``.claude/`` equivalents, returning structured JSON with the front-matter and
body for each file. Useful for inspecting what context Copilot would load
without leaving the Lisa tool surface.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from sdk.content import (
    AgentFile,
    CopilotInstructionsFile,
    CustomizationBundle,
    InstructionFile,
    PromptFile,
    SkillFile,
    discover_copilot_customizations,
    matches_any_instruction,
)

TOOL_ID = "coding_read_copilot_customizations"

KindFilter = Literal["all", "instructions", "agents", "prompts", "skills", "always_on"]


class Parameters(BaseModel):
    """Structured request for Copilot customization discovery."""

    file_path: str | None = Field(
        default=None,
        description=(
            "Optional workspace-relative file path. When supplied, only "
            "instructions whose applyTo glob matches this path are returned."
        ),
    )
    kind: KindFilter = Field(
        default="all",
        description="Restrict the result to one customization kind.",
    )


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Discover and return Copilot customization files for the workspace."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        workspace_root = config.resolve_workspace_root()
    except (ValueError, OSError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    bundle = discover_copilot_customizations(workspace_root)
    kind = params.kind

    output: dict[str, Any] = {
        "workspace_root": str(workspace_root),
        "file_path_filter": params.file_path,
        "kind_filter": kind,
        "always_on": [
            _always_on_to_json(f) for f in _filter_always_on(bundle, kind)
        ],
        "instructions": [
            _instruction_to_json(i) for i in _filter_instructions(bundle, kind, params.file_path)
        ],
        "agents": [_agent_to_json(a) for a in _filter_agents(bundle, kind)],
        "prompts": [_prompt_to_json(p) for p in _filter_prompts(bundle, kind)],
        "skills": [_skill_to_json(s) for s in _filter_skills(bundle, kind)],
        "diagnostics": [_diagnostic_to_json(d) for d in bundle.diagnostics],
        "counts": {
            "always_on": len(_filter_always_on(bundle, kind)),
            "instructions": len(_filter_instructions(bundle, kind, params.file_path)),
            "agents": len(_filter_agents(bundle, kind)),
            "prompts": len(_filter_prompts(bundle, kind)),
            "skills": len(_filter_skills(bundle, kind)),
            "diagnostics": len(bundle.diagnostics),
        },
    }

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=(
            f"Discovered {sum(output['counts'].values())} Copilot "
            f"customization entries in {workspace_root}."
        ),
        output_json=output,
    )


# --------------------------------------------------------------------------- #
# Kind / file_path filtering
# --------------------------------------------------------------------------- #


def _filter_always_on(
    bundle: CustomizationBundle, kind: KindFilter
) -> tuple[CopilotInstructionsFile, ...]:
    if kind not in ("all", "always_on"):
        return ()
    return bundle.always_on


def _filter_instructions(
    bundle: CustomizationBundle, kind: KindFilter, file_path: str | None
) -> tuple[InstructionFile, ...]:
    if kind not in ("all", "instructions"):
        return ()
    if file_path is None:
        return bundle.instructions
    return matches_any_instruction(bundle, file_path)


def _filter_agents(
    bundle: CustomizationBundle, kind: KindFilter
) -> tuple[AgentFile, ...]:
    if kind not in ("all", "agents"):
        return ()
    return bundle.agents


def _filter_prompts(
    bundle: CustomizationBundle, kind: KindFilter
) -> tuple[PromptFile, ...]:
    if kind not in ("all", "prompts"):
        return ()
    return bundle.prompts


def _filter_skills(
    bundle: CustomizationBundle, kind: KindFilter
) -> tuple[SkillFile, ...]:
    if kind not in ("all", "skills"):
        return ()
    return bundle.skills


# --------------------------------------------------------------------------- #
# JSON serialization
# --------------------------------------------------------------------------- #


def _always_on_to_json(f: CopilotInstructionsFile) -> dict[str, Any]:
    return {"path": f.path, "body": f.body}


def _instruction_to_json(i: InstructionFile) -> dict[str, Any]:
    return {
        "path": i.path,
        "name": i.name,
        "description": i.description,
        "apply_to": list(i.apply_to),
        "format": i.format,
        "body": i.body,
    }


def _agent_to_json(a: AgentFile) -> dict[str, Any]:
    return {
        "path": a.path,
        "name": a.name,
        "description": a.description,
        "tools": list(a.tools),
        "agents": list(a.agents) if a.agents is not None else None,
        "model": a.model,
        "user_invocable": a.user_invocable,
        "disable_model_invocation": a.disable_model_invocation,
        "argument_hint": a.argument_hint,
        "handoffs": list(a.handoffs),
        "format": a.format,
        "body": a.body,
    }


def _prompt_to_json(p: PromptFile) -> dict[str, Any]:
    return {
        "path": p.path,
        "name": p.name,
        "description": p.description,
        "agent": p.agent,
        "argument_hint": p.argument_hint,
        "tools": list(p.tools),
        "model": p.model,
        "body": p.body,
    }


def _skill_to_json(s: SkillFile) -> dict[str, Any]:
    return {
        "path": s.path,
        "name": s.name,
        "description": s.description,
        "argument_hint": s.argument_hint,
        "user_invocable": s.user_invocable,
        "disable_model_invocation": s.disable_model_invocation,
        "context": s.context,
        "expected_dir_name": s.expected_dir_name,
        "body": s.body,
    }


def _diagnostic_to_json(d: Any) -> dict[str, Any]:
    return {"path": d.path, "message": d.message, "kind": d.kind}