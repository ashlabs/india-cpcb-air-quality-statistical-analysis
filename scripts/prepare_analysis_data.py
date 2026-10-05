"""Prepare cleaned hourly and daily PM2.5 datasets for statistical analysis."""

import json
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATIONS_INPUT_PATH = Path(
    "data/processed/analysis_stations.csv"
)

MEASUREMENTS_DIR = Path(
    "data/processed/measurements_15min"
)

CLEAN_OUTPUT_PATH = Path(
    "data/processed/analysis_measurements_15min.parquet"
)

HOURLY_OUTPUT_PATH = Path(
    "data/processed/analysis_hourly.parquet"
)

DAILY_OUTPUT_PATH = Path(
    "data/processed/analysis_daily_station.parquet"
)

SUMMARY_OUTPUT_PATH = Path(
    "data/processed/preprocessing_summary.json"
)

LOCAL_TIMEZONE = "Asia/Kolkata"

UPPER_VALID_VALUE = 1000.0

EXPECTED_MEASUREMENTS_PER_HOUR = 4
MINIMUM_MEASUREMENTS_PER_HOUR = 3

EXPECTED_HOURS_PER_DAY = 24
MINIMUM_HOURS_PER_DAY = 18

READ_COLUMNS = [
    "location_id",
    "sensor_id",
    "station_name",
    "metro",
    "datetime_utc",
    "value",
]


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def get_primary_stations(
    stations: pd.DataFrame,
) -> pd.DataFrame:
    """Return monitoring stations in the primary analysis cohort.

    Parameters
    ----------
    stations : pandas.DataFrame
        Station inventory containing analysis eligibility flags.

    Returns
    -------
    pandas.DataFrame
        Primary-cohort station metadata.
    """
    return (
        stations[
            stations["primary_eligible"]
        ][
            [
                "location_id",
                "sensitivity_eligible",
            ]
        ]
        .copy()
        .reset_index(drop=True)
    )


def get_measurement_files(
    location_ids: set[int],
) -> list[Path]:
    """Find measurement files for the requested monitoring locations.

    Parameters
    ----------
    location_ids : set[int]
        Monitoring-location identifiers.

    Returns
    -------
    list[pathlib.Path]
        Sorted monthly measurement Parquet files.

    Raises
    ------
    FileNotFoundError
        If no measurement files exist for a requested location.
    """
    files: list[Path] = []

    for location_id in sorted(location_ids):
        location_dir = (
            MEASUREMENTS_DIR
            / f"location_id={location_id}"
        )

        location_files = sorted(
            location_dir.glob(
                "year=*/month=*/measurements.parquet"
            )
        )

        if not location_files:
            raise FileNotFoundError(
                "No measurement files found for "
                f"location {location_id}."
            )

        files.extend(location_files)

    return files


def load_measurements(
    files: list[Path],
) -> pd.DataFrame:
    """Load PM2.5 observations needed for preprocessing.

    Parameters
    ----------
    files : list[pathlib.Path]
        Monthly measurement Parquet files.

    Returns
    -------
    pandas.DataFrame
        Combined PM2.5 observations.
    """
    frames: list[pd.DataFrame] = []

    total_files = len(files)

    for position, path in enumerate(
        files,
        start=1,
    ):
        if position == 1 or position % 100 == 0:
            print(
                f"Reading file "
                f"{position:,}/{total_files:,}..."
            )

        frames.append(
            pd.read_parquet(
                path,
                columns=READ_COLUMNS,
            )
        )

    return pd.concat(
        frames,
        ignore_index=True,
    )


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


