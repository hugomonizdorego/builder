"""Embedded terminal for the chroot session.

A deliberately small terminal: the shell runs on a real pseudo terminal, so
line editing, tab completion, Ctrl+C and job control work, and the widget
renders plain line oriented output (``TERM=dumb``). That covers ``apt``,
``add-apt-repository``, ``dpkg`` and friends. Full screen programs (nano,
vim, dialog based package prompts) need a real terminal emulator — the page
offers a button that opens the same session in the desktop's own terminal.
"""

from __future__ import annotations

import codecs
import fcntl
import os
import pty
import re
import signal
import struct
import subprocess
import termios

from PyQt6.QtCore import QSocketNotifier, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFontDatabase, QGuiApplication, QKeyEvent, QResizeEvent, QTextCursor
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

# CSI sequences, OSC sequences (terminated by BEL or ST) and charset selects.
_ESCAPE = re.compile(r"\x1b(?:\[([0-9;?]*)([@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-B]|[=>])")
_PARTIAL_ESCAPE = re.compile(r"\x1b(?:\[[0-9;?]*|\][^\x07\x1b]*)?$")

_KEYS: dict[int, bytes] = {
    Qt.Key.Key_Return: b"\r",
    Qt.Key.Key_Enter: b"\r",
    Qt.Key.Key_Backspace: b"\x7f",
    Qt.Key.Key_Tab: b"\t",
    Qt.Key.Key_Escape: b"\x1b",
    Qt.Key.Key_Up: b"\x1b[A",
    Qt.Key.Key_Down: b"\x1b[B",
    Qt.Key.Key_Right: b"\x1b[C",
    Qt.Key.Key_Left: b"\x1b[D",
    Qt.Key.Key_Home: b"\x01",
    Qt.Key.Key_End: b"\x05",
    Qt.Key.Key_Delete: b"\x04",
}


