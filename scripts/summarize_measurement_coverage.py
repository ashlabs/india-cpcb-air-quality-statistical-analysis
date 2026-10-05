"""Summarize measurement completeness by monitoring location."""

from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MANIFEST_INPUT_PATH = Path("data/processed/measurement_download_manifest.csv")

OUTPUT_PATH = Path("data/processed/location_measurement_coverage.csv")

EXPECTED_MEASUREMENTS_PER_DAY = 96
EXPECTED_MONTH_COUNT = 18


# ---------------------------------------------------------------------------
# Preparation
# ---------------------------------------------------------------------------


def add_coverage_metrics(
    manifest: pd.DataFrame, expected_measurements_per_day: int
) -> pd.DataFrame:
    """Add monthly archive, day, and measurement coverage metrics.

    Parameters
    ----------
    manifest : pandas.DataFrame
        Monthly measurement-download manifest.
    expected_measurements_per_day : int
        Expected number of measurements in a complete day.

    Returns
    -------
    pandas.DataFrame
        Manifest with additional monthly completeness metrics.
    """
    coverage = manifest.copy()

    coverage["expected_measurements"] = (
        coverage["expected_days"] * expected_measurements_per_day
    )

    coverage["archive_file_coverage_pct"] = (
        coverage["archive_file_count"] / coverage["expected_days"] * 100
    )

    coverage["day_coverage_pct"] = (
        coverage["days_with_target_data"] / coverage["expected_days"] * 100
    )

    coverage["measurement_coverage_pct"] = (
        coverage["measurement_rows"] / coverage["expected_measurements"] * 100
    )

    coverage["has_measurements"] = coverage["measurement_rows"] > 0

    return coverage


# ---------------------------------------------------------------------------
# Location summary
# ---------------------------------------------------------------------------


def summarize_locations(coverage: pd.DataFrame) -> pd.DataFrame:
    """Aggregate monthly measurement coverage by monitoring location.

    Parameters
    ----------
    coverage : pandas.DataFrame
        Monthly manifest with derived completeness metrics.

    Returns
    -------
    pandas.DataFrame
        One row per monitoring location with overall and monthly
        completeness statistics.
    """
    summary = coverage.groupby(
        ["location_id", "station_name", "metro"], as_index=False
    ).agg(
        total_months=("month", "count"),
        months_with_measurements=("has_measurements", "sum"),
        expected_days=("expected_days", "sum"),
        archive_file_count=("archive_file_count", "sum"),
        days_with_target_data=("days_with_target_data", "sum"),
        expected_measurements=("expected_measurements", "sum"),
        measurement_rows=("measurement_rows", "sum"),
        mean_monthly_measurement_coverage_pct=(
            "measurement_coverage_pct",
            "mean"
        ),
        median_monthly_measurement_coverage_pct=(
            "measurement_coverage_pct",
            "median"
        ),
        minimum_monthly_measurement_coverage_pct=(
            "measurement_coverage_pct",
            "min"
        ),
        maximum_monthly_measurement_coverage_pct=(
            "measurement_coverage_pct",
            "max"
        ),
        duplicate_timestamp_rows=("duplicate_timestamp_rows", "sum"),
        duplicate_sensor_timestamp_rows=(
            "duplicate_sensor_timestamp_rows",
            "sum"
        ),
    )

    summary["archive_file_coverage_pct"] = (
        summary["archive_file_count"] / summary["expected_days"] * 100
    )

    summary["day_coverage_pct"] = (
        summary["days_with_target_data"] / summary["expected_days"] * 100
    )

    summary["measurement_coverage_pct"] = (
        summary["measurement_rows"] / summary["expected_measurements"] * 100
    )

    return summary.sort_values(
        ["metro", "measurement_coverage_pct"],
        ascending=[True, False],
    ).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_summary(summary: pd.DataFrame, expected_month_count: int) -> None:
    """Validate the location-level measurement coverage summary.

    Parameters
    ----------
    summary : pandas.DataFrame
        Location-level measurement coverage summary.
    expected_month_count : int
        Expected number of months for each location.

    Returns
    -------
    None

    Raises
    ------
    ValueError
        If a location has an unexpected number of monthly records
        or duplicate timestamp records exist.
    """
    unexpected_months = (
        summary[summary["total_months"] != expected_month_count]
    )

    if not unexpected_months.empty:
        raise ValueError(
            "Some locations do not contain the expected "
            f"{expected_month_count} monthly records."
        )

    duplicate_rows = summary["duplicate_timestamp_rows"].sum()

    if duplicate_rows != 0:
        raise ValueError(
            "Duplicate timestamps were found in the " "measurement dataset."
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_summary(summary: pd.DataFrame) -> None:
    """Print measurement completeness statistics.

    Parameters
    ----------
    summary : pandas.DataFrame
        Location-level measurement coverage summary.

    Returns
    -------
    None
    """
    print()
    print("Location measurement coverage")
    print("-----------------------------")

    print(f"Locations: {len(summary):,}")

    print()
    print("Overall station coverage distribution:")

    columns = [
        "archive_file_coverage_pct",
        "day_coverage_pct",
        "measurement_coverage_pct",
        "minimum_monthly_measurement_coverage_pct",
    ]

    print(summary[columns].describe().to_string())

    print()
    print("Coverage by metro:")

    metro = (
        summary.groupby("metro")
        .agg(
            station_count=("location_id", "count"),
            expected_days=("expected_days", "sum"),
            days_with_target_data=("days_with_target_data", "sum"),
            expected_measurements=("expected_measurements", "sum"),
            measurement_rows=("measurement_rows", "sum"),
        )
        .reset_index()
    )

    metro["day_coverage_pct"] = (
        metro["days_with_target_data"] / metro["expected_days"] * 100
    )

    metro["measurement_coverage_pct"] = (
        metro["measurement_rows"] / metro["expected_measurements"] * 100
    )

    print(
        metro[
            [
                "metro",
                "station_count",
                "day_coverage_pct",
                "measurement_coverage_pct"
            ]
        ].to_string(index=False)
    )

    print()
    print("15 lowest-coverage locations:")

    weakest = summary.nsmallest(15, "measurement_coverage_pct")[
        [
            "location_id",
            "station_name",
            "metro",
            "months_with_measurements",
            "day_coverage_pct",
            "measurement_coverage_pct",
            "minimum_monthly_measurement_coverage_pct",
        ]
    ]

    print(weakest.to_string(index=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Create the location-level measurement coverage summary."""
    manifest = pd.read_csv(MANIFEST_INPUT_PATH)

    coverage = add_coverage_metrics(
        manifest=manifest,
        expected_measurements_per_day=(EXPECTED_MEASUREMENTS_PER_DAY)
    )

    summary = summarize_locations(coverage=coverage)

    validate_summary(
        summary=summary,
        expected_month_count=EXPECTED_MONTH_COUNT
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    summary.to_csv(OUTPUT_PATH, index=False)

    print_summary(summary=summary)


if __name__ == "__main__":
    main()
