"""Exception hierarchy shared by the backend modules."""

from __future__ import annotations

from collections.abc import Sequence


class BuilderError(Exception):
    """Base class for every error the backend raises on purpose."""


class ScraperError(BuilderError):
    """Release information could not be fetched or parsed."""


class DownloadError(BuilderError):
    """An ISO download failed."""


class ChecksumMismatchError(DownloadError):
    """The downloaded file does not match the published SHA-256."""


class PreflightError(BuilderError):
    """A required host tool is missing."""


class WorkspaceError(BuilderError):
    """The workspace is missing, incomplete or in an unsafe state."""


class InsufficientDiskSpaceError(BuilderError):
    """Not enough free space for the requested operation."""

    def __init__(self, path: str, required: int, available: int) -> None:
        self.path, self.required, self.available = path, required, available
        gib = 1024**3
        super().__init__(
            f"Not enough free disk space in {path}: "
            f"{required / gib:.1f} GiB needed, {available / gib:.1f} GiB available."
        )


class OperationCancelledError(BuilderError):
    """The user cancelled the running operation."""


class CommandError(BuilderError):
    """An external command exited with a non-zero status."""

    def __init__(self, argv: Sequence[str], returncode: int, output_tail: str = "") -> None:
        self.argv, self.returncode, self.output_tail = list(argv), returncode, output_tail
        message = f"Command failed with exit code {returncode}: {' '.join(self.argv)}"
        if output_tail:
            message += f"\n{output_tail}"
        super().__init__(message)
