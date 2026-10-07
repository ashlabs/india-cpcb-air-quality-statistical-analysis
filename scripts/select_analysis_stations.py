"""Create the primary and sensitivity analysis station cohorts."""

from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INPUT_PATH = Path("data/processed/location_measurement_coverage.csv")

OUTPUT_PATH = Path("data/processed/analysis_stations.csv")

REQUIRED_MONTHS = 18
PRIMARY_COVERAGE_THRESHOLD = 60.0
SENSITIVITY_COVERAGE_THRESHOLD = 70.0


# ---------------------------------------------------------------------------
# Cohort classification
# ---------------------------------------------------------------------------


def add_eligibility_flags(
    stations: pd.DataFrame,
    required_months: int,
    primary_threshold: float,
    sensitivity_threshold: float,
) -> pd.DataFrame:
    """Add primary and sensitivity cohort eligibility flags.

    Parameters
    ----------
    stations : pandas.DataFrame
        Station-level measurement coverage summary.
    required_months : int
        Number of months in which measurements must be present.
    primary_threshold : float
        Minimum overall measurement completeness percentage for the
        primary analysis cohort.
    sensitivity_threshold : float
        Minimum overall measurement completeness percentage for the
        sensitivity analysis cohort.

    Returns
    -------
    pandas.DataFrame
        Station-level dataset with cohort eligibility flags.
    """
    result = stations.copy()

    has_full_month_coverage = result["months_with_measurements"] == required_months

    result["primary_eligible"] = has_full_month_coverage & (
        result["measurement_coverage_pct"] >= primary_threshold
    )

    result["sensitivity_eligible"] = has_full_month_coverage & (
        result["measurement_coverage_pct"] >= sensitivity_threshold
    )

    return result


def add_exclusion_reason(
    stations: pd.DataFrame, required_months: int, primary_threshold: float
) -> pd.DataFrame:
    """Add the reason a station is excluded from the primary cohort.

    Parameters
    ----------
    stations : pandas.DataFrame
        Station-level dataset containing primary eligibility flags.
    required_months : int
        Number of required months with measurements.
    primary_threshold : float
        Minimum overall completeness percentage for primary eligibility.

    Returns
    -------
    pandas.DataFrame
        Dataset with a primary exclusion reason column.
    """
    result = stations.copy()

    result["primary_exclusion_reason"] = ""

    incomplete_months = result["months_with_measurements"] < required_months

    low_coverage = result["measurement_coverage_pct"] < primary_threshold

    result.loc[
        incomplete_months & low_coverage,
        "primary_exclusion_reason",
    ] = (
        "Incomplete temporal coverage and " "measurement completeness below threshold"
    )

    result.loc[
        incomplete_months & ~low_coverage,
        "primary_exclusion_reason",
    ] = "Measurements not present in all analysis months"

    result.loc[
        ~incomplete_months & low_coverage,
        "primary_exclusion_reason",
    ] = "Measurement completeness below threshold"

    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_cohorts(stations: pd.DataFrame) -> None:
    """Validate relationships between analysis cohorts.

    Parameters
    ----------
    stations : pandas.DataFrame
        Station dataset containing eligibility flags.

    Returns
    -------
    None

    Raises
    ------
    ValueError
        If sensitivity stations are not a subset of the primary cohort,
        duplicate location IDs exist, or a metro has no eligible station.
    """
    if stations["location_id"].duplicated().any():
        raise ValueError("Duplicate location IDs found in station dataset.")

    invalid_sensitivity = stations[
        stations["sensitivity_eligible"] & ~stations["primary_eligible"]
    ]

    if not invalid_sensitivity.empty:
        raise ValueError(
            "Sensitivity cohort must be a subset " "of the primary cohort."
        )

    primary = stations[stations["primary_eligible"]]
    sensitivity = stations[stations["sensitivity_eligible"]]

    all_metros = set(stations["metro"].dropna().unique())
    primary_metros = set(primary["metro"].dropna().unique())
    sensitivity_metros = set(sensitivity["metro"].dropna().unique())

    if primary_metros != all_metros:
        raise ValueError("Primary cohort does not contain all metros.")

    if sensitivity_metros != all_metros:
        raise ValueError("Sensitivity cohort does not contain all metros.")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_cohort_summary(stations: pd.DataFrame) -> None:
    """Print primary and sensitivity cohort summaries.

    Parameters
    ----------
    stations : pandas.DataFrame
        Station dataset containing eligibility flags.

    Returns
    -------
    None
    """
    print()
    print("Analysis station cohorts")
    print("------------------------")

    print(f"Candidate stations: {len(stations):,}")

    print(
        "Primary cohort "
        f"(>= {PRIMARY_COVERAGE_THRESHOLD:.0f}%): "
        f"{stations['primary_eligible'].sum():,}"
    )

    print(
        "Sensitivity cohort "
        f"(>= {SENSITIVITY_COVERAGE_THRESHOLD:.0f}%): "
        f"{stations['sensitivity_eligible'].sum():,}"
    )

    print()
    print("Primary cohort by metro:")

    primary_counts = (
        stations[stations["primary_eligible"]]["metro"]
        .value_counts()
        .sort_index()
        .rename_axis("metro")
        .reset_index(name="station_count")
    )

    print(primary_counts.to_string(index=False))

    print()
    print("Sensitivity cohort by metro:")

    sensitivity_counts = (
        stations[stations["sensitivity_eligible"]]["metro"]
        .value_counts()
        .sort_index()
        .rename_axis("metro")
        .reset_index(name="station_count")
    )

    print(sensitivity_counts.to_string(index=False))

    print()
    print("Stations excluded from primary cohort:")

    excluded = stations[~stations["primary_eligible"]][
        [
            "location_id",
            "station_name",
            "metro",
            "months_with_measurements",
            "measurement_coverage_pct",
            "primary_exclusion_reason",
        ]
    ].sort_values(["metro", "measurement_coverage_pct"])

    if excluded.empty:
        print("None")
    else:
        print(excluded.to_string(index=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Create and save reproducible analysis station cohorts."""
    stations = pd.read_csv(INPUT_PATH)

    stations = add_eligibility_flags(
        stations=stations,
        required_months=REQUIRED_MONTHS,
        primary_threshold=PRIMARY_COVERAGE_THRESHOLD,
        sensitivity_threshold=SENSITIVITY_COVERAGE_THRESHOLD,
    )

    stations = add_exclusion_reason(
        stations=stations,
        required_months=REQUIRED_MONTHS,
        primary_threshold=PRIMARY_COVERAGE_THRESHOLD,
    )

    validate_cohorts(stations=stations)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    stations.to_csv(OUTPUT_PATH, index=False)

    print_cohort_summary(stations=stations)


if __name__ == "__main__":
    main()
