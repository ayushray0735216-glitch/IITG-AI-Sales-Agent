"""Read-only Hunter masked people search for prospect previews."""

from __future__ import annotations

import json
import re
from difflib import get_close_matches
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SEARCH_URL = "https://api.hunter.io/v2/multi-domain-search"
REVEAL_URL = "https://api.hunter.io/v2/multi-domain-search/reveal"
DISCOVER_URL = "https://api.hunter.io/v2/discover"
PAGE_SIZE = 100
COMPANY_DISCOVERY_ENABLED = False


class ProspectSourceError(Exception):
    """A safe, user-facing search failure without provider response data."""


COUNTRY_CODES = {
    "india": "IN", "united states": "US", "united states of america": "US",
    "usa": "US", "united kingdom": "GB", "uk": "GB", "canada": "CA",
    "australia": "AU", "singapore": "SG", "united arab emirates": "AE",
    "uae": "AE", "germany": "DE", "france": "FR", "japan": "JP",
    "malaysia": "MY", "indonesia": "ID", "philippines": "PH",
    "bangladesh": "BD", "nepal": "NP", "sri lanka": "LK",
}
HEADCOUNT_BUCKETS = ((1, 10, "1-10"), (11, 50, "11-50"),
                     (51, 200, "51-200"), (201, 500, "201-500"),
                     (501, 1000, "501-1000"), (1001, 5000, "1001-5000"),
                     (5001, 10000, "5001-10000"), (10001, None, "10001+"))


def _industry_key(value: str) -> str:
    """Treat case, punctuation and a final plural as typing differences."""
    words = re.findall(r"[a-z0-9]+", value.casefold())
    return " ".join(word[:-1] if word.endswith("s") and len(word) > 3 else word
                    for word in words)


def _hunter_industries(values: list[str]) -> list[str]:
    """Send Hunter's canonical labels, including for previously saved searches."""
    with Path(__file__).with_name("hunter_industries.json").open(encoding="utf-8") as stream:
        catalog = json.load(stream)
    by_key = {}
    for label in catalog:
        by_key.setdefault(_industry_key(label), []).append(label)
    selected = []
    for value in values:
        matches = by_key.get(_industry_key(str(value).strip()), [])
        if len(matches) != 1:
            suggestions = get_close_matches(str(value), catalog, n=3, cutoff=0.6)
            hint = f" Try {', '.join(suggestions)}." if suggestions else ""
            raise ValueError(f"Hunter does not recognize the industry {value!r}.{hint}")
        if matches[0] not in selected:
            selected.append(matches[0])
    return selected


def _discover_error(exc: HTTPError) -> str:
    """Return Hunter's error category without exposing response data or credentials."""
    try:
        errors = json.load(exc).get("errors", [])
    except (ValueError, TypeError, OSError):
        errors = []
    ids = {str(item.get("id", "")) for item in errors if isinstance(item, dict)} if isinstance(errors, list) else set()
    if "invalid_industry" in ids:
        return "Hunter rejected an industry. Choose a label from Hunter's industry list."
    if "invalid_keywords" in ids or "invalid_filter_values" in ids:
        return "Hunter rejected a keyword or filter value. Check the saved search terms."
    if any(item.startswith("invalid_headquarters_location") for item in ids):
        return "Hunter rejected a saved company location. Check the country or city."
    if "invalid_headcount" in ids:
        return "Hunter rejected the company size filter. Check the employee range."
    if ids:
        return "Hunter rejected the company search (" + ", ".join(sorted(ids)) + ")."
    return "Hunter rejected a company filter. Check the saved search criteria."


