"""Inventory monthly OpenAQ archive availability by monitoring location."""

import calendar
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

ARCHIVE_BUCKET_URL = "https://openaq-data-archive.s3.amazonaws.com/"

INVENTORY_INPUT_PATH = Path("data/processed/station_inventory.csv")

CACHE_DIR = Path("data/raw/archive_inventory")

OUTPUT_PATH = Path("data/processed/archive_monthly_coverage.csv")

ANALYSIS_START_DATE = pd.Timestamp("2025-03-01")
ANALYSIS_END_DATE = pd.Timestamp("2026-08-31")

REQUEST_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 5


# ----------------------------------------------------------------------------
# Analysis period
# ----------------------------------------------------------------------------


def get_target_months(
    start_date: pd.Timestamp, end_date: pd.Timestamp
) -> list[pd.Timestamp]:
    """Generate monthly timestamps for the analysis period.

    Parameters
    ----------
    start_date : pandas.Timestamp
        First date included in the analysis period.
    end_date : pandas.Timestamp
        Last date included in the analysis period.

    Returns
    -------
    list[pandas.Timestamp]
        Month-start timestamps spanning the requested period.
    """
    return list(pd.date_range(start=start_date, end=end_date, freq="MS"))


# -----------------------------------------------------------------------------
# Archive paths
# -----------------------------------------------------------------------------


def build_archive_prefix(location_id: int, year: int) -> str:
    """Build an OpenAQ archive prefix for one location and year.

    Parameters
    ----------
    location_id : int
        OpenAQ monitoring-location identifier.
    year : int
        Four-digit calendar year.

    Returns
    -------
    str
        S3 object prefix for the requested location and year.
    """
    return f"records/csv.gz/locationid={location_id}/year={year}/"


def get_cache_path(location_id: int, year: int) -> Path:
    """Build the cache path for a location-year archive listing.

    Parameters
    ----------
    location_id : int
        OpenAQ monitoring-location identifier.
    year : int
        Four-digit calendar year.

    Returns
    -------
    pathlib.Path
        Local JSON cache path.
    """
    return CACHE_DIR / f"location_{location_id}_year_{year}.json"


# ----------------------------------------------------------------------------
# Archive listing
# ----------------------------------------------------------------------------


def parse_s3_keys(xml_text: str) -> list[str]:
    """Extract object keys from an S3 ListObjectsV2 response.

    Parameters
    ----------
    xml_text : str
        XML response returned by the public S3 bucket.

    Returns
    -------
    list[str]
        Object keys contained in the response.
    """
    root = ET.fromstring(xml_text)

    namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

    return [
        element.text
        for element in root.findall(".//s3:Key", namespace)
        if element.text
    ]


def fetch_archive_keys(
    session: requests.Session,
    location_id: int,
    year: int,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
) -> list[str]:
    """List OpenAQ archive objects for one location and year.

    Parameters
    ----------
    session : requests.Session
        Reusable HTTP session.
    location_id : int
        OpenAQ monitoring-location identifier.
    year : int
        Four-digit calendar year.
    timeout : int, optional
        Maximum seconds to wait for each request.
        Defaults to REQUEST_TIMEOUT_SECONDS.
    max_attempts : int, optional
        Maximum number of attempts for transient failures.
        Defaults to MAX_ATTEMPTS.

    Returns
    -------
    list[str]
        Archive object keys for the location and year.

    Raises
    ------
    RuntimeError
        If the archive listing cannot be retrieved after all attempts.
    """
    prefix = build_archive_prefix(location_id=location_id, year=year)

    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = session.get(
                ARCHIVE_BUCKET_URL,
                params={"list-type": "2", "prefix": prefix, "max-keys": 1000},
                timeout=timeout,
            )

            response.raise_for_status()

            return parse_s3_keys(response.text)

        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.HTTPError,
            ET.ParseError,
        ) as exc:
            last_error = exc

            if attempt == max_attempts:
                break

            delay = 2 ** (attempt - 1)

            print(
                f"Archive request failed for "
                f"location={location_id}, year={year}. "
                f"Retrying in {delay} seconds..."
            )

            time.sleep(delay)

    raise RuntimeError(
        "Unable to retrieve archive listing for "
        f"location {location_id}, year {year}."
    ) from last_error


# ----------------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------------


def load_cached_keys(location_id: int, year: int) -> list[str] | None:
    """Load cached archive keys for a location and year.

    Parameters
    ----------
    location_id : int
        OpenAQ monitoring-location identifier.
    year : int
        Four-digit calendar year.

    Returns
    -------
    list[str] or None
        Cached object keys, or None when no cache exists.
    """
    cache_path = get_cache_path(location_id=location_id, year=year)

    if not cache_path.exists():
        return None

    return json.loads(cache_path.read_text(encoding="utf-8"))


