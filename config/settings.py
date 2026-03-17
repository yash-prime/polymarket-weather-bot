"""
config/settings.py — All typed parameters, .env loading, startup validation.

Static config (edge thresholds, Kelly fraction, rate limits, etc.) loads from
.env at startup. Dashboard-adjustable config (kill switch, trading mode, threshold
overrides, position size overrides) is read from the system_config DB table at the
start of each job — never from in-memory state after startup.
"""
import logging
import os
import stat

from dotenv import load_dotenv

load_dotenv()

# --- Trading ---
TRADING_MODE: str = os.getenv("TRADING_MODE", "paper")  # "paper" | "live"
MIN_EDGE_THRESHOLD: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.08"))
KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))
MAX_POSITION_USDC: float = float(os.getenv("MAX_POSITION_USDC", "50.0"))
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.10"))
MIN_MARKET_VOLUME: float = float(os.getenv("MIN_MARKET_VOLUME", "500.0"))
MIN_DAYS_TO_RESOLVE: float = float(os.getenv("MIN_DAYS_TO_RESOLVE", "0.1"))
POST_CANCEL_WAIT_SECONDS: int = int(os.getenv("POST_CANCEL_WAIT_SECONDS", "5"))
STALE_ORDER_MAX_AGE_MIN: int = int(os.getenv("STALE_ORDER_MAX_AGE_MIN", "30"))

# --- Scheduler ---
SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))
LLM_PARSE_INTERVAL_MINUTES: int = int(os.getenv("LLM_PARSE_INTERVAL_MINUTES", "30"))
STALE_ORDER_CHECK_MINUTES: int = int(os.getenv("STALE_ORDER_CHECK_MINUTES", "10"))

# --- Rate Limits (calls per hour per source) ---
RATE_LIMIT_OPEN_METEO: int = int(os.getenv("RATE_LIMIT_OPEN_METEO", "200"))
RATE_LIMIT_NOAA: int = int(os.getenv("RATE_LIMIT_NOAA", "100"))
RATE_LIMIT_GAMMA: int = int(os.getenv("RATE_LIMIT_GAMMA", "60"))
RATE_LIMIT_CLOB: int = int(os.getenv("RATE_LIMIT_CLOB", "120"))

# --- Cache TTLs (seconds) ---
CACHE_TTL_OPEN_METEO_ENSEMBLE: int = int(os.getenv("CACHE_TTL_OPEN_METEO_ENSEMBLE", "3600"))    # 1h
CACHE_TTL_OPEN_METEO_HISTORICAL: int = int(os.getenv("CACHE_TTL_OPEN_METEO_HISTORICAL", "86400"))  # 24h
CACHE_TTL_NOAA: int = int(os.getenv("CACHE_TTL_NOAA", "3600"))                                  # 1h
CACHE_TTL_METEOSTAT: int = int(os.getenv("CACHE_TTL_METEOSTAT", "86400"))                       # 24h

# --- LLM ---
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")  # Optional

# --- Polymarket ---
CLOB_HOST: str = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
CHAIN_ID: int = int(os.getenv("CHAIN_ID", "137"))  # Polygon mainnet

# --- Secrets (required for live mode only) ---
PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
POLY_SECRET: str = os.getenv("POLY_SECRET", "")
POLY_PASSPHRASE: str = os.getenv("POLY_PASSPHRASE", "")

# --- Notifications ---
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Database ---
DB_PATH: str = os.getenv("DB_PATH", "db/trades.db")


def get_rate_limit(source: str) -> int:
    """Return the hourly call budget for a named data source."""
    mapping = {
        "open_meteo": RATE_LIMIT_OPEN_METEO,
        "noaa": RATE_LIMIT_NOAA,
        "gamma": RATE_LIMIT_GAMMA,
        "clob": RATE_LIMIT_CLOB,
    }
    return mapping.get(source, 60)  # default 60/hr for unlisted sources


def validate() -> None:
    """
    Validate required environment variables.

    Raises ValueError if any required live-mode secret is absent.
    Logs warnings for missing optional configuration (Telegram, Anthropic).
    Warns if .env file is world-readable.
    """
    if TRADING_MODE == "live":
        required = {
            "PRIVATE_KEY": PRIVATE_KEY,
            "POLY_API_KEY": POLY_API_KEY,
            "POLY_SECRET": POLY_SECRET,
            "POLY_PASSPHRASE": POLY_PASSPHRASE,
        }
        missing = [name for name, val in required.items() if not val]
        if missing:
            raise ValueError(
                f"Required env var(s) missing for live trading mode: {', '.join(missing)}. "
                "Set these in your .env file."
            )

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning(
            "TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID not configured — alerts disabled"
        )

    if not ANTHROPIC_API_KEY:
        logging.warning(
            "ANTHROPIC_API_KEY not set — Claude API calls will fall back to Ollama"
        )

    _check_env_file_permissions()


def _check_env_file_permissions() -> None:
    """Warn if .env file is world-readable (insecure)."""
    env_path = ".env"
    if not os.path.exists(env_path):
        return
    mode = os.stat(env_path).st_mode
    if mode & stat.S_IROTH or mode & stat.S_IWOTH:
        logging.warning(
            ".env file is world-readable. Run: chmod 600 .env"
        )
