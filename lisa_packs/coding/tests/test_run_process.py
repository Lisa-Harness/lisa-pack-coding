"""``run_process`` must never hang its caller or orphan its child.

Regression background (2026-10-11): two ``coding_edit_files`` preflights
hung until the kernel's 30s tool deadline cancelled them. The only subprocess
in that path is ``git apply --check`` with the patch on stdin.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from lisa_packs.coding.workflow import run_process


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
async def test_child_stdin_is_devnull_without_stdin_text(tmp_path: Path) -> None:
    result = await run_process(
        [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
        cwd=tmp_path,
        timeout_seconds=10,
        max_output_bytes=1024,
    )
    assert result.stdout.strip() == "''"


@pytest.mark.asyncio
async def test_stdin_feed_is_bounded_by_the_timeout(tmp_path: Path) -> None:
    # The child never reads stdin, so a payload larger than the pipe buffer
    # blocks the writer; the timeout must still fire.
    result = await run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout_seconds=1,
        max_output_bytes=1024,
        stdin_text="x" * (4 * 1024 * 1024),
    )
    assert result.timed_out


@pytest.mark.asyncio
async def test_child_exiting_before_reading_stdin_is_not_an_error(
    tmp_path: Path,
) -> None:
    result = await run_process(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        cwd=tmp_path,
        timeout_seconds=10,
        max_output_bytes=1024,
        stdin_text="x" * (4 * 1024 * 1024),
    )
    assert result.return_code == 3 and not result.timed_out


@pytest.mark.asyncio
async def test_cancellation_kills_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    task = asyncio.create_task(
        run_process(
            [sys.executable, "-c", script],
            cwd=tmp_path,
            timeout_seconds=60,
            max_output_bytes=1024,
        )
    )
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if not _alive(pid):
            break
        await asyncio.sleep(0.05)
    assert not _alive(pid)
