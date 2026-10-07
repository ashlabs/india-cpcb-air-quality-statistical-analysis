"""Download target measurements from the OpenAQ historical archive."""

import argparse
import calendar
import gzip
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ARCHIVE_OBJECT_BASE_URL = "https://openaq-data-archive.s3.amazonaws.com"
SELECTED_LOCATIONS_PATH = Path("data/processed/selected_metro_stations.csv")
ARCHIVE_CACHE_DIR = Path("data/raw/archive_inventory")
OUTPUT_DIR = Path("data/processed/measurements_15min")

MANIFEST_OUTPUT_PATH = Path("data/processed/measurement_download_manifest.csv")

ANALYSIS_START_DATE = pd.Timestamp("2025-03-01")
ANALYSIS_END_DATE = pd.Timestamp("2026-08-31")

TARGET_PARAMETER = "pm25"
TARGET_UNITS = "µg/m³"

REQUEST_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 5
MAX_WORKERS = 8

REQUIRED_SOURCE_COLUMNS = {
    "location_id",
    "sensors_id",
    "location",
    "datetime",
    "lat",
    "lon",
    "parameter",
    "units",
    "value",
}


# ---------------------------------------------------------------------------
# Command-line arguments
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns
    -------
    argparse.Namespace
        Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(
        description=("Download target measurements from the OpenAQ historical archive.")
    )

    parser.add_argument(
        "--location-id",
        type=int,
        default=None,
        help="Download only one location. Useful for testing the pipeline.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download months that already have completed output.",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Archive inventory
# ---------------------------------------------------------------------------


def load_archive_keys(location_id: int, years: tuple[int, ...]) -> list[str]:
    """Load cached archive keys for a monitoring location.

    Parameters
    ----------
    location_id : int
        OpenAQ monitoring-location identifier.
    years : tuple[int, ...]
        Calendar years to load from the archive inventory cache.

    Returns
    -------
    list[str]
        Sorted archive object keys for the requested location.

    Raises
    ------
    FileNotFoundError
        If an expected archive-inventory cache file is missing.
    """
    keys = []

    for year in years:
        cache_path = ARCHIVE_CACHE_DIR / f"location_{location_id}_year_{year}.json"

        if not cache_path.exists():
            raise FileNotFoundError(f"Missing archive cache: {cache_path}")

        year_keys = json.loads(cache_path.read_text(encoding="utf-8"))

        keys.extend(year_keys)

    return sorted(keys)


def extract_archive_date(key: str) -> pd.Timestamp | None:
    """Extract the source date from an archive object key.

    Parameters
    ----------
    key : str
        OpenAQ archive object key.

    Returns
    -------
    pandas.Timestamp or None
        Date encoded in the archive filename, or None when the
        filename does not match the expected structure.
    """
    match = re.search(r"location-\d+-(\d{8})\.csv\.gz$", key)

    if not match:
        return None

    parsed_date = pd.to_datetime(match.group(1), format="%Y%m%d", errors="coerce")

    if not isinstance(parsed_date, pd.Timestamp):
        return None

    return parsed_date


def filter_archive_keys(
    keys: list[str], start_date: pd.Timestamp, end_date: pd.Timestamp
) -> list[str]:
    """Keep archive files within the analysis date range.

    Parameters
    ----------
    keys : list[str]
        OpenAQ archive object keys.
    start_date : pandas.Timestamp
        First date included in the analysis.
    end_date : pandas.Timestamp
        Last date included in the analysis.

    Returns
    -------
    list[str]
        Archive keys whose source dates fall within the range.
    """
    selected_keys = []

    for key in keys:
        archive_date = extract_archive_date(key)

        if archive_date is None:
            continue

        if start_date <= archive_date <= end_date:
            selected_keys.append(key)

    return selected_keys


def group_keys_by_month(keys: list[str]) -> dict[tuple[int, int], list[str]]:
    """Group archive object keys by calendar year and month.

    Parameters
    ----------
    keys : list[str]
        Archive object keys containing dates in their filenames.

    Returns
    -------
    dict[tuple[int, int], list[str]]
        Mapping of ``(year, month)`` to sorted archive keys.
    """
    grouped: dict[tuple[int, int], list[str]] = {}

    for key in keys:
        archive_date = extract_archive_date(key)

        if archive_date is None:
            continue

        month_key = (archive_date.year, archive_date.month)

        grouped.setdefault(month_key, []).append(key)

    for month_keys in grouped.values():
        month_keys.sort()

    return grouped


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download_archive_file(
    key: str, timeout: int = REQUEST_TIMEOUT_SECONDS, max_attempts: int = MAX_ATTEMPTS
) -> pd.DataFrame:
    """Download and decompress one OpenAQ archive file.

    Parameters
    ----------
    key : str
        S3 object key for the archive file.
    timeout : int, optional
        Maximum number of seconds to wait for each request.
        Defaults to REQUEST_TIMEOUT_SECONDS.
    max_attempts : int, optional
        Maximum number of download attempts.
        Defaults to MAX_ATTEMPTS.

    Returns
    -------
    pandas.DataFrame
        Data loaded from the compressed CSV file.

    Raises
    ------
    RuntimeError
        If the archive file cannot be retrieved after all attempts.
    """
    url = f"{ARCHIVE_OBJECT_BASE_URL}/{key}"

    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            content = gzip.decompress(response.content)

            return pd.read_csv(io.BytesIO(content))

        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.HTTPError,
            gzip.BadGzipFile,
            pd.errors.ParserError,
        ) as exc:
            last_error = exc

            if attempt == max_attempts:
                break

            delay = 2 ** (attempt - 1)

            time.sleep(delay)

    raise RuntimeError(f"Unable to download archive file: {key}") from last_error


