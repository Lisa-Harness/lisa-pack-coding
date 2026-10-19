"""``run_process`` must never hang its caller or orphan its child.

Regression background (2026-10-11): two ``coding_edit_files`` preflights
hung until the kernel's 30s tool deadline cancelled them. The only subprocess
in that path is ``git apply --check`` with the patch on stdin.

2026-10-12: the hang's root cause was a lost asyncio exit notification for
the git child, which left ``wait()`` pending forever. Cleanup must therefore
be bounded, and must reach the child's whole process group. Descendants that
outlive the child (hooks, helpers) keep the output pipes open.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from lisa_packs.coding import workflow
from lisa_packs.coding.config import CodingPackConfig
from lisa_packs.coding.workflow import run_git, run_process


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_dead(pid: int) -> bool:
    for _ in range(100):
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


def _spawn_sleeper(pid_file: Path, *, hold_stdout: bool) -> str:
    """Child script that leaves a sleeping grandchild behind and records its pid."""
    stdout = "None" if hold_stdout else "subprocess.DEVNULL"
    return (
        "import subprocess, sys\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        f" stdout={stdout}, stderr={stdout})\n"
        f"open({str(pid_file)!r}, 'w').write(str(g.pid))\n"
        "print('child done', flush=True)\n"
    )


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


@pytest.mark.asyncio
async def test_grandchild_holding_stdout_does_not_outlive_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild"
    result = await asyncio.wait_for(
        run_process(
            [sys.executable, "-c", _spawn_sleeper(pid_file, hold_stdout=True)],
            cwd=tmp_path,
            timeout_seconds=60,
            max_output_bytes=1024,
        ),
        timeout=10,
    )
    assert result.return_code == 0 and not result.timed_out
    assert "child done" in result.stdout
    assert await _wait_dead(int(pid_file.read_text()))


@pytest.mark.asyncio
async def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild"
    script = _spawn_sleeper(pid_file, hold_stdout=False) + "import time; time.sleep(30)\n"
    result = await run_process(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        timeout_seconds=1,
        max_output_bytes=1024,
    )
    assert result.timed_out
    assert await _wait_dead(int(pid_file.read_text()))


@pytest.mark.asyncio
async def test_cancellation_kills_the_whole_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild"
    script = _spawn_sleeper(pid_file, hold_stdout=False) + "import time; time.sleep(30)\n"
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
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_dead(int(pid_file.read_text()))


@pytest.mark.asyncio
async def test_lost_wait_notification_does_not_hide_a_completed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never_notified(self: asyncio.subprocess.Process) -> int:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(asyncio.subprocess.Process, "wait", never_notified)
    monkeypatch.setattr(workflow, "_KILL_GRACE_SECONDS", 0.5)
    result = await asyncio.wait_for(
        run_process(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            timeout_seconds=1,
            max_output_bytes=1024,
        ),
        timeout=10,
    )
    assert result.return_code == 0 and not result.timed_out


@pytest.mark.asyncio
async def test_child_runs_in_its_own_session_without_a_terminal(tmp_path: Path) -> None:
    result = await run_process(
        [sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid())"],
        cwd=tmp_path,
        timeout_seconds=10,
        max_output_bytes=1024,
    )
    assert result.stdout.strip() == "True"


@pytest.mark.asyncio
async def test_git_never_prompts_or_takes_optional_locks(tmp_path: Path) -> None:
    result = await run_git(
        tmp_path,
        ("-c", "alias.envdump=!env", "envdump"),
        config=CodingPackConfig(workspace_root=str(tmp_path)),
        env={**os.environ, "GIT_TERMINAL_PROMPT": "1"},
    )
    lines = set(result.stdout.splitlines())
    assert "GIT_TERMINAL_PROMPT=0" in lines
    assert "GIT_OPTIONAL_LOCKS=0" in lines
