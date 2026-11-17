"""Run external commands with live output capture and cancellation."""

from __future__ import annotations

import codecs
import fcntl
import os
import pty
import re
import selectors
import shlex
import signal
import struct
import subprocess
import termios
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ubuntu_builder.core.errors import CommandError, OperationCancelledError

LineCallback = Callable[[str], None]

_LINE_BREAK = re.compile(r"[\r\n]+")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
_SQUASHFS_BAR = re.compile(r"\]\s*\d+/\d+\s+(\d{1,3})%")


def parse_percent(line: str) -> float | None:
    """Extract the last ``NN%`` figure of a progress line, if any."""
    found = _PERCENT.findall(line)
    if not found:
        return None
    value = float(found[-1])
    return value if 0.0 <= value <= 100.0 else None


def parse_squashfs_progress(line: str) -> float | None:
    """Percentage of a (un)squashfs progress bar line: ``[===/ ] 12/340  3%``."""
    match = _SQUASHFS_BAR.search(line)
    return float(match.group(1)) if match else None


class CommandRunner:
    """Thin wrapper around :mod:`subprocess` used by every backend module.

    * Output is delivered line by line while the command runs. Carriage
      returns count as line breaks, so progress bars update live.
    * Setting ``cancel_event`` terminates the whole process group and raises
      :class:`OperationCancelledError`.
    """

    def __init__(self, log: LineCallback | None = None, cancel_event: threading.Event | None = None) -> None:
        self._log = log or (lambda _line: None)
        self.cancel_event = cancel_event or threading.Event()

    def log(self, message: str) -> None:
        self._log(message)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise OperationCancelledError("Operation cancelled.")

    def run(
        self,
        argv: Sequence[str | Path],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        on_line: LineCallback | None = None,
        use_pty: bool = False,
        quiet: bool = False,
        display: str | None = None,
    ) -> int:
        """Run ``argv`` and stream its combined stdout/stderr.

        ``on_line`` receives every output line (in addition to the log,
        unless ``quiet``). ``use_pty`` attaches the command to a pseudo
        terminal, which makes tools like ``mksquashfs`` print their
        progress bar even though they are not run interactively. ``display``
        replaces the command line shown in the log.
        """
        command = [str(arg) for arg in argv]
        self.check_cancelled()
        if not quiet:
            self._log(display or f"$ {shlex.join(command)}")

        master_fd: int | None = None
        try:
            if use_pty:
                master_fd, slave_fd = pty.openpty()
                # Without a window size the squashfs tools skip their bar.
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
                stdout: int = slave_fd
            else:
                stdout = subprocess.PIPE
            process = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
                command,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own process group → clean cancellation
            )
        except FileNotFoundError as exc:
            if master_fd is not None:
                os.close(master_fd)
                os.close(slave_fd)
            raise CommandError(command, 127, f"Command not found: {exc.filename}") from exc
        if use_pty:
            os.close(slave_fd)  # the child keeps its own copy
            read_fd = master_fd
        else:
            assert process.stdout is not None
            read_fd = process.stdout.fileno()
        assert read_fd is not None

        tail: deque[str] = deque(maxlen=25)

        def emit(line: str) -> None:
            line = _ANSI.sub("", line).rstrip()
            if not line:
                return
            tail.append(line)
            if on_line is not None:
                on_line(line)
            if not quiet:
                self._log(line)

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        selector = selectors.DefaultSelector()
        selector.register(read_fd, selectors.EVENT_READ)
        try:
            while True:
                if self.cancel_event.is_set():
                    self._terminate(process)
                    raise OperationCancelledError("Operation cancelled.")
                if not selector.select(timeout=0.2):
                    continue
                try:
                    chunk = os.read(read_fd, 65536)
                except OSError:  # EIO: the pty closed because the child exited
                    chunk = b""
                if not chunk:
                    break
                pending += decoder.decode(chunk)
                *lines, pending = _LINE_BREAK.split(pending)
                for line in lines:
                    emit(line)
            emit(pending)
        finally:
            selector.close()
            if master_fd is not None:
                os.close(master_fd)
            elif process.stdout is not None:
                process.stdout.close()
        returncode = process.wait()
        if check and returncode != 0:
            raise CommandError(command, returncode, "\n".join(tail))
        return returncode

    def capture(self, argv: Sequence[str | Path], *, check: bool = True) -> str:
        """Run a short command silently and return its standard output.

        stderr is kept apart (tools like xorriso print notes there) and
        only surfaces in the error raised for a failing command.
        """
        command = [str(arg) for arg in argv]
        self.check_cancelled()
        try:
            done = subprocess.run(  # noqa: S603 - argv list, never a shell string
                command, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", check=False
            )
        except FileNotFoundError as exc:
            raise CommandError(command, 127, f"Command not found: {exc.filename}") from exc
        if check and done.returncode != 0:
            raise CommandError(command, done.returncode, "\n".join(done.stderr.splitlines()[-25:]))
        return done.stdout.strip()

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        """SIGTERM the process group, escalate to SIGKILL after 5 seconds."""
        for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue
