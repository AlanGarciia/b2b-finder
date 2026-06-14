"""
API REST del buscador B2B.
Ejecuta:  uvicorn api:app --reload
Docs interactivas en  http://localhost:8000/docs
"""
import io
import csv
import asyncio
from pathlib import Path
from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse

import db
from overpass import fetch_companies, CATEGORIES
from scraper import scrape_company
from website_finder import find_website, website_from_email, website_from_social
from email_verify import verify_emails, best_emails
from sales_signals import detect_signals, SIGNAL_LABELS
from compliance import (has_personal_data, apply_opt_out, filter_for_resale,
                        split_emails, ATTRIBUTION_TEXT)

app = FastAPI(title="Buscador B2B", version="0.1")

db.init_db()

BASE_DIR = Path(__file__).parent


@app.get("/", response_class=HTMLResponse)
def home():
    """Sirve la interfaz web."""
    index = BASE_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html no encontrado</h1>")


@app.get("/api")
def api_info():
    return {
        "service": "Buscador de nicho B2B",
        "endpoints": {
            "GET /categories": "categorías disponibles",
            "POST /ingest": "buscar empresas de un área (params: area, category, limit)",
            "POST /enrich": "enriquecer empresas pendientes (emails, redes, tech)",
            "GET /companies": "consultar resultados (filtros: area, category, tech, has_email)",
            "GET /export.csv": "descargar todo en CSV",
        },
    }


