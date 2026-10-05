"""Inventory yearly data coverage for sensors using the OpenAQ API."""

import json
import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

OPENAQ_API_BASE_URL = "https://api.openaq.org/v3"

ENV_PATH = Path(".env")
INVENTORY_INPUT_PATH = Path("data/processed/station_inventory.csv")
CACHE_DIR = Path("data/raw/sensor_year_coverage")
COVERAGE_OUTPUT_PATH = Path("data/processed/sensor_year_coverage.csv")

START_DATE = "2024-01-01"
END_DATE = "2025-12-31"
TARGET_YEARS = (2024, 2025)

REQUEST_INTERVAL_SECONDS = 1.1
REQUEST_TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 5

HTTP_ERROR_TOO_MANY_REQUESTS = 429
HTTP_INTERNAL_SERVER_ERROR = 500


# ----------------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------------


def get_api_key(env_path: Path = ENV_PATH) -> str:
    """Load the OpenAQ API key from a local environment file.

    Parameters
    ----------
    env_path : pathlib.Path, optional
        Path to the environment file containing OPENAQ_API_KEY.
        Defaults to ENV_PATH.

    Returns
    -------
    str
        OpenAQ API key.

    Raises
    ------
    RuntimeError
        If OPENAQ_API_KEY cannot be loaded.
    """
    load_dotenv(dotenv_path=env_path)

    api_key = os.getenv("OPENAQ_API_KEY")

    if not api_key:
        raise RuntimeError(
            "OPENAQ_API_KEY was not loaded from the environment."
        )

    return api_key


# ----------------------------------------------------------------------------
# Sensor inventory
# ----------------------------------------------------------------------------


def parse_sensor_ids(value: object) -> list[int]:
    """Parse a comma-separated sensor ID field.

    Parameters
    ----------
    value : object
        Value containing one or more comma-separated sensor IDs.

    Returns
    -------
    list[int]
        Parsed sensor IDs. Returns an empty list when no IDs are present.
    """
    if value is None:
        return []

    text = str(value).strip()

    if not text or text.lower() in {"nan", "<na>", "none"}:
        return []

    sensor_ids = []

    for sensor_id in text.split(","):
        sensor_id = sensor_id.strip()

        if sensor_id:
            sensor_ids.append(int(sensor_id))

    return sensor_ids


