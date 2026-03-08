"""Configuration loader and constants for the Polymarket trading bot."""

import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

# --- API Endpoints ---
CLOB_HOST = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# --- Timing ---
WS_PING_INTERVAL = 10  # seconds
UI_REFRESH_HZ = 4
BALANCE_POLL_INTERVAL = 30  # seconds
STALE_ORDERBOOK_THRESHOLD = 30  # seconds

# --- Market Alias Mapping ---
MARKET_ALIASES = {
    "BTC-5": "Bitcoin 5 minute",
    "BTC-15": "Bitcoin 15 minute",
}


@dataclass(frozen=True)
class Settings:
    private_key: str
    api_key: str
    api_secret: str
    api_passphrase: str
    default_market: str
    trade_amount_pct: float
    stop_loss_pct: float
    max_slippage_pct: float
    dry_run: bool


def load_settings() -> Settings:
    """Load and validate settings from .env file."""
    load_dotenv()

    def _require(key: str) -> str:
        val = os.getenv(key)
        if not val:
            print(f"[ERROR] Missing required env var: {key}")
            print(f"        Copy .env.template to .env and fill in your values.")
            sys.exit(1)
        return val.strip()

    private_key = _require("POLYGON_PRIVATE_KEY").removeprefix("0x")
    if len(private_key) != 64 or not all(c in "0123456789abcdefABCDEF" for c in private_key):
        print("[ERROR] POLYGON_PRIVATE_KEY must be a 64-char hex string")
        sys.exit(1)

    api_key = _require("POLY_API_KEY")
    api_secret = _require("POLY_API_SECRET")
    api_passphrase = _require("POLY_API_PASS")

    default_market = os.getenv("DEFAULT_MARKET", "BTC-5").strip()

    def _parse_float(key: str, default: float, low: float, high: float) -> float:
        raw = os.getenv(key, str(default)).strip()
        try:
            val = float(raw)
        except ValueError:
            print(f"[ERROR] {key}={raw!r} is not a valid number")
            sys.exit(1)
        if not (low <= val <= high):
            print(f"[ERROR] {key}={val} out of range [{low}, {high}]")
            sys.exit(1)
        return val

    trade_pct = _parse_float("TRADE_AMOUNT_PERCENT", 0.10, 0.001, 1.0)
    stop_loss = _parse_float("STOP_LOSS_PERCENT", 0.05, 0.001, 0.99)
    max_slip = _parse_float("MAX_SLIPPAGE_PERCENT", 0.02, 0.0, 0.50)

    dry_run_str = os.getenv("DRY_RUN", "true").strip().lower()
    dry_run = dry_run_str in ("true", "1", "yes")

    return Settings(
        private_key=private_key,
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
        default_market=default_market,
        trade_amount_pct=trade_pct,
        stop_loss_pct=stop_loss,
        max_slippage_pct=max_slip,
        dry_run=dry_run,
    )
