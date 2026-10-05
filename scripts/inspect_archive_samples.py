"""Inspect sample OpenAQ archive files for selected monitoring locations."""

import gzip
import io
import json
from pathlib import Path

import pandas as pd
import requests

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

ARCHIVE_OBJECT_BASE_URL = "https://openaq-data-archive.s3.amazonaws.com"
CACHE_DIR = Path("data/raw/archive_inventory")

SAMPLE_LOCATION_IDS = [
    5586,  # Delhi
    6973,  # Bengaluru
    11611,  # Mumbai
]

TARGET_PARAMETER = "pm25"
TARGET_YEARS = (2025, 2026)
SAMPLES_PER_LOCATION = 3

REQUEST_TIMEOUT_SECONDS = 30


# ----------------------------------------------------------------------------
# Archive inventory
# ----------------------------------------------------------------------------


def load_archive_keys(location_id: int, years: tuple[int, ...]) -> list[str]:
    """Load cached archive keys for a monitoring location.

    Parameters
    ----------
    location_id : int
        OpenAQ monitoring-location identifier.
    years : tuple[int, ...]
        Calendar years whose cached listings should be loaded.

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
        cache_path = CACHE_DIR / f"location_{location_id}_year_{year}.json"

        if not cache_path.exists():
            raise FileNotFoundError(f"Missing archive cache: {cache_path}")

        year_keys = json.loads(cache_path.read_text(encoding="utf-8"))

        keys.extend(year_keys)

    return sorted(keys)


def select_sample_keys(keys: list[str], sample_count: int) -> list[str]:
    """Select archive files spanning the available time range.

    Parameters
    ----------
    keys : list[str]
        Sorted archive object keys.
    sample_count : int
        Number of files to sample.

    Returns
    -------
    list[str]
        Selected archive object keys.
    """
    if not keys:
        return []

    if len(keys) <= sample_count:
        return keys

    indexes = [
        round(index * (len(keys) - 1) / (sample_count - 1))
        for index in range(sample_count)
    ]

    return [keys[index] for index in indexes]


# ----------------------------------------------------------------------------
# Archive download
# ----------------------------------------------------------------------------


def download_archive_file(session: requests.Session, key: str) -> pd.DataFrame:
    """Download and read one compressed OpenAQ archive CSV.

    Parameters
    ----------
    session : requests.Session
        Reusable HTTP session.
    key : str
        S3 object key for the archive file.

    Returns
    -------
    pandas.DataFrame
        Data contained in the compressed archive file.

    Raises
    ------
    requests.HTTPError
        If the archive file cannot be downloaded successfully.
    """
    url = f"{ARCHIVE_OBJECT_BASE_URL}/{key}"

    response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)

    response.raise_for_status()

    decompressed = gzip.decompress(response.content)

    return pd.read_csv(io.BytesIO(decompressed))


# ----------------------------------------------------------------------------
# Inspection
# ----------------------------------------------------------------------------


def inspect_file(
    dataframe: pd.DataFrame,
    key: str,
    target_parameter: str
) -> None:
    """Print structural and target-parameter details for an archive file.

    Parameters
    ----------
    dataframe : pandas.DataFrame
        OpenAQ archive data loaded from one compressed CSV file.
    key : str
        Archive object key identifying the source file.
    target_parameter : str
        Parameter to inspect, such as "pm25".

    Returns
    -------
    None
    """
    print()
    print("-" * 80)
    print(key)
    print("-" * 80)

    print(f"Rows: {len(dataframe):,}")
    print(f"Columns: {len(dataframe.columns):,}")

    print()
    print("Columns:")
    print(list(dataframe.columns))

    if "parameter" not in dataframe.columns:
        print()
        print("No 'parameter' column found.")
        return

    print()
    print("Parameter counts:")
    print(dataframe["parameter"].value_counts(dropna=False).to_string())

    target = dataframe[dataframe["parameter"] == target_parameter].copy()

    print()
    print(f"{target_parameter} rows: " f"{len(target):,}")

    if target.empty:
        return

    if "units" in target.columns:
        print(
            f"{target_parameter} units: "
            f"{sorted(target['units'].dropna().unique())}"
        )

    if "sensors_id" in target.columns:
        print()
        print(f"{target_parameter} observations by sensor:")

        sensor_summary = (
            target.groupby("sensors_id")
            .agg(
                rows=("value", "size"),
                first_timestamp=("datetime", "min"),
                last_timestamp=("datetime", "max"),
            )
            .reset_index()
        )

        print(sensor_summary.to_string(index=False))

    if "datetime" not in target.columns:
        print()
        print("No 'datetime' column found.")
        return

    timestamps = pd.to_datetime(
        target["datetime"],
        errors="coerce",
        utc=True,
    )

    print()
    print(f"First timestamp: " f"{timestamps.min()}")

    print(f"Last timestamp: " f"{timestamps.max()}")

    print(f"Unique timestamps: " f"{timestamps.nunique():,}")

    print("Duplicate timestamps: " f"{timestamps.duplicated().sum():,}")

    if "sensors_id" in target.columns:
        sensor_timestamp_duplicates = target.duplicated(
            subset=[
                "sensors_id",
                "datetime",
            ]
        ).sum()

        print(
            "Duplicate sensor/timestamp pairs: "
            f"{sensor_timestamp_duplicates:,}"
        )

    intervals = (
        timestamps.dropna()
        .sort_values()
        .drop_duplicates()
        .diff()
        .dropna()
        .value_counts()
        .sort_index()
    )

    print()
    print("Timestamp interval counts:")

    if intervals.empty:
        print("No timestamp intervals available.")
    else:
        print(intervals.to_string())

    if "value" in target.columns:
        print()
        print(f"{target_parameter} value summary:")

        print(target["value"].describe().to_string())


def main() -> None:
    """Inspect archive samples for selected monitoring locations."""
    session = requests.Session()

    for location_id in SAMPLE_LOCATION_IDS:
        print()
        print("=" * 80)
        print(f"LOCATION {location_id}")
        print("=" * 80)

        keys = load_archive_keys(location_id=location_id, years=TARGET_YEARS)

        print(f"Available daily files: {len(keys):,}")

        sample_keys = select_sample_keys(
            keys=keys,
            sample_count=SAMPLES_PER_LOCATION
        )

        for key in sample_keys:
            dataframe = download_archive_file(session=session, key=key)

            inspect_file(
                dataframe=dataframe,
                key=key,
                target_parameter=TARGET_PARAMETER,
            )


if __name__ == "__main__":
    main()
