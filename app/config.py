from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_CONFIG_DIR = ROOT / "config" / "providers"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    telegram_admin_ids: str = ""  # comma separated chat ids allowed to use the bot ("" = anyone)
    bot_admin_ids: str = ""  # comma separated Telegram ids with access to the admin panel (/admin)
    admin_pin: str = ""  # second factor for sensitive admin actions (/auth <PIN>); empty = those actions off
    payments_enabled: bool = False  # real payments stay OFF until the owner explicitly enables them
    payments_provider: str = "telegram_stars"  # Telegram Stars (XTR): confirmed by Telegram's successful_payment
    support_contact: str = ""  # e.g. @kirbet_support or an e-mail, shown in /support

    database_url: str = f"sqlite+aiosqlite:///{(ROOT / 'data' / 'arb.db').as_posix()}"

    odds_api_key: str = ""  # https://the-odds-api.com (free tier 500 credits / month)
    betfair_app_key: str = ""
    betfair_session_token: str = ""

    scan_interval_seconds: int = 90
    default_capital_usd: Decimal = Decimal("1000")
    default_min_roi_pct: Decimal = Decimal("3")  # real KZ prematch arbs are mostly 0.1–3 %
    default_max_quote_age_seconds: int = 20
    min_match_confidence: float = 0.9
    user_country: str = "KZ"
    kzt_per_usd_fallback: Decimal = Decimal("520")
    http_proxy: str = ""  # optional, for your own network setup; never used to bypass geo-blocks
    # Zendriver (Chrome) for DOM bookmaker adapters
    browser_headless: bool = True
    browser_path: str = ""  # empty = auto-detect chrome / chromium
    browser_no_sandbox: bool = False  # needed in most Docker containers
    # Proxy manager (app/proxy_manager). Off by default; proxies are used only for providers listed in
    # PROXY_ALLOWED_PROVIDERS and never to get around a block, never for Telegram or API keys.
    proxy_manager_enabled: bool = False
    proxy_tools_api_key: str = ""  # proxy-tools.com API needs a paid "Pro Status" account
    proxy_list: str = ""  # your own proxies: "socks5://host:port https://host:port"
    proxy_allowed_providers: str = ""
    proxy_refresh_minutes: int = 30
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def provider_config(name: str) -> dict:
    path = PROVIDER_CONFIG_DIR / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}
