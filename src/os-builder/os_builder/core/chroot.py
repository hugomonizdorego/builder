"""Mount handling and command execution inside the image (chroot).

A :class:`ChrootSession` is the only way the backend enters the image:

* classic images: ``edit/`` is bind-mounted on ``rootfs/``;
* layered images (Ubuntu 23.04+): the lower squashfs layers are loop-mounted
  read-only and combined with ``edit/`` — the unpacked layer being customised
  — through overlayfs, which is how casper itself stacks them at boot. All
  changes therefore land in the edited layer only.

On top of that come ``/dev``, ``/dev/pts``, ``/proc``, ``/sys`` and a private
``/run``, the ISO tree at ``/cdrom`` (an apt source of the image) and working
DNS. Everything is torn down again on exit, including
after errors, and :func:`unmount_tree` can always recover a workspace from a
crashed session.
"""

from __future__ import annotations

import fcntl
import os
import shlex
import signal
import time
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

from os_builder.core.errors import CommandError, WorkspaceError
from os_builder.core.runner import CommandRunner, LineCallback
from os_builder.models.workspace import ProjectState, Workspace

#: Environment of every command run inside the image. Deliberately minimal:
#: nothing of the host session (DISPLAY, DBUS address, …) leaks in.
CHROOT_ENV: dict[str, str] = {
    "HOME": "/root",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C.UTF-8",
    "DEBIAN_FRONTEND": "noninteractive",
    "NEEDRESTART_SUSPEND": "1",  # nothing to restart inside a chroot
}

_POLICY_RC_D = "#!/bin/sh\n# Installed by os-builder: no services may start inside the chroot.\nexit 101\n"


def mount_points_under(root: Path) -> list[Path]:
    """Active mount points at or below ``root``, deepest/newest first."""
    prefix = str(root.resolve())
    found: list[Path] = []
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            if len(fields) < 5:
                continue
            # The kernel escapes blanks and friends as octal (\040).
            point = fields[4].encode().decode("unicode_escape")
            if point == prefix or point.startswith(prefix + "/"):
                found.append(Path(point))
    found.reverse()  # later mounts sit on top of earlier ones
    return sorted(found, key=lambda p: len(p.parts), reverse=True)


def processes_in(root: Path) -> list[int]:
    """PIDs of processes whose root directory is ``root`` (i.e. inside the chroot)."""
    target = str(root.resolve())
    found = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                if os.readlink(proc / "root") == target:
                    found.append(int(proc.name))
            except OSError:
                continue
    return found


def stop_processes_in(runner: CommandRunner, root: Path) -> None:
    """SIGTERM, then SIGKILL, whatever still runs inside the image.

    A daemon started by a package script or a program left open would keep
    the mounts busy.
    """
    for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
        pids = processes_in(root)
        if not pids:
            return
        runner.log(f"Stopping {len(pids)} process(es) still running inside the image.")
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and processes_in(root):
            time.sleep(0.1)


def unmount_tree(runner: CommandRunner, root: Path) -> None:
    """Unmount everything below ``root``; raise if something stays mounted."""
    for point in mount_points_under(root):
        if runner.run(["umount", point], check=False, quiet=True) != 0:
            runner.log(f"umount {point} is busy — detaching lazily")
            runner.run(["umount", "-l", point], check=False, quiet=True)
    left = mount_points_under(root)
    if left:
        raise WorkspaceError(
            "Could not unmount: " + ", ".join(str(p) for p in left) + ". Close programs using the image and retry."
        )


