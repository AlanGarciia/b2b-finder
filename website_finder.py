"""
Buscador de webs de empresa cuando OSM no la tiene.
Replica lo que haría una persona: buscar el negocio y dar con su web oficial.

Dos estrategias gratis combinadas:
  1) Adivinar el dominio a partir del nombre (rápido, gratis).
  2) Buscar en DuckDuckGo HTML (su versión sin JS se deja scrapear).

100% gratis, sin API keys.
"""
import re
import asyncio
import unicodedata
from urllib.parse import urlparse, quote_plus

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# dominios que NO son la web oficial (redes, directorios, mapas...)
NOT_OFFICIAL = (
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.", "tripadvisor.", "yelp.",
    "google.", "goo.gl", "maps.", "waze.", "foursquare.",
    "paginasamarillas.", "paginas-amarillas.", "11870.com", "cylex.",
    "infoempresa.", "einforma.", "axesor.", "empresia.", "expansion.com",
    "wikipedia.org", "booking.com", "thefork.", "eltenedor.", "glovo",
    "ubereats.", "justeat.", "deliveroo.", "amazon.", "ebay.",
    "wordpress.com", "blogspot.", "wixsite.com", "milanuncios.",
)

TLD_CANDIDATES = [".cat", ".es", ".com", ".eus", ".gal"]


def _slugify(name: str) -> str:
    """Convierte 'Restaurant Cal Pep, S.L.' -> 'restaurantcalpep'."""
    # quitar acentos
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.lower()
    # quitar sufijos societarios comunes
    for suf in [" sl", " s.l.", " sa", " s.a.", " scp", " s.c.p.", " slu",
                " cb", " s.l", " sociedad limitada"]:
        if name.endswith(suf):
            name = name[: -len(suf)]
    # quitar todo lo que no sea letra o número
    name = re.sub(r"[^a-z0-9]", "", name)
    return name


def _is_official(url: str) -> bool:
    """¿Parece la web oficial (no una red social ni directorio)?"""
    host = urlparse(url).netloc.lower()
    if not host:
        return False
    return not any(bad in host for bad in NOT_OFFICIAL)


async def _domain_responds(client: httpx.AsyncClient, domain: str):
    """Comprueba si un dominio existe y responde. Devuelve (url_final, html) o ('', '')."""
    for scheme in ("https://", "http://"):
        url = scheme + domain
        try:
            r = await client.get(url, timeout=8.0, follow_redirects=True)
            if r.status_code < 400:
                ctype = r.headers.get("content-type", "")
                html = r.text if "html" in ctype else ""
                return str(r.url), html
        except Exception:
            continue
    return "", ""


def _name_tokens(name: str) -> list:
    """Palabras significativas del nombre (>=3 letras), sin acentos ni sufijos."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    stop = {"the", "and", "los", "las", "del", "de", "la", "el", "sl", "sa",
            "scp", "cb", "restaurant", "restaurante", "bar", "cafe", "cafeteria",
            "hotel", "tienda", "shop", "store", "studio", "estudio"}
    words = re.findall(r"[a-z0-9]{3,}", n)
    return [w for w in words if w not in stop]


def _page_matches_company(html: str, name: str, domain: str) -> bool:
    """¿La página adivinada pertenece de verdad a esta empresa?
    Exige que el nombre (o sus palabras clave) aparezca en el contenido."""
    if not html:
        return False
    text = unicodedata.normalize("NFKD", html).encode("ascii", "ignore").decode().lower()

    # señal fuerte: el slug completo del nombre está en el dominio Y hay contenido del nombre
    tokens = _name_tokens(name)
    if not tokens:
        return False

    # cuántas palabras clave del nombre aparecen en la página
    hits = sum(1 for t in tokens if t in text)

    # criterio estricto:
    #  - si el nombre tiene 1 palabra clave: esa palabra debe estar (y ser distintiva, >=5 letras)
    #  - si tiene 2+: al menos 2 deben aparecer
    if len(tokens) == 1:
        return tokens[0] in text and len(tokens[0]) >= 5
    return hits >= 2


async def _guess_domain(client: httpx.AsyncClient, name: str) -> str:
    """Estrategia 1: adivinar nombrenegocio.cat/.es/.com Y VERIFICAR que es esa empresa.
    Si no hay certeza razonable, devuelve '' (mejor nada que una web equivocada)."""
    slug = _slugify(name)
    if len(slug) < 4:  # nombres muy cortos dan demasiados falsos positivos
        return ""
    for tld in TLD_CANDIDATES:
        url, html = await _domain_responds(client, slug + tld)
        if url and _is_official(url) and _page_matches_company(html, name, url):
            return url
    return ""


async def _search_duckduckgo(client: httpx.AsyncClient, query: str) -> str:
    """Estrategia 2: buscar en DuckDuckGo HTML y coger el primer resultado oficial."""
    try:
        r = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=12.0,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            # DDG a veces envuelve el link; extraer el destino real
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                from urllib.parse import unquote
                href = unquote(m.group(1))
            if href.startswith("http") and _is_official(href):
                return href
    except Exception:
        return ""
    return ""


async def find_website(name: str, city: str = "", delay: float = 1.0,
                       mode: str = "strict") -> dict:
    """
    Intenta encontrar la web oficial de una empresa.
    mode:
      - "off":    no busca nada (devuelve vacío siempre)
      - "strict": solo devuelve una web si hay certeza razonable de que es la empresa
                  (verifica que el nombre aparece en la página). RECOMENDADO.
      - "loose":  acepta resultados aunque no pueda verificarlos (más cobertura, más errores)
    Devuelve {'website': url, 'method': 'guess'|'search'|''}.
    """
    if mode == "off":
        return {"website": "", "method": ""}

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "es,ca,en"}
    async with httpx.AsyncClient(headers=headers) as client:
        # estrategia 1: adivinar dominio (ya verifica el nombre internamente)
        guessed = await _guess_domain(client, name)
        if guessed:
            return {"website": guessed, "method": "guess"}

        # estrategia 2: buscar en DuckDuckGo
        await asyncio.sleep(delay)
        query = f"{name} {city}".strip()
        found = await _search_duckduckgo(client, query)
        if found:
            if mode == "strict":
                # verificamos que la web del buscador es realmente de la empresa
                _, html = await _domain_responds(client, urlparse(found).netloc)
                if _page_matches_company(html, name, found):
                    return {"website": found, "method": "search"}
                # no verificable -> no lo damos por bueno
                return {"website": "", "method": ""}
            else:  # loose
                return {"website": found, "method": "search"}

    return {"website": "", "method": ""}