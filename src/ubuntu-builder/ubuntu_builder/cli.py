"""Command line front end and privileged helper.

Two audiences:

* people who want to script a remaster (``ubuntu-builder-cli build …``);
* the GUI, which starts this module through ``pkexec`` for everything that
  needs root. With ``--events`` each log line and progress update is printed
  as one JSON object per line, and closing stdin cancels the operation —
  the GUI cannot signal a root process, but it can always close a pipe.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from ubuntu_builder import APP_NAME, __version__
from ubuntu_builder.core import graphical
from ubuntu_builder.core.backend import COMPRESSIONS, BuilderBackend
from ubuntu_builder.core.errors import BuilderError, OperationCancelledError
from ubuntu_builder.core.preflight import check_host_tools, install_command, missing_required
from ubuntu_builder.models.customization import CustomizationConfig
from ubuntu_builder.models.workspace import Workspace

EXIT_OK, EXIT_ERROR, EXIT_CANCELLED = 0, 1, 130


class Reporter:
    """Writes log/progress either human readable or as JSON events."""

    def __init__(self, events: bool) -> None:
        self._events = events
        self._lock = threading.Lock()

    def _emit(self, payload: dict[str, object]) -> None:
        with self._lock:
            print(json.dumps(payload), flush=True)

    def log(self, message: str) -> None:
        if self._events:
            self._emit({"event": "log", "message": message})
        else:
            print(message, flush=True)

    def progress(self, percent: int, stage: str) -> None:
        if self._events:
            self._emit({"event": "progress", "percent": percent, "stage": stage})
        elif sys.stdout.isatty():
            print(f"\r[{percent:3d}%] {stage}\033[K", end="\n" if percent >= 100 else "", flush=True)

    def result(self, **data: object) -> None:
        if self._events:
            self._emit({"event": "result", **data})

    def error(self, message: str, *, cancelled: bool = False) -> None:
        if self._events:
            self._emit({"event": "error", "message": message, "cancelled": cancelled})
        else:
            print(f"error: {message}", file=sys.stderr)


def _watch_stdin(cancel_event: threading.Event) -> None:
    """Cancel when the parent closes our stdin (or dies)."""

    def watch() -> None:
        # Raw read on the descriptor: going through sys.stdin would hold its
        # buffer lock and abort the interpreter at shutdown.
        try:
            while os.read(0, 4096):
                pass
        except OSError:
            pass
        cancel_event.set()

    threading.Thread(target=watch, daemon=True, name="stdin-watch").start()


# -- sub-commands --------------------------------------------------------------


def cmd_preflight(_backend: BuilderBackend, _args: argparse.Namespace, reporter: Reporter) -> int:
    statuses = check_host_tools()
    for status in statuses:
        mark = "ok     " if status.found else ("MISSING" if status.tool.required else "absent ")
        kind = "" if status.tool.required else " (optional)"
        reporter.log(f"[{mark}] {status.tool.name:<18} {status.tool.package}{kind} — {status.tool.purpose}")
    command = install_command(statuses, include_optional=True)
    if command:
        reporter.log(f"\nInstall what is missing with:\n  {command}")
    return EXIT_ERROR if missing_required(statuses) else EXIT_OK


def cmd_inspect(backend: BuilderBackend, args: argparse.Namespace, reporter: Reporter) -> int:
    inspection = backend.inspect_iso(args.iso)
    reporter.log(f"Volume id: {inspection.volume_id}")
    for name, size in inspection.squashfs.items():
        default = "  (default)" if name == inspection.default_target else ""
        reporter.log(f"  {name}  {size / 1024**2:.0f} MiB{default}")
    reporter.result(
        volume_id=inspection.volume_id, squashfs=inspection.squashfs, default_target=inspection.default_target
    )
    return EXIT_OK


def cmd_extract(backend: BuilderBackend, args: argparse.Namespace, reporter: Reporter) -> int:
    if args.replace and backend.workspace.state_file.exists():
        backend.delete_workspace()
    state = backend.extract_iso(args.iso, args.layer)
    reporter.log(f"Extracted. Customising {state.target_squashfs}.")
    return EXIT_OK


def cmd_shell(backend: BuilderBackend, _args: argparse.Namespace, _reporter: Reporter) -> int:
    """Interactive root shell inside the image, attached to our terminal."""
    with backend.session() as session:
        print(f"Entering the image ({backend.workspace.root}). Type 'exit' when done.", flush=True)
        code = subprocess.call(session.argv(["/bin/bash", "-l"], interactive=True))  # noqa: S603
        print("Leaving the image — unmounting.", flush=True)
    return EXIT_OK if code in (0, 130) else code


def _load_config(path: Path) -> CustomizationConfig:
    try:
        return CustomizationConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError, KeyError) as exc:
        raise BuilderError(f"Invalid customization file {path}: {exc}") from exc


def cmd_customize(backend: BuilderBackend, args: argparse.Namespace, _reporter: Reporter) -> int:
    backend.apply_customizations(_load_config(args.config))
    return EXIT_OK


def cmd_build(backend: BuilderBackend, args: argparse.Namespace, reporter: Reporter) -> int:
    if args.config is not None:  # one privileged run: apply, then build
        config = _load_config(args.config)
        if not config.is_empty():
            backend.apply_customizations(config)
    output = backend.build_iso(args.output, args.volume_id, args.compression)
    reporter.result(output=str(output))
    return EXIT_OK


def cmd_synaptic(backend: BuilderBackend, _args: argparse.Namespace, _reporter: Reporter) -> int:
    with backend.session() as session:
        graphical.run_synaptic(session, backend.runner)
    return EXIT_OK


def cmd_desktop(backend: BuilderBackend, args: argparse.Namespace, _reporter: Reporter) -> int:
    with backend.session() as session:
        graphical.run_desktop(session, backend.runner, args.resolution)
    return EXIT_OK


def cmd_cleanup(backend: BuilderBackend, _args: argparse.Namespace, reporter: Reporter) -> int:
    backend.cleanup_mounts()
    reporter.log("No mounts left in the workspace.")
    return EXIT_OK


def cmd_delete(backend: BuilderBackend, _args: argparse.Namespace, reporter: Reporter) -> int:
    backend.delete_workspace()
    reporter.log("Workspace deleted.")
    return EXIT_OK


Command = Callable[[BuilderBackend, argparse.Namespace, Reporter], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ubuntu-builder-cli", description=f"{APP_NAME} command line interface")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--events", action="store_true", help="emit JSON events (used by the GUI)")
    parser.add_argument(
        "--setenv", action="append", default=[], metavar="NAME=VALUE",
        help=f"set an environment variable ({', '.join(PASSED_ENV)}); used through pkexec",
    )  # fmt: skip
    parser.add_argument(
        "-w", "--workspace", type=Path, default=Path.home() / "ubuntu-builder-workspace",
        help="project directory (default: %(default)s)",
    )  # fmt: skip
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func: Command, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text)
        command.set_defaults(func=func)
        return command

    add("preflight", cmd_preflight, "check the host for required tools")
    add("inspect", cmd_inspect, "show the file systems inside an ISO").add_argument("iso", type=Path)
    extract = add("extract", cmd_extract, "extract an ISO into the workspace (root)")
    extract.add_argument("iso", type=Path)
    extract.add_argument("--layer", help="squashfs to customise, e.g. casper/minimal.standard.squashfs")
    extract.add_argument("--replace", action="store_true", help="delete an existing extracted image first")
    add("shell", cmd_shell, "open a root shell inside the image (root)")
    customize = add("customize", cmd_customize, "apply a customization file (root)")
    customize.add_argument("config", type=Path, help="JSON file with CustomizationConfig fields")
    build = add("build", cmd_build, "repack the image and build the ISO (root)")
    build.add_argument("output", type=Path)
    build.add_argument("--volume-id", help="ISO volume label (max. 32 characters)")
    build.add_argument("--compression", choices=COMPRESSIONS, help="squashfs compression (default: as source)")
    build.add_argument("--config", type=Path, help="customization file to apply before building")
    add("synaptic", cmd_synaptic, "run Synaptic inside the image on this display (root)")
    desktop = add("desktop", cmd_desktop, "run the image's desktop in a Xephyr window (root)")
    desktop.add_argument("--resolution", default="1280x800")
    add("cleanup", cmd_cleanup, "unmount leftovers of a crashed session (root)")
    add("delete", cmd_delete, "delete the extracted image (root)")
    return parser


#: Variables the GUI may hand to the privileged helper (pkexec clears the
#: environment): proxies, the X display for Synaptic/Desktop, the terminal.
PASSED_ENV = ("http_proxy", "https_proxy", "no_proxy", "DISPLAY", "XAUTHORITY", "TERM")


def _apply_setenv(assignments: list[str]) -> None:
    for assignment in assignments:
        name, sep, value = assignment.partition("=")
        if not sep or name not in PASSED_ENV:
            raise SystemExit(f"--setenv: {name!r} is not allowed")
        os.environ[name] = value


def _raise_cancelled(signum: int, _frame: object) -> None:
    raise OperationCancelledError(f"Terminated by {signal.Signals(signum).name}.")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _apply_setenv(args.setenv)
    # A closed terminal or a plain `kill` must unwind the context managers
    # (and thereby unmount the image) instead of killing us on the spot.
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _raise_cancelled)
    reporter = Reporter(args.events)
    cancel_event = threading.Event()
    if args.events:
        _watch_stdin(cancel_event)
    backend = BuilderBackend(
        Workspace(args.workspace.absolute()),
        log=reporter.log,
        progress=reporter.progress,
        cancel_event=cancel_event,
    )
    try:
        return args.func(backend, args, reporter)
    except OperationCancelledError as exc:
        reporter.error(str(exc), cancelled=True)
        return EXIT_CANCELLED
    except KeyboardInterrupt:
        reporter.error("Interrupted.", cancelled=True)
        return EXIT_CANCELLED
    except BuilderError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR
    except OSError as exc:  # permissions, vanished files, full disk …
        reporter.error(f"{type(exc).__name__}: {exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    os.umask(0o022)
    raise SystemExit(main())
