"""Build an inventoru of CPCB monitoring locations from OpenAQ metadata"""

import json
import os
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

OPENAQ_LOCATIONS_URL = "https://api.openaq.org/v3/locations"

COUNTRY_ID = 9  # India
PROVIDER_ID = 168  # CPCB
PRIMARY_PARAMETER = "pm25"

ENV_PATH = Path(".env")
RAW_OUTPUT_PATH = Path("data/raw/locations.json")
INVENTORY_OUTPUT_PATH = Path("data/processed/station_inventory.csv")


# ----------------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------------


def get_api_key(env_path: Path = ENV_PATH) -> str:
    """Load the OpenAQ SPI key from a local environment file.

    Parameters
    ----------
    env_path : pathlib.Path, optional
        Path to the environment file containing OPENAQ_API_KEY.
        Defaults to ".env"

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
# Data acquisition
# ----------------------------------------------------------------------------


def fetch_locations(
    api_key: str,
    country_id: int,
    provider_id: int,
    limit: int = 1000,
    timeout: int = 30,
) -> list[dict]:
    """Fetch monitoring locations from OpenAQ.

    Parameters
    ----------
    api_key : str
        OpenAQ API key.
    country_id : int
        OpenAQ country identifier used to filter locations.
    provider_id : int
        OpenAQ provider identifier used to filter locations.
    limit : int, optional
        Maximum number of locations requested per API page.
        Defaults to 1000.
    timeout : int, optional
        Maximum number of seconds to wait for each API request.
        Defaults to 30 seconds.

    Returns
    -------
    list[dict]
        Monitoring-location records returned by OpenAQ.

    Raises
    ------
    requests.HTTPError
        If an OpenAQ API request returns an unsuccessful response.
    ValueError
        If no monitoring locations are returned.
    """
    locations = []
    page = 1

    while True:
        print(f"Fetching locations page {page}...")

        params: dict[str, str | int] = {
            "countries_id": country_id,
            "providers_id": provider_id,
            "limit": limit,
            "page": page,
            "order_by": "id",
            "sort_order": "asc",
        }
        response = requests.get(
            OPENAQ_LOCATIONS_URL,
            headers={"X-API-Key": api_key},
            params=params,
            timeout=timeout,
        )

        response.raise_for_status()

        data = response.json()
        results = data.get("results", [])

        print(f"Locations returned: {len(results):,}")

        if not results:
            break

        locations.extend(results)

        if len(results) < limit:
            break

        page += 1

    if not locations:
        raise ValueError(
            "OpenAQ returned no locations for the requested "
            "country and provider."
        )

    return locations


# ----------------------------------------------------------------------------
# Metadata extraction
# ----------------------------------------------------------------------------


def get_parameter_names(location: dict) -> list[str]:
    """Extract unique parameter names from a location record.

    Parameters
    ----------
    location : dict
        OpenAQ location record.

    Returns
    -------
    list[str]
        Sorted unique parameter names available at the location.
    """
    parameter_names = {
        sensor["parameter"]["name"]
        for sensor in location.get("sensors") or []
        if sensor.get("parameter", {}).get("name")
    }

    return sorted(parameter_names)


def get_sensor_ids(
    location: dict,
    parameter: str,
) -> list[int]:
    """Extract sensor IDs for a parameter from a location record.

    Parameters
    ----------
    location : dict
        OpenAQ location record.
    parameter : str
        Parameter name to match, such as "pm25", "pm10", or "no2".

    Returns
    -------
    list[int]
        Sorted sensor IDs associated with the requested parameter.
    """
    sensor_ids = []

    for sensor in location.get("sensors", []):
        sensor_parameter = sensor.get("parameter") or {}

        if sensor_parameter.get("name") == parameter:
            sensor_ids.append(sensor["id"])

    return sorted(sensor_ids)


# ----------------------------------------------------------------------------
# Data transformation
# ----------------------------------------------------------------------------


def flatten_locations(
    locations: list[dict],
    target_parameter: str
) -> pd.DataFrame:
    """Convert OpenAQ location metadata into a tabular inventory.

    Parameters
    ----------
    locations : list[dict]
        Monitoring-location records returned by OpenAQ.
    target_parameter : str
        Parameter of primary interest for the analysis.

    Returns
    -------
    pandas.DataFrame
        One row per OpenAQ monitoring location.
    """
    rows = []

    for location in locations:
        coordinates = location.get("coordinates") or {}
        owner = location.get("owner") or {}
        provider = location.get("provider") or {}
        country = location.get("country") or {}
        datetime_first = location.get("datetimeFirst") or {}
        datetime_last = location.get("datetimeLast") or {}

        parameter_names = get_parameter_names(location)

        target_sensor_ids = get_sensor_ids(
            location=location,
            parameter=target_parameter,
        )

        rows.append(
            {
                "location_id": location["id"],
                "station_name": location.get("name"),
                "locality": location.get("locality"),
                "latitude": coordinates.get("latitude"),
                "longitude": coordinates.get("longitude"),
                "timezone": location.get("timezone"),
                "country_id": country.get("id"),
                "country_code": country.get("code"),
                "country_name": country.get("name"),
                "owner_id": owner.get("id"),
                "owner_name": owner.get("name"),
                "provider_id": provider.get("id"),
                "provider_name": provider.get("name"),
                "is_mobile": location.get("isMobile"),
                "is_monitor": location.get("isMonitor"),
                "datetime_first_utc": datetime_first.get("utc"),
                "datetime_last_utc": datetime_last.get("utc"),
                "sensor_count": len(location.get("sensors") or []),
                "target_parameter": target_parameter,
                "has_target_parameter": bool(target_sensor_ids),
                "target_sensor_count": len(target_sensor_ids),
                "target_sensor_ids": ",".join(
                    str(sensor_id) for sensor_id in target_sensor_ids
                ),
                "available_parameters": ",".join(parameter_names),
            }
        )

    inventory = pd.DataFrame(rows)

    inventory["datetime_first_utc"] = pd.to_datetime(
        inventory["datetime_first_utc"], utc=True, errors="coerce"
    )

    inventory["datetime_last_utc"] = pd.to_datetime(
        inventory["datetime_last_utc"], utc=True, errors="coerce"
    )

    return inventory.sort_values([
        "station_name",
        "location_id"
    ]).reset_index(drop=True)


# ----------------------------------------------------------------------------
# output
# ----------------------------------------------------------------------------


def save_outputs(
    locations: list[dict],
    inventory: pd.DataFrame,
    raw_output_path: Path = RAW_OUTPUT_PATH,
    inventory_output_path: Path = INVENTORY_OUTPUT_PATH,
) -> None:
    """Save raw location metadata and the flattened inventory.

    Parameters
    ----------
    locations : list[dict]
        Raw monitoring-location records returned by OpenAQ.
    inventory : pandas.DataFrame
        Flattened monitoring-station inventory.
    raw_output_path : pathlib.Path, optional
        Destination for raw location metadata.
        Defaults to RAW_OUTPUT_PATH.
    inventory_output_path : pathlib.Path, optional
        Destination for the processed station inventory.
        Defaults to INVENTORY_OUTPUT_PATH.

    Returns
    -------
    None
    """
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)

    inventory_output_path.parent.mkdir(parents=True, exist_ok=True)

    raw_output_path.write_text(
        json.dumps(locations, indent=2),
        encoding="utf-8"
    )

    inventory.to_csv(inventory_output_path, index=False)


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------


def print_summary(inventory: pd.DataFrame, target_parameter: str) -> None:
    """Print a summary of the monitoring-station inventory.

    Parameters
    ----------
    inventory : pandas.DataFrame
        Flattened monitoring-station inventory.
    target_parameter : str
        Parameter used to identify relevant sensors.

    Returns
    -------
    None
    """
    target_inventory = inventory[inventory["has_target_parameter"]]

    print()
    print(f"Locations: {len(inventory):,}")

    print(
        f"Locations with {target_parameter} sensors: "
        f"{len(target_inventory):,}"
    )

    print(
        f"Locations with multiple {target_parameter} sensors: "
        f"{(target_inventory['target_sensor_count'] > 1).sum():,}"
    )

    print(
        "Locations missing datetimeLast: "
        f"{inventory['datetime_last_utc'].isna().sum():,}"
    )

    print()
    print(f"Most recently reporting {target_parameter} locations:")

    columns = [
        "location_id",
        "station_name",
        "provider_name",
        "datetime_last_utc",
        "target_sensor_count",
    ]

    recent_locations = target_inventory.sort_values(
        "datetime_last_utc", ascending=False, na_position="last"
    ).head(10)

    print(recent_locations[columns].to_string(index=False))


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def main() -> None:
    """Build and save the monitoring-station inventory."""
    api_key = get_api_key()

    locations = fetch_locations(
        api_key=api_key, country_id=COUNTRY_ID, provider_id=PROVIDER_ID
    )

    inventory = flatten_locations(
        locations=locations, target_parameter=PRIMARY_PARAMETER
    )

    save_outputs(locations=locations, inventory=inventory)

    print_summary(inventory=inventory, target_parameter=PRIMARY_PARAMETER)


if __name__ == "__main__":
    main()
