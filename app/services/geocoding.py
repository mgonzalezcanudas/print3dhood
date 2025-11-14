from __future__ import annotations

import httpx

from dataclasses import dataclass

import httpx

from ..config import get_settings
from ..models import GeocodeResult


@dataclass
class GeocodingError(Exception):
    message: str
    status_code: int = 503

    def __str__(self) -> str:  # pragma: no cover - simple helper
        return self.message


async def search_address(query: str, limit: int = 5) -> list[GeocodeResult]:
    if not query.strip():
        return []

    settings = get_settings()
    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
    }

    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.overpass_timeout,
        ) as client:
            response = await client.get(settings.nominatim_url, params=params)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:  # pragma: no cover - thin wrapper
        status = exc.response.status_code
        if status == 403:
            raise GeocodingError(
                "Geocoding request rejected (HTTP 403). Update USER_AGENT in your settings to a unique, contactable identifier.",
                status_code=503,
            ) from exc
        if status == 429:
            raise GeocodingError(
                "Geocoding service rate-limited the request (HTTP 429). Please wait a few seconds before trying again.",
                status_code=503,
            ) from exc
        raise GeocodingError(
            f"Geocoding request failed with status {status}.", status_code=503
        ) from exc
    except httpx.RequestError as exc:  # pragma: no cover
        raise GeocodingError("Unable to reach the geocoding service.") from exc

    payload = response.json()
    results: list[GeocodeResult] = []
    for item in payload:
        try:
            results.append(
                GeocodeResult(
                    display_name=item.get("display_name", "Unknown location"),
                    latitude=float(item["lat"]),
                    longitude=float(item["lon"]),
                )
            )
        except (KeyError, ValueError):
            continue
    return results