class ChrootSession:
    """Context manager owning the mounts of one workspace."""

    def __init__(self, workspace: Workspace, state: ProjectState, runner: CommandRunner) -> None:
        self._ws = workspace
        self._state = state
        self._runner = runner
        self._lock_fd: int | None = None
        self._created: list[Path] = []  # helper files to remove on exit
        self._created_dirs: list[Path] = []  # mount points added to edit/

    @property
    def rootfs(self) -> Path:
        return self._ws.rootfs_dir

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> ChrootSession:
        self.mount()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.unmount()

    # -- mounting ----------------------------------------------------------

    def mount(self) -> None:
        if not self._state.extracted or not self._ws.edit_dir.is_dir():
            raise WorkspaceError("No extracted image in this workspace yet.")
        if any(c in str(self._ws.root) for c in ":,"):
            raise WorkspaceError("The workspace path must not contain ':' or ','.")
        self._acquire_lock()
        try:
            unmount_tree(self._runner, self._ws.root)  # leftovers of a crash
            self._mount_root()
            self._mount_kernel_filesystems()
            self._mount_install_media()
            self._setup_dns()
            self._block_service_starts()
        except BaseException:
            self.unmount()
            raise

    def _acquire_lock(self) -> None:
        """One session per workspace: mounts must not be pulled from under it."""
        fd = os.open(self._ws.root / ".session.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise WorkspaceError(
                "The image is in use by another session (is the terminal still open?). "
                "Close it and try again."
            ) from None
        self._lock_fd = fd

    def _mount_root(self) -> None:
        run = self._runner.run
        rootfs = self._ws.rootfs_dir
        rootfs.mkdir(parents=True, exist_ok=True)
        if not self._state.is_layered:
            run(["mount", "--bind", self._ws.edit_dir, rootfs], quiet=True)
            return
        lower_mounts: list[Path] = []
        for index, layer in enumerate(self._state.lower_squashfs):
            point = self._ws.lower_dir / str(index)
            point.mkdir(parents=True, exist_ok=True)
            run(["mount", "-t", "squashfs", "-o", "loop,ro", self._ws.iso_dir / layer, point], quiet=True)
            lower_mounts.append(point)
        self._ws.overlay_work_dir.mkdir(parents=True, exist_ok=True)
        # overlayfs wants the top-most lower layer first. The feature flags
        # keep the upper directory free of metadata casper cannot interpret.
        options = ",".join(
            [
                "lowerdir=" + ":".join(str(p) for p in reversed(lower_mounts)),
                f"upperdir={self._ws.edit_dir}",
                f"workdir={self._ws.overlay_work_dir}",
                "index=off,metacopy=off,redirect_dir=off",
            ]
        )
        run(["mount", "-t", "overlay", "overlay", "-o", options, rootfs], quiet=True)

    def _mount_kernel_filesystems(self) -> None:
        run = self._runner.run
        rootfs = self._ws.rootfs_dir
        for name in ("dev", "proc", "sys", "run"):
            (rootfs / name).mkdir(exist_ok=True)
        run(["mount", "--bind", "/dev", rootfs / "dev"], quiet=True)
        run(["mount", "--bind", "/dev/pts", rootfs / "dev" / "pts"], quiet=True)
        run(["mount", "-t", "proc", "proc", rootfs / "proc"], quiet=True)
        run(["mount", "-t", "sysfs", "sysfs", rootfs / "sys"], quiet=True)
        # A private tmpfs instead of the host's /run: package scripts must
        # not reach the host's systemd, D-Bus or snapd sockets.
        run(["mount", "-t", "tmpfs", "-o", "mode=755,nosuid,nodev", "tmpfs", rootfs / "run"], quiet=True)

    def _mount_install_media(self) -> None:
        """Expose the ISO tree at ``/cdrom``, as on the booted live system.

        Ubuntu images list the package pool of their own medium as an apt
        source (``file:///cdrom``); without it ``apt-get update`` fails.
        """
        if not (self._ws.iso_dir / "dists").is_dir():
            return
        point = self._ws.rootfs_dir / "cdrom"
        if not point.exists():
            point.mkdir()
            self._created_dirs.append(self._ws.edit_dir / "cdrom")  # where the new directory really lives
        self._runner.run(["mount", "--bind", "-o", "ro", self._ws.iso_dir, point], quiet=True)

    def _setup_dns(self) -> None:
        """Make name resolution work without changing the image."""
        host = next(
            (p for p in (Path("/run/systemd/resolve/resolv.conf"), Path("/etc/resolv.conf")) if p.is_file()),
            None,
        )
        if host is None:
            self._runner.log("WARNING: host has no resolv.conf; DNS will not work inside the image.")
            return
        rootfs = self._ws.rootfs_dir
        resolv = rootfs / "etc" / "resolv.conf"
        if resolv.is_symlink():
            # Ubuntu links to ../run/systemd/resolve/stub-resolv.conf. Provide
            # that file on the private /run; the image itself stays untouched.
            link = os.readlink(resolv)
            inside = Path(os.path.normpath(link if link.startswith("/") else f"/etc/{link}"))
            if inside.parts[:2] == ("/", "run"):
                target = rootfs / inside.relative_to("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(host.read_bytes())
                return
            self._runner.log(f"WARNING: unexpected resolv.conf link ({link}); DNS may not work.")
        elif resolv.is_file():
            self._runner.run(["mount", "--bind", "-o", "ro", host, resolv], quiet=True)
        else:
            self._runner.log("WARNING: image has no /etc/resolv.conf; DNS may not work.")

    def _block_service_starts(self) -> None:
        policy = self._ws.rootfs_dir / "usr" / "sbin" / "policy-rc.d"
        if policy.parent.is_dir() and not policy.exists():
            policy.write_text(_POLICY_RC_D, encoding="utf-8")
            policy.chmod(0o755)
            self._created.append(policy)

    def unmount(self) -> None:
        """Tear the session down. Safe to call more than once."""
        try:
            for path in self._created:
                path.unlink(missing_ok=True)
            self._created.clear()
            if self._ws.rootfs_dir.is_dir():
                stop_processes_in(self._runner, self._ws.rootfs_dir)
            unmount_tree(self._runner, self._ws.root)
            for path in self._created_dirs:
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            self._created_dirs.clear()
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)  # closing drops the flock
                self._lock_fd = None

    # -- running commands --------------------------------------------------

    def argv(
        self, command: Sequence[str], *, interactive: bool = False, extra_env: dict[str, str] | None = None
    ) -> list[str]:
        """Full host command line that runs ``command`` inside the image."""
        env = dict(CHROOT_ENV)
        for name in ("http_proxy", "https_proxy", "no_proxy"):  # honour a host proxy
            if os.environ.get(name):
                env[name] = os.environ[name]
        if interactive:
            env.pop("DEBIAN_FRONTEND")
            env["TERM"] = os.environ.get("TERM", "dumb")
            env["debian_chroot"] = "os-builder"  # shown in bash's prompt
        env.update(extra_env or {})
        assignments = [f"{key}={value}" for key, value in env.items()]
        return ["chroot", str(self._ws.rootfs_dir), "/usr/bin/env", "-i", *assignments, *command]

    def run(self, command: Sequence[str], *, check: bool = True, on_line: LineCallback | None = None) -> int:
        """Run ``command`` (an argv list, never a shell string) in the image."""
        shown = f"(image) # {shlex.join(command)}"
        return self._runner.run(self.argv(command), check=check, on_line=on_line, display=shown)

    def capture(self, command: Sequence[str]) -> str:
        return self._runner.capture(self.argv(command))

    def has_command(self, name: str) -> bool:
        try:
            self._runner.capture(self.argv(["/bin/sh", "-c", 'command -v "$1"', "sh", name]))
        except CommandError:
            return False
        return True