# ---------------------------------------------------------------------------
# Measurement extraction
# ---------------------------------------------------------------------------


def validate_source_schema(dataframe: pd.DataFrame, key: str) -> None:
    """Validate required columns in a source archive file.

    Parameters
    ----------
    dataframe : pandas.DataFrame
        OpenAQ archive data.
    key : str
        Archive object key used for error reporting.

    Returns
    -------
    None

    Raises
    ------
    ValueError
        If required source columns are missing.
    """
    missing_columns = REQUIRED_SOURCE_COLUMNS - set(dataframe.columns)

    if missing_columns:
        raise ValueError(
            f"Archive file {key} is missing columns: " f"{sorted(missing_columns)}"
        )


def extract_target_measurements(
    dataframe: pd.DataFrame,
    key: str,
    expected_location_id: int,
    target_parameter: str,
    target_units: str,
) -> pd.DataFrame:
    """Extract target measurements from one archive file.

    Parameters
    ----------
    dataframe : pandas.DataFrame
        Complete OpenAQ archive data for one location-day.
    key : str
        Archive object key identifying the source file.
    expected_location_id : int
        Location ID expected in the source records.
    target_parameter : str
        Parameter to retain, such as ``"pm25"``.
    target_units : str
        Required measurement units.

    Returns
    -------
    pandas.DataFrame
        Target measurements from the source file.

    Raises
    ------
    ValueError
        If the source schema, location ID, or units are inconsistent
        with the configured dataset.
    """
    validate_source_schema(dataframe=dataframe, key=key)

    location_ids = set(dataframe["location_id"].dropna().astype(int).unique())

    if location_ids and location_ids != {expected_location_id}:
        raise ValueError(
            f"Unexpected location IDs in {key}: " f"{sorted(location_ids)}"
        )

    parameter_rows = dataframe[dataframe["parameter"] == target_parameter].copy()

    if parameter_rows.empty:
        return parameter_rows

    observed_units = set(parameter_rows["units"].dropna().unique())

    unexpected_units = observed_units - {target_units}

    if unexpected_units:
        raise ValueError(
            f"Unexpected {target_parameter} units in {key}: "
            f"{sorted(unexpected_units)}"
        )

    target = parameter_rows[parameter_rows["units"] == target_units].copy()

    source_date = extract_archive_date(key)

    target = target.rename(
        columns={
            "sensors_id": "sensor_id",
            "location": "source_location_name",
            "datetime": "datetime_source",
            "lat": "latitude",
            "lon": "longitude",
        }
    )

    target["datetime_utc"] = pd.to_datetime(
        target["datetime_source"], errors="coerce", utc=True
    )

    target["source_date"] = source_date
    target["source_key"] = key

    columns = [
        "location_id",
        "sensor_id",
        "source_location_name",
        "datetime_source",
        "datetime_utc",
        "latitude",
        "longitude",
        "parameter",
        "units",
        "value",
        "source_date",
        "source_key",
    ]

    return target[columns]


