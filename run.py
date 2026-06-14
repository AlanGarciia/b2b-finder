"""
CLI para probar el pipeline completo sin levantar la API.

Uso:
    python run.py ingest "Reus" empresas 30
    python run.py enrich 10
    python run.py list --tech WordPress --has-email
    python run.py export csv
"""
import sys
import json
import asyncio
import csv

import db
from overpass import fetch_companies
from scraper import scrape_company

db.init_db()


async def cmd_ingest(area, category, limit):
    print(f"→ Buscando '{category}' en '{area}' (max {limit})...")
    companies = await fetch_companies(area, category, int(limit))
    for comp in companies:
        db.upsert_company(comp, area)
    with_web = sum(1 for c in companies if c["website"])
    print(f"✓ {len(companies)} empresas guardadas ({with_web} con web).")


async def cmd_enrich(limit):
    pending = db.get_unenriched(int(limit))
    print(f"→ Enriqueciendo {len(pending)} empresas con web...")
    for comp in pending:
        print(f"   · {comp['name']} ({comp['website']})")
        data = await scrape_company(comp["website"])
        existing = json.loads(comp.get("emails") or "[]")
        all_emails = sorted(set(existing) | set(data["emails"]))
        db.update_enrichment(comp["osm_id"], all_emails, data["social"], data["technologies"])
        print(f"      emails={len(all_emails)} redes={list(data['social'])} tech={data['technologies']}")
    print("✓ Enriquecimiento terminado.")


def cmd_list(args):
    tech = None
    has_email = "--has-email" in args
    if "--tech" in args:
        tech = args[args.index("--tech") + 1]
    rows = db.get_companies(tech=tech, has_email=has_email, limit=200)
    print(f"\n{len(rows)} resultados:\n")
    for r in rows:
        print(f"• {r['name']}")
        print(f"  web:   {r['website']}")
        if r['emails']:    print(f"  email: {', '.join(r['emails'])}")
        if r['social']:    print(f"  redes: {r['social']}")
        if r['technologies']: print(f"  tech:  {', '.join(r['technologies'])}")
        print()


def cmd_export(fmt):
    rows = db.get_companies(limit=10000)
    if fmt == "csv":
        with open("export.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "website", "phone", "address", "emails", "social", "technologies"])
            for r in rows:
                w.writerow([r["name"], r["website"], r["phone"], r["address"],
                            "; ".join(r["emails"]),
                            "; ".join(f"{k}:{v}" for k, v in r["social"].items()),
                            "; ".join(r["technologies"])])
        print(f"✓ {len(rows)} filas exportadas a export.csv")
    else:
        with open("export.json", "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"✓ {len(rows)} filas exportadas a export.json")


def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd = sys.argv[1]
    if cmd == "ingest":
        area = sys.argv[2]; cat = sys.argv[3] if len(sys.argv) > 3 else "empresas"
        lim = sys.argv[4] if len(sys.argv) > 4 else "30"
        asyncio.run(cmd_ingest(area, cat, lim))
    elif cmd == "enrich":
        asyncio.run(cmd_enrich(sys.argv[2] if len(sys.argv) > 2 else "10"))
    elif cmd == "list":
        cmd_list(sys.argv[2:])
    elif cmd == "export":
        cmd_export(sys.argv[2] if len(sys.argv) > 2 else "csv")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
