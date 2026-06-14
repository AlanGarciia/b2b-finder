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
        ]:
            try:
                c.execute(ddl)
            except Exception:
                pass
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
    with get_conn() as c:
        c.execute("""
        INSERT INTO companies (osm_id, name, website, phone, address, category, area, lat, lon, emails, social, technologies, enriched)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)
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