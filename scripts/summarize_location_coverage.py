"""Summarize archive coverage across the full analysis period by location."""

from pathlib import Path

import pandas as pd

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

COVERAGE_INPUT_PATH = Path("data/processed/archive_monthly_coverage.csv")
SUMMARY_OUTPUT_PATH = Path("data/processed/location_archive_summary.csv")
HIGH_COVERAGE_THRESHOLD = 90.0


# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------


def summarize_location_coverage(
    coverage: pd.DataFrame, high_coverage_threshold: float
) -> pd.DataFrame:
    """Summarize monthly archive coverage for each monitoring location.

    Parameters
    ----------
    coverage : pandas.DataFrame
        Monthly archive coverage with one row per location-month.
    high_coverage_threshold : float
        Percentage threshold used to count months with high archive
        coverage.

    Returns
    -------
    pandas.DataFrame
        One row per monitoring location with continuity and coverage
        summary statistics.
    """
    coverage = coverage.copy()

    coverage["has_files"] = coverage["archive_file_count"] > 0

    coverage["has_high_coverage"] = (
        coverage["archive_file_coverage_pct"] >= high_coverage_threshold
    )

    summary = (
        coverage.groupby(
            [
                "location_id",
                "station_name",
                "provider_id",
                "provider_name",
                "country_id",
                "country_code",
                "country_name",
                "latitude",
                "longitude",
                "target_parameter",
            ],
            dropna=False,
        )
        .agg(
            total_months=("month", "count"),
            months_with_files=("has_files", "sum"),
            months_with_high_coverage=("has_high_coverage", "sum"),
            mean_archive_coverage_pct=("archive_file_coverage_pct", "mean"),
            median_archive_coverage_pct=(
                "archive_file_coverage_pct",
                "median"
            ),
            minimum_archive_coverage_pct=("archive_file_coverage_pct", "min"),
            maximum_archive_coverage_pct=("archive_file_coverage_pct", "max"),
        )
        .reset_index()
    )

    summary["month_continuity_pct"] = (
        summary["months_with_files"] / summary["total_months"] * 100
    )

    return summary.sort_values(
        [
            "months_with_files",
            "median_archive_coverage_pct",
            "mean_archive_coverage_pct",
        ],
        ascending=False,
    ).reset_index(drop=True)


def add_yearly_coverage(
    summary: pd.DataFrame,
    coverage: pd.DataFrame,
) -> pd.DataFrame:
    """Add average yearly archive coverage to the location summary.

    Parameters
    ----------
    summary : pandas.DataFrame
        Location-level archive summary.
    coverage : pandas.DataFrame
        Monthly archive coverage records.

    Returns
    -------
    pandas.DataFrame
        Location summary with yearly mean coverage columns added.
    """
    yearly = (
        coverage.groupby(["location_id", "year"])["archive_file_coverage_pct"]
        .mean()
        .unstack("year")
    )

    yearly.columns = [
        f"{int(year)}_mean_coverage_pct"
        for year in yearly.columns
    ]

    return summary.merge(yearly, on="location_id", how="left")


def print_summary(summary: pd.DataFrame) -> None:
    """Print continuity and coverage summaries.

    Parameters
    ----------
    summary : pandas.DataFrame
        Location-level archive coverage summary.

    Returns
    -------
    None
    """
    print()
    print("Location archive summary")
    print("------------------------")

    print(f"Locations: {len(summary):,}")

    print()
    print("Locations by number of months with archive files:")

    continuity = (
        summary["months_with_files"]
        .value_counts()
        .sort_index(ascending=False)
        .rename_axis("months_with_files")
        .reset_index(name="location_count")
    )

    print(continuity.to_string(index=False))

    print()
    print("Coverage distribution for locations with all months present:")

    complete_locations = summary[
        summary["months_with_files"] == summary["total_months"]
    ]

    print(
        complete_locations[
            [
                "mean_archive_coverage_pct",
                "median_archive_coverage_pct",
                "minimum_archive_coverage_pct",
                "months_with_high_coverage",
            ]
        ]
        .describe()
        .to_string()
    )

    print()
    print("Top 10 locations by continuity and coverage:")

    columns = [
        "location_id",
        "station_name",
        "months_with_files",
        "months_with_high_coverage",
        "mean_archive_coverage_pct",
        "median_archive_coverage_pct",
        "minimum_archive_coverage_pct",
    ]

    print(summary[columns].head(10).to_string(index=False))


def main() -> None:
    """Build and save the location-level archive coverage summary."""
    coverage = pd.read_csv(COVERAGE_INPUT_PATH)

    summary = summarize_location_coverage(
        coverage=coverage, high_coverage_threshold=HIGH_COVERAGE_THRESHOLD
    )

    summary = add_yearly_coverage(summary=summary, coverage=coverage)

    SUMMARY_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    summary.to_csv(SUMMARY_OUTPUT_PATH, index=False)

    print_summary(summary)


if __name__ == "__main__":
    main()
