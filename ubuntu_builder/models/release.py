"""Data models describing Ubuntu releases, flavors and downloadable images."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class SupportStatus(Enum):
    """Where a release currently sits in the Ubuntu release cycle."""

    SUPPORTED = "Supported"
    ESM = "ESM only (Ubuntu Pro)"
    EOL = "End of Life"
    UNKNOWN = "Unknown"


def version_key(version: str) -> tuple[int, ...]:
    """Sort key for dotted versions, so that ``24.04.10`` > ``24.04.9``."""
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def _date_to_json(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _date_from_json(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


@dataclass(frozen=True, slots=True)
class UbuntuRelease:
    """One Ubuntu series (``24.04``), aggregated over its point releases."""

    version: str
    codename: str
    is_lts: bool = False
    release_date: date | None = None
    end_standard_support: date | None = None
    end_of_life: date | None = None
    latest_point_release: str | None = None
    #: True while the series is still served from releases.ubuntu.com
    #: (EOL series move to old-releases.ubuntu.com).
    on_main_mirror: bool = False

    @property
    def series(self) -> str:
        """Archive series name, e.g. ``noble`` for "Noble Numbat"."""
        return self.codename.split()[0].lower() if self.codename else ""

    def status(self, today: date | None = None) -> SupportStatus:
        """Compute the support status relative to ``today``."""
        today = today or date.today()
        if self.end_standard_support and today <= self.end_standard_support:
            return SupportStatus.SUPPORTED
        if self.end_of_life and today <= self.end_of_life:
            # Past standard support but before the final EOL date: only
            # Expanded Security Maintenance is left.
            return SupportStatus.ESM
        if self.end_standard_support or self.end_of_life:
            return SupportStatus.EOL
        return SupportStatus.UNKNOWN

    def label(self, today: date | None = None) -> str:
        """Human readable label used in the release drop-down."""
        lts = " LTS" if self.is_lts else ""
        status = self.status(today)
        detail = status.value
        if status is SupportStatus.SUPPORTED and self.end_standard_support:
            detail = f"Supported until {self.end_standard_support:%b %Y}"
        elif status is SupportStatus.ESM and self.end_of_life:
            detail = f"ESM only until {self.end_of_life:%b %Y}"
        elif status is SupportStatus.EOL:
            eol = self.end_of_life or self.end_standard_support
            detail = f"EOL since {eol:%b %Y}" if eol else "EOL"
        return f"Ubuntu {self.version}{lts} — {self.codename} [{detail}]"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("release_date", "end_standard_support", "end_of_life"):
            data[key] = _date_to_json(data[key])
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UbuntuRelease:
        data = dict(data)
        for key in ("release_date", "end_standard_support", "end_of_life"):
            data[key] = _date_from_json(data.get(key))
        return cls(**data)


@dataclass(frozen=True, slots=True)
class Flavor:
    """An official Ubuntu flavor (Kubuntu, Xubuntu, …) or Ubuntu itself."""

    name: str
    description: str = ""
    homepage: str = ""
    #: Candidate directory names on cdimage.ubuntu.com, most likely first.
    #: Empty for the main Ubuntu product, which lives on releases.ubuntu.com.
    slug_candidates: tuple[str, ...] = ()

    @property
    def is_main_ubuntu(self) -> bool:
        return not self.slug_candidates

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["slug_candidates"] = list(self.slug_candidates)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Flavor:
        data = dict(data)
        data["slug_candidates"] = tuple(data.get("slug_candidates", ()))
        return cls(**data)


UBUNTU_FLAVOR = Flavor(
    name="Ubuntu",
    description="The main Ubuntu Desktop and Server images.",
    homepage="https://ubuntu.com/download",
)


@dataclass(frozen=True, slots=True)
class IsoImage:
    """A downloadable ISO image found in a release directory listing."""

    filename: str
    url: str
    version: str = ""
    kind: str = ""  # "desktop", "live-server", …
    arch: str = ""
    size_bytes: int | None = None  # approximate, parsed from the listing
    sha256: str | None = None

    def label(self) -> str:
        size = f" — {self.size_bytes / 1024**3:.1f} GiB" if self.size_bytes else ""
        return f"{self.filename}{size}"


@dataclass(slots=True)
class Catalog:
    """Everything the scraper learned in one refresh."""

    releases: list[UbuntuRelease] = field(default_factory=list)
    flavors: list[Flavor] = field(default_factory=list)
    fetched_at: datetime | None = None
    from_cache: bool = False
    warnings: list[str] = field(default_factory=list)

    def releases_since(self, min_version: str | None) -> list[UbuntuRelease]:
        """Releases newer than or equal to ``min_version``, newest first."""
        floor = version_key(min_version) if min_version else ()
        picked = [r for r in self.releases if version_key(r.version) >= floor]
        return sorted(picked, key=lambda r: version_key(r.version), reverse=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "releases": [r.to_dict() for r in self.releases],
            "flavors": [f.to_dict() for f in self.flavors],
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        fetched = data.get("fetched_at")
        return cls(
            releases=[UbuntuRelease.from_dict(r) for r in data.get("releases", [])],
            flavors=[Flavor.from_dict(f) for f in data.get("flavors", [])],
            fetched_at=datetime.fromisoformat(fetched) if fetched else None,
        )
