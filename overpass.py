"""
Cliente de Overpass API (OpenStreetMap) para listar empresas locales.
Gratis e ilimitado (con uso justo). No requiere API key.
"""
import httpx

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# Categorías OSM útiles para B2B. Cada una es un tag key=value.
# Amplía según el nicho que busques.
CATEGORIES = {
    "todos":        "ALL",  # caso especial: todos los negocios de la zona
    "empresas":     [("office", "company")],
    "consultoras":  [("office", "consulting")],
    "abogados":     [("office", "lawyer")],
    "tech":         [("office", "it"), ("office", "telecommunication")],
    "marketing":    [("office", "advertising")],
    "inmobiliarias":[("office", "estate_agent")],
    "restaurantes": [("amenity", "restaurant")],
    "tiendas":      [("shop", "")],  # value vacío = cualquier shop
    "talleres":     [("shop", "car_repair")],
    "gimnasios":    [("leisure", "fitness_centre")],
    "clinicas":     [("amenity", "clinic"), ("amenity", "doctors")],
    "hoteles":      [("tourism", "hotel")],
}


def build_query(area_name: str, category: str, limit: int = 50) -> str:
    """Construye una query Overpass QL para un área (ciudad/región/país) y categoría."""
    cat = CATEGORIES.get(category, [("office", "company")])

    if cat == "ALL":
        # "todos": cualquier negocio/comercio/oficina/servicio CON nombre.
        # Cubrimos las grandes familias de tags comerciales de OSM.
        # El filtro [name] evita traer elementos sin nombre (paradas, bancos de parque...).
        selector_block = "\n  ".join([
            'nwr["shop"]["name"](area.searchArea);',
            'nwr["office"]["name"](area.searchArea);',
            'nwr["craft"]["name"](area.searchArea);',
            'nwr["amenity"~"restaurant|cafe|bar|pub|fast_food|pharmacy|clinic|doctors|dentist|veterinary|bank|fuel|car_rental|car_wash|driving_school"]["name"](area.searchArea);',
            'nwr["tourism"~"hotel|guest_house|hostel|apartment"]["name"](area.searchArea);',
            'nwr["leisure"~"fitness_centre|sports_centre"]["name"](area.searchArea);',
            'nwr["healthcare"]["name"](area.searchArea);',
        ])
    else:
        selectors = []
        for key, value in cat:
            if value:
                selectors.append(f'nwr["{key}"="{value}"](area.searchArea);')
            else:
                selectors.append(f'nwr["{key}"](area.searchArea);')
        selector_block = "\n  ".join(selectors)

    query = f"""
[out:json][timeout:90];
area["name"="{area_name}"]->.searchArea;
(
  {selector_block}
);
out center {limit};
"""
    return query.strip()


async def fetch_companies(area_name: str, category: str = "empresas", limit: int = 50) -> list[dict]:
    """Devuelve una lista de empresas con nombre, web, teléfono, dirección, coords."""
    query = build_query(area_name, category, limit)

    headers = {
        "User-Agent": "B2BFinderBot/0.1 (contacto: tu-email@ejemplo.com)",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    last_error = None
    async with httpx.AsyncClient(timeout=90.0, headers=headers) as client:
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                r = await client.post(endpoint, content=f"data={query}".encode("utf-8"))
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                last_error = e
                continue
        else:
            raise RuntimeError(f"Todos los servidores Overpass fallaron. Último error: {last_error}")

    companies = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue

        # coordenadas (nodos tienen lat/lon; ways/relations tienen 'center')
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")

        website = (tags.get("website") or tags.get("contact:website")
                   or tags.get("url") or "")
        phone = tags.get("phone") or tags.get("contact:phone") or ""
        email = tags.get("email") or tags.get("contact:email") or ""

        # dirección compuesta
        addr_parts = [
            tags.get("addr:street", ""),
            tags.get("addr:housenumber", ""),
            tags.get("addr:postcode", ""),
            tags.get("addr:city", ""),
        ]
        address = " ".join(p for p in addr_parts if p).strip()

        # si pedimos "todos", clasificamos cada negocio por su tipo real de OSM
        if category == "todos":
            real_cat = (tags.get("shop") or tags.get("office") or tags.get("craft")
                        or tags.get("amenity") or tags.get("tourism")
                        or tags.get("leisure") or tags.get("healthcare") or "otros")
        else:
            real_cat = category

        companies.append({
            "osm_id": f"{el.get('type')}/{el.get('id')}",
            "name": name,
            "website": website,
            "phone": phone,
            "email_osm": email,
            "address": address,
            "category": real_cat,
            "lat": lat,
            "lon": lon,
        })

    return companies