def clean_measurements(
    measurements: pd.DataFrame,
    stations: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, int | float]]:
    """Apply conservative PM2.5 validity rules.

    Parameters
    ----------
    measurements : pandas.DataFrame
        Raw PM2.5 measurements for the primary cohort.
    stations : pandas.DataFrame
        Primary-cohort station metadata.

    Returns
    -------
    tuple[pandas.DataFrame, dict[str, int | float]]
        Cleaned 15-minute observations and cleaning summary statistics.
    """
    data = measurements.copy()

    data["value"] = pd.to_numeric(
        data["value"],
        errors="coerce",
    )

    data["datetime_utc"] = pd.to_datetime(
        data["datetime_utc"],
        errors="coerce",
        utc=True,
    )

    missing_value = data["value"].isna()
    missing_datetime = data["datetime_utc"].isna()
    negative_value = data["value"] < 0
    extreme_value = data["value"] >= UPPER_VALID_VALUE

    invalid = (
        missing_value
        | missing_datetime
        | negative_value
        | extreme_value
    )

    summary: dict[str, int | float] = {
        "raw_measurement_rows": len(data),
        "missing_value_rows": int(
            missing_value.sum()
        ),
        "missing_datetime_rows": int(
            missing_datetime.sum()
        ),
        "negative_value_rows": int(
            negative_value.sum()
        ),
        "upper_extreme_rows": int(
            extreme_value.sum()
        ),
        "zero_value_rows_retained": int(
            (data["value"] == 0).sum()
        ),
        "removed_rows": int(
            invalid.sum()
        ),
    }

    summary["removed_pct"] = (
        summary["removed_rows"]
        / summary["raw_measurement_rows"]
        * 100
    )

    clean = data[
        ~invalid
    ].copy()

    clean = clean.merge(
        stations,
        on="location_id",
        how="left",
        validate="many_to_one",
    )

    if clean["sensitivity_eligible"].isna().any():
        raise ValueError(
            "Measurement rows could not be matched "
            "to station eligibility metadata."
        )

    duplicate_count = clean.duplicated(
        subset=[
            "location_id",
            "datetime_utc",
        ]
    ).sum()

    if duplicate_count:
        raise ValueError(
            "Duplicate station timestamps found "
            "after cleaning."
        )

    clean["datetime_local"] = (
        clean["datetime_utc"]
        .dt.tz_convert(
            LOCAL_TIMEZONE
        )
    )

    clean = clean.sort_values(
        [
            "location_id",
            "datetime_utc",
        ]
    ).reset_index(drop=True)

    summary["valid_measurement_rows"] = len(
        clean
    )

    return clean, summary


# ---------------------------------------------------------------------------
# Hourly aggregation
# ---------------------------------------------------------------------------


