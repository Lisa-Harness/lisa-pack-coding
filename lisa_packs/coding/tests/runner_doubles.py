"""Test doubles for the provider-neutral sandbox runner surface.

The pack's checks and preparation recipes run through
``ctx.sandbox_runner`` (``kernel.sandbox_floor.SandboxRunner``), not a named
sandbox tool. These doubles stand in for a real implementation so unit and
eval tests can assert the *consumer* side of the contract: the request the
pack builds, and the fail-closed behavior when a runner cannot verify its
own result.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kernel.sandbox_floor import (
    SandboxExecutionEnvelope,
    SandboxRunnerContractError,
    SandboxRunRequest,
    SandboxRunResult,
)

__all__ = [
    "DirectWriteRunner",
    "UnverifiedRunner",
    "runner_payload",
]


def runner_payload(
    parameters: Mapping[str, Any],
    *,
    return_code: int = 0,
    stderr: str = "",
    timed_out: bool = False,
) -> dict[str, Any]:
    """A result payload the stub runner accepts as verified containment."""
    cwd = str(parameters["cwd"])
    return {
        "return_code": return_code,
        "stderr": stderr,
        "sandboxed": True,
        "persistence": "direct-write",
        "cwd": cwd,
        "writable_paths": [cwd],
        "timed_out": timed_out,
    }


class DirectWriteRunner:
    """Accepts the request the pack builds and verifies a well-formed result.

    Mirrors what a real implementation does with the neutral request: argv,
    cwd, and timeout map to its own parameters, and the result must prove
    containment or the run is rejected.
    """

    def __init__(self, tool_id: str = "bwrap_run") -> None:
        self.tool_id = tool_id
        self.path_attestation = "result"

    def build_request(self, request: SandboxRunRequest, *, floor: Any = None) -> Mapping[str, Any]:
        return {
            "argv": list(request.argv),
            "cwd": request.cwd,
            "timeout_seconds": request.timeout_seconds,
            "overrides": {"persistence": request.persistence, "paths": dict(request.paths)},
        }

    def interpret_result(
        self, request: SandboxRunRequest, envelope: SandboxExecutionEnvelope
    ) -> SandboxRunResult:
        payload = dict(envelope.output_json or {})
        if envelope.error:
            raise SandboxRunnerContractError(envelope.error)
        if payload.get("sandboxed") is not True:
            raise SandboxRunnerContractError("the runner did not prove containment.")
        return SandboxRunResult(
            return_code=int(payload.get("return_code", 0)),
            stdout=envelope.output_text or "",
            stderr=str(payload.get("stderr", "")),
            timed_out=bool(payload.get("timed_out", False)),
            output_truncated=bool(payload.get("truncated", False)),
        )


class UnverifiedRunner(DirectWriteRunner):
    """Rejects every result: models an implementation that cannot prove it ran."""

    def interpret_result(
        self, request: SandboxRunRequest, envelope: SandboxExecutionEnvelope
    ) -> SandboxRunResult:
        raise SandboxRunnerContractError("the runner could not prove containment.")