def download_target_file(
    key: str, location_id: int, target_parameter: str, target_units: str
) -> pd.DataFrame:
    """Download one archive file and extract target measurements.

    Parameters
    ----------
    key : str
        OpenAQ archive object key.
    location_id : int
        Expected monitoring-location identifier.
    target_parameter : str
        Parameter to extract.
    target_units : str
        Required measurement units.

    Returns
    -------
    pandas.DataFrame
        Target measurements from one source file.
    """
    dataframe = download_archive_file(key=key)

    return extract_target_measurements(
        dataframe=dataframe,
        key=key,
        expected_location_id=location_id,
        target_parameter=target_parameter,
        target_units=target_units,
    )


# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------


def get_month_output_dir(location_id: int, year: int, month: int) -> Path:
    """Build the output directory for one location-month.

    Parameters
    ----------
    location_id : int
        Monitoring-location identifier.
    year : int
        Calendar year.
    month : int
        Calendar month.

    Returns
    -------
    pathlib.Path
        Partition directory for the location-month.
    """
    return (
        OUTPUT_DIR
        / f"location_id={location_id}"
        / f"year={year}"
        / f"month={month:02d}"
    )


def month_is_complete(location_id: int, year: int, month: int) -> bool:
    """Check whether a location-month has completed output.

    Parameters
    ----------
    location_id : int
        Monitoring-location identifier.
    year : int
        Calendar year.
    month : int
        Calendar month.

    Returns
    -------
    bool
        True when both measurement and metadata output exist.
    """
    output_dir = get_month_output_dir(location_id=location_id, year=year, month=month)

    return (output_dir / "measurements.parquet").exists() and (
        output_dir / "metadata.json"
    ).exists()


# ---------------------------------------------------------------------------
# Monthly processing
# ---------------------------------------------------------------------------


def build_month_metadata(
    measurements: pd.DataFrame,
    location_id: int,
    station_name: str,
    metro: str,
    year: int,
    month: int,
    archive_file_count: int,
) -> dict:
    """Build quality and provenance metadata for one location-month.

    Parameters
    ----------
    measurements : pandas.DataFrame
        Combined target measurements for the month.
    location_id : int
        Monitoring-location identifier.
    station_name : str
        Human-readable monitoring-station name.
    metro : str
        Assigned metropolitan area.
    year : int
        Calendar year.
    month : int
        Calendar month.
    archive_file_count : int
        Number of daily source files available for the month.

    Returns
    -------
    dict
        Monthly download and data-quality metadata.
    """
    expected_days = calendar.monthrange(year, month)[1]

    if measurements.empty:
        return {
            "location_id": location_id,
            "station_name": station_name,
            "metro": metro,
            "year": year,
            "month": month,
            "expected_days": expected_days,
            "archive_file_count": archive_file_count,
            "days_with_target_data": 0,
            "measurement_rows": 0,
            "unique_timestamps": 0,
            "unique_sensors": 0,
            "duplicate_timestamp_rows": 0,
            "duplicate_sensor_timestamp_rows": 0,
            "first_timestamp_utc": None,
            "last_timestamp_utc": None,
        }

    return {
        "location_id": location_id,
        "station_name": station_name,
        "metro": metro,
        "year": year,
        "month": month,
        "expected_days": expected_days,
        "archive_file_count": archive_file_count,
        "days_with_target_data": (measurements["source_date"].nunique()),
        "measurement_rows": len(measurements),
        "unique_timestamps": (measurements["datetime_utc"].nunique()),
        "unique_sensors": (measurements["sensor_id"].nunique()),
        "duplicate_timestamp_rows": (int(measurements["duplicate_timestamp"].sum())),
        "duplicate_sensor_timestamp_rows": int(
            measurements["duplicate_sensor_timestamp"].sum()
        ),
        "first_timestamp_utc": (measurements["datetime_utc"].min().isoformat()),
        "last_timestamp_utc": (measurements["datetime_utc"].max().isoformat()),
    }


