"""Read-only Apollo People Search adapter for prospect discovery previews."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
PAGE_SIZE = 25


class ProspectSourceError(Exception):
    """A safe, user-facing search failure without provider response data."""


def search_parameters(profile: dict, page: int = 1) -> dict:
    """Map a saved search to documented Apollo People Search filters."""
    if not isinstance(page, int) or not 1 <= page <= 20:
        raise ValueError("Choose a page between 1 and 20.")
    locations = profile.get("cities") or profile.get("states") or profile.get("countries") or []
    if not locations:
        raise ValueError("Save at least one target region before searching.")
    params = {"person_locations": locations, "page": page, "per_page": PAGE_SIZE}
    if profile.get("job_titles"):
        params["person_titles"] = profile["job_titles"]
    if profile.get("industries"):
        # Apollo requires exact, lowercase industry names.
        params["organization_industries"] = [name.casefold() for name in profile["industries"]]
    if profile.get("keywords"):
        params["q_keywords"] = " ".join(profile["keywords"])
    low, high = profile.get("size_min"), profile.get("size_max")
    if low or high:
        params["organization_num_employees_ranges"] = [f"{int(low or 1)},{int(high or 1000000)}"]
    return params


def _candidate(person: dict) -> dict:
    organization = person.get("organization") or {}
    if not isinstance(organization, dict):
        organization = {}
    name = " ".join(str(person.get(key) or "").strip() for key in ("first_name", "last_name_obfuscated")).strip()
    return {
        "source": "Apollo",
        "source_id": str(person.get("id") or ""),
        "name": name or "Name unavailable",
        "title": str(person.get("title") or ""),
        "company": str(organization.get("name") or ""),
        "email_available": bool(person.get("has_email")),
    }


def search_people(profile: dict, api_key: str, page: int = 1, opener=urlopen) -> dict:
    """Search a single page; return only limited person fields, with no CRM writes."""
    if not api_key or not api_key.strip():
        raise ProspectSourceError("Add an Apollo API key to enable searches.")
    params = search_parameters(profile, page)
    request = Request(
        SEARCH_URL,
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "x-api-key": api_key.strip()},
        method="POST",
    )
    try:
        with opener(request, timeout=15) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ProspectSourceError("Apollo rejected the API key or People Search access. Check the key and its permissions.") from None
        if exc.code == 422:
            raise ProspectSourceError("Apollo could not apply these filters. Check industry names and search criteria.") from None
        if exc.code == 429:
            raise ProspectSourceError("Apollo's search limit was reached. Try again later.") from None
        raise ProspectSourceError(f"Apollo search is unavailable (HTTP {exc.code}).") from None
    except (TimeoutError, URLError):
        raise ProspectSourceError("Apollo could not be reached. Try again shortly.") from None
    except (ValueError, TypeError):
        raise ProspectSourceError("Apollo returned a response Sharngon could not read.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("people"), list):
        raise ProspectSourceError("Apollo returned an unexpected search response.")
    people = [_candidate(person) for person in payload["people"] if isinstance(person, dict)]
    # A search result may have no source ID, but cannot be reliably reviewed without one.
    people = [person for person in people if person["source_id"]]
    return {"people": people, "total": int(payload.get("total_entries") or 0), "page": page, "filters": params}
