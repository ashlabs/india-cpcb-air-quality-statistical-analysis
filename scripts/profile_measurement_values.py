"""Profile PM2.5 measurement values for the primary analysis cohort."""

import json
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATIONS_INPUT_PATH = Path("data/processed/analysis_stations.csv")
MEASUREMENTS_DIR = Path("data/processed/measurements_15min")
STATION_OUTPUT_PATH = Path("data/processed/station_value_profile.csv")
METRO_OUTPUT_PATH = Path("data/processed/metro_value_profile.csv")
SUMMARY_OUTPUT_PATH = Path("data/processed/measurement_value_profile.json")
EXTREME_VALUES_OUTPUT_PATH = Path("data/processed/extreme_measurements.csv")

LOW_VALUE_THRESHOLD = 1.0
HIGH_VALUE_THRESHOLD = 500.0
VERY_HIGH_VALUE_THRESHOLD = 1000.0

EXTREME_RECORD_COUNT = 25

READ_COLUMNS = [
    "location_id",
    "sensor_id",
    "station_name",
    "metro",
    "datetime_source",
    "datetime_utc",
    "value"
]


# ---------------------------------------------------------------------------
# Measurement loading
# ---------------------------------------------------------------------------


def get_primary_location_ids(
    stations: pd.DataFrame
) -> set[int]:
    """Extract location IDs belonging to the primary analysis cohort.

    Parameters
    ----------
    stations : pandas.DataFrame
        Station inventory containing primary eligibility flags.

    Returns
    -------
    set[int]
        Location IDs included in the primary analysis cohort.
    """
    eligible = stations[stations["primary_eligible"]]

    return set(
        eligible["location_id"]
        .astype(int)
        .tolist()
    )


def get_measurement_files(
    measurement_dir: Path,
    location_ids: set[int]
) -> list[Path]:
    """Find monthly measurement files for eligible locations.

    Parameters
    ----------
    measurement_dir : pathlib.Path
        Root directory containing partitioned measurement Parquet files.
    location_ids : set[int]
        Eligible monitoring-location identifiers.

    Returns
    -------
    list[pathlib.Path]
        Sorted Parquet files belonging to eligible locations.
    """
    files = []

    for location_id in sorted(location_ids):
        location_dir = (measurement_dir / f"location_id={location_id}")

        if not location_dir.exists():
            raise FileNotFoundError(
                f"Missing measurement directory: "
                f"{location_dir}"
            )

        location_files = sorted(
            location_dir.glob("year=*/month=*/measurements.parquet")
        )

        if not location_files:
            raise FileNotFoundError(
                "No measurement files found for "
                f"location {location_id}."
            )

        files.extend(location_files)

    return files


def load_measurements(
    files: list[Path]
) -> pd.DataFrame:
    """Load the required columns from monthly measurement files.

    Parameters
    ----------
    files : list[pathlib.Path]
        Monthly Parquet measurement files.

    Returns
    -------
    pandas.DataFrame
        Combined measurement dataset containing only columns required
        for value-quality profiling.
    """
    frames = []

    total_files = len(files)

    for position, path in enumerate(files, start=1):
        if position == 1 or position % 100 == 0:
            print(
                f"Reading file "
                f"{position:,}/{total_files:,}..."
            )

        frame = pd.read_parquet(path, columns=READ_COLUMNS)

        frames.append(frame)

    if not frames:
        return pd.DataFrame(columns=READ_COLUMNS)

    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Value profiling
# ---------------------------------------------------------------------------