def process_month(
    location: pd.Series, year: int, month: int, keys: list[str], force: bool
) -> dict:
    """Download and save one location-month of target measurements.

    Parameters
    ----------
    location : pandas.Series
        Selected monitoring-location metadata.
    year : int
        Calendar year.
    month : int
        Calendar month.
    keys : list[str]
        Daily archive files available for the month.
    force : bool
        Whether existing completed output should be replaced.

    Returns
    -------
    dict
        Monthly download and quality metadata.
    """
    location_id = int(location["location_id"])

    station_name = location["station_name"]
    metro = location["metro"]

    output_dir = get_month_output_dir(location_id=location_id, year=year, month=month)

    parquet_path = output_dir / "measurements.parquet"

    metadata_path = output_dir / "metadata.json"

    if not force and month_is_complete(location_id=location_id, year=year, month=month):
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    frames = []

    with ThreadPoolExecutor(
        max_workers=min(MAX_WORKERS, max(1, len(keys)))
    ) as executor:
        futures = {
            executor.submit(
                download_target_file,
                key=key,
                location_id=location_id,
                target_parameter=TARGET_PARAMETER,
                target_units=TARGET_UNITS,
            ): key
            for key in keys
        }

        for future in as_completed(futures):
            frame = future.result()

            if not frame.empty:
                frames.append(frame)

    if frames:
        measurements = pd.concat(frames, ignore_index=True)

        measurements["station_name"] = station_name

        measurements["metro"] = metro

        measurements = measurements.sort_values(
            ["datetime_utc", "sensor_id"]
        ).reset_index(drop=True)

        measurements["duplicate_timestamp"] = measurements.duplicated(
            subset=["location_id", "datetime_utc"], keep=False
        )

        measurements["duplicate_sensor_timestamp"] = measurements.duplicated(
            subset=["location_id", "sensor_id", "datetime_utc"], keep=False
        )

    else:
        measurements = pd.DataFrame(
            columns=[
                "location_id",
                "sensor_id",
                "source_location_name",
                "datetime_source",
                "datetime_utc",
                "latitude",
                "longitude",
                "parameter",
                "units",
                "value",
                "source_date",
                "source_key",
                "station_name",
                "metro",
                "duplicate_timestamp",
                "duplicate_sensor_timestamp",
            ]
        )

    metadata = build_month_metadata(
        measurements=measurements,
        location_id=location_id,
        station_name=station_name,
        metro=metro,
        year=year,
        month=month,
        archive_file_count=len(keys),
    )

    output_dir.mkdir(parents=True, exist_ok=True)

    temp_parquet_path = output_dir / "measurements.parquet.tmp"

    measurements.to_parquet(
        temp_parquet_path,
        index=False,
    )

    temp_parquet_path.replace(parquet_path)

    temp_metadata_path = output_dir / "metadata.json.tmp"

    temp_metadata_path.write_text(
        json.dumps(
            metadata,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp_metadata_path.replace(metadata_path)

    return metadata


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def save_manifest(rows: list[dict]) -> None:
    """Save monthly download metadata as a manifest.

    Parameters
    ----------
    rows : list[dict]
        Monthly metadata records.

    Returns
    -------
    None
    """
    manifest = pd.DataFrame(rows)

    if manifest.empty:
        return

    manifest = manifest.sort_values(
        ["metro", "station_name", "year", "month"]
    ).reset_index(drop=True)

    MANIFEST_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    manifest.to_csv(MANIFEST_OUTPUT_PATH, index=False)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def download_measurements(locations: pd.DataFrame, force: bool) -> pd.DataFrame:
    """Download target measurements for selected locations.

    Parameters
    ----------
    locations : pandas.DataFrame
        Selected monitoring locations.
    force : bool
        Whether completed location-month outputs should be replaced.

    Returns
    -------
    pandas.DataFrame
        Monthly download manifest.
    """
    years = tuple(range(ANALYSIS_START_DATE.year, ANALYSIS_END_DATE.year + 1))

    manifest_rows = []

    total_locations = len(locations)

    for position, (_, location) in enumerate(locations.iterrows(), start=1):
        location_id = int(location["location_id"])

        print()
        print(
            f"[{position}/{total_locations}] "
            f"{location['metro']} | "
            f"{location['station_name']} "
            f"(location {location_id})"
        )

        archive_keys = load_archive_keys(location_id=location_id, years=years)

        archive_keys = filter_archive_keys(
            keys=archive_keys,
            start_date=ANALYSIS_START_DATE,
            end_date=ANALYSIS_END_DATE,
        )

        monthly_keys = group_keys_by_month(archive_keys)

        for month_start in pd.date_range(
            start=ANALYSIS_START_DATE, end=ANALYSIS_END_DATE, freq="MS"
        ):
            year = month_start.year
            month = month_start.month

            keys = monthly_keys.get((year, month), [])

            print(f"    {year}-{month:02d}: " f"{len(keys):>2} archive files", end="")

            was_complete = not force and month_is_complete(
                location_id=location_id, year=year, month=month
            )

            metadata = process_month(
                location=location, year=year, month=month, keys=keys, force=force
            )

            status = "cached" if was_complete else "downloaded"

            print(f" | {status} | " f"{metadata['measurement_rows']:,} rows")

            manifest_rows.append(metadata)

            # Preserve progress after every completed month.
            save_manifest(manifest_rows)

    return pd.DataFrame(manifest_rows)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_summary(manifest: pd.DataFrame) -> None:
    """Print a summary of the downloaded measurement dataset.

    Parameters
    ----------
    manifest : pandas.DataFrame
        Monthly download manifest.

    Returns
    -------
    None
    """
    if manifest.empty:
        print("No measurement data processed.")
        return

    print()
    print("Measurement download complete")
    print("-----------------------------")

    print(f"Locations: {manifest['location_id'].nunique():,}")

    print(f"Location-months: {len(manifest):,}")

    print("15-minute measurement rows: " f"{manifest['measurement_rows'].sum():,}")

    print(
        "Duplicate timestamp rows: " f"{manifest['duplicate_timestamp_rows'].sum():,}"
    )

    print(
        "Duplicate sensor/timestamp rows: "
        f"{manifest['duplicate_sensor_timestamp_rows'].sum():,}"
    )

    print()
    print("Measurements by metro:")

    metro_summary = (
        manifest.groupby("metro")
        .agg(
            locations=(
                "location_id",
                "nunique",
            ),
            measurement_rows=(
                "measurement_rows",
                "sum",
            ),
            days_with_data=(
                "days_with_target_data",
                "sum",
            ),
        )
        .reset_index()
    )

    print(metro_summary.to_string(index=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Download the target measurement dataset."""
    args = parse_args()

    locations = pd.read_csv(SELECTED_LOCATIONS_PATH)

    if args.location_id is not None:
        locations = locations[locations["location_id"] == args.location_id].copy()

        if locations.empty:
            raise ValueError(
                "Requested location ID is not present "
                "in the selected station inventory."
            )

    manifest = download_measurements(
        locations=locations,
        force=args.force,
    )

    print_summary(manifest)


if __name__ == "__main__":
    main()
