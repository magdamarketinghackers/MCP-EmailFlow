"""
Client store: Postgres-backed config for multi-tenant ESP access.
API credentials are encrypted at rest with Fernet (MASTER_KEY).
"""
import os
import json
import logging
from typing import Optional, List, Dict

from cryptography.fernet import Fernet
import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

_DB_URL = os.environ.get("DATABASE_URL", "")
_MASTER = os.environ.get("MASTER_KEY", "")
_fernet = Fernet(_MASTER.encode()) if _MASTER else None


def available() -> bool:
    """True when both Postgres and the encryption key are configured."""
    return bool(_DB_URL and _fernet)


def _conn():
    return psycopg.connect(_DB_URL, row_factory=dict_row)


def init_db() -> None:
    if not available():
        logger.warning("store: DATABASE_URL or MASTER_KEY missing — client store disabled")
        return
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id          SERIAL PRIMARY KEY,
                slug        TEXT UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                esp_type    TEXT NOT NULL,
                credentials BYTEA NOT NULL,
                defaults    JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        c.commit()
    logger.info("store: clients table ready")


def _encrypt(d: Dict) -> bytes:
    return _fernet.encrypt(json.dumps(d).encode())


def _decrypt(b: bytes) -> Dict:
    return json.loads(_fernet.decrypt(bytes(b)).decode())


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}••••{value[-4:]}"


def list_clients() -> List[Dict]:
    """Public list — NO secrets. Credentials shown masked."""
    if not available():
        return []
    with _conn() as c:
        rows = c.execute(
            "SELECT slug, name, esp_type, credentials, defaults FROM clients ORDER BY name"
        ).fetchall()
    out = []
    for r in rows:
        creds = _decrypt(r["credentials"])
        masked = {k: _mask(str(v)) for k, v in creds.items()}
        out.append({
            "slug": r["slug"],
            "name": r["name"],
            "esp_type": r["esp_type"],
            "credentials_masked": masked,
            "defaults": r["defaults"],
        })
    return out


def get_client(slug: str) -> Optional[Dict]:
    """Internal use — returns DECRYPTED credentials. Never expose via MCP/panel."""
    if not available():
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT slug, name, esp_type, credentials, defaults FROM clients WHERE slug=%s",
            (slug,),
        ).fetchone()
    if not row:
        return None
    row["credentials"] = _decrypt(row["credentials"])
    return row


def create_client(slug: str, name: str, esp_type: str,
                   credentials: Dict, defaults: Dict) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO clients (slug, name, esp_type, credentials, defaults)
               VALUES (%s, %s, %s, %s, %s)""",
            (slug, name, esp_type, _encrypt(credentials), json.dumps(defaults)),
        )
        c.commit()


def update_client(slug: str, name: str, esp_type: str,
                  credentials: Optional[Dict], defaults: Dict) -> None:
    """If credentials is None, keep existing (e.g. panel edit without re-entering keys)."""
    with _conn() as c:
        if credentials is None:
            c.execute(
                """UPDATE clients SET name=%s, esp_type=%s, defaults=%s, updated_at=now()
                   WHERE slug=%s""",
                (name, esp_type, json.dumps(defaults), slug),
            )
        else:
            c.execute(
                """UPDATE clients SET name=%s, esp_type=%s, credentials=%s,
                   defaults=%s, updated_at=now() WHERE slug=%s""",
                (name, esp_type, _encrypt(credentials), json.dumps(defaults), slug),
            )
        c.commit()


def delete_client(slug: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM clients WHERE slug=%s", (slug,))
        c.commit()


def count_clients() -> int:
    if not available():
        return 0
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM clients").fetchone()
    return row["n"]
