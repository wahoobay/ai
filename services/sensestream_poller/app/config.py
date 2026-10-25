"""Where the poller learns which site it serves (same contract as the dashboard).

Nothing site-specific is written into the code: SENSESTREAM_DEPLOYMENT_URI is
required, weather rows default to the same tag so the dashboard's default query
finds them, and the database address is DATABASE_URL or the DB_* parts.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class Settings:
    location_id: str
    deployment_uri: str  # tag on sonde readings; required
    weather_deployment_uri: str  # tag on weather rows; defaults to deployment_uri
    tz: str
    database_url: str


def database_url_from_env(env: Mapping[str, str] = os.environ) -> str:
    """DATABASE_URL, or the same URL assembled from DB_HOST, DB_PORT (5432),
    DB_NAME, DB_USER and DB_PASSWORD. App Runner and Lambda inject the password
    as its own variable straight from Secrets Manager, so the parts form is
    what the cloud uses; compose and the bare-metal launchers pass one URL."""
    url = env.get("DATABASE_URL", "").strip()
    if url:
        return url
    missing = [k for k in ("DB_HOST", "DB_NAME", "DB_USER") if not env.get(k)]
    if missing:
        raise RuntimeError(
            "set DATABASE_URL, or DB_HOST/DB_NAME/DB_USER (+ DB_PASSWORD, DB_PORT) "
            f"(missing: {', '.join(missing)})"
        )
    user = quote(env["DB_USER"], safe="")
    password = quote(env.get("DB_PASSWORD", ""), safe="")
    auth = f"{user}:{password}" if password else user
    port = env.get("DB_PORT", "5432")
    return f"postgresql://{auth}@{env['DB_HOST']}:{port}/{env['DB_NAME']}"


def settings(env: Mapping[str, str] = os.environ) -> Settings:
    deployment_uri = env.get("SENSESTREAM_DEPLOYMENT_URI", "").strip()
    if not deployment_uri:
        raise RuntimeError(
            "SENSESTREAM_DEPLOYMENT_URI is required: the SenseStream deployment this "
            "site's readings are tagged with (there is no default site)"
        )
    return Settings(
        location_id=env.get("LOCATION_ID", "wahoobay").strip() or "wahoobay",
        deployment_uri=deployment_uri,
        weather_deployment_uri=env.get("SENSESTREAM_WEATHER_DEPLOYMENT_URI", "").strip()
        or deployment_uri,
        tz=env.get("TZ", "UTC").strip() or "UTC",
        database_url=database_url_from_env(env),
    )
