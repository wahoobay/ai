"""Where the dashboard learns which site it serves.

Nothing site-specific is written into the code: the environment names the
site, and every default here is a container path or a neutral value. The same
contract (LOCATION_ID, SENSESTREAM_DEPLOYMENT_URI, TZ, DATABASE_URL or DB_*)
is read by the poller and the worker; see .env.example for the whole list.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class Settings:
    location_id: str  # short id for rows, export filenames and object prefixes
    deployment_uri: str  # the SenseStream deployment readings are tagged with
    tz: str  # IANA zone the dashboard renders in
    camera_metadata_path: str
    negative_regions_path: str
    export_prefix: str  # CSV/Parquet filename prefix (default: location_id)
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


def site_tz(env: Mapping[str, str] = os.environ) -> str:
    """IANA zone the dashboard renders in (TZ; default UTC), read when asked so the
    environment stays authoritative."""
    return env.get("TZ", "UTC").strip() or "UTC"


def settings(env: Mapping[str, str] = os.environ) -> Settings:
    deployment_uri = env.get("SENSESTREAM_DEPLOYMENT_URI", "").strip()
    if not deployment_uri:
        raise RuntimeError(
            "SENSESTREAM_DEPLOYMENT_URI is required: the SenseStream deployment this "
            "site's readings are tagged with (there is no default site)"
        )
    location_id = env.get("LOCATION_ID", "wahoobay").strip() or "wahoobay"
    return Settings(
        location_id=location_id,
        deployment_uri=deployment_uri,
        tz=site_tz(env),
        camera_metadata_path=env.get("CAMERA_METADATA_PATH", "/data/camera_metadata.json"),
        negative_regions_path=env.get(
            "NEGATIVE_REGIONS_PATH", "/data/camera_negative_regions.json"
        ),
        export_prefix=env.get("EXPORT_PREFIX", "").strip() or location_id,
        database_url=database_url_from_env(env),
    )
