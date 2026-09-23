"""Shared lifecycle boundary for external media workers."""

import asyncio
import logging
import os
import shutil
import signal
from pathlib import Path

from app.errors import AppError

log = logging.getLogger(__name__)
_CLEANUP_LEASE_FILE = ".aiinbox-process-group-lease"
_DEFERRED_CLEANUP_TASKS: set[asyncio.Task[None]] = set()


def cleanup_temporary_directory(directory: Path) -> bool:
    """Respect a child-process lease so outer finally blocks cannot race its writes."""
    directory = Path(directory)
    lease = directory / _CLEANUP_LEASE_FILE
    if lease.is_file():
        try:
            process_group_id = int(lease.read_text(encoding="ascii"))
        except (OSError, ValueError):
            # An unreadable lease is safer to retain than to remove under an unknown writer.
            return False
        if _process_group_exists(process_group_id):
            return False
    shutil.rmtree(directory, ignore_errors=True)
    return True


async def run_killable_subprocess(
    command: list[str],
    payload: bytes,
    *,
    timeout_seconds: float,
    output_limit_bytes: int,
    operation_name: str,
    cleanup_dir: Path | None = None,
    retain_dir_on_success: bool = False,
) -> bytes:
    """Own a media worker until it exits, so cancellation cannot race temp cleanup."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )

    async def exchange() -> bytes:
        """Send a bounded request and drain only a bounded worker response."""
        if process.stdin is None or process.stdout is None:
            raise AppError("DOWNLOAD_FAILED", f"{operation_name} worker pipes are unavailable")
        process.stdin.write(payload)
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        process.stdin.close()

        chunks = []
        size = 0
        while True:
            chunk = await process.stdout.read(min(16_384, output_limit_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > output_limit_bytes:
                raise AppError(
                    "EXTRACTION_FAILED",
                    f"{operation_name} worker response exceeded its limit",
                    True,
                )
            chunks.append(chunk)
        await process.wait()
        if process.returncode != 0:
            raise AppError("DOWNLOAD_FAILED", f"{operation_name} worker process failed")
        return b"".join(chunks)

    communication = asyncio.create_task(exchange())
    try:
        output = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout_seconds)
    except TimeoutError as exc:
        await _stop_and_release(process, communication, cleanup_dir, operation_name)
        raise AppError("TIMEOUT", f"{operation_name} timed out") from exc
    except asyncio.CancelledError:
        await _stop_and_release(process, communication, cleanup_dir, operation_name)
        raise
    except Exception:
        await _stop_and_release(process, communication, cleanup_dir, operation_name)
        raise

    if cleanup_dir is not None and not retain_dir_on_success:
        cleanup_temporary_directory(cleanup_dir)
    return output


async def _stop_and_release(
    process: asyncio.subprocess.Process,
    communication: asyncio.Task,
    cleanup_dir: Path | None,
    operation_name: str,
) -> None:
    """Transfer directory ownership to a reaper if process-group exit is uncertain."""
    stopped = await _stop_process(process, communication)
    if cleanup_dir is None:
        return
    if stopped:
        cleanup_temporary_directory(cleanup_dir)
        return

    cleanup_dir.mkdir(parents=True, exist_ok=True)
    lease = cleanup_dir / _CLEANUP_LEASE_FILE
    lease.write_text(str(process.pid), encoding="ascii")
    task = asyncio.create_task(
        _cleanup_after_process_group_exit(process, communication, cleanup_dir)
    )
    _DEFERRED_CLEANUP_TASKS.add(task)
    task.add_done_callback(_forget_cleanup_task)
    log.error(
        "%s worker did not stop; preserving temporary directory process_group=%s",
        operation_name,
        process.pid,
    )


async def _cleanup_after_process_group_exit(
    process: asyncio.subprocess.Process,
    communication: asyncio.Task,
    cleanup_dir: Path,
) -> None:
    """Reap a leased directory asynchronously once no worker can still write to it."""
    while _process_group_exists(process.pid):
        await asyncio.sleep(0.05)
    await process.wait()
    try:
        await communication
    except Exception:
        pass
    cleanup_temporary_directory(cleanup_dir)


def _forget_cleanup_task(task: asyncio.Task[None]) -> None:
    """Keep reapers alive until completion and consume their terminal exception."""
    _DEFERRED_CLEANUP_TASKS.discard(task)
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            log.error("deferred media cleanup failed: %s", error)


def _process_group_exists(process_group_id: int) -> bool:
    """Return whether a worker group may still own files; permission errors fail closed."""
    try:
        if os.name == "posix":
            os.killpg(process_group_id, 0)
        else:
            os.kill(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _stop_process(process: asyncio.subprocess.Process, communication: asyncio.Task) -> bool:
    """Terminate the worker and every same-group media helper before cleanup."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        process.terminate()

    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            return False

    if not communication.done():
        try:
            await asyncio.wait_for(communication, timeout=1.0)
        except TimeoutError:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(communication, timeout=1.0)
            except (asyncio.CancelledError, Exception, TimeoutError):
                return False
        except (asyncio.CancelledError, Exception):
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return process.returncode is not None
        return await _wait_for_process_group_exit(process.pid)
    return process.returncode is not None


async def _wait_for_process_group_exit(process_group_id: int) -> bool:
    """Confirm process-group exit before releasing files used by yt-dlp/ffmpeg."""
    deadline = asyncio.get_running_loop().time() + 1.0
    while True:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.01)
