"""Inspect anomalous PM2.5 values in the primary analysis cohort."""

from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATIONS_INPUT_PATH = Path("data/processed/analysis_stations.csv")
MEASUREMENTS_DIR = Path("data/processed/measurements_15min")
VALUE_FREQUENCY_OUTPUT_PATH = Path(
    "data/processed/anomaly_value_frequencies.csv"
)
SENSOR_SUMMARY_OUTPUT_PATH = Path("data/processed/anomaly_sensor_summary.csv")
STATION_SUMMARY_OUTPUT_PATH = Path(
    "data/processed/anomaly_station_summary.csv"
)
SAMPLE_OUTPUT_PATH = Path("data/processed/anomaly_measurement_samples.csv")

LOW_VALUE_THRESHOLD = 1.0
HIGH_VALUE_THRESHOLD = 500.0
EXTREME_VALUE_THRESHOLD = 1000.0

SAMPLES_PER_CATEGORY = 50

READ_COLUMNS = [
    "location_id",
    "sensor_id",
    "station_name",
    "metro",
    "datetime_source",
    "datetime_utc",
    "value",
]


# ---------------------------------------------------------------------------
# Cohort and file discovery
# ---------------------------------------------------------------------------


def get_primary_location_ids(stations: pd.DataFrame) -> set[int]:
    """Extract location IDs in the primary analysis cohort.

    Parameters
    ----------
    stations : pandas.DataFrame
        Station inventory containing primary eligibility flags.

    Returns
    -------
    set[int]
        Location IDs belonging to the primary cohort.
    """
    eligible = stations[stations["primary_eligible"]]

    return set(eligible["location_id"].astype(int).tolist())


def get_measurement_files(
    measurement_dir: Path,
    location_ids: set[int]
) -> list[Path]:
    """Find monthly measurement files for eligible locations.

    Parameters
    ----------
    measurement_dir : pathlib.Path
        Root directory containing partitioned Parquet files.
    location_ids : set[int]
        Eligible monitoring-location identifiers.

    Returns
    -------
    list[pathlib.Path]
        Sorted monthly Parquet files.

    Raises
    ------
    FileNotFoundError
        If an expected location directory or measurement file is missing.
    """
    files = []

    for location_id in sorted(location_ids):
        location_dir = measurement_dir / f"location_id={location_id}"

        if not location_dir.exists():
            raise FileNotFoundError(
                f"Missing measurement directory: " f"{location_dir}"
            )

        location_files = sorted(
            location_dir.glob("year=*/month=*/measurements.parquet")
        )

        if not location_files:
            raise FileNotFoundError(
                "No measurement files found for " f"location {location_id}."
            )

        files.extend(location_files)

    return files


# ---------------------------------------------------------------------------
# Measurement loading
# ---------------------------------------------------------------------------


def load_measurements(files: list[Path]) -> pd.DataFrame:
    """Load measurement columns required for anomaly analysis.

    Parameters
    ----------
    files : list[pathlib.Path]
        Monthly Parquet files to read.

    Returns
    -------
    pandas.DataFrame
        Combined PM2.5 measurement observations.
    """
    frames = []

    total_files = len(files)

    for position, path in enumerate(files, start=1):
        if position == 1 or position % 100 == 0:
            print(f"Reading file {position:,}/{total_files:,}...")

        frame = pd.read_parquet(path, columns=READ_COLUMNS)

        frames.append(frame)

    if not frames:
        return pd.DataFrame(columns=READ_COLUMNS)

    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Anomaly classification
# ---------------------------------------------------------------------------


def classify_measurements(measurements: pd.DataFrame) -> pd.DataFrame:
    """Classify PM2.5 observations into diagnostic value categories.

    Parameters
    ----------
    measurements : pandas.DataFrame
        PM2.5 measurement observations.

    Returns
    -------
    pandas.DataFrame
        Measurements with numeric values and anomaly classifications.

    Notes
    -----
    The classifications are diagnostic only. No observations are removed
    or modified by this function.
    """
    result = measurements.copy()

    result["value"] = pd.to_numeric(result["value"], errors="coerce")

    result["anomaly_type"] = "normal"

    missing = result["value"].isna()

    negative = result["value"] < 0

    zero = result["value"] == 0

    low_positive = (
        (result["value"] > 0)
        & (result["value"] <= LOW_VALUE_THRESHOLD)
    )

    high = (result["value"] >= HIGH_VALUE_THRESHOLD) & (
        result["value"] < EXTREME_VALUE_THRESHOLD
    )

    extreme = result["value"] >= EXTREME_VALUE_THRESHOLD

    result.loc[missing, "anomaly_type"] = "missing"
    result.loc[negative, "anomaly_type"] = "negative"
    result.loc[zero, "anomaly_type"] = "zero"
    result.loc[low_positive, "anomaly_type"] = "low_positive"
    result.loc[high, "anomaly_type"] = "high"
    result.loc[extreme, "anomaly_type"] = "extreme"

    return result