def save_cached_keys(location_id: int, year: int, keys: list[str]) -> None:
    """Cache archive keys for a location and year.

    Parameters
    ----------
    location_id : int
        OpenAQ monitoring-location identifier.
    year : int
        Four-digit calendar year.
    keys : list[str]
        S3 object keys returned for the location and year.

    Returns
    -------
    None
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cache_path = get_cache_path(location_id=location_id, year=year)

    cache_path.write_text(json.dumps(keys, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# Archive key parsing
# ----------------------------------------------------------------------------


def get_key_month(key: str) -> int | None:
    """Extract the month number from an OpenAQ archive key.

    Parameters
    ----------
    key : str
        OpenAQ S3 archive object key.

    Returns
    -------
    int or None
        Month number from 1 through 12, or None when unavailable.
    """
    marker = "month="

    if marker not in key:
        return None

    month_text = key.split(marker, maxsplit=1)[1].split("/", maxsplit=1)[0]

    try:
        return int(month_text)
    except ValueError:
        return None


def count_month_files(keys: list[str]) -> dict[int, int]:
    """Count archive files by calendar month.

    Parameters
    ----------
    keys : list[str]
        S3 archive object keys.

    Returns
    -------
    dict[int, int]
        Mapping from month number to number of archive files.
    """
    counts = {month: 0 for month in range(1, 13)}

    for key in keys:
        month = get_key_month(key)

        if month in counts:
            counts[month] += 1

    return counts


# ----------------------------------------------------------------------------
# Coverage inventory
# ----------------------------------------------------------------------------


def build_monthly_rows(
    location: pd.Series,
    year: int,
    keys: list[str],
    target_months: set[tuple[int, int]]
) -> list[dict]:
    """Build monthly archive-coverage rows for one location-year.

    Parameters
    ----------
    location : pandas.Series
        Location metadata from the station inventory.
    year : int
        Calendar year being processed.
    keys : list[str]
        Archive object keys for the location and year.
    target_months : set[tuple[int, int]]
        Year-month pairs included in the analysis period.

    Returns
    -------
    list[dict]
        Monthly archive-coverage records.
    """
    file_counts = count_month_files(keys)

    rows = []

    for month in range(1, 13):
        if (year, month) not in target_months:
            continue

        expected_days = calendar.monthrange(year, month)[1]

        file_count = file_counts[month]

        coverage_pct = file_count / expected_days * 100

        rows.append(
            {
                "location_id": location["location_id"],
                "station_name": location["station_name"],
                "provider_id": location["provider_id"],
                "provider_name": location["provider_name"],
                "country_id": location["country_id"],
                "country_code": location["country_code"],
                "country_name": location["country_name"],
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "target_parameter": location["target_parameter"],
                "year": year,
                "month": month,
                "expected_days": expected_days,
                "archive_file_count": file_count,
                "archive_file_coverage_pct": coverage_pct,
            }
        )

    return rows


def collect_archive_coverage(
    inventory: pd.DataFrame, target_months: list[pd.Timestamp]
) -> pd.DataFrame:
    """Collect monthly archive availability for all target locations.

    Parameters
    ----------
    inventory : pandas.DataFrame
        Monitoring-location inventory.
    target_months : list[pandas.Timestamp]
        Months included in the analysis period.

    Returns
    -------
    pandas.DataFrame
        One archive-coverage row per location and target month.
    """
    eligible_locations = inventory[inventory["has_target_parameter"]].copy()

    year_month_pairs = {(month.year, month.month) for month in target_months}

    target_years = sorted({month.year for month in target_months})

    session = requests.Session()

    rows = []

    total_locations = len(eligible_locations)

    for position, (_, location) in enumerate(
        eligible_locations.iterrows(), start=1
    ):
        location_id = int(location["location_id"])

        print(
            f"[{position:>3}/{total_locations}] "
            f"location={location_id} "
            f"{location['station_name']}"
        )

        for year in target_years:
            keys = load_cached_keys(location_id=location_id, year=year)

            source = "cache"

            if keys is None:
                source = "s3"

                keys = fetch_archive_keys(
                    session=session, location_id=location_id, year=year
                )

                save_cached_keys(location_id=location_id, year=year, keys=keys)

            print(f"    {year}: " f"{len(keys):>3} files " f"({source})")

            rows.extend(
                build_monthly_rows(
                    location=location,
                    year=year,
                    keys=keys,
                    target_months=year_month_pairs,
                )
            )

    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------


def print_summary(coverage: pd.DataFrame) -> None:
    """Print archive availability summary statistics.

    Parameters
    ----------
    coverage : pandas.DataFrame
        Monthly archive-coverage inventory.

    Returns
    -------
    None
    """
    print()
    print("Archive coverage inventory complete")
    print("-----------------------------------")

    print(f"Location-month rows: {len(coverage):,}")

    print(f"Unique locations: {coverage['location_id'].nunique():,}")

    print()

    monthly_summary = (
        coverage.groupby(["year", "month"])
        .agg(
            locations_with_files=(
                "archive_file_count",
                lambda values: (values > 0).sum(),
            ),
            median_file_coverage_pct=("archive_file_coverage_pct", "median"),
            mean_file_coverage_pct=("archive_file_coverage_pct", "mean"),
        )
        .reset_index()
    )

    print("Monthly archive availability:")
    print(monthly_summary.to_string(index=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Build and save the monthly archive-coverage inventory."""
    inventory = pd.read_csv(INVENTORY_INPUT_PATH)

    target_months = get_target_months(
        start_date=ANALYSIS_START_DATE,
        end_date=ANALYSIS_END_DATE,
    )

    coverage = collect_archive_coverage(
        inventory=inventory,
        target_months=target_months,
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    coverage.to_csv(OUTPUT_PATH, index=False)

    print_summary(coverage)


if __name__ == "__main__":
    main()
