"""
Client store: Postgres-backed, multi-connection per client.

Schema:
  clients     (id, slug, name)            — brand identity
  connections (id, client_id, label,      — one ESP integration per row
               esp_type, credentials,
               defaults, is_primary)

A client can have N connections (e.g. during a migration from GR → Klaviyo).
The 'primary' connection is what MCP tools default to when no label is given.
API credentials encrypted at rest with Fernet (MASTER_KEY).
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
    return bool(_DB_URL and _fernet)


def _conn():
    return psycopg.connect(_DB_URL, row_factory=dict_row)


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


# ── schema + migration ───────────────────────────────────────────────────────

def init_db() -> None:
    if not available():
        logger.warning("store: DATABASE_URL or MASTER_KEY missing — client store disabled")
        return
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS clients (
            id SERIAL PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS connections (
            id SERIAL PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            esp_type TEXT NOT NULL,
            credentials BYTEA NOT NULL,
            defaults JSONB NOT NULL DEFAULT '{}'::jsonb,
            is_primary BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE(client_id, label)
        )""")

        # Migrate from old single-connection schema if present
        has_old = c.execute("""SELECT 1 FROM information_schema.columns
                               WHERE table_name='clients' AND column_name='esp_type'""").fetchone()
        if has_old:
            old = c.execute("""SELECT id, esp_type, credentials, defaults
                               FROM clients WHERE esp_type IS NOT NULL""").fetchall()
            for row in old:
                # Use esp_type as the initial label so users recognise it
                c.execute("""INSERT INTO connections
                             (client_id, label, esp_type, credentials, defaults, is_primary)
                             VALUES (%s, %s, %s, %s, %s, true)
                             ON CONFLICT (client_id, label) DO NOTHING""",
                          (row["id"], row["esp_type"], row["esp_type"],
                           bytes(row["credentials"]), json.dumps(row["defaults"])))
            c.execute("ALTER TABLE clients DROP COLUMN IF EXISTS esp_type")
            c.execute("ALTER TABLE clients DROP COLUMN IF EXISTS credentials")
            c.execute("ALTER TABLE clients DROP COLUMN IF EXISTS defaults")
            logger.info(f"store: migrated {len(old)} legacy single-connection clients")
        c.commit()
    logger.info("store: schema ready (clients + connections)")


# ── reads ─────────────────────────────────────────────────────────────────────

def list_clients() -> List[Dict]:
    """All clients with their connections; credentials masked."""
    if not available():
        return []
    with _conn() as c:
        clients = c.execute("SELECT id, slug, name FROM clients ORDER BY name").fetchall()
        for cli in clients:
            conns = c.execute("""SELECT label, esp_type, credentials, defaults, is_primary
                                 FROM connections WHERE client_id=%s
                                 ORDER BY is_primary DESC, label""",
                              (cli["id"],)).fetchall()
            out_conns = []
            for cn in conns:
                creds = _decrypt(cn["credentials"])
                out_conns.append({
                    "label": cn["label"],
                    "esp_type": cn["esp_type"],
                    "credentials_masked": {k: _mask(str(v)) for k, v in creds.items()},
                    "defaults": cn["defaults"],
                    "is_primary": cn["is_primary"],
                })
            cli["connections"] = out_conns
            del cli["id"]
    return clients


def get_client(slug: str) -> Optional[Dict]:
    """Public client info (with connections, masked credentials)."""
    return next((c for c in list_clients() if c["slug"] == slug), None)


def get_connection(slug: str, label: Optional[str] = None) -> Optional[Dict]:
    """
    Internal — returns DECRYPTED credentials.
    If label is None, returns the primary connection.
    Return shape matches what get_esp() expects:
      { esp_type, credentials: {...}, defaults: {...}, label, client_slug, client_name }
    """
    if not available():
        return None
    with _conn() as c:
        cli = c.execute("SELECT id, slug, name FROM clients WHERE slug=%s",
                        (slug,)).fetchone()
        if not cli:
            return None
        if label:
            cn = c.execute("""SELECT label, esp_type, credentials, defaults, is_primary
                              FROM connections WHERE client_id=%s AND label=%s""",
                           (cli["id"], label)).fetchone()
        else:
            cn = c.execute("""SELECT label, esp_type, credentials, defaults, is_primary
                              FROM connections WHERE client_id=%s
                              ORDER BY is_primary DESC, label LIMIT 1""",
                           (cli["id"],)).fetchone()
    if not cn:
        return None
    return {
        "client_slug": cli["slug"],
        "client_name": cli["name"],
        "label": cn["label"],
        "esp_type": cn["esp_type"],
        "credentials": _decrypt(cn["credentials"]),
        "defaults": cn["defaults"],
        "is_primary": cn["is_primary"],
    }