def build_value_profile(
    measurements: pd.DataFrame,
    group_columns: list[str]
) -> pd.DataFrame:
    """Create measurement-quality statistics for grouped observations.

    Parameters
    ----------
    measurements : pandas.DataFrame
        PM2.5 measurement observations.
    group_columns : list[str]
        Columns defining the grouping level, such as station or metro.

    Returns
    -------
    pandas.DataFrame
        Measurement counts, value-quality indicators, and distribution
        statistics for each group.
    """
    rows = []

    grouped = measurements.groupby(group_columns, dropna=False)

    for group_key, group in grouped:
        if not isinstance(group_key, tuple):
            group_key = (group_key,)

        values = pd.to_numeric(group["value"], errors="coerce")

        valid_values = values.dropna()

        row = dict(zip(group_columns, group_key, strict=True))

        row.update(
            {
                "measurement_rows": len(group),
                "missing_value_count": int(
                    values.isna().sum()
                ),
                "negative_value_count": int(
                    (valid_values < 0).sum()
                ),
                "zero_value_count": int(
                    (valid_values == 0).sum()
                ),
                "low_positive_value_count": int(
                    (
                        (valid_values > 0)
                        & (valid_values <= LOW_VALUE_THRESHOLD)
                    ).sum()
                ),
                "high_value_count": int(
                    (valid_values >= HIGH_VALUE_THRESHOLD).sum()
                ),
                "very_high_value_count": int(
                    (valid_values >= VERY_HIGH_VALUE_THRESHOLD).sum()
                ),
                "minimum": valid_values.min(),
                "p01": valid_values.quantile(0.01),
                "p05": valid_values.quantile(0.05),
                "p25": valid_values.quantile(0.25),
                "median": valid_values.median(),
                "mean": valid_values.mean(),
                "p75": valid_values.quantile(0.75),
                "p95": valid_values.quantile(0.95),
                "p99": valid_values.quantile(0.99),
                "maximum": valid_values.max(),
                "standard_deviation": (valid_values.std()),
                "unique_sensors": (group["sensor_id"].nunique())
            }
        )

        rows.append(row)

    return pd.DataFrame(rows)


def add_rate_columns(
    profile: pd.DataFrame
) -> pd.DataFrame:
    """Add percentage columns for measurement-quality indicators.

    Parameters
    ----------
    profile : pandas.DataFrame
        Group-level measurement profile.

    Returns
    -------
    pandas.DataFrame
        Profile containing measurement-quality percentages.
    """
    result = profile.copy()

    denominator = result["measurement_rows"]

    count_columns = [
        "missing_value_count",
        "negative_value_count",
        "zero_value_count",
        "low_positive_value_count",
        "high_value_count",
        "very_high_value_count"
    ]

    for column in count_columns:
        pct_column = column.replace("_count", "_pct")

        result[pct_column] = result[column] / denominator * 100

    return result


# ---------------------------------------------------------------------------
# Extreme-value inspection
# ---------------------------------------------------------------------------


def build_extreme_measurements(
    measurements: pd.DataFrame,
    record_count: int
) -> pd.DataFrame:
    """Create a small dataset containing the lowest and highest values.

    Parameters
    ----------
    measurements : pandas.DataFrame
        PM2.5 measurement observations.
    record_count : int
        Number of low and high observations to retain.

    Returns
    -------
    pandas.DataFrame
        Lowest and highest PM2.5 observations with provenance fields.
    """
    valid = measurements[measurements["value"].notna()].copy()

    lowest = valid.nsmallest(record_count, "value").copy()

    lowest["extreme_type"] = "lowest"

    highest = valid.nlargest(record_count, "value").copy()

    highest["extreme_type"] = "highest"

    return pd.concat([lowest, highest], ignore_index=True)


# ---------------------------------------------------------------------------
# Overall summary
# ---------------------------------------------------------------------------