def search_parameters(profile: dict, cursor: str | None = None,
                      valid_only: bool = True, limit: int = PAGE_SIZE,
                      domains: list[str] | None = None) -> dict:
    """Map supported saved criteria to Hunter's free masked search filters."""
    countries = profile.get("countries") or []
    if not countries:
        raise ValueError("Save at least one target country before searching.")
    codes = []
    for country in countries:
        normalized = str(country).strip().casefold()
        code = COUNTRY_CODES.get(normalized)
        if not code and len(normalized) == 2 and normalized.isalpha():
            code = normalized.upper()
        if not code:
            raise ValueError(f"Hunter needs a two-letter country code for {country!r}. Edit the saved country and try again.")
        if code not in codes:
            codes.append(code)
    if not 1 <= limit <= PAGE_SIZE:
        raise ValueError("Hunter search size must be between 1 and 100.")
    params = {"limit": limit, "type": "personal"}
    if domains is not None:
        if not domains or len(domains) > 25:
            raise ValueError("Choose between 1 and 25 company domains.")
        params["domain"] = ",".join(domains)
    else:
        params["location"] = ",".join(codes)
    if valid_only:
        params["verification_status"] = "valid"
    if profile.get("job_titles"):
        params["job_title"] = ",".join(profile["job_titles"])
    low, high = profile.get("size_min"), profile.get("size_max")
    if (low or high) and domains is None:
        buckets = [label for minimum, maximum, label in HEADCOUNT_BUCKETS
                   if (not high or minimum <= high) and (not low or maximum is None or maximum >= low)]
        if buckets:
            params["headcount"] = ",".join(buckets)
    if cursor:
        params["search_after"] = cursor
    return params


def _candidate(person: dict) -> dict:
    return {
        "name": str(person.get("name") or "Name unavailable"),
        "title": str(person.get("position") or ""),
        "company": str(person.get("company_name") or ""),
        "domain": str(person.get("domain") or ""),
        "email_available": bool(person.get("reveal_handle")),
        "verification": str((person.get("verification") or {}).get("status") or ""),
        "handle": str(person.get("reveal_handle") or ""),
        "decision_maker": bool(person.get("decision_maker")),
        "linkedin_exists": bool(person.get("linkedin_exists")),
    }