@app.get("/export.csv")
def export_csv():
    """Descarga todas las empresas en CSV."""
    rows = db.get_companies(limit=10000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "website", "phone", "address", "heat", "signals",
                "emails", "social", "technologies"])
    for r in rows:
        w.writerow([
            r["name"], r["website"], r["phone"], r["address"],
            r.get("heat", ""), "; ".join(r.get("signals", [])),
            "; ".join(r["emails"]),
            "; ".join(f"{k}:{v}" for k, v in r["social"].items()),
            "; ".join(r["technologies"]),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads_b2b.csv"},
    )


@app.get("/categories")
def categories():
    return {"categories": list(CATEGORIES.keys())}


@app.post("/ingest")
async def ingest(
    area: str = Query(..., description="Nombre del área OSM: 'Reus', 'Catalunya', 'España'"),
    category: str = Query("empresas"),
    limit: int = Query(50, le=200),
):
    """Trae empresas de OpenStreetMap y las guarda."""
    companies = await fetch_companies(area, category, limit)
    for comp in companies:
        db.upsert_company(comp, area)
    return {
        "area": area, "category": category,
        "found": len(companies),
        "with_website": sum(1 for c in companies if c["website"]),
    }


async def _enrich_one(comp, find_mode="strict"):
    """Enriquece una empresa: busca web si falta + scraping + verificación email + señales."""
    import json
    website = comp.get("website") or ""
    found_method = ""
    found_confidence = 0

    # si OSM no trae web, intentamos encontrarla
    if not website:
        # 1º) ¿OSM trae un email de empresa? su dominio ES la web (definitivo)
        osm_emails = json.loads(comp.get("emails") or "[]")
        if osm_emails:
            web_from_mail = website_from_email(osm_emails)
            if web_from_mail:
                website = web_from_mail
                found_method = "email"
                found_confidence = 95

        # 2º) si aún no hay, adivinar dominio + buscadores (con scoring)
        if not website:
            try:
                city = (comp.get("address") or "").split(",")[-1].strip() or comp.get("area", "")
                wf = await find_website(comp["name"], city,
                                        phone=comp.get("phone", ""), mode=find_mode)
                if wf["website"]:
                    website = wf["website"]
                    found_method = wf["method"]
                    found_confidence = wf.get("confidence", 0)
            except Exception:
                pass

    data = {"emails": [], "social": {}, "technologies": []}
    if website:
        try:
            data = await scrape_company(website)
        except Exception:
            pass

    # 3º) RESCATE: si seguimos sin web (o poco fiable) pero el scraping/OSM
    # reveló email de dominio propio o redes sociales, deducir la web de ahí.
    if not website or found_confidence < 50:
        recovered = ""
        rec_method = ""
        # web desde el email encontrado (dominio propio)
        scraped_emails = data.get("emails", [])
        web_mail = website_from_email(scraped_emails)
        if web_mail:
            recovered = web_mail
            rec_method = "email"
        # web desde el perfil de redes sociales
        if not recovered and data.get("social"):
            try:
                import httpx as _httpx
                async with _httpx.AsyncClient(
                    headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True
                ) as _c:
                    web_soc = await website_from_social(_c, data["social"])
                if web_soc:
                    recovered = web_soc
                    rec_method = "social"
            except Exception:
                pass
        # si recuperamos una web nueva, scrapearla para sacar más datos
        if recovered and recovered.rstrip("/") != (website or "").rstrip("/"):
            website = recovered
            found_method = rec_method
            found_confidence = 90 if rec_method == "email" else 75
            try:
                data2 = await scrape_company(website)
                # fusionar datos de ambos scrapeos
                data["emails"] = sorted(set(data.get("emails", [])) | set(data2.get("emails", [])))
                data["technologies"] = sorted(set(data.get("technologies", [])) | set(data2.get("technologies", [])))
                merged_social = dict(data.get("social", {}))
                merged_social.update(data2.get("social", {}))
                data["social"] = merged_social
            except Exception:
                pass

    existing = json.loads(comp.get("emails") or "[]")
    raw_emails = sorted(set(existing) | set(data.get("emails", [])))

    # verificación de emails (MX + formato + scoring)
    verified = await verify_emails(raw_emails) if raw_emails else []
    good_emails = best_emails(verified, min_score=50)
    top_score = max((v["score"] for v in verified), default=0)

    # RGPD: aplicar lista de exclusión (opt-out) — quitar emails que pidieron baja
    opt_out = db.get_opt_out_set()
    good_emails = apply_opt_out(good_emails, opt_out)

    # RGPD: marcar si la empresa tiene datos personales (nombre.apellido@...)
    contains_personal = has_personal_data(good_emails)

    # construimos el objeto para detectar señales
    enriched_company = {
        "website": website,
        "emails": good_emails,
        "social": data.get("social", {}),
        "technologies": data.get("technologies", []),
        "phone": comp.get("phone", ""),
    }
    sig = detect_signals(enriched_company)

    # guardar la web si la encontramos automáticamente
    if found_method and website:
        db.update_website(comp["osm_id"], website)

    db.update_enrichment(
        comp["osm_id"], good_emails, data.get("social", {}), data.get("technologies", []),
        signals=sig["signals"], heat=sig["heat"], priority=sig["priority"],
        email_score=top_score,
    )
    db.mark_personal(comp["osm_id"], contains_personal)
    return {
        "name": comp["name"],
        "website": website,
        "website_found": found_method,  # 'guess', 'search' o ''
        "website_confidence": found_confidence,  # 0-100
        "emails": good_emails,
        "social": data.get("social", {}),
        "technologies": data.get("technologies", []),
        "signals": sig["signals"],
        "heat": sig["heat"],
        "priority": sig["priority"],
    }


@app.post("/enrich")
async def enrich(limit: int = Query(15, le=50), concurrency: int = Query(3, le=8),
                 find_websites: str = Query("strict", description="strict | loose | off — buscar webs que faltan")):
    """Enriquece webs pendientes en paralelo: emails verificados, redes, tech y señales de venta."""
    pending = db.get_unenriched(limit)
    sem = asyncio.Semaphore(concurrency)

    async def bounded(comp):
        async with sem:
            return await _enrich_one(comp, find_mode=find_websites)

    results = await asyncio.gather(*[bounded(c) for c in pending], return_exceptions=True)
    ok = [r for r in results if isinstance(r, dict)]
    # ordenamos por prioridad: los leads más calientes primero
    ok.sort(key=lambda r: r["priority"], reverse=True)
    return {"enriched": len(ok), "results": ok}


@app.get("/companies")
def companies(
    area: str = None,
    category: str = None,
    tech: str = Query(None, description="Filtrar por tecnología, ej: 'WordPress'"),
    has_email: bool = False,
    heat: str = Query(None, description="frio/tibio/caliente/muy_caliente"),
    signal: str = Query(None, description="Filtrar por señal, ej: 'sin_web'"),
    sort_by_priority: bool = Query(True, description="Ordenar por lead más caliente"),
    limit: int = Query(100, le=500),
):
    """Consulta los datos recopilados, ordenados por oportunidad de venta."""
    data = db.get_companies(area, category, tech, has_email, heat, signal,
                            sort_by_priority, limit)
    return {"count": len(data), "companies": data}


@app.get("/signals")
def signals_info():
    """Devuelve las señales de venta disponibles y su descripción."""
    return {"signals": SIGNAL_LABELS}


# --- Endpoints de cumplimiento RGPD ---

@app.get("/optout")
def list_optout():
    """Lista de exclusión actual (emails/dominios que pidieron baja)."""
    return {"opt_out": db.list_opt_out()}


@app.post("/optout")
def add_optout(value: str = Query(..., description="email o dominio a excluir"),
               kind: str = Query("email", description="email | domain"),
               reason: str = Query("", description="motivo (opcional)")):
    """Añade un email o dominio a la lista de exclusión (derecho de supresión RGPD)."""
    db.add_opt_out(value, kind, reason)
    return {"ok": True, "excluded": value.strip().lower()}


@app.delete("/optout")
def del_optout(value: str = Query(...)):
    """Quita un valor de la lista de exclusión."""
    db.remove_opt_out(value)
    return {"ok": True, "removed": value.strip().lower()}


@app.get("/export-safe.csv")
def export_safe_csv(include_personal: bool = Query(False, description="incluir emails personales (bajo tu responsabilidad)")):
    """
    Exportación MODO SEGURO para reventa (RGPD).
    Por defecto excluye emails personales (nombre.apellido@) y solo incluye
    datos genéricos de empresa. Incluye la atribución obligatoria de OpenStreetMap.
    """
    rows = db.get_companies(limit=10000)
    opt_out = db.get_opt_out_set()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "website", "phone", "address", "category", "heat",
                "emails_empresa", "social", "technologies", "fuente"])
    for r in rows:
        # aplicar opt-out y filtrar personales
        emails = apply_opt_out(r.get("emails", []), opt_out)
        r_filtered = filter_for_resale({"emails": emails}, include_personal)
        w.writerow([
            r["name"], r["website"], r["phone"], r["address"], r.get("category", ""),
            r.get("heat", ""),
            "; ".join(r_filtered["emails"]),
            "; ".join(f"{k}:{v}" for k, v in r["social"].items()),
            "; ".join(r["technologies"]),
            r.get("source", "openstreetmap"),
        ])
    # atribución obligatoria al final
    w.writerow([])
    w.writerow([ATTRIBUTION_TEXT])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads_seguro.csv"},
    )