def build_overall_summary(
    measurements: pd.DataFrame
) -> dict[str, int | float | None]:
    """Build overall PM2.5 value-quality summary statistics.

    Parameters
    ----------
    measurements : pandas.DataFrame
        PM2.5 measurements for the primary analysis cohort.

    Returns
    -------
    dict[str, int | float | None]
        Overall measurement counts and distribution statistics.
    """
    values = pd.to_numeric(measurements["value"], errors="coerce")

    valid_values = values.dropna()

    return {
        "locations": int(measurements["location_id"].nunique()),
        "metros": int(measurements["metro"].nunique()),
        "measurement_rows": int(len(measurements)),
        "missing_value_count": int(values.isna().sum()),
        "negative_value_count": int((valid_values < 0).sum()),
        "zero_value_count": int((valid_values == 0).sum()),
        "low_positive_value_count": int(
            (
                (valid_values > 0)
                & (valid_values <= LOW_VALUE_THRESHOLD)
            ).sum()
        ),
        "high_value_count": int(
            (valid_values >= HIGH_VALUE_THRESHOLD).sum()
        ),
        "very_high_value_count": int(
            (valid_values >= VERY_HIGH_VALUE_THRESHOLD).sum()
        ),
        "minimum": (
            float(valid_values.min())
            if not valid_values.empty
            else None
        ),
        "p01": (
            float(valid_values.quantile(0.01))
            if not valid_values.empty
            else None
        ),
        "median": (
            float(valid_values.median())
            if not valid_values.empty
            else None
        ),
        "mean": (
            float(valid_values.mean())
            if not valid_values.empty
            else None
        ),
        "p99": (
            float(valid_values.quantile(0.99))
            if not valid_values.empty
            else None
        ),
        "maximum": (
            float(valid_values.max())
            if not valid_values.empty
            else None
        )
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_summary(
    overall: dict[str, int | float | None],
    station_profile: pd.DataFrame,
    metro_profile: pd.DataFrame
) -> None:
    """Print the main PM2.5 value-quality findings.

    Parameters
    ----------
    overall : dict[str, int | float | None]
        Overall measurement-value summary.
    station_profile : pandas.DataFrame
        Station-level value profile.
    metro_profile : pandas.DataFrame
        Metro-level value profile.

    Returns
    -------
    None
    """
    print()
    print("PM2.5 measurement value profile")
    print("-------------------------------")

    print(f"Locations: {overall['locations']:,}")

    print(f"Metros: {overall['metros']:,}")

    print(f"Measurement rows: {overall['measurement_rows']:,}")

    print()
    print("Potential value-quality issues:")

    print(f"Missing values: {overall['missing_value_count']:,}")

    print(f"Negative values: {overall['negative_value_count']:,}")

    print(f"Zero values: {overall['zero_value_count']:,}")

    print(
        f"Positive values <= "
        f"{LOW_VALUE_THRESHOLD:g}: "
        f"{overall['low_positive_value_count']:,}"
    )

    print(
        f"Values >= "
        f"{HIGH_VALUE_THRESHOLD:g}: "
        f"{overall['high_value_count']:,}"
    )

    print(
        f"Values >= "
        f"{VERY_HIGH_VALUE_THRESHOLD:g}: "
        f"{overall['very_high_value_count']:,}"
    )

    print()
    print("Overall distribution:")

    for key in [
        "minimum",
        "p01",
        "median",
        "mean",
        "p99",
        "maximum"
    ]:
        print(f"{key:>8}: {overall[key]}")

    print()
    print("Value distribution by metro:")

    columns = [
        "metro",
        "measurement_rows",
        "negative_value_count",
        "zero_value_count",
        "median",
        "mean",
        "p95",
        "p99",
        "maximum"
    ]

    print(
        metro_profile[columns]
        .sort_values("metro")
        .to_string(index=False)
    )

    print()
    print("Stations with the most zero values:")

    zero_stations = (
        station_profile
        .sort_values(
            ["zero_value_count", "zero_value_pct"],
            ascending=False
        )
        .head(15)
    )

    print(
        zero_stations[
            [
                "location_id",
                "station_name",
                "metro",
                "measurement_rows",
                "zero_value_count",
                "zero_value_pct",
                "minimum",
                "median"
            ]
        ].to_string(index=False)
    )

    print()
    print("Stations with highest observed values:")

    high_stations = (
        station_profile
        .sort_values("maximum", ascending=False,)
        .head(15)
    )

    print(
        high_stations[
            [
                "location_id",
                "station_name",
                "metro",
                "measurement_rows",
                "p99",
                "maximum"
            ]
        ].to_string(index=False)
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Profile PM2.5 values for the primary analysis cohort."""
    stations = pd.read_csv(STATIONS_INPUT_PATH)

    location_ids = get_primary_location_ids(stations=stations)

    print(f"Primary cohort locations: {len(location_ids):,}")

    files = get_measurement_files(
        measurement_dir=MEASUREMENTS_DIR,
        location_ids=location_ids
    )

    print(f"Monthly Parquet files: {len(files):,}")

    measurements = load_measurements(files=files)

    overall = build_overall_summary(measurements=measurements)

    station_profile = build_value_profile(
        measurements=measurements,
        group_columns=["location_id", "station_name", "metro"]
    )

    station_profile = add_rate_columns(profile=station_profile)

    metro_profile = build_value_profile(
        measurements=measurements,
        group_columns=["metro"]
    )

    metro_profile = add_rate_columns(profile=metro_profile)

    extreme_measurements = (
        build_extreme_measurements(
            measurements=measurements,
            record_count=EXTREME_RECORD_COUNT
        )
    )

    STATION_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    station_profile.to_csv(STATION_OUTPUT_PATH, index=False)

    metro_profile.to_csv(METRO_OUTPUT_PATH, index=False)

    EXTREME_VALUES_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    extreme_measurements.to_csv(EXTREME_VALUES_OUTPUT_PATH, index=False)

    SUMMARY_OUTPUT_PATH.write_text(
        json.dumps(overall, indent=2),
        encoding="utf-8"
    )

    print_summary(
        overall=overall,
        station_profile=station_profile,
        metro_profile=metro_profile
    )


if __name__ == "__main__":
    main()
