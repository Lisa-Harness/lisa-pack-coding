"""Verify an owned coding worktree with every configured check it needs.

See :mod:`lisa_packs.coding.verification` for the pipeline: upstream sync,
on-demand preparation, fix checks to a fixed point, then verify checks with
``needs`` ordering and ``inputs``-based reuse. Every check runs through the
session's sandbox runner (whatever implementation the session selected); an
absent, unavailable, or unverifiable sandbox fails the check rather than
running it on the host.
"""

from __future__ import annotations

from kernel.models import ToolExecutionContext, ToolResult
from lisa_packs.coding.config import get_coding_config
from lisa_packs.coding.freshness import with_sync_note
from lisa_packs.coding.verification import VerificationAborted, verify_artifact
from lisa_packs.coding.workflow import CodingWorkflowError, load_owned_artifact
from pydantic import BaseModel, Field

TOOL_ID = "coding_verify"


class Parameters(BaseModel):
    """Structured verification request."""

    artifact_id: str = Field(min_length=1, max_length=100)
    #: Extra checks to run beyond the fix checks; empty runs the required set.
    check_ids: list[str] = Field(default_factory=list, max_length=50)


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Run the verification pipeline and report it compactly."""

    params = Parameters.model_validate(ctx.parameters)
    try:
        config = get_coding_config(ctx)
        artifact = await load_owned_artifact(
            ctx, artifact_id=params.artifact_id, config=config, claim=True
        )
        report = await verify_artifact(
            ctx, artifact, config=config, check_ids=params.check_ids
        )
    except VerificationAborted as exc:
        return ToolResult(
            tool_id=TOOL_ID,
            error=with_sync_note(str(exc), exc.sync),
            # A rebase that landed before the failure still stales reads.
            output_json={"artifact_id": params.artifact_id, "sync": exc.sync.to_json()},
        )
    except (CodingWorkflowError, OSError, ValueError) as exc:
        return ToolResult(tool_id=TOOL_ID, error=str(exc))

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=report.text(revision=artifact.revision),
        # Every check in this report ran through a verified sandbox runner;
        # an unverified run raises instead of reaching this line.
        output_json={**report.to_json(artifact=artifact), "sandboxed": True},
    )