def build_hourly_data(
    measurements: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    """Aggregate valid 15-minute observations to station-hour values.

    Parameters
    ----------
    measurements : pandas.DataFrame
        Cleaned 15-minute PM2.5 observations.

    Returns
    -------
    tuple[pandas.DataFrame, int]
        Eligible hourly observations and number of incomplete hours removed.
    """
    data = measurements.copy()

    data["hour_local"] = (
        data["datetime_local"]
        .dt.floor("h")
    )

    hourly = (
        data.groupby(
            [
                "location_id",
                "station_name",
                "metro",
                "sensitivity_eligible",
                "hour_local",
            ],
            as_index=False,
        )
        .agg(
            pm25_mean=(
                "value",
                "mean",
            ),
            pm25_median=(
                "value",
                "median",
            ),
            valid_15min_count=(
                "value",
                "size",
            ),
        )
    )

    hourly["coverage_pct"] = (
        hourly["valid_15min_count"]
        / EXPECTED_MEASUREMENTS_PER_HOUR
        * 100
    )

    incomplete_count = int(
        (
            hourly["valid_15min_count"]
            < MINIMUM_MEASUREMENTS_PER_HOUR
        ).sum()
    )

    hourly = hourly[
        hourly["valid_15min_count"]
        >= MINIMUM_MEASUREMENTS_PER_HOUR
    ].copy()

    return (
        hourly.reset_index(drop=True),
        incomplete_count,
    )


# ---------------------------------------------------------------------------
# Daily aggregation
# ---------------------------------------------------------------------------


def get_season(
    month: int,
) -> str:
    """Return the IMD meteorological season for a month.

    Parameters
    ----------
    month : int
        Calendar month from 1 through 12.

    Returns
    -------
    str
        Meteorological season name.
    """
    if month in (1, 2):
        return "Winter"

    if month in (3, 4, 5):
        return "Pre-monsoon"

    if month in (6, 7, 8, 9):
        return "Southwest monsoon"

    return "Post-monsoon"


def build_daily_data(
    hourly: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    """Aggregate station-hour observations to station-day values.

    Parameters
    ----------
    hourly : pandas.DataFrame
        Valid station-hour PM2.5 observations.

    Returns
    -------
    tuple[pandas.DataFrame, int]
        Eligible station-day observations and number of incomplete
        station-days removed.
    """
    data = hourly.copy()

    data["date_local"] = (
        data["hour_local"].dt.date
    )

    daily = (
        data.groupby(
            [
                "location_id",
                "station_name",
                "metro",
                "sensitivity_eligible",
                "date_local",
            ],
            as_index=False,
        )
        .agg(
            pm25_mean=(
                "pm25_mean",
                "mean",
            ),
            pm25_median=(
                "pm25_mean",
                "median",
            ),
            valid_hour_count=(
                "hour_local",
                "size",
            ),
            valid_15min_count=(
                "valid_15min_count",
                "sum",
            ),
        )
    )

    daily["hour_coverage_pct"] = (
        daily["valid_hour_count"]
        / EXPECTED_HOURS_PER_DAY
        * 100
    )

    incomplete_count = int(
        (
            daily["valid_hour_count"]
            < MINIMUM_HOURS_PER_DAY
        ).sum()
    )

    daily = daily[
        daily["valid_hour_count"]
        >= MINIMUM_HOURS_PER_DAY
    ].copy()

    daily["date_local"] = pd.to_datetime(
        daily["date_local"]
    )

    daily["year"] = (
        daily["date_local"].dt.year
    )

    daily["month"] = (
        daily["date_local"].dt.month
    )

    daily["month_name"] = (
        daily["date_local"]
        .dt.month_name()
    )

    daily["season"] = (
        daily["month"]
        .apply(get_season)
    )

    daily["day_of_week"] = (
        daily["date_local"]
        .dt.day_name()
    )

    daily["is_weekend"] = (
        daily["date_local"]
        .dt.dayofweek
        .isin([5, 6])
    )

    return (
        daily.reset_index(drop=True),
        incomplete_count,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_summary(
    summary: dict[str, int | float],
) -> None:
    """Print preprocessing results.

    Parameters
    ----------
    summary : dict[str, int | float]
        Cleaning and aggregation summary statistics.

    Returns
    -------
    None
    """
    print()
    print("Analysis-data preparation complete")
    print("----------------------------------")

    for key, value in summary.items():
        if isinstance(value, float):
            print(
                f"{key}: {value:.4f}"
            )
        else:
            print(
                f"{key}: {value:,}"
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Prepare cleaned hourly and daily PM2.5 analysis datasets."""
    stations = pd.read_csv(
        STATIONS_INPUT_PATH
    )

    primary_stations = get_primary_stations(
        stations=stations
    )

    location_ids = set(
        primary_stations[
            "location_id"
        ]
        .astype(int)
        .tolist()
    )

    print(
        f"Primary cohort locations: "
        f"{len(location_ids):,}"
    )

    files = get_measurement_files(
        location_ids=location_ids
    )

    print(
        f"Monthly Parquet files: "
        f"{len(files):,}"
    )

    measurements = load_measurements(
        files=files
    )

    clean, summary = clean_measurements(
        measurements=measurements,
        stations=primary_stations,
    )

    hourly, incomplete_hours = (
        build_hourly_data(
            measurements=clean
        )
    )

    daily, incomplete_days = (
        build_daily_data(
            hourly=hourly
        )
    )

    summary[
        "hourly_rows"
    ] = len(hourly)

    summary[
        "incomplete_hours_removed"
    ] = incomplete_hours

    summary[
        "daily_station_rows"
    ] = len(daily)

    summary[
        "incomplete_station_days_removed"
    ] = incomplete_days

    CLEAN_OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    clean.to_parquet(
        CLEAN_OUTPUT_PATH,
        index=False,
    )

    hourly.to_parquet(
        HOURLY_OUTPUT_PATH,
        index=False,
    )

    daily.to_parquet(
        DAILY_OUTPUT_PATH,
        index=False,
    )

    SUMMARY_OUTPUT_PATH.write_text(
        json.dumps(
            summary,
            indent=2,
        ),
        encoding="utf-8",
    )

    print_summary(
        summary=summary
    )


if __name__ == "__main__":
    main()