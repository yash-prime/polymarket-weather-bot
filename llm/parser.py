"""
llm/parser.py — Market question parser with 3-path fallback.

Converts a raw Polymarket question string into a structured JSON dict
that the Weather Engine can act on.

Fallback chain
--------------
Path 1 — Ollama (Llama 3.1 8B, local):
    Prompts the model to extract city, lat, lon, metric, threshold, operator,
    window dates, aggregation, and resolution source.

Path 2 — Regex:
    Handles common English patterns for the most frequent question types:
      "Will <city> exceed X°F ..."
      "Will <city> reach/hit X°F ..."
      "Will <city> see above/below X inches ..."
    parse_status is set to "regex_fallback".

Path 3 — Failure:
    Market is flagged parse_status="failed" and skipped by the scanner.

Caching
-------
Successful parses (both Ollama and regex) are stored in the llm_cache table
keyed by sha256(question). Repeated questions (e.g. recurring markets) are
served from cache without another Ollama call.

DB writes
---------
This module writes to llm_cache only. The scanner writes to the markets table.
"""
import hashlib
import json
import logging
import re
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a weather market parser. Extract structured data from Polymarket
weather prediction questions and return ONLY valid JSON with these fields:
{
  "city": string (city name),
  "lat": float (decimal degrees, use known lat for the city),
  "lon": float (decimal degrees, use known lon for the city),
  "metric": one of ["temperature_2m_max", "temperature_2m_min", "precipitation_sum", "wind_speed_10m_max"],
  "threshold": float (the numeric value in the question),
  "unit": "fahrenheit" | "celsius" | "inches" | "mm" | "mph" | "kmh",
  "operator": ">" | ">=" | "<" | "<=" | "==",
  "window_start": "YYYY-MM-DD",
  "window_end": "YYYY-MM-DD",
  "aggregation": "any" | "all" | "total",
  "resolution_source": "nws_official" | "metar" | "model_grid" | "unknown",
  "parse_status": "success"
}
Return only the JSON object, no explanation."""

# Regex patterns for the most common question formats
# Group names: city, threshold, unit, direction, month, day, year
_PATTERNS = [
    # "Will Chicago reach 90°F on June 15, 2026?"
    # "Will Miami exceed 95 degrees Fahrenheit on July 4?"
    re.compile(
        r"Will (?P<city>[A-Za-z\s]+?) "
        r"(?:exceed|reach|hit|top|surpass) "
        r"(?P<threshold>\d+(?:\.\d+)?)\s*°?\s*(?P<unit>[FfCc]|degrees?\s*(?:fahrenheit|celsius|F|C))",
        re.IGNORECASE,
    ),
    # "Will Phoenix be above 110°F ..."
    re.compile(
        r"Will (?P<city>[A-Za-z\s]+?) "
        r"be (?P<direction>above|below|over|under) "
        r"(?P<threshold>\d+(?:\.\d+)?)\s*°?\s*(?P<unit>[FfCc]|degrees?\s*(?:fahrenheit|celsius|F|C))",
        re.IGNORECASE,
    ),
    # "Will Seattle see more than 2 inches of rain ..."
    re.compile(
        r"Will (?P<city>[A-Za-z\s]+?) "
        r"(?:see|receive|get) (?:more than|at least|over) "
        r"(?P<threshold>\d+(?:\.\d+)?) (?:inch(?:es)?|in\.?)(?:\s+of rain)?",
        re.IGNORECASE,
    ),
    # "Will Boston temperatures fall below 32°F ..."
    re.compile(
        r"Will (?P<city>[A-Za-z\s]+?) (?:temperatures? )?(?:fall|drop) below "
        r"(?P<threshold>\d+(?:\.\d+)?)\s*°?\s*(?P<unit>[FfCc]|degrees?\s*(?:fahrenheit|celsius|F|C))",
        re.IGNORECASE,
    ),
]

# Known city → (lat, lon) for regex path (best-effort, not exhaustive)
_CITY_COORDS: dict[str, tuple[float, float]] = {
    "chicago": (41.88, -87.63),
    "new york": (40.71, -74.00),
    "new york city": (40.71, -74.00),
    "nyc": (40.71, -74.00),
    "los angeles": (34.05, -118.24),
    "la": (34.05, -118.24),
    "houston": (29.76, -95.37),
    "phoenix": (33.45, -112.07),
    "philadelphia": (39.95, -75.17),
    "san antonio": (29.42, -98.49),
    "san diego": (32.72, -117.16),
    "dallas": (32.78, -96.80),
    "miami": (25.77, -80.19),
    "atlanta": (33.75, -84.39),
    "seattle": (47.61, -122.33),
    "denver": (39.74, -104.98),
    "boston": (42.36, -71.06),
    "washington": (38.89, -77.04),
    "washington dc": (38.89, -77.04),
    "las vegas": (36.17, -115.14),
    "portland": (45.52, -122.68),
    "nashville": (36.17, -86.78),
    "memphis": (35.15, -90.05),
    "louisville": (38.25, -85.76),
    "baltimore": (39.29, -76.61),
    "milwaukee": (43.04, -87.91),
    "albuquerque": (35.08, -106.65),
    "tucson": (32.22, -110.97),
    "fresno": (36.74, -119.77),
    "sacramento": (38.58, -121.49),
    "kansas city": (39.10, -94.58),
    "mesa": (33.42, -111.82),
    "omaha": (41.26, -95.93),
    "raleigh": (35.78, -78.64),
    "colorado springs": (38.83, -104.82),
    "minneapolis": (44.98, -93.27),
    "cleveland": (41.50, -81.69),
    "wichita": (37.69, -97.34),
    "arlington": (32.74, -97.11),
    "tampa": (27.95, -82.46),
    "new orleans": (29.95, -90.07),
    "aurora": (39.73, -104.83),
    "anaheim": (33.84, -117.91),
    "corpus christi": (27.80, -97.40),
    "pittsburgh": (40.44, -79.99),
    "st. louis": (38.63, -90.20),
    "st louis": (38.63, -90.20),
    "cincinnati": (39.10, -84.51),
    "henderson": (36.04, -114.98),
    "greensboro": (36.07, -79.79),
    "plano": (33.02, -96.70),
    "newark": (40.74, -74.17),
    "norfolk": (36.85, -76.29),
    "orlando": (28.54, -81.38),
    "jacksonville": (30.33, -81.66),
    "detroit": (42.33, -83.05),
    "indianapolis": (39.77, -86.16),
    "columbus": (39.96, -82.99),
    "austin": (30.27, -97.74),
    "charlotte": (35.23, -80.84),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse(question: str, db_path: str | None = None) -> dict[str, Any]:
    """
    Parse a Polymarket weather question into a structured dict.

    Returns a dict with at minimum:
      parse_status: "success" | "regex_fallback" | "failed"

    On success or regex_fallback, also includes:
      city, lat, lon, metric, threshold, operator, window_start, ...

    On failed, returns: {"parse_status": "failed"}
    """
    question_hash = hashlib.sha256(question.encode()).hexdigest()

    # --- Cache hit ---
    cached = _load_cache(question_hash, db_path)
    if cached is not None:
        logger.debug("parser: cache HIT for question hash %s", question_hash[:8])
        return cached

    # --- Path 1: Ollama ---
    result = _try_ollama(question)

    # --- Path 2: Regex fallback ---
    if result is None:
        result = _try_regex(question)

    # --- Path 3: Failure ---
    if result is None:
        logger.warning("parser: all paths failed for question: %.80s", question)
        return {"parse_status": "failed"}

    # Cache the successful parse
    _save_cache(question_hash, question, result, db_path)

    return result


# ---------------------------------------------------------------------------
# Internal — Ollama path
# ---------------------------------------------------------------------------


def _try_ollama(question: str) -> dict[str, Any] | None:
    """
    Attempt to parse the question using the local Ollama model.

    Returns parsed dict on success, None on any failure.
    """
    try:
        from llm.ollama_client import OllamaUnavailableError, generate

        prompt = f"Parse this Polymarket weather question:\n\n{question}"
        raw = generate(prompt, system=_SYSTEM_PROMPT)

        # Extract JSON from the response (model may include markdown fences)
        json_str = _extract_json(raw)
        if json_str is None:
            logger.warning("parser: Ollama response contained no valid JSON")
            return None

        data = json.loads(json_str)
        data["parse_status"] = "success"
        _validate_parsed(data)
        return data

    except (OllamaUnavailableError, ImportError):
        logger.debug("parser: Ollama unavailable — falling back to regex")
        return None
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("parser: Ollama parse failed: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("parser: Ollama unexpected error: %s", exc)
        return None


def _extract_json(text: str) -> str | None:
    """Extract a JSON object string from a possibly markdown-wrapped response."""
    # Strip code fences
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    # Find the first complete {...} block
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _validate_parsed(data: dict) -> None:
    """Raise ValueError if required fields are missing or clearly wrong."""
    required = ("lat", "lon", "metric", "threshold", "operator", "window_start")
    for field in required:
        if field not in data or data[field] is None:
            raise ValueError(f"Missing required field: {field}")
    if not (-90 <= float(data["lat"]) <= 90):
        raise ValueError(f"lat out of range: {data['lat']}")
    if not (-180 <= float(data["lon"]) <= 180):
        raise ValueError(f"lon out of range: {data['lon']}")


# ---------------------------------------------------------------------------
# Internal — Regex fallback path
# ---------------------------------------------------------------------------


def _try_regex(question: str) -> dict[str, Any] | None:
    """
    Attempt to parse common question patterns using regular expressions.

    Returns parsed dict with parse_status="regex_fallback" on success, else None.
    """
    for pattern in _PATTERNS:
        m = pattern.search(question)
        if not m:
            continue

        groups = m.groupdict()
        city_raw = groups.get("city", "").strip().rstrip("'s").strip()
        city_key = city_raw.lower()

        coords = _CITY_COORDS.get(city_key)
        if coords is None:
            # Try partial match
            for known, c in _CITY_COORDS.items():
                if city_key in known or known in city_key:
                    coords = c
                    city_key = known
                    break

        if coords is None:
            logger.debug("parser: regex matched but city '%s' coords unknown", city_raw)
            continue

        lat, lon = coords
        threshold = float(groups.get("threshold", 0))
        unit_raw = groups.get("unit", "F").upper()
        direction = groups.get("direction", "exceed").lower()

        metric, operator, unit = _infer_metric_operator(pattern, groups, direction, unit_raw)

        # Extract date from question (best-effort)
        window_start, window_end = _extract_dates(question)

        result: dict[str, Any] = {
            "city": city_raw,
            "lat": lat,
            "lon": lon,
            "metric": metric,
            "threshold": threshold,
            "unit": unit,
            "operator": operator,
            "window_start": window_start,
            "window_end": window_end,
            "aggregation": "any",
            "resolution_source": "unknown",
            "parse_status": "regex_fallback",
        }
        return result

    return None


def _infer_metric_operator(
    pattern: re.Pattern,
    groups: dict[str, str | None],
    direction: str,
    unit_raw: str,
) -> tuple[str, str, str]:
    """Infer metric, operator, and unit from a regex match."""
    pattern_src = pattern.pattern

    if "inch" in pattern_src or "rain" in pattern_src:
        return "precipitation_sum", ">=", "inches"

    # Temperature-based
    if "F" in unit_raw or "FAHRENHEIT" in unit_raw:
        unit = "fahrenheit"
    elif "C" in unit_raw or "CELSIUS" in unit_raw:
        unit = "celsius"
    else:
        unit = "fahrenheit"

    if "below" in direction or "under" in direction or "fall" in pattern_src:
        return "temperature_2m_min", "<", unit

    return "temperature_2m_max", ">", unit


def _extract_dates(question: str) -> tuple[str, str]:
    """
    Attempt to extract a date window from the question text.

    Returns (window_start, window_end) as "YYYY-MM-DD" strings.
    Falls back to today + 7 days if no date found.
    """
    from datetime import date, timedelta

    today = date.today()

    # Try to find "Month Day, Year" or "Month Day Year"
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    date_pattern = re.compile(
        r"(?P<month>" + "|".join(months) + r")\s+"
        r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
        r"(?:[,\s]+(?P<year>\d{4}))?",
        re.IGNORECASE,
    )
    m = date_pattern.search(question)
    if m:
        month_str = m.group("month").lower()
        month = months[month_str]
        day = int(m.group("day"))
        year = int(m.group("year")) if m.group("year") else today.year
        try:
            target = date(year, month, day)
            return str(target), str(target)
        except ValueError:
            pass

    # Fallback: 7 days from today
    return str(today), str(today + timedelta(days=7))


# ---------------------------------------------------------------------------
# Internal — Cache helpers
# ---------------------------------------------------------------------------


def _load_cache(question_hash: str, db_path: str | None) -> dict[str, Any] | None:
    """Load a cached parse result from the llm_cache table."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT parsed FROM llm_cache WHERE question_hash = ?",
                (question_hash,),
            ).fetchone()

        if row and row["parsed"]:
            return json.loads(row["parsed"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("parser: cache read failed: %s", exc)
    return None


def _save_cache(
    question_hash: str,
    question: str,
    result: dict[str, Any],
    db_path: str | None,
) -> None:
    """Save a parsed result to the llm_cache table."""
    try:
        from db.init import get_connection

        with get_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO llm_cache (question_hash, question, parsed, parse_status, model)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(question_hash) DO UPDATE SET
                    parsed       = excluded.parsed,
                    parse_status = excluded.parse_status,
                    model        = excluded.model
                """,
                (
                    question_hash,
                    question,
                    json.dumps(result),
                    result.get("parse_status", "success"),
                    settings.OLLAMA_MODEL,
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("parser: cache write failed: %s", exc)
