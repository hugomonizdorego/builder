"""Resumable ISO download with SHA-256 verification."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from pathlib import Path

import requests

from os_builder import APP_ID, __version__
from os_builder.core.errors import (
    ChecksumMismatchError,
    DownloadError,
    InsufficientDiskSpaceError,
    OperationCancelledError,
)
from os_builder.core.system import free_space
from os_builder.models.release import IsoImage

ProgressCallback = Callable[[int, int], None]  # bytes done, bytes total (0 = unknown)
_CHUNK = 1024 * 1024


def sha256_of(path: Path, cancel_event: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * _CHUNK):
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelledError("Operation cancelled.")
            digest.update(chunk)
    return digest.hexdigest()


def download_iso(
    image: IsoImage,
    dest_dir: Path,
    *,
    progress: ProgressCallback | None = None,
    log: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    session: requests.Session | None = None,
) -> Path:
    """Download ``image`` into ``dest_dir`` and return the file path.

    An interrupted download is resumed from its ``.part`` file. A complete
    file that already matches the published checksum is reused as is.
    """
    log = log or (lambda _message: None)
    cancel_event = cancel_event or threading.Event()
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / image.filename
    partial = dest_dir / (image.filename + ".part")

    if final.exists():
        if image.sha256 is None or sha256_of(final, cancel_event) == image.sha256:
            log(f"{final.name} is already downloaded.")
            return final
        log(f"{final.name} exists but does not match its checksum — downloading again.")
        final.unlink()

    http = session or requests.Session()
    http.headers.setdefault("User-Agent", f"{APP_ID}/{__version__}")
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    try:
        with http.get(image.url, headers=headers, stream=True, timeout=30) as response:
            if response.status_code == 416:  # the .part file is already complete
                response.close()
            else:
                response.raise_for_status()
                if offset and response.status_code != 206:
                    offset = 0  # server ignored the range request: start over
                remaining = int(response.headers.get("Content-Length") or 0)
                total = offset + remaining
                available = free_space(dest_dir)
                if remaining and available < remaining + 64 * _CHUNK:
                    raise InsufficientDiskSpaceError(str(dest_dir), remaining, available)
                log(f"Downloading {image.url}" + (f" (resuming at {offset / 1024**2:.0f} MiB)" if offset else ""))
                done = offset
                with partial.open("ab" if offset else "wb") as handle:
                    for chunk in response.iter_content(_CHUNK):
                        if cancel_event.is_set():
                            raise OperationCancelledError("Download cancelled (it can be resumed later).")
                        handle.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total)
    except requests.RequestException as exc:
        raise DownloadError(f"Download of {image.filename} failed: {exc}") from exc
    except OSError as exc:
        raise DownloadError(f"Could not write {partial}: {exc}") from exc

    if image.sha256:
        log("Verifying SHA-256 checksum…")
        actual = sha256_of(partial, cancel_event)
        if actual != image.sha256:
            partial.unlink(missing_ok=True)
            raise ChecksumMismatchError(
                f"Checksum mismatch for {image.filename}: expected {image.sha256}, got {actual}. "
                "The corrupt download was removed."
            )
        log("Checksum OK.")
    else:
        log("WARNING: no published checksum for this image; the download was not verified.")
    partial.rename(final)
    return final
