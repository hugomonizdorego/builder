"""Live release data from the official Ubuntu web sites.

The module is split in two layers so that the HTML handling can be unit
tested without network access:

* ``parse_*`` functions are pure: HTML/text in, models out.
* :class:`UbuntuScraper` does the HTTP work, merges the sources into a
  :class:`~ubuntu_builder.models.release.Catalog` and keeps an on-disk cache
  that is used when the network is unavailable.

Nothing in here hard-codes a version number or a flavor name: new releases
show up in the application as soon as Canonical publishes them.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ubuntu_builder import APP_ID, __version__
from ubuntu_builder.core.errors import ScraperError
from ubuntu_builder.models.release import (
    UBUNTU_FLAVOR,
    Catalog,
    Flavor,
    IsoImage,
    UbuntuRelease,
    version_key,
)

log = logging.getLogger(__name__)

RELEASES_URL = "https://releases.ubuntu.com/"
OLD_RELEASES_URL = "https://old-releases.ubuntu.com/releases/"
CDIMAGE_URL = "https://cdimage.ubuntu.com/"
RELEASE_CYCLE_URL = "https://ubuntu.com/about/release-cycle"
FLAVORS_URL = "https://ubuntu.com/desktop/flavors"
RELEASE_LIST_URL = "https://ubuntu.com/project/docs/release-team/list-of-releases/"

_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_abbr) if name}
_VERSION_CELL = re.compile(r"Ubuntu\s+(\d+\.\d+)((?:\.\d+)*)(\s+LTS)?", re.IGNORECASE)
_DATE = re.compile(r"\b([A-Z][a-z]{2})[a-z]*\.?\s+(?:(\d{1,2}),\s*)?(\d{4})\b")
_VERSION_DIR = re.compile(r"^(\d+\.\d+)((?:\.\d+)*)/$")
_ISO_NAME = re.compile(
    r"^(?P<product>[a-z][a-z-]*?)-(?P<version>\d+(?:\.\d+)+)-(?P<kind>[a-z-]+)-(?P<arch>[a-z0-9]+)\.iso$"
)
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT])?$", re.IGNORECASE)


# --------------------------------------------------------------------------
# Pure parsing helpers
# --------------------------------------------------------------------------


def parse_date(text: str, *, end_of_month: bool = False) -> date | None:
    """Parse ``"Oct 15, 2026"`` or the month-only form ``"Jul 2027"``.

    Month-only dates resolve to the first day of the month, or to the last
    one when ``end_of_month`` is set (appropriate for end-of-support dates:
    a release is supported *through* that month).
    """
    match = _DATE.search(text or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    year = int(match.group(3))
    if match.group(2):
        day = int(match.group(2))
    else:
        day = calendar.monthrange(year, month)[1] if end_of_month else 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _cell_text(cell: Tag) -> str:
    return " ".join(cell.get_text(" ", strip=True).split())


def parse_release_list(html: str) -> dict[str, UbuntuRelease]:
    """Parse the release team's "List of releases" page.

    The page holds several tables (current releases, ESM, end-of-life …).
    Tables are recognised by their column headers rather than by position,
    and the per-point-release rows are folded into one entry per series.
    """
    soup = BeautifulSoup(html, "lxml")
    releases: dict[str, UbuntuRelease] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [_cell_text(c).lower() for c in rows[0].find_all(["th", "td"])]
        if "version" not in headers or "code name" not in headers or "release" not in headers:
            continue  # ESM / legacy coverage tables
        column = {name: index for index, name in enumerate(headers)}
        for row in rows[1:]:
            cells = [_cell_text(c) for c in row.find_all(["th", "td"])]
            if len(cells) < len(headers):
                continue
            match = _VERSION_CELL.fullmatch(cells[column["version"]])
            if not match:
                continue  # placeholders such as "Ubuntu xx.yy"
            series, point = match.group(1), match.group(1) + match.group(2)
            released = parse_date(cells[column["release"]])
            eol = parse_date(cells[column["end of life"]], end_of_month=True) if "end of life" in column else None
            standard = (
                parse_date(cells[column["end of standard support"]], end_of_month=True)
                if "end of standard support" in column
                else eol  # the end-of-life table has a single date
            )
            new = UbuntuRelease(
                version=series,
                codename=cells[column["code name"]],
                is_lts=bool(match.group(3)),
                release_date=released,
                end_standard_support=standard,
                end_of_life=eol or standard,
                latest_point_release=point,
            )
            releases[series] = _merge_point_release(releases.get(series), new)
    return releases


def _merge_point_release(old: UbuntuRelease | None, new: UbuntuRelease) -> UbuntuRelease:
    """Fold two rows of the same series (e.g. 24.04 and 24.04.5) together."""
    if old is None:
        return new
    dates = [d for d in (old.release_date, new.release_date) if d]
    latest = max(old.latest_point_release or "", new.latest_point_release or "", key=version_key)
    return replace(
        old,
        is_lts=old.is_lts or new.is_lts,
        release_date=min(dates) if dates else None,  # date of the .0 release
        end_standard_support=old.end_standard_support or new.end_standard_support,
        end_of_life=old.end_of_life or new.end_of_life,
        latest_point_release=latest or None,
    )


def parse_release_cycle(html: str) -> dict[str, tuple[bool, date | None, date | None]]:
    """Parse ubuntu.com/about/release-cycle.

    Returns ``{version: (is_lts, end_of_standard_support, end_of_esm)}``.
    Used to complement (or, if needed, replace) the release list.
    """
    soup = BeautifulSoup(html, "lxml")
    result: dict[str, tuple[bool, date | None, date | None]] = {}
    for table in soup.select("table.release-cycle-table"):
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            match = re.match(r"\s*(\d+\.\d+)(\s+LTS)?", cells[0].get_text(" ", strip=True))
            if not match or match.group(1) in result:
                continue
            standard = parse_date(_cell_text(cells[1]), end_of_month=True)
            esm = parse_date(_cell_text(cells[2]), end_of_month=True) if len(cells) > 2 else None
            result[match.group(1)] = (bool(match.group(2)), standard, esm)
    return result


def parse_releases_index(html: str) -> dict[str, str]:
    """Parse the releases.ubuntu.com index: ``{series: newest point release}``."""
    soup = BeautifulSoup(html, "lxml")
    newest: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        match = _VERSION_DIR.match(anchor["href"])
        if not match:
            continue
        series, point = match.group(1), match.group(1) + match.group(2)
        if version_key(point) > version_key(newest.get(series, "0")):
            newest[series] = point
    return newest


def _slug_candidates(name: str) -> tuple[str, ...]:
    """Possible cdimage directory names for a flavor name.

    cdimage is not consistent ("ubuntu-mate" but "ubuntustudio"), so both
    spellings are produced and verified lazily against the server.
    """
    words = name.lower().split()
    candidates = ["-".join(words), "".join(words)]
    return tuple(dict.fromkeys(candidates))  # de-duplicate, keep order


def parse_flavors(html: str) -> list[Flavor]:
    """Parse the official flavors from ubuntu.com/desktop/flavors."""
    soup = BeautifulSoup(html, "lxml")
    flavors: list[Flavor] = []
    for heading in soup.select("h2.p-heading-icon__title"):
        name = _cell_text(heading)
        if not name:
            continue
        # Walk up to the card that also holds the description and the
        # "Get <flavor>" button.
        card: Tag | None = heading
        button: Tag | None = None
        for _ in range(6):
            card = card.parent if card else None
            if card is None:
                break
            button = card.select_one("a[class*=p-button]")
            if button is not None:
                break
        paragraph = card.find("p") if card else None
        flavors.append(
            Flavor(
                name=name,
                description=_cell_text(paragraph) if paragraph else "",
                homepage=button["href"] if button is not None and button.has_attr("href") else "",
                slug_candidates=_slug_candidates(name),
            )
        )
    return flavors


def parse_size(text: str) -> int | None:
    """Convert an Apache listing size such as ``6.1G`` into bytes."""
    match = _SIZE.match(text.strip())
    if not match:
        return None
    factor = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[(match.group(2) or "").upper()]
    return int(float(match.group(1)) * factor)


def parse_iso_listing(html: str, base_url: str) -> list[IsoImage]:
    """Extract the ``.iso`` files from an Apache-style directory listing."""
    soup = BeautifulSoup(html, "lxml")
    images: dict[str, IsoImage] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        filename = href.rsplit("/", 1)[-1]
        if not filename.endswith(".iso"):
            continue
        size: int | None = None
        row = anchor.find_parent("tr")
        if row is not None:
            for cell in row.find_all("td"):
                size = parse_size(_cell_text(cell))
                if size:
                    break
        known = images.get(filename)
        if known is not None:
            # The same file is often linked twice (prose + listing row);
            # keep the first entry but pick up the size from the listing.
            if known.size_bytes is None and size:
                images[filename] = replace(known, size_bytes=size)
            continue
        match = _ISO_NAME.match(filename)
        images[filename] = IsoImage(
            filename=filename,
            url=urljoin(base_url, href),
            version=match.group("version") if match else "",
            kind=match.group("kind") if match else "",
            arch=match.group("arch") if match else "",
            size_bytes=size,
        )
    return sorted(images.values(), key=lambda i: (version_key(i.version), i.kind), reverse=True)


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse a ``SHA256SUMS`` file into ``{filename: hex digest}``."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            sums[parts[1].lstrip("*").strip()] = parts[0].lower()
    return sums


# --------------------------------------------------------------------------
# Network layer
# --------------------------------------------------------------------------


def default_cache_file() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / APP_ID / "catalog.json"


class UbuntuScraper:
    """Fetches and merges release information from the official sources."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = 20.0,
        cache_file: Path | None = None,
    ) -> None:
        self._timeout = timeout
        self._cache_file = cache_file or default_cache_file()
        self._session = session or self._build_session()
        self._slug_cache: dict[str, str] = {}

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        session.headers["User-Agent"] = f"{APP_ID}/{__version__}"
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504))
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def _get(self, url: str) -> str:
        try:
            response = self._session.get(url, timeout=self._timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ScraperError(f"Could not fetch {url}: {exc}") from exc
        return response.text

    # -- catalog -----------------------------------------------------------

    def fetch_releases(self) -> tuple[list[UbuntuRelease], list[str]]:
        """Merge the three release sources. Returns (releases, warnings)."""
        warnings: list[str] = []
        listed: dict[str, UbuntuRelease] = {}
        cycle: dict[str, tuple[bool, date | None, date | None]] = {}
        index: dict[str, str] = {}
        for name, url, parser, target in (
            ("release list", RELEASE_LIST_URL, parse_release_list, listed),
            ("release cycle", RELEASE_CYCLE_URL, parse_release_cycle, cycle),
            ("download index", RELEASES_URL, parse_releases_index, index),
        ):
            try:
                target.update(parser(self._get(url)))  # type: ignore[arg-type]
            except ScraperError as exc:
                warnings.append(f"{name}: {exc}")
        if not listed and not cycle:
            raise ScraperError("No release information available: " + "; ".join(warnings))

        merged: dict[str, UbuntuRelease] = dict(listed)
        for version, (is_lts, standard, esm) in cycle.items():
            known = merged.get(version)
            if known is None:
                # Only on the release-cycle page: usable, but without codename.
                merged[version] = UbuntuRelease(
                    version=version,
                    codename="",
                    is_lts=is_lts,
                    end_standard_support=standard,
                    end_of_life=esm or standard,
                )
            else:
                merged[version] = replace(
                    known,
                    is_lts=known.is_lts or is_lts,
                    end_standard_support=known.end_standard_support or standard,
                    end_of_life=known.end_of_life or esm or standard,
                )
        for version, point in index.items():
            if version in merged:
                known = merged[version]
                latest = max(known.latest_point_release or version, point, key=version_key)
                merged[version] = replace(known, on_main_mirror=True, latest_point_release=latest)
        ordered = sorted(merged.values(), key=lambda r: version_key(r.version), reverse=True)
        return ordered, warnings

    def fetch_flavors(self) -> list[Flavor]:
        """The main Ubuntu product followed by the official flavors."""
        return [UBUNTU_FLAVOR, *parse_flavors(self._get(FLAVORS_URL))]

    def fetch_catalog(self, *, allow_cache: bool = True) -> Catalog:
        """Fetch everything; fall back to the cached copy when offline."""
        try:
            releases, warnings = self.fetch_releases()
            try:
                flavors = self.fetch_flavors()
            except ScraperError as exc:
                warnings.append(f"flavors: {exc}")
                flavors = [UBUNTU_FLAVOR]
            catalog = Catalog(releases, flavors, datetime.now(), warnings=warnings)
            self._save_cache(catalog)
            return catalog
        except ScraperError as exc:
            cached = self._load_cache() if allow_cache else None
            if cached is None:
                raise
            cached.from_cache = True
            cached.warnings.append(f"Offline — showing cached data. ({exc})")
            return cached

    # -- images ------------------------------------------------------------

    def _resolve_flavor_base(self, flavor: Flavor) -> str:
        """Find the cdimage directory of a flavor by probing the candidates."""
        if flavor.name in self._slug_cache:
            return self._slug_cache[flavor.name]
        for slug in flavor.slug_candidates:
            url = f"{CDIMAGE_URL}{slug}/releases/"
            try:
                if self._session.head(url, timeout=self._timeout, allow_redirects=True).ok:
                    self._slug_cache[flavor.name] = url
                    return url
            except requests.RequestException as exc:
                raise ScraperError(f"Could not reach {url}: {exc}") from exc
        raise ScraperError(f"No image directory found for {flavor.name} on cdimage.ubuntu.com.")

    def image_directories(self, release: UbuntuRelease, flavor: Flavor) -> list[str]:
        """Candidate directory URLs holding the ISOs, most likely first."""
        if flavor.is_main_ubuntu:
            urls = [f"{RELEASES_URL}{release.version}/", f"{OLD_RELEASES_URL}{release.version}/"]
            return urls if release.on_main_mirror else urls[::-1]
        base = self._resolve_flavor_base(flavor)
        return [f"{base}{release.version}/release/"]

    def fetch_iso_images(self, release: UbuntuRelease, flavor: Flavor) -> list[IsoImage]:
        """List the ISO images of ``flavor`` for ``release`` with checksums."""
        last_error: ScraperError | None = None
        for url in self.image_directories(release, flavor):
            try:
                images = parse_iso_listing(self._get(url), url)
            except ScraperError as exc:
                last_error = exc
                continue
            if not images:
                continue
            try:
                sums = parse_sha256sums(self._get(urljoin(url, "SHA256SUMS")))
            except ScraperError:
                sums = {}  # the download still works, just unverified
            return [replace(image, sha256=sums.get(image.filename)) for image in images]
        if last_error is not None:
            raise ScraperError(
                f"{flavor.name} {release.version} has no downloadable images ({last_error})."
            )
        return []

    # -- cache -------------------------------------------------------------

    def _save_cache(self, catalog: Catalog) -> None:
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(json.dumps(catalog.to_dict(), indent=2), encoding="utf-8")
        except OSError as exc:  # a read-only home must not break the app
            log.warning("Could not write catalog cache: %s", exc)

    def _load_cache(self) -> Catalog | None:
        try:
            return Catalog.from_dict(json.loads(self._cache_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, KeyError):
            return None