def list_connection_labels(slug: str) -> List[str]:
    if not available():
        return []
    with _conn() as c:
        cli = c.execute("SELECT id FROM clients WHERE slug=%s", (slug,)).fetchone()
        if not cli:
            return []
        rows = c.execute("""SELECT label FROM connections WHERE client_id=%s
                            ORDER BY is_primary DESC, label""", (cli["id"],)).fetchall()
    return [r["label"] for r in rows]


def count_clients() -> int:
    if not available():
        return 0
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM clients").fetchone()["n"]


# ── writes ────────────────────────────────────────────────────────────────────

def _get_or_create_client(c, slug: str, name: str) -> int:
    row = c.execute("SELECT id FROM clients WHERE slug=%s", (slug,)).fetchone()
    if row:
        # update name in case it changed
        c.execute("UPDATE clients SET name=%s, updated_at=now() WHERE id=%s",
                  (name, row["id"]))
        return row["id"]
    row = c.execute("INSERT INTO clients (slug, name) VALUES (%s,%s) RETURNING id",
                    (slug, name)).fetchone()
    return row["id"]


def add_connection(slug: str, name: str, label: str, esp_type: str,
                   credentials: Dict, defaults: Dict) -> None:
    """Create client if missing, then add a new connection. First connection is primary."""
    with _conn() as c:
        client_id = _get_or_create_client(c, slug, name)
        n = c.execute("SELECT COUNT(*) AS n FROM connections WHERE client_id=%s",
                      (client_id,)).fetchone()["n"]
        c.execute("""INSERT INTO connections
                     (client_id, label, esp_type, credentials, defaults, is_primary)
                     VALUES (%s,%s,%s,%s,%s,%s)""",
                  (client_id, label, esp_type, _encrypt(credentials),
                   json.dumps(defaults), n == 0))
        c.commit()


def update_connection(slug: str, label: str, esp_type: str,
                       credentials: Optional[Dict], defaults: Dict,
                       new_label: Optional[str] = None) -> None:
    with _conn() as c:
        cli = c.execute("SELECT id FROM clients WHERE slug=%s", (slug,)).fetchone()
        if not cli:
            return
        target = new_label or label
        if credentials is None:
            c.execute("""UPDATE connections SET label=%s, esp_type=%s, defaults=%s,
                         updated_at=now() WHERE client_id=%s AND label=%s""",
                      (target, esp_type, json.dumps(defaults), cli["id"], label))
        else:
            c.execute("""UPDATE connections SET label=%s, esp_type=%s, credentials=%s,
                         defaults=%s, updated_at=now()
                         WHERE client_id=%s AND label=%s""",
                      (target, esp_type, _encrypt(credentials),
                       json.dumps(defaults), cli["id"], label))
        c.commit()


def delete_connection(slug: str, label: str) -> None:
    """Delete a connection. If the deleted one was primary, promote another (oldest) to primary.
       If it was the last connection, the client row stays — call delete_client() to remove."""
    with _conn() as c:
        cli = c.execute("SELECT id FROM clients WHERE slug=%s", (slug,)).fetchone()
        if not cli:
            return
        was_primary = c.execute("""SELECT is_primary FROM connections
                                   WHERE client_id=%s AND label=%s""",
                                (cli["id"], label)).fetchone()
        c.execute("DELETE FROM connections WHERE client_id=%s AND label=%s",
                  (cli["id"], label))
        if was_primary and was_primary["is_primary"]:
            # promote oldest remaining as new primary
            nxt = c.execute("""SELECT id FROM connections WHERE client_id=%s
                               ORDER BY created_at LIMIT 1""", (cli["id"],)).fetchone()
            if nxt:
                c.execute("UPDATE connections SET is_primary=true WHERE id=%s", (nxt["id"],))
        c.commit()


def rename_client(slug: str, name: str) -> None:
    with _conn() as c:
        c.execute("UPDATE clients SET name=%s, updated_at=now() WHERE slug=%s", (name, slug))
        c.commit()


def set_primary(slug: str, label: str) -> None:
    with _conn() as c:
        cli = c.execute("SELECT id FROM clients WHERE slug=%s", (slug,)).fetchone()
        if not cli:
            return
        c.execute("UPDATE connections SET is_primary=false WHERE client_id=%s", (cli["id"],))
        c.execute("UPDATE connections SET is_primary=true WHERE client_id=%s AND label=%s",
                  (cli["id"], label))
        c.commit()


def delete_client(slug: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM clients WHERE slug=%s", (slug,))
        c.commit()
