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
  "city": string (city or region name; for hurricanes use the most relevant coastal city),
  "lat": float (decimal degrees, use known lat for the city/region),
  "lon": float (decimal degrees, use known lon for the city/region),
  "metric": one of ["temperature_2m_max", "temperature_2m_min", "precipitation_sum", "wind_speed_10m_max"],
  "threshold": float (the lower bound for 'between' questions, otherwise the single threshold; for hurricane/storm questions use 0.0),
  "threshold_high": float or null (upper bound for 'between' questions, null for single-threshold questions),
  "unit": "fahrenheit" | "celsius" | "inches" | "mm" | "mph" | "kmh",
  "operator": ">" | ">=" | "<" | "<=" | "==" | "between",
  "window_start": "YYYY-MM-DD",
  "window_end": "YYYY-MM-DD",
  "aggregation": "any" | "all" | "total",
  "resolution_source": "nws_official" | "metar" | "model_grid" | "unknown",
  "market_type": "temperature" | "precipitation" | "wind" | "hurricane" | "storm" | "tornado" | "other",
  "parse_status": "success"
}
For 'between X and Y' or 'X to Y' range questions, set threshold=X (lower), threshold_high=Y (upper), and operator='between'.
For hurricane/tropical storm/tornado questions: set market_type accordingly, use wind_speed_10m_max as
metric, use the most relevant US coastal coordinates (Gulf Coast: 25.0,-90.0; Atlantic: 25.0,-75.0;
Eastern Seaboard: 35.0,-75.0), and threshold=0.0.
Return only the JSON object, no explanation."""

# Regex patterns for the most common question formats
# Group names: city, threshold, unit, direction, month, day, year
_PATTERNS = [
    # "Will the highest temperature in Seattle be between 56-57°F on March 18?"
    re.compile(
        r"(?:highest|high|maximum|max|lowest|low|minimum|min)?\s*temperature.*?"
        r"(?:be\s+)?between\s+(?P<threshold_low>\d+(?:\.\d+)?)\s*(?:and|-|\u2013|to)\s*(?P<threshold_high>\d+(?:\.\d+)?)\s*\u00b0?\s*(?P<unit>[FfCc])",
        re.IGNORECASE,
    ),
    # "Will Seattle have between 3 and 4 inches of precipitation in March?"
    re.compile(
        r"(?P<city>[A-Za-z\s]+?)\s+have\s+between\s+(?P<threshold_low>\d+(?:\.\d+)?)\s+and\s+(?P<threshold_high>\d+(?:\.\d+)?)\s+inch",
        re.IGNORECASE,
    ),
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

# Hurricane/storm/tornado question patterns — no threshold, use basin coordinates
_STORM_PATTERNS = [
    re.compile(r"hurricane", re.IGNORECASE),
    re.compile(r"tropical\s+storm", re.IGNORECASE),
    re.compile(r"named\s+storm", re.IGNORECASE),
    re.compile(r"category\s+[1-5]", re.IGNORECASE),
    re.compile(r"cyclone", re.IGNORECASE),
    re.compile(r"\btornado", re.IGNORECASE),
    re.compile(r"landfall", re.IGNORECASE),
    re.compile(r"hurricane\s+season", re.IGNORECASE),
]

# Basin/region → representative monitoring coords (near main landfall risk area)
_STORM_REGION_COORDS = {
    # US Gulf Coast — most hurricane questions target this area
    "gulf":     ("Gulf Coast", 25.0, -90.0),
    "atlantic": ("Atlantic Basin", 25.0, -75.0),
    "florida":  ("Florida Coast", 25.5, -80.5),
    "texas":    ("Texas Coast", 27.8, -97.4),
    "carolina": ("Carolina Coast", 34.0, -77.9),
    "default":  ("Gulf Coast", 25.0, -90.0),
}

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

    # --- Path 1: OpenRouter (if configured) or Ollama ---
    result = _try_llm(question)

    # --- Path 2: Regex fallback (temperature/precipitation) ---
    if result is None:
        result = _try_regex(question)

    # --- Path 2b: Storm/hurricane regex fallback ---
    if result is None:
        result = _try_storm_regex(question)

    # --- Path 3: Failure ---
    if result is None:
        logger.warning("parser: all paths failed for question: %.80s", question)
        return {"parse_status": "failed"}

    # Cache the successful parse
    _save_cache(question_hash, question, result, db_path)

    return result


# ---------------------------------------------------------------------------
# Internal — LLM path (OpenRouter preferred, Ollama fallback)
# ---------------------------------------------------------------------------


def _try_llm(question: str) -> dict[str, Any] | None:
    """
    Attempt to parse the question using the best available LLM:
      1. OpenRouter (if OPENROUTER_API_KEY is set)
      2. Ollama (local fallback)

    Returns parsed dict on success, None on any failure.
    """
    prompt = f"Parse this Polymarket weather question:\n\n{question}"
    raw = None

    # Try OpenRouter first
    try:
        from llm.openrouter_client import generate as or_generate, is_configured
        if is_configured():
            raw = or_generate(prompt, system=_SYSTEM_PROMPT)
            logger.debug("parser: used OpenRouter (%s)", __import__("config").settings.OPENROUTER_MODEL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("parser: OpenRouter failed: %s — trying Ollama", exc)
        raw = None

    # Fall back to Ollama if OpenRouter not configured or failed
    if raw is None:
        try:
            from llm.ollama_client import OllamaUnavailableError, generate
            raw = generate(prompt, system=_SYSTEM_PROMPT)
            logger.debug("parser: used Ollama")
        except Exception as exc:  # noqa: BLE001
            logger.debug("parser: Ollama unavailable: %s", exc)
            return None

    try:
        json_str = _extract_json(raw)
        if json_str is None:
            logger.warning("parser: LLM response contained no valid JSON")
            return None

        data = json.loads(json_str)
        data["parse_status"] = "success"
        _validate_parsed(data)
        return data

    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("parser: LLM parse failed: %s", exc)
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
    # Validate threshold_high for "between" operator
    if data.get("operator") == "between":
        if data.get("threshold_high") is None:
            raise ValueError("operator='between' requires threshold_high")
        if float(data["threshold_high"]) <= float(data["threshold"]):
            raise ValueError(
                f"threshold_high ({data['threshold_high']}) must be > threshold ({data['threshold']})"
            )


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
        unit_raw = groups.get("unit", "F").upper()
        direction = groups.get("direction", "exceed").lower()

        # Check if this is a range/between match
        threshold_low_str = groups.get("threshold_low")
        threshold_high_str = groups.get("threshold_high")

        if threshold_low_str is not None and threshold_high_str is not None:
            # Range question: "between X and Y"
            threshold = float(threshold_low_str)
            threshold_high: float | None = float(threshold_high_str)
            operator = "between"
        else:
            threshold = float(groups.get("threshold", 0))
            threshold_high = None
            operator = None  # will be inferred below

        metric, inferred_operator, unit = _infer_metric_operator(pattern, groups, direction, unit_raw)
        if operator is None:
            operator = inferred_operator

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
        if threshold_high is not None:
            result["threshold_high"] = threshold_high
        return result

    return None


def _try_storm_regex(question: str) -> dict[str, Any] | None:
    """
    Detect hurricane/tropical-storm/tornado questions and assign representative
    basin coordinates so the weather engine can fetch ambient conditions
    (SST-correlated wind, pressure, temperature anomalies) for those markets.
    """
    is_storm = any(p.search(question) for p in _STORM_PATTERNS)
    if not is_storm:
        return None

    q_lower = question.lower()

    # Pick the most specific region
    if any(w in q_lower for w in ("florida", "gulf", "mexico")):
        region_key = "florida" if "florida" in q_lower else "gulf"
    elif any(w in q_lower for w in ("texas",)):
        region_key = "texas"
    elif any(w in q_lower for w in ("carolina", "virginia", "northeast")):
        region_key = "carolina"
    elif any(w in q_lower for w in ("atlantic",)):
        region_key = "atlantic"
    else:
        region_key = "default"

    city, lat, lon = _STORM_REGION_COORDS[region_key]
    window_start, window_end = _extract_dates(question)

    return {
        "city": city,
        "lat": lat,
        "lon": lon,
        "metric": "wind_speed_10m_max",
        "threshold": 0.0,
        "unit": "mph",
        "operator": ">",
        "window_start": window_start,
        "window_end": window_end,
        "aggregation": "any",
        "resolution_source": "unknown",
        "market_type": "hurricane" if any(p.search(question) for p in [
            re.compile(r"hurricane|cyclone|landfall|category", re.IGNORECASE)
        ]) else "storm",
        "parse_status": "regex_fallback",
    }


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
