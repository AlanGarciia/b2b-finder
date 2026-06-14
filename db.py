"""
Capa de base de datos SQLite. Sin dependencias externas.
"""
import sqlite3
import json
from contextlib import contextmanager

DB_PATH = "b2b.db"


def init_db():
    with get_conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            osm_id        TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            website       TEXT,
            phone         TEXT,
            address       TEXT,
            category      TEXT,
            area          TEXT,
            lat           REAL,
            lon           REAL,
            emails        TEXT,   -- JSON array
            social        TEXT,   -- JSON object
            technologies  TEXT,   -- JSON array
            signals       TEXT,   -- JSON array (señales de venta)
            heat          TEXT,   -- frio/tibio/caliente/muy_caliente
            priority      INTEGER DEFAULT 0,
            email_score   INTEGER DEFAULT 0,
            source        TEXT DEFAULT 'openstreetmap',  -- procedencia del dato (RGPD)
            source_url    TEXT,                           -- de dónde vino exactamente
            has_personal  INTEGER DEFAULT 0,              -- ¿contiene datos personales?
            first_seen    TIMESTAMP,                      -- cuándo se recopiló
            enriched      INTEGER DEFAULT 0,
            scraped_at    TIMESTAMP
        )
        """)
        # migración suave: añade columnas nuevas si la DB ya existía (ANTES de los índices)
        for col, ddl in [
            ("signals", "ALTER TABLE companies ADD COLUMN signals TEXT"),
            ("heat", "ALTER TABLE companies ADD COLUMN heat TEXT"),
            ("priority", "ALTER TABLE companies ADD COLUMN priority INTEGER DEFAULT 0"),
            ("email_score", "ALTER TABLE companies ADD COLUMN email_score INTEGER DEFAULT 0"),
            ("source", "ALTER TABLE companies ADD COLUMN source TEXT DEFAULT 'openstreetmap'"),
            ("source_url", "ALTER TABLE companies ADD COLUMN source_url TEXT"),
            ("has_personal", "ALTER TABLE companies ADD COLUMN has_personal INTEGER DEFAULT 0"),
            ("first_seen", "ALTER TABLE companies ADD COLUMN first_seen TIMESTAMP"),
        ]:
            try:
                c.execute(ddl)
            except Exception:
                pass
        # tabla de exclusión (opt-out): emails/dominios que NUNCA deben aparecer
        c.execute("""
        CREATE TABLE IF NOT EXISTS opt_out (
            value      TEXT PRIMARY KEY,   -- email o dominio a excluir
            kind       TEXT,               -- 'email' | 'domain'
            added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reason     TEXT
        )
        """)
        # índices (después de garantizar que las columnas existen)
        c.execute("CREATE INDEX IF NOT EXISTS idx_area ON companies(area)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cat ON companies(category)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_heat ON companies(heat)")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_company(comp: dict, area: str):
    source = comp.get("source", "openstreetmap")
    source_url = comp.get("source_url", "")
    with get_conn() as c:
        c.execute("""
        INSERT INTO companies (osm_id, name, website, phone, address, category, area, lat, lon, emails, social, technologies, source, source_url, first_seen, enriched)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,0)
        ON CONFLICT(osm_id) DO UPDATE SET
            name=excluded.name, website=excluded.website, phone=excluded.phone,
            address=excluded.address, category=excluded.category, area=excluded.area,
            lat=excluded.lat, lon=excluded.lon
        """, (
            comp["osm_id"], comp["name"], comp.get("website", ""), comp.get("phone", ""),
            comp.get("address", ""), comp.get("category", ""), area,
            comp.get("lat"), comp.get("lon"),
            json.dumps([comp["email_osm"]] if comp.get("email_osm") else []),
            json.dumps({}), json.dumps([]),
            source, source_url,
        ))


def update_website(osm_id, website):
    """Guarda la web encontrada automáticamente."""
    with get_conn() as c:
        c.execute("UPDATE companies SET website=? WHERE osm_id=?", (website, osm_id))


def update_enrichment(osm_id, emails, social, technologies,
                      signals=None, heat="frio", priority=0, email_score=0):
    with get_conn() as c:
        c.execute("""
        UPDATE companies
        SET emails=?, social=?, technologies=?, signals=?, heat=?, priority=?,
            email_score=?, enriched=1, scraped_at=CURRENT_TIMESTAMP
        WHERE osm_id=?
        """, (json.dumps(emails), json.dumps(social), json.dumps(technologies),
              json.dumps(signals or []), heat, priority, email_score, osm_id))


def get_companies(area=None, category=None, tech=None, has_email=False,
                  heat=None, signal=None, sort_by_priority=False, limit=100):
    q = "SELECT * FROM companies WHERE 1=1"
    params = []
    if area:
        q += " AND area=?"; params.append(area)
    if category:
        q += " AND category=?"; params.append(category)
    if tech:
        q += " AND technologies LIKE ?"; params.append(f'%"{tech}"%')
    if has_email:
        q += " AND emails != '[]' AND emails IS NOT NULL"
    if heat:
        q += " AND heat=?"; params.append(heat)
    if signal:
        q += " AND signals LIKE ?"; params.append(f'%"{signal}"%')
    if sort_by_priority:
        q += " ORDER BY priority DESC"
    q += " LIMIT ?"; params.append(limit)

    with get_conn() as c:
        rows = c.execute(q, params).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["emails"] = json.loads(d["emails"] or "[]")
        d["social"] = json.loads(d["social"] or "{}")
        d["technologies"] = json.loads(d["technologies"] or "[]")
        d["signals"] = json.loads(d.get("signals") or "[]")
        out.append(d)
    return out


def get_unenriched(limit=20):
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM companies WHERE enriched=0 LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- Lista de exclusión (opt-out / RGPD) ---

def add_opt_out(value, kind="email", reason=""):
    """Añade un email o dominio a la lista de exclusión."""
    with get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO opt_out (value, kind, reason) VALUES (?,?,?)",
            (value.strip().lower(), kind, reason),
        )


def remove_opt_out(value):
    with get_conn() as c:
        c.execute("DELETE FROM opt_out WHERE value=?", (value.strip().lower(),))


def get_opt_out_set():
    """Devuelve un set con todos los valores excluidos (para filtrar rápido)."""
    with get_conn() as c:
        rows = c.execute("SELECT value FROM opt_out").fetchall()
    return {r["value"] for r in rows}


def list_opt_out():
    with get_conn() as c:
        rows = c.execute("SELECT * FROM opt_out ORDER BY added_at DESC").fetchall()
    return [dict(r) for r in rows]


def mark_personal(osm_id, has_personal):
    with get_conn() as c:
        c.execute("UPDATE companies SET has_personal=? WHERE osm_id=?",
                  (1 if has_personal else 0, osm_id))