# ---------------------------------------------------------------------------
# Exact-value frequencies
# ---------------------------------------------------------------------------


def build_value_frequencies(measurements: pd.DataFrame) -> pd.DataFrame:
    """Count recurring exact values within diagnostic categories.

    Parameters
    ----------
    measurements : pandas.DataFrame
        Measurements containing anomaly classifications.

    Returns
    -------
    pandas.DataFrame
        Exact anomalous-value frequencies by anomaly category.
    """
    anomalous = measurements[measurements["anomaly_type"] != "normal"].copy()

    frequencies = (
        anomalous.groupby(["anomaly_type", "value"], dropna=False)
        .size()
        .reset_index(name="measurement_count")
    )

    total_rows = len(measurements)

    frequencies["measurement_pct"] = (
        frequencies["measurement_count"] / total_rows * 100
    )

    return frequencies.sort_values(
        ["anomaly_type", "measurement_count"],
        ascending=[True, False],
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Group summaries
# ---------------------------------------------------------------------------


def add_indicator_columns(measurements: pd.DataFrame) -> pd.DataFrame:
    """Add Boolean columns for each diagnostic value category.

    Parameters
    ----------
    measurements : pandas.DataFrame
        Measurements containing anomaly classifications.

    Returns
    -------
    pandas.DataFrame
        Measurement dataset with category indicator columns.
    """
    result = measurements.copy()

    categories = [
        "missing",
        "negative",
        "zero",
        "low_positive",
        "high",
        "extreme"
    ]

    for category in categories:
        result[f"is_{category}"] = result["anomaly_type"] == category

    result["is_anomalous"] = result["anomaly_type"] != "normal"

    return result


def build_group_summary(
    measurements: pd.DataFrame, group_columns: list[str]
) -> pd.DataFrame:
    """Summarize anomaly counts for stations or sensors.

    Parameters
    ----------
    measurements : pandas.DataFrame
        Classified PM2.5 measurement observations.
    group_columns : list[str]
        Columns defining the summary grouping.

    Returns
    -------
    pandas.DataFrame
        Group-level anomaly counts and percentages.
    """
    flagged = add_indicator_columns(measurements=measurements)

    aggregation = {
        "measurement_rows": ("value", "size"),
        "missing_count": ("is_missing", "sum"),
        "negative_count": ("is_negative", "sum"),
        "zero_count": ("is_zero", "sum"),
        "low_positive_count": ("is_low_positive", "sum"),
        "high_count": ("is_high", "sum"),
        "extreme_count": ("is_extreme", "sum"),
        "anomaly_count": ("is_anomalous", "sum"),
        "minimum": ("value", "min"),
        "median": ("value", "median"),
        "maximum": ("value", "max"),
    }

    summary = (
        flagged
        .groupby(group_columns, dropna=False)
        .agg(**aggregation).reset_index()
    )

    count_columns = [
        "missing_count",
        "negative_count",
        "zero_count",
        "low_positive_count",
        "high_count",
        "extreme_count",
        "anomaly_count",
    ]

    for column in count_columns:
        percentage_column = column.replace("_count", "_pct")

        summary[percentage_column] = (
            summary[column] / summary["measurement_rows"] * 100
        )

    return summary


def add_sensor_date_range(
    sensor_summary: pd.DataFrame, measurements: pd.DataFrame
) -> pd.DataFrame:
    """Add first and last anomalous timestamps to sensor summaries.

    Parameters
    ----------
    sensor_summary : pandas.DataFrame
        Sensor-level anomaly summary.
    measurements : pandas.DataFrame
        Classified PM2.5 measurements.

    Returns
    -------
    pandas.DataFrame
        Sensor summary with anomaly date ranges.
    """
    anomalous = measurements[measurements["anomaly_type"] != "normal"]

    anomaly_dates = (
        anomalous.groupby(["location_id", "sensor_id"])
        .agg(
            first_anomaly_utc=("datetime_utc", "min"),
            last_anomaly_utc=("datetime_utc", "max"),
            distinct_anomaly_values=("value", "nunique"),
        )
        .reset_index()
    )

    return sensor_summary.merge(
        anomaly_dates, on=["location_id", "sensor_id"], how="left"
    )


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------


def build_anomaly_samples(
    measurements: pd.DataFrame, samples_per_category: int
) -> pd.DataFrame:
    """Create deterministic samples of anomalous observations.

    Parameters
    ----------
    measurements : pandas.DataFrame
        Classified PM2.5 measurements.
    samples_per_category : int
        Maximum observations retained for each anomaly category.

    Returns
    -------
    pandas.DataFrame
        Sample anomalous observations with station and sensor provenance.
    """
    anomalous = measurements[measurements["anomaly_type"] != "normal"].copy()

    anomalous = anomalous.sort_values(
        ["anomaly_type", "datetime_utc", "location_id", "sensor_id"]
    )

    return (
        anomalous.groupby("anomaly_type", group_keys=False)
        .head(samples_per_category)
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_exact_values(
    frequencies: pd.DataFrame, anomaly_type: str, row_count: int = 20
) -> None:
    """Print the most frequent exact values for one anomaly category.

    Parameters
    ----------
    frequencies : pandas.DataFrame
        Exact anomalous-value frequencies.
    anomaly_type : str
        Diagnostic category to display.
    row_count : int, optional
        Maximum number of values to print. Defaults to 20.

    Returns
    -------
    None
    """
    subset = frequencies[
        frequencies["anomaly_type"] == anomaly_type
    ].head(row_count)

    print()
    print(f"Most frequent exact {anomaly_type} values:")

    if subset.empty:
        print("None")
        return

    print(
        subset[[
            "value",
            "measurement_count",
            "measurement_pct"
        ]].to_string(index=False)
    )


def print_summary(
    measurements: pd.DataFrame,
    frequencies: pd.DataFrame,
    sensor_summary: pd.DataFrame,
    station_summary: pd.DataFrame,
) -> None:
    """Print key anomalous-value findings.

    Parameters
    ----------
    measurements : pandas.DataFrame
        Classified PM2.5 measurements.
    frequencies : pandas.DataFrame
        Exact anomalous-value frequencies.
    sensor_summary : pandas.DataFrame
        Sensor-level anomaly summary.
    station_summary : pandas.DataFrame
        Station-level anomaly summary.

    Returns
    -------
    None
    """
    print()
    print("PM2.5 anomalous-value inspection")
    print("--------------------------------")

    print(f"Measurement rows: {len(measurements):,}")

    counts = measurements["anomaly_type"].value_counts()

    print()
    print("Diagnostic categories:")

    for category in [
        "negative",
        "zero",
        "low_positive",
        "high",
        "extreme",
        "missing",
        "normal",
    ]:
        count = int(counts.get(category, 0))

        percentage = count / len(measurements) * 100

        print(f"{category:>12}: " f"{count:>10,} " f"({percentage:>7.4f}%)")

    print_exact_values(frequencies=frequencies, anomaly_type="negative")

    print_exact_values(frequencies=frequencies, anomaly_type="extreme")

    print()
    print("Sensors with most extreme (>= 1000) values:")

    extreme_sensors = sensor_summary.sort_values(
        ["extreme_count", "anomaly_count"], ascending=False
    ).head(15)

    print(
        extreme_sensors[
            [
                "location_id",
                "sensor_id",
                "station_name",
                "metro",
                "measurement_rows",
                "extreme_count",
                "extreme_pct",
                "maximum",
            ]
        ].to_string(index=False)
    )

    print()
    print("Stations with most negative values:")

    negative_stations = station_summary.sort_values(
        ["negative_count", "negative_pct"], ascending=False
    ).head(15)

    print(
        negative_stations[
            [
                "location_id",
                "station_name",
                "metro",
                "measurement_rows",
                "negative_count",
                "negative_pct",
                "minimum",
            ]
        ].to_string(index=False)
    )

    print()
    print("Stations with most zero values:")

    zero_stations = station_summary.sort_values(
        ["zero_count", "zero_pct"], ascending=False
    ).head(15)

    print(
        zero_stations[
            [
                "location_id",
                "station_name",
                "metro",
                "measurement_rows",
                "zero_count",
                "zero_pct",
                "median",
            ]
        ].to_string(index=False)
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Inspect anomalous PM2.5 values in the primary analysis cohort."""
    stations = pd.read_csv(STATIONS_INPUT_PATH)
    location_ids = get_primary_location_ids(stations=stations)

    print(f"Primary cohort locations: {len(location_ids):,}")

    files = get_measurement_files(
        measurement_dir=MEASUREMENTS_DIR, location_ids=location_ids
    )

    print(f"Monthly Parquet files: {len(files):,}")

    measurements = load_measurements(files=files)

    measurements = classify_measurements(measurements=measurements)

    frequencies = build_value_frequencies(measurements=measurements)

    sensor_summary = build_group_summary(
        measurements=measurements,
        group_columns=["location_id", "sensor_id", "station_name", "metro"],
    )

    sensor_summary = add_sensor_date_range(
        sensor_summary=sensor_summary, measurements=measurements
    )

    station_summary = build_group_summary(
        measurements=measurements,
        group_columns=["location_id", "station_name", "metro"],
    )

    samples = build_anomaly_samples(
        measurements=measurements, samples_per_category=(SAMPLES_PER_CATEGORY)
    )

    VALUE_FREQUENCY_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    frequencies.to_csv(VALUE_FREQUENCY_OUTPUT_PATH, index=False)

    sensor_summary.to_csv(SENSOR_SUMMARY_OUTPUT_PATH, index=False)

    station_summary.to_csv(STATION_SUMMARY_OUTPUT_PATH, index=False)

    samples.to_csv(SAMPLE_OUTPUT_PATH, index=False)

    print_summary(
        measurements=measurements,
        frequencies=frequencies,
        sensor_summary=sensor_summary,
        station_summary=station_summary,
    )


if __name__ == "__main__":
    main()
