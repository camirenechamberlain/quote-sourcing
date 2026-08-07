from __future__ import annotations

import os
import time

import requests

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

SEARCH_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location"
DETAILS_FIELD_MASK = "id,displayName,formattedAddress,nationalPhoneNumber,websiteUri,rating,userRatingCount"

MAX_PAGES = 3
PAGE_TOKEN_DELAY_S = 2  # Google requires a short delay before a next_page_token becomes valid


class PlacesApiError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        raise PlacesApiError("GOOGLE_PLACES_API_KEY is not set in the environment (.env)")
    return key


def geocode(location_text: str) -> tuple[float, float]:
    resp = requests.get(
        GEOCODE_URL,
        params={"address": location_text, "key": _api_key()},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK" or not data.get("results"):
        raise PlacesApiError(f"Geocoding failed for '{location_text}': {data.get('status')}")
    loc = data["results"][0]["geometry"]["location"]
    return loc["lat"], loc["lng"]


def search_companies(category: str, lat: float, lng: float, radius_m: int) -> list[dict]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": SEARCH_FIELD_MASK,
    }
    body = {
        "textQuery": category,
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": min(radius_m, 50000),  # Places API caps bias radius at 50km
            }
        },
    }

    results: list[dict] = []
    page_token: str | None = None
    for _ in range(MAX_PAGES):
        if page_token:
            body = {"textQuery": category, "pageToken": page_token}
            time.sleep(PAGE_TOKEN_DELAY_S)
        resp = requests.post(SEARCH_TEXT_URL, json=body, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for place in data.get("places", []):
            results.append(
                {
                    "place_id": place.get("id"),
                    "name": place.get("displayName", {}).get("text"),
                    "address": place.get("formattedAddress"),
                }
            )
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return results


def get_place_details(place_id: str) -> dict:
    headers = {
        "X-Goog-Api-Key": _api_key(),
        "X-Goog-FieldMask": DETAILS_FIELD_MASK,
    }
    resp = requests.get(PLACE_DETAILS_URL.format(place_id=place_id), headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return {
        "phone": data.get("nationalPhoneNumber"),
        "website": data.get("websiteUri"),
        "rating": data.get("rating"),
    }
