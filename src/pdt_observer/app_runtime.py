from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from pdt_observer.harvest import append_harvest_log
from pdt_observer.jobs import (
    mark_job_cancelled,
    mark_job_starting,
    run_managed_job,
)


class ActiveCodexRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._tasks: set[str] = set()
        self._task_jobs: dict[str, str] = {}
        self._cancelled_tasks: set[str] = set()

    def _run_id_from_command(self, command: Sequence[str]) -> str:
        try:
            output_path = Path(command[command.index("-o") + 1])
            return output_path.stem
        except (ValueError, IndexError):
            return "unknown"

    def runner(
        self,
        command: Sequence[str],
        prompt: str,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        run_id = self._run_id_from_command(command)
        if self.is_cancel_requested(run_id):
            append_harvest_log(self.root, run_id, "Codex subprocess skipped after cancellation.")
            return subprocess.CompletedProcess(
                command,
                -15,
                "",
                "Harvest cancelled by user.",
            )
        append_harvest_log(self.root, run_id, "Codex subprocess starting.")
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
        )
        with self._lock:
            self._processes[run_id] = process
        try:
            stdout, stderr = process.communicate(input=prompt)
        finally:
            with self._lock:
                if self._processes.get(run_id) is process:
                    del self._processes[run_id]
        if process.returncode is not None and process.returncode < 0:
            stderr = f"{stderr.strip()}\nHarvest cancelled by user.".strip()
        append_harvest_log(self.root, run_id, "Codex subprocess finished.")
        return subprocess.CompletedProcess(command, process.returncode or 0, stdout, stderr)

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            if any(
                active_id == run_id or active_id.startswith(f"{run_id}-")
                for active_id in self._tasks
            ):
                return True
            return any(
                active_id == run_id or active_id.startswith(f"{run_id}-")
                for active_id in self._processes
            )

    def active_count(self) -> int:
        with self._lock:
            return len(self._processes) + len(self._tasks)

    def mark_task_active(self, run_id: str, job_id: str | None = None) -> None:
        with self._lock:
            self._tasks.add(run_id)
            if job_id is not None:
                self._task_jobs[run_id] = job_id

    def mark_task_inactive(self, run_id: str) -> None:
        with self._lock:
            self._tasks.discard(run_id)
            self._task_jobs.pop(run_id, None)
            self._cancelled_tasks.discard(run_id)

    def is_cancel_requested(self, run_id: str) -> bool:
        with self._lock:
            return any(
                cancelled_id == run_id or run_id.startswith(f"{cancelled_id}-")
                for cancelled_id in self._cancelled_tasks
            )

    def cancel(self, run_id: str) -> int:
        with self._lock:
            matched_tasks = [
                active_id
                for active_id in self._tasks
                if active_id == run_id or active_id.startswith(f"{run_id}-")
            ]
            parent_task_active = run_id in matched_tasks
            job_ids = tuple(
                self._task_jobs[active_id]
                for active_id in matched_tasks
                if active_id in self._task_jobs
            )
            matches = [
                (active_id, process)
                for active_id, process in self._processes.items()
                if parent_task_active
                or active_id == run_id
                or active_id.startswith(f"{run_id}-")
            ]
            self._cancelled_tasks.update(matched_tasks)
        for job_id in job_ids:
            mark_job_cancelled(self.root, job_id)
        for active_id, process in matches:
            append_harvest_log(self.root, active_id, "Cancellation requested.")
            process.terminate()
        return max(len(matches), len(matched_tasks))

    def cancel_all(self) -> int:
        with self._lock:
            matches = list(self._processes.items())
            job_ids = tuple(self._task_jobs.values())
        for job_id in job_ids:
            mark_job_cancelled(self.root, job_id, error_message="Application exit requested.")
        for active_id, process in matches:
            append_harvest_log(self.root, active_id, "Application exit requested.")
            process.terminate()
        return len(matches)


def run_background_job[T](
    *,
    root: Path,
    registry: ActiveCodexRegistry,
    identity: str,
    job_id: str,
    log: Callable[[str], None],
    task: Callable[[], T],
    manifest_path: Callable[[T], str | None] | None = None,
    summary: Callable[[T], dict[str, object] | None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    mark_job_starting(root, job_id)
    registry.mark_task_active(identity, job_id=job_id)
    try:
        run_managed_job(
            root=root,
            job_id=job_id,
            log=log,
            task=task,
            manifest_path=manifest_path,
            summary=summary,
            on_error=on_error,
        )
    finally:
        registry.mark_task_inactive(identity)


def delayed_hard_exit(delay_seconds: float = 0.25) -> Callable[[], None]:
    def shutdown() -> None:
        def exit_later() -> None:
            time.sleep(delay_seconds)
            os._exit(0)

        threading.Thread(target=exit_later, daemon=True).start()

    return shutdown
