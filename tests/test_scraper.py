"""Parser tests. The HTML snippets mirror the markup of the live pages."""

from datetime import date

from os_builder.core import scraper
from os_builder.models.release import SupportStatus

RELEASE_LIST = """
<table class="release-table"><thead><tr>
 <th>Version</th><th>Code name</th><th>Docs</th><th>Release</th><th>End of Standard Support</th><th>End of Life</th>
</tr></thead><tbody>
 <tr><td>Ubuntu 26.10</td><td>Stonking Stingray</td><td>Notes</td><td>Oct 15, 2026</td><td>Jul 2027</td><td>Jul 2027</td></tr>
 <tr><td>Ubuntu 24.04.5 LTS</td><td>Noble Numbat</td><td>Notes</td><td>Sep 10, 2026</td><td>Jun 2029</td><td>Apr 2039</td></tr>
 <tr><td>Ubuntu 24.04 LTS</td><td>Noble Numbat</td><td>Notes</td><td>Apr 25, 2024</td><td>Jun 2029</td><td>Apr 2039</td></tr>
 <tr><td>Ubuntu 20.04.6 LTS</td><td>Focal Fossa</td><td>Notes</td><td>Mar 23, 2023</td><td>May 2025</td><td>Apr 2030</td></tr>
</tbody></table>
<table class="release-table"><tr><th>Version</th><th>Code name</th><th>Schedule</th><th>Docs</th><th>Release</th>
 <th>End of Standard Support</th></tr>
 <tr><td>Ubuntu xx.yy</td><td>Tenacious T-Rex</td><td>TBD</td><td>TBD</td><td>Apr 2027</td><td>Jan 2028</td></tr></table>
<table class="release-table"><tr><th>Version</th><th>Detailed ESM coverage</th><th>Start of ESM</th>
 <th># of years</th><th>End of Life</th></tr>
 <tr><td>Ubuntu 20.04 ESM</td><td>x</td><td>Jun 2025</td><td>5 years</td><td>Apr 2030</td></tr></table>
<table class="release-table"><tr><th>Version</th><th>Code name</th><th>Docs</th><th>Release</th><th>End of Life</th></tr>
 <tr><td>Ubuntu 23.10</td><td>Mantic Minotaur</td><td>Notes</td><td>Oct 12, 2023</td><td>Jul 11, 2024</td></tr></table>
"""

RELEASE_CYCLE = """
<table class="release-cycle-table is-desktop"><tr><th>Version</th><th>Standard</th></tr>
 <tr><td><span class="p-heading--5"> 26.04 LTS </span><p>Ubuntu 26.04 LTS</p></td><td> May 2031 </td>
     <td> May 2036 </td></tr>
 <tr><td><span class="p-heading--5"> 26.10 </span></td><td> Jul 2027 </td><td> Not applicable </td></tr>
</table>
"""

INDEX = '<a href="?C=N;O=D">Name</a><a href="24.04/">24.04/</a><a href="24.04.5/">x</a>' \
        '<a href="24.04.5.1/">x</a><a href="24.04.10/">x</a><a href="noble/">noble/</a><a href="26.10/">x</a>'

FLAVORS = """
<div class="row--50-50"><div class="col"><div class="p-heading-icon"><div class="p-heading-icon__header">
 <h2 class="p-heading-icon__title">Ubuntu Studio</h2></div></div></div>
 <div class="col"><p>Ubuntu Studio is for   creators.</p>
 <a class="p-button--positive" href="https://ubuntustudio.org/">Get Ubuntu Studio</a></div></div>
<h2>Looking for Ubuntu?</h2>
"""

LISTING = """
<p><a href="ubuntu-24.04.5-desktop-amd64.iso">64-bit PC (AMD64) desktop image</a></p>
<table><tr><td></td><td><a href="ubuntu-24.04.5-desktop-amd64.iso">ubuntu-24.04.5-desktop-amd64.iso</a></td>
 <td>2026-09-10 10:00</td><td>6.1G</td><td>Desktop image</td></tr>
<tr><td></td><td><a href="ubuntu-24.04.5-desktop-amd64.iso.zsync">zsync</a></td><td>x</td><td>14M</td></tr>
<tr><td></td><td><a href="ubuntu-24.04.5-live-server-amd64.iso">server</a></td><td>x</td><td>2.7G</td></tr></table>
"""