class TerminalWidget(QPlainTextEdit):
    """Displays a process attached to a pty and forwards the keyboard to it."""

    session_finished = pyqtSignal(int)  # exit code

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setMaximumBlockCount(10000)
        self.setUndoRedoEnabled(False)
        self.setStyleSheet("QPlainTextEdit { background: #1e1e1e; color: #e6e6e6; }")
        self._master_fd: int | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._notifier: QSocketNotifier | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""  # incomplete escape sequence carried between reads
        self._line: list[str] = []  # the line the cursor is on
        self._column = 0

    @property
    def is_running(self) -> bool:
        return self._process is not None

    # -- process handling ---------------------------------------------------

    def start(self, command: list[str]) -> None:
        """Start ``command`` on a new pseudo terminal."""
        if self.is_running:
            return
        master_fd, slave_fd = pty.openpty()

        def become_session_leader() -> None:  # runs in the child before exec
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # stdin (the pty) becomes the controlling terminal

        try:
            self._process = subprocess.Popen(  # noqa: S603 - argv built by the controller
                command, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                preexec_fn=become_session_leader, close_fds=True,
                env={**os.environ, "TERM": "dumb"},
            )  # fmt: skip
        except OSError as exc:
            os.close(master_fd)
            self._write_local(f"Could not start the session: {exc}\n")
            return
        finally:
            os.close(slave_fd)
        self._master_fd = master_fd
        self._sync_window_size()
        self._notifier = QSocketNotifier(master_fd, QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._read_ready)
        self.setFocus()

    def stop(self) -> None:
        """End the session politely: interrupt, then ``exit`` the shell."""
        if self._process is None or self._process.poll() is not None:
            return
        self.send(b"\x03")  # abandon a half typed line / stop a running command
        # The terminal flushes pending input on Ctrl+C, so "exit" must follow
        # a little later or it would be discarded with it.
        QTimer.singleShot(300, lambda: self.send(b"exit\r"))
        QTimer.singleShot(8000, self.kill)  # no-op if the shell has exited by then

    def kill(self) -> None:
        """Hang up the terminal. The root helper unmounts on SIGHUP."""
        if self._process is None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGHUP)
        except (ProcessLookupError, PermissionError):
            pass  # root owned child: closing the pty below hangs it up instead
        if self._master_fd is not None:
            if self._notifier is not None:
                self._notifier.setEnabled(False)
            os.close(self._master_fd)
            self._master_fd = None
        try:
            self._process.wait(timeout=15)  # let the helper unmount
        except subprocess.TimeoutExpired:
            pass
        self._finish()

    def send(self, data: bytes) -> None:
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, data)
            except OSError:
                pass

    def _read_ready(self) -> None:
        assert self._master_fd is not None
        try:
            data = os.read(self._master_fd, 65536)
        except OSError:
            data = b""
        if not data:
            self._finish()
            return
        self._feed(self._decoder.decode(data))

    def _finish(self) -> None:
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        if self._master_fd is not None:
            os.close(self._master_fd)
            self._master_fd = None
        code = 0
        if self._process is not None:
            try:
                code = self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                code = -1
        self._process = None
        self._write_local(f"\n[session ended, exit code {code}]\n")
        self.session_finished.emit(code)

    # -- rendering ----------------------------------------------------------

    def _write_local(self, text: str) -> None:
        self._feed(text.replace("\n", "\r\n"))

    def _feed(self, text: str) -> None:
        """Interpret a chunk of terminal output and update the display."""
        text = self._pending + text
        self._pending = ""
        partial = _PARTIAL_ESCAPE.search(text)
        if partial:
            self._pending, text = text[partial.start():], text[: partial.start()]

        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        position = 0
        while position < len(text):
            char = text[position]
            if char == "\x1b":
                match = _ESCAPE.match(text, position)
                if match is None:
                    position += 1
                    continue
                if match.group(2) == "K":  # erase to end of line
                    del self._line[self._column:]
                position = match.end()
                continue
            position += 1
            if char == "\n":
                self._render_line(cursor)
                cursor.insertBlock()
                self._line, self._column = [], 0
            elif char == "\r":
                self._column = 0
            elif char == "\b":
                self._column = max(0, self._column - 1)
            elif char == "\t":
                for _ in range(8 - self._column % 8):
                    self._put(" ")
            elif char >= " ":
                self._put(char)
        self._render_line(cursor)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def _put(self, char: str) -> None:
        if self._column < len(self._line):
            self._line[self._column] = char
        else:
            self._line.append(char)
        self._column += 1

    def _render_line(self, cursor: QTextCursor) -> None:
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText("".join(self._line))

    # -- input --------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        key = event.key()
        if ctrl and shift and key == Qt.Key.Key_C:
            self.copy()
            return
        if (ctrl and shift and key == Qt.Key.Key_V) or (shift and key == Qt.Key.Key_Insert):
            self.send(QGuiApplication.clipboard().text().encode())
            return
        if not self.is_running:
            return
        if key in _KEYS:
            self.send(_KEYS[key])
        elif ctrl and Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            self.send(bytes([key - Qt.Key.Key_A + 1]))  # Ctrl+C → 0x03 …
        elif event.text():
            self.send(event.text().encode())

    def insertFromMimeData(self, source) -> None:  # type: ignore[no-untyped-def]  # noqa: N802, ANN001
        """Middle-click / drag & drop paste goes to the shell, not the widget."""
        if source.hasText():
            self.send(source.text().encode())

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._sync_window_size()

    def _sync_window_size(self) -> None:
        """Tell the pty how many columns/rows are visible."""
        if self._master_fd is None:
            return
        metrics = self.fontMetrics()
        columns = max(20, self.viewport().width() // max(1, metrics.horizontalAdvance("M")))
        rows = max(5, self.viewport().height() // max(1, metrics.lineSpacing()))
        try:
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        except OSError:
            pass


class TerminalPage(QWidget):
    """Terminal plus the buttons controlling the chroot session."""

    open_requested = pyqtSignal()
    external_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.terminal = TerminalWidget()
        self.open_button = QPushButton("Open chroot session")
        self.close_button = QPushButton("Close session")
        self.external_button = QPushButton("Open in system terminal…")
        self.external_button.setToolTip("For full screen programs such as nano or dialog based package prompts.")
        hint = QLabel(
            "You are root inside the image. Use apt, add-apt-repository, edit files… "
            "Type <b>exit</b> (or close the session) before building."
        )
        hint.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.close_button)
        buttons.addStretch(1)
        buttons.addWidget(self.external_button)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.terminal, 1)
        layout.addLayout(buttons)

        self.open_button.clicked.connect(self.open_requested)
        self.external_button.clicked.connect(self.external_requested)
        self.close_button.clicked.connect(self.terminal.stop)
        self.terminal.session_finished.connect(lambda _code: self.refresh_buttons())
        self._available = False
        self.refresh_buttons()

    def set_available(self, available: bool) -> None:
        """Enable the page once an image has been extracted."""
        self._available = available
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        running = self.terminal.is_running
        self.open_button.setEnabled(self._available and not running)
        self.external_button.setEnabled(self._available and not running)
        self.close_button.setEnabled(running)
