"""Background workers keeping the GUI responsive.

Two kinds of long running work exist:

* :class:`TaskWorker` runs a Python callable (scraping, downloading,
  inspecting an ISO) on a ``QThread``.
* :class:`PrivilegedWorker` runs a backend operation that needs root. It
  starts ``ubuntu_builder.cli --events`` through ``pkexec`` on a ``QThread``
  and translates the JSON event stream into Qt signals.

Both expose the same signals, so the controller treats them alike. Signals
are delivered to the GUI thread by Qt's queued connections; workers never
touch widgets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

import ubuntu_builder
from ubuntu_builder.core.errors import BuilderError, OperationCancelledError


@dataclass(slots=True)
class TaskContext:
    """Hooks handed to a task function running on a worker thread."""

    log: Callable[[str], None]
    progress: Callable[[int, str], None]
    cancel_event: threading.Event


class BaseWorker(QThread):
    """Signals shared by all workers."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int, str)  # percent, stage
    succeeded = pyqtSignal(object)  # result value
    failed = pyqtSignal(str, bool)  # message, was cancelled

    def cancel(self) -> None:
        raise NotImplementedError


class TaskWorker(BaseWorker):
    """Run ``func(context)`` in a background thread."""

    def __init__(self, func: Callable[[TaskContext], Any]) -> None:
        super().__init__()
        self._func = func
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:  # executed on the worker thread
        context = TaskContext(self.log.emit, self.progress.emit, self._cancel_event)
        try:
            result = self._func(context)
        except OperationCancelledError as exc:
            self.failed.emit(str(exc), True)
        except BuilderError as exc:
            self.failed.emit(str(exc), False)
        except Exception as exc:  # noqa: BLE001 - a worker must never die silently
            self.failed.emit(f"Unexpected error: {type(exc).__name__}: {exc}", False)
        else:
            self.succeeded.emit(result)


def helper_command(
    workspace: Path, arguments: list[str], *, events: bool = True, extra_env: dict[str, str] | None = None
) -> list[str]:
    """Command line running the CLI helper with root rights.

    pkexec resets the environment and the working directory, so the module
    search path (and a proxy, if configured) is handed over explicitly.
    """
    package_parent = str(Path(ubuntu_builder.__file__).resolve().parent.parent)
    env = {"PYTHONPATH": package_parent, "PYTHONUNBUFFERED": "1", **(extra_env or {})}
    for name in ("http_proxy", "https_proxy", "no_proxy"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    command = ["env", *(f"{key}={value}" for key, value in env.items())]
    command += [sys.executable, "-m", "ubuntu_builder.cli"]
    command += ["--events"] if events else []
    command += ["-w", str(workspace), *arguments]
    return command if os.geteuid() == 0 else ["pkexec", *command]


class PrivilegedWorker(BaseWorker):
    """Run one CLI sub-command as root and relay its events."""

    def __init__(self, workspace: Path, arguments: list[str]) -> None:
        super().__init__()
        self._command = helper_command(workspace, arguments)
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._cancelled = False

    def cancel(self) -> None:
        """Ask the helper to stop by closing its stdin (see cli._watch_stdin)."""
        with self._lock:
            self._cancelled = True
            if self._process is not None and self._process.stdin is not None:
                try:
                    self._process.stdin.close()
                except OSError:
                    pass

    def run(self) -> None:  # executed on the worker thread
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv
                self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1,
            )  # fmt: skip
        except FileNotFoundError:
            self.failed.emit("pkexec is not installed; cannot obtain administrator rights.", False)
            return
        with self._lock:
            self._process = process
            if self._cancelled and process.stdin is not None:
                process.stdin.close()

        result: dict[str, Any] = {}
        error: tuple[str, bool] | None = None
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            try:
                event = json.loads(line)
                kind = event["event"]
            except (ValueError, KeyError, TypeError):
                if line:
                    self.log.emit(line)  # e.g. a traceback or pkexec message
                continue
            if kind == "log":
                self.log.emit(str(event.get("message", "")))
            elif kind == "progress":
                self.progress.emit(int(event.get("percent", 0)), str(event.get("stage", "")))
            elif kind == "result":
                result = {k: v for k, v in event.items() if k != "event"}
            elif kind == "error":
                error = (str(event.get("message", "")), bool(event.get("cancelled")))
        code = process.wait()
        if error is not None:
            self.failed.emit(*error)
        elif code in (126, 127) and os.geteuid() != 0:
            self.failed.emit("Administrator authentication was cancelled or denied.", True)
        elif code != 0:
            self.failed.emit(f"The operation failed (exit code {code}). See the log for details.", False)
        else:
            self.succeeded.emit(result)