def search_people(profile: dict, api_key: str, cursor: str | None = None,
                  valid_only: bool = True, opener=urlopen, limit: int = PAGE_SIZE,
                  domains: list[str] | None = None) -> dict:
    """Return one masked page; never call Hunter's credit-charging reveal endpoint."""
    if not api_key or not api_key.strip():
        raise ProspectSourceError("Add HUNTER_API_KEY to enable searches.")
    params = search_parameters(profile, cursor, valid_only, limit, domains)
    request = Request(
        SEARCH_URL + "?" + urlencode(params), data=b"",
        headers={"Accept": "application/json", "X-API-KEY": api_key.strip()},
        method="POST",
    )
    try:
        with opener(request, timeout=15) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 401:
            raise ProspectSourceError("Hunter rejected the API key. Check HUNTER_API_KEY and restart Sharngon.") from None
        if exc.code == 403:
            raise ProspectSourceError("Hunter denied this search for the current account or rate limit.") from None
        if exc.code in (400, 422):
            raise ProspectSourceError("Hunter could not apply the saved filters. Try fewer job titles or a broader country search.") from None
        if exc.code == 429:
            raise ProspectSourceError("Hunter's search limit was reached. Try again later.") from None
        raise ProspectSourceError(f"Hunter search is unavailable (HTTP {exc.code}).") from None
    except (TimeoutError, URLError):
        raise ProspectSourceError("Hunter could not be reached. Try again shortly.") from None
    except (ValueError, TypeError):
        raise ProspectSourceError("Hunter returned a response Sharngon could not read.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProspectSourceError("Hunter returned an unexpected search response.")
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    people = [_candidate(item) for item in payload["data"] if isinstance(item, dict)]
    return {
        "people": people, "total": int(meta.get("results") or len(people)),
        "next_cursor": str(meta.get("next_search_after") or ""),
    }


def uses_company_discovery(profile: dict) -> bool:
    return COMPANY_DISCOVERY_ENABLED and any(
        profile.get(field) for field in ("states", "cities", "industries", "keywords")
    )


def discover_filters(profile: dict) -> dict:
    """Build exact Hunter company filters; reject locations it cannot apply."""
    countries = profile.get("countries") or []
    codes = []
    for country in countries:
        normalized = str(country).strip().casefold()
        code = COUNTRY_CODES.get(normalized)
        if not code and len(normalized) == 2 and normalized.isalpha():
            code = normalized.upper()
        if not code:
            raise ValueError(f"Use a two-letter country code for {country!r}.")
        codes.append(code)
    if not codes:
        raise ValueError("Save a target country before searching.")
    states = profile.get("states") or []
    cities = profile.get("cities") or []
    if states and (len(codes) != 1 or codes[0] != "US"):
        raise ValueError("Hunter can filter states only in the United States. Remove the state or specify target cities instead.")
    if states and any(len(str(state).strip()) != 2 for state in states):
        raise ValueError("For US state searches, use two-letter state codes such as CA or NY.")
    if cities:
        locations = [dict(country=code, city=city, **({"state": state.upper()} if states else {}))
                     for code in codes for city in cities for state in (states or [None])]
    elif states:
        locations = [dict(country="US", state=state.upper()) for state in states]
    else:
        locations = [dict(country=code) for code in codes]
    if len(locations) > 50:
        raise ValueError("Use at most 50 country, state and city combinations in one search.")
    filters = {"headquarters_location": {"include": locations}}
    if profile.get("industries"):
        filters["industry"] = {"include": _hunter_industries(profile["industries"])}
    if profile.get("keywords"):
        filters["keywords"] = {"match": "any", "include": profile["keywords"]}
    low, high = profile.get("size_min"), profile.get("size_max")
    if low or high:
        buckets = [label for minimum, maximum, label in HEADCOUNT_BUCKETS
                   if (not high or minimum <= high) and (not low or maximum is None or maximum >= low)]
        filters["headcount"] = buckets
    return filters


def discover_domains(profile: dict, api_key: str, opener=urlopen) -> tuple[list[str], int]:
    """Find up to 100 companies matching advanced filters without revealing email."""
    if not api_key or not api_key.strip():
        raise ProspectSourceError("Add HUNTER_API_KEY to enable searches.")
    body = json.dumps(discover_filters(profile)).encode("utf-8")
    request = Request(DISCOVER_URL + "?limit=100", data=body,
                      headers={"Accept": "application/json", "Content-Type": "application/json",
                               "X-API-KEY": api_key.strip()}, method="POST")
    try:
        with opener(request, timeout=15) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code in (400, 422):
            raise ProspectSourceError(_discover_error(exc)) from None
        if exc.code in (401, 403):
            raise ProspectSourceError("Hunter rejected access to company discovery. Check the API key and account permissions.") from None
        if exc.code == 429:
            raise ProspectSourceError("Hunter's discovery limit was reached. Try again later.") from None
        raise ProspectSourceError(f"Hunter company discovery is unavailable (HTTP {exc.code}).") from None
    except (TimeoutError, URLError):
        raise ProspectSourceError("Hunter company discovery could not be reached. Try again shortly.") from None
    except (TypeError, ValueError):
        raise ProspectSourceError("Hunter returned a company response Sharngon could not read.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProspectSourceError("Hunter returned an unexpected company response.")
    domains = list(dict.fromkeys(str(item.get("domain") or "").strip().lower()
                                 for item in payload["data"] if isinstance(item, dict)))
    domains = [domain for domain in domains if domain]
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    try:
        total = int(meta.get("results") or len(domains))
    except (TypeError, ValueError):
        total = len(domains)
    return domains[:100], total


def search_profile(profile: dict, api_key: str, cursor: str | None = None,
                   valid_only: bool = True, limit: int = PAGE_SIZE,
                   opener=urlopen) -> dict:
    """Apply all saved provider-supported criteria before showing people."""
    if not uses_company_discovery(profile):
        return search_people(profile, api_key, cursor, valid_only, opener, limit)
    if cursor and not re.fullmatch(r"company:(?:[0-9]|[1-9][0-9])(?::[A-Za-z0-9_+/=-]{1,1000})?", cursor):
        raise ValueError("The saved company search position is invalid. Start again.")
    domains, company_total = discover_domains(profile, api_key, opener)
    parts = cursor.split(":", 2) if cursor else []
    company_index = int(parts[1]) if parts else 0
    person_cursor = parts[2] if len(parts) == 3 else None
    if company_index >= len(domains):
        return {"people": [], "total": 0, "next_cursor": "",
                "coverage_note": f"No people found in the first {len(domains)} matching companies."}
    result = search_people(profile, api_key, person_cursor, valid_only, opener, limit,
                           [domains[company_index]])
    hunter_next = result.get("next_cursor")
    if hunter_next:
        result["next_cursor"] = f"company:{company_index}:{hunter_next}"
    else:
        result["next_cursor"] = f"company:{company_index + 1}" if company_index + 1 < len(domains) else ""
    result["coverage_note"] = (
        f"Company filters applied to {len(domains)} of {company_total} matching companies available on this Hunter plan; "
        f"showing company {company_index + 1} of {len(domains)}."
    )
    return result


def _words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def qualify_people(people: list[dict], profile: dict, strict_role: bool = True,
                   company_limit: int = 2) -> tuple[list[dict], dict[str, int]]:
    """Review one page without confusing Hunter's fuzzy title matches with exact roles."""
    titles = [_words(title) for title in profile.get("job_titles", []) if _words(title)]
    exclusions = [_words(term) for term in profile.get("exclusions", []) if _words(term)]
    visible, counts, domains, seen = [], {"role": 0, "excluded": 0, "repeated": 0}, {}, set()
    for person in people:
        title = _words(person.get("title", ""))
        if strict_role and titles and not any(f" {wanted} " in f" {title} " for wanted in titles):
            counts["role"] += 1
            continue
        haystack = _words(" ".join((person.get("name", ""), person.get("title", ""), person.get("company", ""))))
        if any(f" {term} " in f" {haystack} " for term in exclusions):
            counts["excluded"] += 1
            continue
        handle = person.get("handle")
        identity = handle or (person.get("name"), title, person.get("domain"))
        domain = (person.get("domain") or person.get("company") or "Unknown").casefold()
        if identity in seen or domains.get(domain, 0) >= company_limit:
            counts["repeated"] += 1
            continue
        seen.add(identity)
        domains[domain] = domains.get(domain, 0) + 1
        visible.append(person)
    return visible, counts


def reveal_emails(handles: list[str], api_key: str, opener=urlopen) -> dict:
    """Reveal only staff-selected handles; Hunter may charge one credit per personal email."""
    if not 1 <= len(handles) <= 10 or len(set(handles)) != len(handles) or not all(handles):
        raise ValueError("Choose 1 to 10 distinct candidates to reveal.")
    if not api_key or not api_key.strip():
        raise ProspectSourceError("Add HUNTER_API_KEY to reveal selected emails.")
    request = Request(
        REVEAL_URL, data=json.dumps({"handles": handles}).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "X-API-KEY": api_key.strip()}, method="POST",
    )
    try:
        with opener(request, timeout=20) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ProspectSourceError("Hunter rejected the key or access to email reveal.") from None
        if exc.code == 429:
            raise ProspectSourceError("Hunter could not reveal these emails with the available credits.") from None
        raise ProspectSourceError(f"Hunter email reveal failed (HTTP {exc.code}).") from None
    except (TimeoutError, URLError):
        raise ProspectSourceError("Hunter could not be reached. Try again shortly.") from None
    except (ValueError, TypeError):
        raise ProspectSourceError("Hunter returned a reveal response Sharngon could not read.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProspectSourceError("Hunter returned an unexpected reveal response.")
    rows = {}
    details = {}
    for row in payload["data"]:
        if not isinstance(row, dict) or not row.get("email") or row.get("reveal_handle") not in handles:
            continue
        handle = str(row["reveal_handle"])
        rows[handle] = str(row["email"])
        details[handle] = {
            "name": " ".join(str(row.get(field) or "").strip() for field in ("first_name", "last_name")).strip(),
            "title": str(row.get("position") or "").strip(),
            "phone": str(row.get("phone_number") or "").strip(),
            "linkedin": str(row.get("linkedin_url") or "").strip(),
        }
    meta = payload.get("meta") or {}
    return {"emails": rows, "details": details, "credits_charged": (meta.get("credits_charged") or 0)
            if isinstance(meta, dict) else 0}
