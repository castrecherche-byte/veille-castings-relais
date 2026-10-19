import json
import sqlite3
from pathlib import Path

from casting_watch.models import MatchDecision, RawListing

# source_url des annonces ajoutees a la main (Instagram, Facebook...) : jamais considerees "retirees".
MANUAL_SOURCE_URL = "manual"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    score INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    detail_text TEXT NOT NULL DEFAULT '',
    links TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_listings_accepted_score
ON listings (accepted, score, last_seen_at);

-- Comptes (Instagram, Facebook...) de directeurs de casting a consulter regulierement.
CREATE TABLE IF NOT EXISTS followed_accounts (
    url TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Candidatures preparees (brouillons Gmail crees), pour le suivi.
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_url TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Personnes pour qui on cherche des castings : infos de candidature et criteres de recherche (JSON).
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    criteria TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Recherche web de l'annonce d'origine (Tavily) : une seule recherche par annonce, resultats gardes.
CREATE TABLE IF NOT EXISTS web_origins (
    listing_url TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    results TEXT NOT NULL,
    searched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Comptes Instagram de casting trouves automatiquement et verifies : proposes, suivis ou ignores.
CREATE TABLE IF NOT EXISTS suggested_accounts (
    username TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    followers INTEGER NOT NULL DEFAULT 0,
    castings INTEGER NOT NULL DEFAULT 0,
    enfants INTEGER NOT NULL DEFAULT 0,
    idf INTEGER NOT NULL DEFAULT 0,
    dernier_post TEXT NOT NULL DEFAULT '',
    bio TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'propose',
    found_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Reglages internes : derniere recherche, connexion Instagram, profil de candidature, connexion Gmail.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Colonnes ajoutees apres la premiere version : ajoutees aux bases existantes.
MIGRATIONS = {
    "detail_text": "ALTER TABLE listings ADD COLUMN detail_text TEXT NOT NULL DEFAULT ''",
    "links": "ALTER TABLE listings ADD COLUMN links TEXT NOT NULL DEFAULT ''",
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(listings)")}
    for column, statement in MIGRATIONS.items():
        if column not in columns:
            connection.execute(statement)
    application_columns = {row["name"] for row in connection.execute("PRAGMA table_info(applications)")}
    if "profile_id" not in application_columns:
        # Avant les profils multiples, toutes les candidatures etaient pour le premier profil.
        connection.execute("ALTER TABLE applications ADD COLUMN profile_id INTEGER NOT NULL DEFAULT 1")
    connection.commit()
    return connection


def list_profiles(connection: sqlite3.Connection) -> list[dict]:
    return [
        {"id": row["id"], "label": row["label"], "data": json.loads(row["data"]), "criteria": json.loads(row["criteria"])}
        for row in connection.execute("SELECT * FROM profiles ORDER BY id")
    ]


def insert_profile(connection: sqlite3.Connection, label: str, data: dict, criteria: dict) -> int:
    cursor = connection.execute(
        "INSERT INTO profiles (label, data, criteria) VALUES (?, ?, ?)",
        (label, json.dumps(data, ensure_ascii=False), json.dumps(criteria, ensure_ascii=False)),
    )
    connection.commit()
    return cursor.lastrowid


def update_profile(connection: sqlite3.Connection, profile_id: int, **fields) -> None:
    for name in ("label", "data", "criteria"):
        if name in fields:
            value = fields[name] if name == "label" else json.dumps(fields[name], ensure_ascii=False)
            connection.execute(f"UPDATE profiles SET {name} = ? WHERE id = ?", (value, profile_id))
    connection.commit()


def delete_profile_row(connection: sqlite3.Connection, profile_id: int) -> None:
    connection.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
    connection.commit()


SUGGESTION_FIELDS = ("username", "name", "followers", "castings", "enfants", "idf", "dernier_post", "bio")


def save_suggestion(connection: sqlite3.Connection, account: dict) -> None:
    """Ajoute un compte propose (ou met a jour ses chiffres s'il est encore propose)."""
    numbers = ("followers", "castings", "enfants", "idf")
    values = [account.get(field) or (0 if field in numbers else "") for field in SUGGESTION_FIELDS]
    connection.execute(
        f"INSERT INTO suggested_accounts ({', '.join(SUGGESTION_FIELDS)}) VALUES ({', '.join('?' * len(values))}) "
        "ON CONFLICT(username) DO UPDATE SET "
        + ", ".join(f"{field} = excluded.{field}" for field in SUGGESTION_FIELDS[1:])
        + " WHERE suggested_accounts.status = 'propose'",
        values,
    )
    connection.commit()


def suggestions(connection: sqlite3.Connection, status: str | None = "propose") -> list[sqlite3.Row]:
    if status is None:
        return list(connection.execute("SELECT * FROM suggested_accounts"))
    return list(
        connection.execute(
            "SELECT * FROM suggested_accounts WHERE status = ? ORDER BY idf DESC, enfants DESC, followers DESC",
            (status,),
        )
    )


def set_suggestion_status(connection: sqlite3.Connection, username: str, status: str) -> None:
    connection.execute("UPDATE suggested_accounts SET status = ? WHERE username = ?", (status, username))
    connection.commit()


def web_origins(connection: sqlite3.Connection) -> dict[str, list[dict]]:
    """Resultats web deja trouves, par lien d'annonce."""
    return {
        row["listing_url"]: json.loads(row["results"])
        for row in connection.execute("SELECT listing_url, results FROM web_origins")
    }


def save_web_origin(connection: sqlite3.Connection, listing_url: str, query: str, results: list[dict]) -> None:
    connection.execute(
        "INSERT INTO web_origins (listing_url, query, results) VALUES (?, ?, ?) "
        "ON CONFLICT(listing_url) DO UPDATE SET query = excluded.query, results = excluded.results, "
        "searched_at = CURRENT_TIMESTAMP",
        (listing_url, query, json.dumps(results, ensure_ascii=False)),
    )
    connection.commit()


def upsert_listing(connection: sqlite3.Connection, listing: RawListing, decision: MatchDecision) -> bool:
    before = connection.total_changes
    connection.execute(
        """
        INSERT INTO listings (
            source_name, source_url, title, url, text, fetched_at,
            accepted, score, reasons, detail_text, links
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            text = excluded.text,
            fetched_at = excluded.fetched_at,
            accepted = excluded.accepted,
            score = excluded.score,
            reasons = excluded.reasons,
            detail_text = excluded.detail_text,
            links = excluded.links,
            last_seen_at = CURRENT_TIMESTAMP
        """,
        (
            listing.source_name,
            listing.source_url,
            listing.title,
            listing.url,
            listing.text,
            listing.fetched_at,
            1 if decision.accepted else 0,
            decision.score,
            "; ".join(decision.reasons),
            listing.detail_text,
            "\n".join(listing.links),
        ),
    )
    connection.commit()
    return connection.total_changes > before


def row_to_listing(row: sqlite3.Row) -> RawListing:
    return RawListing(
        source_name=row["source_name"],
        source_url=row["source_url"],
        title=row["title"],
        url=row["url"],
        text=row["text"],
        fetched_at=row["fetched_at"],
        detail_text=row["detail_text"],
        links=tuple(link for link in row["links"].split("\n") if link),
    )


def list_all(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Toutes les annonces, avec still_online (vue au dernier passage sur sa source) et is_new (< 48 h)."""
    cursor = connection.execute(
        """
        SELECT
            l.*,
            (l.source_url = ? OR l.last_seen_at >= datetime(m.latest, '-1 hour')) AS still_online,
            l.first_seen_at >= datetime('now', '-2 days') AS is_new
        FROM listings AS l
        JOIN (
            SELECT source_name, MAX(last_seen_at) AS latest
            FROM listings
            GROUP BY source_name
        ) AS m USING (source_name)
        ORDER BY l.first_seen_at DESC, l.id DESC
        """,
        (MANUAL_SOURCE_URL,),
    )
    return list(cursor.fetchall())


def known_details(connection: sqlite3.Connection) -> dict[str, RawListing]:
    """Annonces dont la page de detail a deja ete lue (pour ne pas la retelecharger)."""
    rows = connection.execute("SELECT * FROM listings WHERE detail_text != ''")
    return {row["url"]: row_to_listing(row) for row in rows}


def save_followed_account(connection: sqlite3.Connection, url: str, label: str, note: str = "") -> None:
    connection.execute(
        """
        INSERT INTO followed_accounts (url, label, note) VALUES (?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET label = excluded.label, note = excluded.note
        """,
        (url, label, note),
    )
    connection.commit()


def followed_accounts(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(connection.execute("SELECT * FROM followed_accounts ORDER BY label COLLATE NOCASE"))


def delete_followed_account(connection: sqlite3.Connection, url: str) -> bool:
    cursor = connection.execute("DELETE FROM followed_accounts WHERE url = ?", (url,))
    connection.commit()
    return cursor.rowcount > 0


def get_meta(connection: sqlite3.Connection, prefix: str) -> dict[str, str]:
    """Valeurs dont la cle commence par `prefix`, sans le prefixe."""
    rows = connection.execute("SELECT key, value FROM meta WHERE key LIKE ?", (prefix + "%",))
    return {row["key"][len(prefix):]: row["value"] for row in rows}


def set_meta(connection: sqlite3.Connection, prefix: str, values: dict[str, str | None]) -> None:
    for key, value in values.items():
        if value is None:
            connection.execute("DELETE FROM meta WHERE key = ?", (prefix + key,))
        else:
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (prefix + key, str(value)),
            )
    connection.commit()


def record_application(connection: sqlite3.Connection, listing_url: str, recipient: str, subject: str,
                       profile_id: int = 1) -> None:
    connection.execute(
        "INSERT INTO applications (listing_url, recipient, subject, profile_id) VALUES (?, ?, ?, ?)",
        (listing_url, recipient, subject, profile_id),
    )
    connection.commit()


def applications_by_listing(connection: sqlite3.Connection, profile_id: int | None = None) -> dict[str, str]:
    """Date du dernier brouillon prepare, par annonce (pour un profil, ou pour tous)."""
    where, params = ("WHERE profile_id = ?", (profile_id,)) if profile_id is not None else ("", ())
    rows = connection.execute(
        f"SELECT listing_url, MAX(created_at) AS last FROM applications {where} GROUP BY listing_url", params
    )
    return {row["listing_url"]: row["last"] for row in rows}


def count_all(connection: sqlite3.Connection) -> dict[str, int]:
    cursor = connection.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN accepted = 1 THEN 1 ELSE 0 END) AS accepted
        FROM listings
        """
    )
    row = cursor.fetchone()
    return {"total": int(row["total"] or 0), "accepted": int(row["accepted"] or 0)}