def build_sensor_inventory(inventory: pd.DataFrame) -> pd.DataFrame:
    """Create one inventory row per target sensor.

    Parameters
    ----------
    inventory : pandas.DataFrame
        Location-level station inventory containing target sensor IDs.

    Returns
    -------
    pandas.DataFrame
        Sensor-level inventory with location and station metadata.

    Raises
    ------
    ValueError
        If the same sensor ID is associated with multiple locations.
    """
    rows = []

    for _, location in inventory.iterrows():
        sensor_ids = parse_sensor_ids(location.get("target_sensor_ids"))

        for sensor_id in sensor_ids:
            rows.append(
                {
                    "sensor_id": sensor_id,
                    "location_id": location["location_id"],
                    "station_name": location["station_name"],
                    "target_parameter": location["target_parameter"],
                    "provider_id": location["provider_id"],
                    "provider_name": location["provider_name"],
                    "country_id": location["country_id"],
                    "country_code": location["country_code"],
                    "country_name": location["country_name"],
                    "latitude": location["latitude"],
                    "longitude": location["longitude"],
                }
            )

    sensor_inventory = pd.DataFrame(rows)

    duplicates = sensor_inventory[
        sensor_inventory.duplicated(subset=["sensor_id"], keep=False)
    ]

    if not duplicates.empty:
        location_counts = (
            duplicates
            .groupby("sensor_id")["location_id"]
            .nunique()
        )

        conflicting_ids = location_counts[location_counts > 1]

        if not conflicting_ids.empty:
            raise ValueError(
                "One or more sensor IDs are associated with "
                "multiple locations."
            )

    return (
        sensor_inventory.drop_duplicates(subset=["sensor_id"])
        .sort_values("sensor_id")
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# API access
# ---------------------------------------------------------------------------


def get_retry_delay(
    response: requests.Response, default_seconds: float = 60.0
) -> float:
    """Determine how long to wait after an API rate-limit response.

    Parameters
    ----------
    response : requests.Response
        HTTP response returned by the OpenAQ API.
    default_seconds : float, optional
        Fallback delay when no usable rate-limit header is available.
        Defaults to 60 seconds.

    Returns
    -------
    float
        Number of seconds to wait before retrying.
    """
    header_value = response.headers.get("Retry-After") or response.headers.get(
        "x-ratelimit-reset"
    )

    if not header_value:
        return default_seconds

    try:
        value = float(header_value)
    except ValueError:
        return default_seconds

    current_time = time.time()

    if value > current_time:
        return max(1.0, value - current_time + 1.0)

    return max(1.0, value + 1.0)


def fetch_sensor_years(
    session: requests.Session,
    api_key: str,
    sensor_id: int,
    date_from: str,
    date_to: str,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict:
    """Fetch yearly coverage data for one sensor.

    Parameters
    ----------
    session : requests.Session
        Reusable HTTP session.
    api_key : str
        OpenAQ API key.
    sensor_id : int
        OpenAQ sensor identifier.
    date_from : str
        Beginning of the requested date range in YYYY-MM-DD format.
    date_to : str
        End of the requested date range in YYYY-MM-DD format.
    timeout : int, optional
        Maximum number of seconds to wait for each API request.
        Defaults to REQUEST_TIMEOUT_SECONDS.
    max_attempts : int, optional
        Maximum number of attempts for transient failures.
        Defaults to MAX_ATTEMPTS.

    Returns
    -------
    dict
        OpenAQ API response containing yearly sensor data.

    Raises
    ------
    RuntimeError
        If the request cannot be completed after all retry attempts.
    """
    url = f"{OPENAQ_API_BASE_URL}/sensors/{sensor_id}/years"

    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            params: dict[str, str | int] = {
                "date_from": date_from,
                "date_to": date_to,
                "limit": 10,
                "page": 1,
            }
            response = session.get(
                url,
                headers={"X-API-Key": api_key},
                params=params,
                timeout=timeout,
            )

            if response.status_code == HTTP_ERROR_TOO_MANY_REQUESTS:
                delay = get_retry_delay(response)

                print(
                    f"Rate limit reached for sensor "
                    f"{sensor_id}. Retrying after API reset."
                )

                time.sleep(delay)
                continue

            if response.status_code >= HTTP_INTERNAL_SERVER_ERROR:
                response.raise_for_status()

            response.raise_for_status()

            return response.json()

        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.HTTPError,
            requests.JSONDecodeError,
        ) as exc:
            last_error = exc

            if attempt == max_attempts:
                break

            delay = 2 ** (attempt - 1)

            print(
                f"Transient failure for sensor {sensor_id}: "
                f"{type(exc).__name__}. "
                f"Retrying after backoff."
            )

            time.sleep(delay)

    raise RuntimeError(
        f"Unable to retrieve yearly data for sensor "
        f"{sensor_id} after {max_attempts} attempts."
    ) from last_error


# ---------------------------------------------------------------------------
# Local API cache
# ---------------------------------------------------------------------------


def get_cache_path(sensor_id: int, cache_dir: Path = CACHE_DIR) -> Path:
    """Build the raw cache path for a sensor response.

    Parameters
    ----------
    sensor_id : int
        OpenAQ sensor identifier.
    cache_dir : pathlib.Path, optional
        Directory used to store cached API responses.
        Defaults to CACHE_DIR.

    Returns
    -------
    pathlib.Path
        Path to the sensor's cached JSON response.
    """
    return cache_dir / f"sensor_{sensor_id}.json"


def load_cached_response(
    sensor_id: int,
    cache_dir: Path = CACHE_DIR
) -> dict | None:
    """Load a previously cached sensor response if available.

    Parameters
    ----------
    sensor_id : int
        OpenAQ sensor identifier.
    cache_dir : pathlib.Path, optional
        Directory containing cached API responses.
        Defaults to CACHE_DIR.

    Returns
    -------
    dict or None
        Cached API response, or None when no cache exists.
    """
    cache_path = get_cache_path(sensor_id=sensor_id, cache_dir=cache_dir)

    if not cache_path.exists():
        return None

    return json.loads(cache_path.read_text(encoding="utf-8"))


def save_cached_response(
    sensor_id: int, response_data: dict, cache_dir: Path = CACHE_DIR
) -> None:
    """Save a successful sensor API response to the local cache.

    Parameters
    ----------
    sensor_id : int
        OpenAQ sensor identifier.
    response_data : dict
        Successful OpenAQ API response.
    cache_dir : pathlib.Path, optional
        Directory used to store cached API responses.
        Defaults to CACHE_DIR.

    Returns
    -------
    None
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_path = get_cache_path(sensor_id=sensor_id, cache_dir=cache_dir)

    cache_path.write_text(
        json.dumps(response_data, indent=2),
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Coverage extraction
# ---------------------------------------------------------------------------


def get_result_year(result: dict) -> int | None:
    """Extract the local calendar year from an annual API result.

    Parameters
    ----------
    result : dict
        OpenAQ yearly aggregation result.

    Returns
    -------
    int or None
        Local calendar year for the aggregation period.
    """
    period = result.get("period") or {}
    datetime_from = period.get("datetimeFrom") or {}

    timestamp = datetime_from.get("local") or datetime_from.get("utc")

    if not timestamp:
        return None

    return pd.Timestamp(timestamp).year


def build_coverage_row(
    sensor: pd.Series,
    year: int,
    result: dict | None
) -> dict:
    """Build one sensor-year coverage record.

    Parameters
    ----------
    sensor : pandas.Series
        Sensor metadata from the sensor inventory.
    year : int
        Calendar year represented by the output row.
    result : dict or None
        OpenAQ yearly result for the sensor and year.
        None indicates that no annual record was returned.

    Returns
    -------
    dict
        Flattened sensor-year coverage record.
    """
    row = {
        "sensor_id": sensor["sensor_id"],
        "location_id": sensor["location_id"],
        "station_name": sensor["station_name"],
        "parameter": sensor["target_parameter"],
        "provider_id": sensor["provider_id"],
        "provider_name": sensor["provider_name"],
        "country_id": sensor["country_id"],
        "country_code": sensor["country_code"],
        "country_name": sensor["country_name"],
        "latitude": sensor["latitude"],
        "longitude": sensor["longitude"],
        "year": year,
        "api_record_found": result is not None,
    }

    if result is None:
        return row

    period = result.get("period") or {}
    period_from = period.get("datetimeFrom") or {}
    period_to = period.get("datetimeTo") or {}

    coverage = result.get("coverage") or {}
    coverage_from = coverage.get("datetimeFrom") or {}
    coverage_to = coverage.get("datetimeTo") or {}

    summary = result.get("summary") or {}
    flag_info = result.get("flagInfo") or {}

    row.update(
        {
            "annual_value": result.get("value"),
            "period_from_utc": period_from.get("utc"),
            "period_from_local": period_from.get("local"),
            "period_to_utc": period_to.get("utc"),
            "period_to_local": period_to.get("local"),
            "expected_count": coverage.get("expectedCount"),
            "observed_count": coverage.get("observedCount"),
            "percent_complete": coverage.get("percentComplete"),
            "percent_coverage": coverage.get("percentCoverage"),
            "first_observation_utc": (coverage_from.get("utc")),
            "first_observation_local": (coverage_from.get("local")),
            "last_observation_utc": (coverage_to.get("utc")),
            "last_observation_local": (coverage_to.get("local")),
            "minimum": summary.get("min"),
            "q02": summary.get("q02"),
            "q25": summary.get("q25"),
            "median": summary.get("median"),
            "q75": summary.get("q75"),
            "q98": summary.get("q98"),
            "maximum": summary.get("max"),
            "mean": summary.get("avg"),
            "standard_deviation": summary.get("sd"),
            "has_flags": flag_info.get("hasFlags"),
        }
    )

    return row


def flatten_sensor_coverage(
    sensor: pd.Series, response_data: dict, target_years: tuple[int, ...]
) -> list[dict]:
    """Convert a sensor API response into sensor-year rows.

    Parameters
    ----------
    sensor : pandas.Series
        Sensor metadata from the sensor inventory.
    response_data : dict
        OpenAQ yearly aggregation response.
    target_years : tuple[int, ...]
        Calendar years that must appear in the output.

    Returns
    -------
    list[dict]
        One coverage record for every requested sensor-year,
        including rows for years with no API result.
    """
    results_by_year = {}

    for result in response_data.get("results", []):
        year = get_result_year(result)

        if year is not None:
            results_by_year[year] = result

    rows = []

    for year in target_years:
        rows.append(
            build_coverage_row(
                sensor=sensor, year=year, result=results_by_year.get(year)
            )
        )

    return rows


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def collect_coverage(
    sensor_inventory: pd.DataFrame,
    api_key: str,
    target_years: tuple[int, ...],
    date_from: str,
    date_to: str,
) -> pd.DataFrame:
    """Collect yearly coverage for all sensors.

    Parameters
    ----------
    sensor_inventory : pandas.DataFrame
        One row per sensor with location metadata.
    api_key : str
        OpenAQ API key.
    target_years : tuple[int, ...]
        Calendar years to include in the coverage inventory.
    date_from : str
        Beginning of the API query period.
    date_to : str
        End of the API query period.

    Returns
    -------
    pandas.DataFrame
        Sensor-year coverage inventory.
    """
    rows = []

    session = requests.Session()

    total_sensors = len(sensor_inventory)

    for position, (_, sensor) in enumerate(
        sensor_inventory.iterrows(),
        start=1
    ):
        sensor_id = int(sensor["sensor_id"])

        cached_data = load_cached_response(sensor_id)

        if cached_data is not None:
            source = "cache"
            response_data = cached_data
        else:
            source = "api"

            response_data = fetch_sensor_years(
                session=session,
                api_key=api_key,
                sensor_id=sensor_id,
                date_from=date_from,
                date_to=date_to,
            )

            save_cached_response(
                sensor_id=sensor_id,
                response_data=response_data
            )

            # Stay safely below the free API request rate.
            time.sleep(REQUEST_INTERVAL_SECONDS)

        result_count = len(response_data.get("results", []))

        print(
            f"[{position:>3}/{total_sensors}] "
            f"sensor={sensor_id:<10} "
            f"source={source:<5} "
            f"year_records={result_count}"
        )

        rows.extend(
            flatten_sensor_coverage(
                sensor=sensor,
                response_data=response_data,
                target_years=target_years
            )
        )

    return pd.DataFrame(rows)


def print_summary(coverage: pd.DataFrame) -> None:
    """Print a summary of sensor-year coverage.

    Parameters
    ----------
    coverage : pandas.DataFrame
        Sensor-year coverage inventory.

    Returns
    -------
    None
    """
    print()
    print("Coverage inventory complete")
    print("---------------------------")

    print(f"Sensor-year rows: {len(coverage):,}")

    print(f"Rows with yearly data: {coverage['api_record_found'].sum():,}")

    print(
        f"Rows without yearly data: {(~coverage['api_record_found']).sum():,}"
    )

    print()
    print("Sensors with yearly records:")

    yearly_counts = (
        coverage[coverage["api_record_found"]]
        .groupby("year")["sensor_id"]
        .nunique()
    )

    print(yearly_counts.to_string())

    print()
    print("Coverage distribution by year:")

    coverage_summary = (
        coverage[coverage["api_record_found"]]
        .groupby("year")["percent_coverage"]
        .describe(percentiles=[0.10, 0.25, 0.50, 0.75, 0.90])
    )

    print(coverage_summary.to_string())


def main() -> None:
    """Build and save the sensor-year coverage inventory."""
    api_key = get_api_key()

    location_inventory = pd.read_csv(INVENTORY_INPUT_PATH)

    sensor_inventory = build_sensor_inventory(location_inventory)

    print(f"Unique target sensors: {len(sensor_inventory):,}")

    coverage = collect_coverage(
        sensor_inventory=sensor_inventory,
        api_key=api_key,
        target_years=TARGET_YEARS,
        date_from=START_DATE,
        date_to=END_DATE,
    )

    COVERAGE_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    coverage.to_csv(COVERAGE_OUTPUT_PATH, index=False)

    print_summary(coverage)


if __name__ == "__main__":
    main()
