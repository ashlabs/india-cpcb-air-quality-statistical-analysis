"""Select monitoring stations for the target metropolitan areas."""

from pathlib import Path

import pandas as pd

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SUMMARY_INPUT_PATH = Path("data/processed/location_archive_summary.csv")
OUTPUT_PATH = Path("data/processed/selected_metro_stations.csv")

TARGET_MONTH_COUNT = 18

METRO_PATTERNS = {
    "Delhi": ["Delhi", "New Delhi"],
    "Mumbai": ["Mumbai", "Navi Mumbai"],
    "Bengaluru": ["Bengaluru"],
    "Hyderabad": ["Hyderabad"],
    "Chennai": ["Chennai"],
    "Kolkata": ["Kolkata"],
}

# ----------------------------------------------------------------------------
# Metro Classification
# ----------------------------------------------------------------------------


def classify_metro(
    station_name: str, metro_patterns: dict[str, list[str]]
) -> str | None:
    """Assign a metropolitan area using configured station-name patterns.

    Parameters
    ----------
    station_name : str
        Monitoring station name.
    metro_patterns : dict[str, list[str]]
        Mapping of metro names to station name patterns.

    Returns
    -------
    str or None
        Metro name when a configured pattern matches, otherwise None.
    """
    if pd.isna(station_name):
        return None

    station_name_lower = station_name.lower()

    for metro, patterns in metro_patterns.items():
        for pattern in patterns:
            if pattern.lower() in station_name_lower:
                return metro

    return None


# ----------------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------------


def select_stations(
    summary: pd.DataFrame,
    metro_patterns: dict[str, list[str]],
    required_months: int
) -> pd.DataFrame:
    """Select stations meeting metro and continuity requirements.

    Parameters
    ----------
    summary : pandas.DataFrame
        Location-level archive coverage summary.
    metro_patterns : dict[str, list[str]]
        Mapping of metro names to station-name patterns.
    required_months : int
        Number of months for which archive files must be present.

    Returns
    -------
    pandas.DataFrame
        Selected monitoring stations with assigned metro labels.
    """
    selected = summary.copy()

    selected["metro"] = selected["station_name"].apply(
        classify_metro, metro_patterns=metro_patterns
    )

    selected = selected[
        selected["metro"].notna()
        & (selected["months_with_files"] == required_months)
    ].copy()

    columns = [
        "location_id",
        "station_name",
        "metro",
        "latitude",
        "longitude",
        "target_parameter",
        "months_with_files",
        "months_with_high_coverage",
        "mean_archive_coverage_pct",
        "median_archive_coverage_pct",
        "minimum_archive_coverage_pct",
        "2025_mean_coverage_pct",
        "2026_mean_coverage_pct",
    ]

    return (
        selected[columns]
        .sort_values(
            [
                "metro",
                "station_name",
            ]
        )
        .reset_index(drop=True)
    )


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------


def validate_selection(
    selected: pd.DataFrame,
    expected_metros: set[str]
) -> None:
    """Validate the selected station set.

    Parameters
    ----------
    selected : pandas.DataFrame
        Selected monitoring stations.
    expected_metros : set[str]
        Metropolitan areas expected in the output.

    Returns
    -------
    None

    Raises
    ------
    ValueError
        If expected metros are missing or duplicate location IDs exist.
    """
    actual_metros = set(selected["metro"].dropna().unique())

    missing_metros = expected_metros - actual_metros

    if missing_metros:
        raise ValueError(
            f"No eligible stations found for metros: {sorted(missing_metros)}"
        )

    if selected["location_id"].duplicated().any():
        raise ValueError("Duplicate location IDs found in selected stations.")


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------


def print_summary(selected: pd.DataFrame) -> None:
    """Print a summary of selected stations by metropolitan area.

    Parameters
    ----------
    selected : pandas.DataFrame
        Selected monitoring stations.

    Returns
    -------
    None
    """
    print()
    print("Selected metro station summary")
    print("------------------------------")

    print(f"Total selected stations: {len(selected):,}")

    print()
    print("Stations by metro:")

    metro_counts = (
        selected["metro"]
        .value_counts()
        .sort_index()
        .rename_axis("metro")
        .reset_index(name="station_count")
    )

    print(metro_counts.to_string(index=False))

    print()
    print("Coverage summary by metro:")

    coverage_summary = (
        selected.groupby("metro")
        .agg(
            station_count=("location_id", "count"),
            mean_archive_coverage_pct=("mean_archive_coverage_pct", "mean"),
            median_archive_coverage_pct=(
                "median_archive_coverage_pct",
                "median"
            ),
        )
        .reset_index()
    )

    print(coverage_summary.to_string(index=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Select and save monitoring stations for the target metros."""
    summary = pd.read_csv(SUMMARY_INPUT_PATH)

    selected = select_stations(
        summary=summary,
        metro_patterns=METRO_PATTERNS,
        required_months=TARGET_MONTH_COUNT,
    )

    validate_selection(selected=selected, expected_metros=set(METRO_PATTERNS))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    selected.to_csv(OUTPUT_PATH, index=False)

    print_summary(selected)


if __name__ == "__main__":
    main()