def test_parse_date_variants():
    assert scraper.parse_date("Oct 15, 2026") == date(2026, 10, 15)
    assert scraper.parse_date("Jul 2027") == date(2027, 7, 1)
    assert scraper.parse_date("Feb 2028", end_of_month=True) == date(2028, 2, 29)
    assert scraper.parse_date("TBD") is None


def test_release_list_merges_point_releases_and_skips_other_tables():
    releases = scraper.parse_release_list(RELEASE_LIST)
    assert set(releases) == {"26.10", "24.04", "20.04", "23.10"}  # no "xx.yy", no ESM rows
    noble = releases["24.04"]
    assert noble.is_lts and noble.codename == "Noble Numbat" and noble.series == "noble"
    assert noble.release_date == date(2024, 4, 25)  # the .0 release, not the latest point release
    assert noble.latest_point_release == "24.04.5"
    assert noble.end_standard_support == date(2029, 6, 30)


def test_support_status_is_computed_from_dates():
    releases = scraper.parse_release_list(RELEASE_LIST)
    today = date(2026, 11, 17)
    assert releases["26.10"].status(today) is SupportStatus.SUPPORTED
    assert releases["20.04"].status(today) is SupportStatus.ESM
    assert releases["23.10"].status(today) is SupportStatus.EOL
    assert "EOL since Jul 2024" in releases["23.10"].label(today)


def test_release_cycle():
    cycle = scraper.parse_release_cycle(RELEASE_CYCLE)
    assert cycle["26.04"] == (True, date(2031, 5, 31), date(2036, 5, 31))
    assert cycle["26.10"] == (False, date(2027, 7, 31), None)


def test_releases_index_picks_numerically_newest_point_release():
    assert scraper.parse_releases_index(INDEX) == {"24.04": "24.04.10", "26.10": "26.10"}


def test_flavors():
    (studio,) = scraper.parse_flavors(FLAVORS)
    assert studio.name == "Ubuntu Studio"
    assert studio.description == "Ubuntu Studio is for creators."
    assert studio.homepage == "https://ubuntustudio.org/"
    assert studio.slug_candidates == ("ubuntu-studio", "ubuntustudio")
    assert not studio.is_main_ubuntu


def test_iso_listing_dedupes_and_reads_sizes():
    images = scraper.parse_iso_listing(LISTING, "https://releases.ubuntu.com/24.04/")
    assert [i.filename for i in images] == [
        "ubuntu-24.04.5-live-server-amd64.iso",
        "ubuntu-24.04.5-desktop-amd64.iso",
    ]
    desktop = images[1]
    assert desktop.url == "https://releases.ubuntu.com/24.04/ubuntu-24.04.5-desktop-amd64.iso"
    assert (desktop.version, desktop.kind, desktop.arch) == ("24.04.5", "desktop", "amd64")
    assert desktop.size_bytes == int(6.1 * 1024**3)


def test_sha256sums():
    digest = "a" * 64
    sums = scraper.parse_sha256sums(f"{digest} *ubuntu.iso\nnot a checksum line\n")
    assert sums == {"ubuntu.iso": digest}


def test_catalog_falls_back_to_cache_when_offline(tmp_path):
    import requests
    from os_builder.models.release import Catalog, UbuntuRelease

    class Offline(requests.Session):
        def get(self, *args, **kwargs):  # noqa: ARG002
            raise requests.ConnectionError("no network")

    cache = tmp_path / "catalog.json"
    client = scraper.UbuntuScraper(Offline(), cache_file=cache)
    try:
        client.fetch_catalog()
    except scraper.ScraperError:
        pass
    else:
        raise AssertionError("expected ScraperError without cache")
    client._save_cache(Catalog(releases=[UbuntuRelease("24.04", "Noble Numbat", is_lts=True)]))
    catalog = client.fetch_catalog()
    assert catalog.from_cache and catalog.releases[0].version == "24.04"
    assert "Offline" in catalog.warnings[-1]
