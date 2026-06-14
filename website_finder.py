"""
Buscador PRO de webs de empresa cuando OSM no la tiene.

Filosofía: máxima fiabilidad. En vez de un sí/no simple, cada web candidata
recibe un SCORE DE CONFIANZA cruzando múltiples evidencias:

  + El dominio contiene el nombre del negocio
  + La ciudad/zona aparece en la web
  + El nombre completo aparece en el <title> o en el contenido
  + Hay datos de contacto coherentes (teléfono, dirección de la zona)
  + El TLD es local (.cat/.es) para negocios locales
  - Penaliza dominios aparcados, "en venta", o sin relación

Solo se acepta la web si supera un umbral alto. Ante la duda -> no pone web.
100% gratis, sin API keys.
"""
import re
import asyncio
import unicodedata
from urllib.parse import urlparse, unquote

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Umbral de confianza (0-100). Por debajo de esto, NO se acepta la web.
CONFIDENCE_THRESHOLD = 50

NOT_OFFICIAL = (
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.", "tripadvisor.", "yelp.",
    "google.", "goo.gl", "maps.", "waze.", "foursquare.",
    "paginasamarillas.", "paginas-amarillas.", "11870.com", "cylex.",
    "infoempresa.", "einforma.", "axesor.", "empresia.", "expansion.com",
    "wikipedia.org", "booking.com", "thefork.", "eltenedor.", "glovo",
    "ubereats.", "justeat.", "deliveroo.", "amazon.", "ebay.",
    "wordpress.com", "blogspot.", "wixsite.com", "milanuncios.", "wanderlog.",
    "yellowpages.", "europages.", "kompass.", "guiatelefonica.",
)

# señales de dominio aparcado / en venta / inactivo
PARKED_SIGNALS = (
    # en venta
    "domain for sale", "this domain is for sale", "dominio en venta",
    "buy this domain", "parked domain", "domain parking", "comprar dominio",
    "this domain may be for sale", "is for sale", "está en venta", "en venta",
    "make an offer", "haz una oferta", "purchase this domain", "domain name",
    "the domain", "este dominio", "dominio está", "buy now", "inquire",
    # registradores / parkings conocidos
    "godaddy", "sedo.com", "sedoparking", "hugedomains", "afternic", "dan.com",
    "namecheap", "name.com", "porkbun", "bodis.com", "parkingcrew", "above.com",
    "domainmarket", "undeveloped", "1and1", "ionos", "ovh", "dynadot",
    "this web page is parked", "free parking", "buydomains", "domainnameshop",
    # placeholder / en construcción
    "página en construcción", "pagina en construccion", "under construction",
    "coming soon", "próximamente", "proximamente", "default web page",
    "apache2 ubuntu", "welcome to nginx", "it works!", "test page",
    "web hosting", "site not found", "no website configured", "future home",
    "account suspended", "cuenta suspendida", "this site can", "página no encontrada",
    "directory listing", "index of /", "forbidden", "404 not found",
)

TLD_CANDIDATES = [".cat", ".es", ".com", ".eus", ".gal", ".net"]

STOPWORDS = {
    "the", "and", "los", "las", "del", "de", "la", "el", "sl", "sa", "slu",
    "scp", "cb", "restaurant", "restaurante", "bar", "cafe", "cafeteria",
    "hotel", "tienda", "shop", "store", "studio", "estudio", "centre", "centro",
    "grup", "grupo", "casa", "can", "cal", "espai", "espacio",
}

# Stopwords MÁS LIGERAS solo para generar dominios: conserva 'cal', 'can', 'casa'
# porque en catalán forman parte del nombre del dominio (calpep.cat, canroca.com)
STOPWORDS_DOMAIN = {
    "the", "and", "los", "las", "del", "de", "la", "el", "sl", "sa", "slu",
    "scp", "cb", "restaurant", "restaurante", "bar", "cafe", "cafeteria",
    "hotel", "tienda", "shop", "store", "studio", "estudio",
}


def _domain_tokens(name: str) -> list:
    """Como _name_tokens pero conserva prefijos catalanes (cal, can, casa)."""
    n = _strip_accents(name)
    words = re.findall(r"[a-z0-9]{2,}", n)
    return [w for w in words if w not in STOPWORDS_DOMAIN]


def _strip_accents(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def _slugify(name: str) -> str:
    name = _strip_accents(name)
    for suf in [" sl", " s.l.", " sa", " s.a.", " scp", " s.c.p.", " slu",
                " cb", " s.l", " sociedad limitada"]:
        if name.endswith(suf):
            name = name[: -len(suf)]
    return re.sub(r"[^a-z0-9]", "", name)


def _name_tokens(name: str) -> list:
    n = _strip_accents(name)
    words = re.findall(r"[a-z0-9]{3,}", n)
    return [w for w in words if w not in STOPWORDS]


def _is_official(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if not host:
        return False
    return not any(bad in host for bad in NOT_OFFICIAL)


def _looks_parked(text_lower: str) -> bool:
    return any(sig in text_lower for sig in PARKED_SIGNALS)


async def _fetch(client, url):
    """Descarga una URL. Devuelve (url_final, html) o ('', '')."""
    for scheme_url in ([url] if url.startswith("http") else [f"https://{url}", f"http://{url}"]):
        try:
            r = await client.get(scheme_url, timeout=9.0, follow_redirects=True)
            if r.status_code < 400:
                ctype = r.headers.get("content-type", "")
                html = r.text if "html" in ctype else ""
                return str(r.url), html
        except Exception:
            continue
    return "", ""


def score_candidate(url: str, html: str, name: str, city: str = "",
                    phone: str = "") -> int:
    """
    Puntúa de 0 a 100 la probabilidad de que 'url' sea la web oficial de 'name'.
    Cruza varias evidencias independientes.

    REGLA CLAVE: si el nombre es corto/genérico (ej. "Suma"), que el dominio
    coincida NO basta — se exige confirmación externa (teléfono, ciudad o el
    nombre completo en el contenido). Así evitamos suma.com = otra empresa.
    """
    if not html:
        return 0

    text = _strip_accents(html)
    host = urlparse(url).netloc.lower().replace("www.", "")
    domain_part = host.split(".")[0] if "." in host else host

    # señal negativa fuerte: dominio aparcado / en venta / placeholder
    if _looks_parked(text):
        return 0

    tokens = _name_tokens(name)
    if not tokens:
        return 0

    slug = _slugify(name)

    # ¿El nombre es "genérico"? (corto, una sola palabra, o palabra común)
    # Estos nombres dan muchos falsos positivos por coincidencia de dominio.
    is_generic = (
        len(tokens) == 1 and (len(slug) <= 6 or len(tokens[0]) <= 6)
    )

    score = 0
    domain_matches = False

    # --- EVIDENCIA 1: el dominio contiene el nombre ---
    if slug and slug in host.replace(".", "").replace("-", ""):
        domain_matches = True
        score += 30 if is_generic else 45  # genérico: vale menos
    else:
        main_token = max(tokens, key=len)
        if len(main_token) >= 5 and main_token in domain_part:
            domain_matches = True
            score += 15 if is_generic else 25

    # --- EVIDENCIA 2: palabras del nombre en el CONTENIDO de la web ---
    hits = sum(1 for t in tokens if t in text)
    ratio = hits / len(tokens)
    content_confirms = False
    if ratio >= 1.0 and len(tokens) >= 2:
        score += 30; content_confirms = True   # nombre completo (2+ palabras) presente
    elif ratio >= 0.5 and len(tokens) >= 2:
        score += 18; content_confirms = True
    elif len(tokens) == 1 and tokens[0] in text and len(tokens[0]) >= 7:
        score += 15; content_confirms = True    # 1 palabra MUY distintiva (>=7)

    # --- EVIDENCIA 3: el <title> menciona el nombre ---
    title_confirms = False
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title = _strip_accents(m.group(1))
        if sum(1 for t in tokens if t in title) >= max(1, len(tokens) // 2):
            score += 15; title_confirms = True

    # --- EVIDENCIA 4: la ciudad/zona aparece en la web ---
    city_confirms = False
    if city:
        city_norm = _strip_accents(city)
        if city_norm and len(city_norm) >= 4 and city_norm in text:
            score += 12; city_confirms = True

    # --- EVIDENCIA 5: el teléfono de OSM aparece en la web (señal definitiva) ---
    phone_confirms = False
    if phone:
        phone_digits = re.sub(r"\D", "", phone)
        if len(phone_digits) >= 9:
            text_digits = re.sub(r"\D", "", html)
            if phone_digits[-9:] in text_digits:
                score += 35; phone_confirms = True

    # --- EVIDENCIA 6: TLD local ---
    if host.endswith(".cat") or host.endswith(".es"):
        score += 5

    # === REGLA ANTI-FALSO-POSITIVO ===
    # Si el nombre es genérico y la ÚNICA prueba es que el dominio coincide
    # (sin que el teléfono, la ciudad o el nombre completo lo confirmen),
    # NO nos fiamos: la web probablemente es de otra empresa con el mismo nombre.
    external_confirm = phone_confirms or city_confirms or (content_confirms and len(tokens) >= 2)
    if is_generic and domain_matches and not external_confirm:
        return 0  # rechazo total: "Suma" -> suma.com sin más pruebas = NO

    # Regla general: una web necesita AL MENOS 2 evidencias para ser creíble,
    # salvo que el teléfono coincida (que por sí solo ya es casi definitivo).
    evidences = sum([domain_matches, content_confirms, title_confirms,
                     city_confirms, phone_confirms])
    if evidences < 2 and not phone_confirms:
        return min(score, 40)  # como mucho 40 -> por debajo del umbral, se rechaza

    return min(score, 100)


def _domain_variants(name: str, city: str = "") -> list:
    """
    Genera variantes inteligentes del dominio a partir del nombre (y ciudad).
    Ej. 'Restaurant Cal Pep' en Reus ->
        calpep.cat, cal-pep.cat, restaurantcalpep.cat, calpepreus.com, etc.
    Cada variante luego pasa por el scoring, así que solo se acepta si coincide.
    """
    tokens = _name_tokens(name)            # palabras significativas para scoring
    dtokens = _domain_tokens(name)         # palabras para dominios (conserva cal/can)
    slug_full = _slugify(name)             # nombre entero junto
    slug_key = "".join(dtokens)            # palabras clave juntas (calpep)
    city_slug = _slugify(city) if city else ""

    # bases (el "nombre" del dominio, sin extensión)
    bases = set()
    if slug_full and len(slug_full) >= 4:
        bases.add(slug_full)
    if slug_key and len(slug_key) >= 4:
        bases.add(slug_key)
    # variante con guiones entre palabras clave: cal-pep
    if len(dtokens) >= 2:
        bases.add("-".join(dtokens))
        bases.add("".join(dtokens))
    # variante nombre + ciudad: calpepreus
    if slug_key and city_slug and len(slug_key) >= 4:
        bases.add(slug_key + city_slug)
    # palabra clave principal sola (si es distintiva, >=6 letras): bonpreu
    if tokens:
        main = max(tokens, key=len)
        if len(main) >= 6:
            bases.add(main)

    # extensiones a probar (locales primero)
    tlds = [".cat", ".es", ".com", ".eus", ".gal", ".net", ".org", ".info"]

    # construir todas las combinaciones base + tld, con y sin www
    domains = []
    seen = set()
    for base in bases:
        for tld in tlds:
            d = base + tld
            if d not in seen:
                seen.add(d)
                domains.append(d)

    # límite de seguridad: no probar cientos de dominios (lento y abusivo)
    return domains[:24]


async def _candidates_from_guess(client, name: str, city: str = "") -> list:
    """Genera y devuelve URLs candidatas adivinando el dominio (con variantes)."""
    slug = _slugify(name)
    if len(slug) < 4:
        return []
    return _domain_variants(name, city)


async def _candidates_from_ddg(client, query: str, delay: float) -> list:
    """Obtiene URLs candidatas de DuckDuckGo con su POSICIÓN (rank).
    Devuelve lista de (url, posicion). La posición 0 = primer resultado."""
    await asyncio.sleep(delay)
    out = []
    try:
        r = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query}, timeout=12.0, follow_redirects=True,
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            pos = 0
            for a in soup.select("a.result__a")[:8]:
                href = a.get("href", "")
                mm = re.search(r"uddg=([^&]+)", href)
                if mm:
                    href = unquote(mm.group(1))
                if href.startswith("http") and _is_official(href):
                    out.append((href, pos))
                    pos += 1
    except Exception:
        pass
    return out


async def _candidates_from_bing(client, query: str, delay: float) -> list:
    """Obtiene URLs candidatas de Bing con su posición."""
    await asyncio.sleep(delay)
    out = []
    try:
        r = await client.get(
            "https://www.bing.com/search",
            params={"q": query, "setlang": "es"},
            timeout=12.0, follow_redirects=True,
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            pos = 0
            # en Bing los resultados están en <li class="b_algo"> con un <h2><a>
            for h2 in soup.select("li.b_algo h2 a")[:8]:
                href = h2.get("href", "")
                if href.startswith("http") and _is_official(href):
                    out.append((href, pos))
                    pos += 1
    except Exception:
        pass
    return out


async def _candidates_from_mojeek(client, query: str, delay: float) -> list:
    """Obtiene URLs candidatas de Mojeek (buscador independiente) con su posición."""
    await asyncio.sleep(delay)
    out = []
    try:
        r = await client.get(
            "https://www.mojeek.com/search",
            params={"q": query},
            timeout=12.0, follow_redirects=True,
        )
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            pos = 0
            # Mojeek: resultados en <ul class="results-standard"> con <a class="title">
            for a in soup.select("a.title, ul.results-standard li a")[:8]:
                href = a.get("href", "")
                if href.startswith("http") and _is_official(href):
                    out.append((href, pos))
                    pos += 1
    except Exception:
        pass
    return out


async def _candidates_from_search(client, query: str, delay: float) -> list:
    """
    Combina varios buscadores. Prueba en orden hasta tener resultados:
    DuckDuckGo -> Bing -> Mojeek. Si uno falla o lo bloquean, prueba el siguiente.
    Devuelve lista de (url, posicion) deduplicada.
    """
    engines = [_candidates_from_ddg, _candidates_from_bing, _candidates_from_mojeek]
    combined = []
    seen = set()

    for engine in engines:
        results = await engine(client, query, delay)
        for url, pos in results:
            # normalizar para deduplicar por dominio
            dom = urlparse(url).netloc.lower().replace("www.", "")
            if dom not in seen:
                seen.add(dom)
                combined.append((url, pos))
        # si ya tenemos suficientes candidatas buenas, no hace falta seguir
        if len(combined) >= 5:
            break

    return combined


def website_from_email(emails: list) -> str:
    """
    Deduce la web a partir del dominio de un email de empresa.
    'info@floristeriamar.cat' -> 'https://floristeriamar.cat'
    Solo usa emails de dominio propio (descarta gmail, hotmail, etc.).
    Señal casi definitiva: el negocio usa ese dominio para su correo.
    """
    FREE = {"gmail.com", "hotmail.com", "hotmail.es", "yahoo.com", "yahoo.es",
            "outlook.com", "outlook.es", "live.com", "icloud.com", "me.com",
            "telefonica.net", "terra.es", "wanadoo.es"}
    for email in emails:
        email = (email or "").strip().lower()
        if "@" not in email:
            continue
        domain = email.split("@")[1]
        if domain in FREE:
            continue
        if "." not in domain or len(domain) < 4:
            continue
        return "https://" + domain
    return ""


async def website_from_social(client, social: dict) -> str:
    """
    Visita el perfil de redes sociales del negocio y extrae el enlace a su web.
    El negocio pone su web oficial en la bio de Instagram/Facebook -> fiabilidad alta.
    Devuelve la URL de la web o '' si no encuentra.
    """
    # orden de preferencia: las que suelen tener web en bio
    for net in ("facebook", "instagram", "linkedin"):
        url = social.get(net)
        if not url:
            continue
        try:
            r = await client.get(url, timeout=10.0, follow_redirects=True)
            if r.status_code != 200:
                continue
            html = r.text
            # buscar URLs externas que NO sean la propia red social ni otra red
            candidates = re.findall(r'https?://[^\s"\'<>)]+', html)
            for c in candidates:
                host = urlparse(c).netloc.lower()
                if not host:
                    continue
                # descartar la propia red social y otras redes/CDN
                if _is_official(c) and not any(
                    bad in host for bad in
                    ("facebook", "instagram", "linkedin", "fbcdn", "cdninstagram",
                     "licdn", "twitter", "x.com", "youtube", "tiktok", "whatsapp",
                     "google", "gstatic", "bit.ly", "linktr.ee")
                ):
                    # heurística: dominio corto y con pinta de web de empresa
                    if host.count(".") <= 2 and len(host) < 40:
                        return c.split("?")[0].rstrip("/")
        except Exception:
            continue
    return ""


async def find_website(name: str, city: str = "", phone: str = "",
                       delay: float = 1.0, mode: str = "strict") -> dict:
    """
    Busca la web oficial de una empresa con SCORING de confianza.
    mode:
      - "off":    no busca nada
      - "strict": solo acepta si confianza >= umbral alto (recomendado, PRO)
      - "loose":  umbral más bajo (más cobertura, algún error)
    Devuelve {'website': url, 'method': 'guess'|'search'|'', 'confidence': int}.
    """
    if mode == "off":
        return {"website": "", "method": "", "confidence": 0}

    threshold = CONFIDENCE_THRESHOLD if mode == "strict" else 40
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "es,ca,en"}

    best = {"website": "", "method": "", "confidence": 0}

    async with httpx.AsyncClient(headers=headers) as client:
        # 1) candidatas por adivinación de dominio
        guesses = await _candidates_from_guess(client, name, city)
        for cand in guesses:
            url, html = await _fetch(client, cand)
            if url and _is_official(url):
                conf = score_candidate(url, html, name, city, phone)
                if conf > best["confidence"]:
                    best = {"website": url, "method": "guess", "confidence": conf}
            # si ya tenemos una con altísima confianza, paramos
            if best["confidence"] >= 85:
                break

        # 2) si no hay una clara, buscar en DuckDuckGo y puntuar resultados
        if best["confidence"] < threshold:
            query = f"{name} {city}".strip()
            ddg = await _candidates_from_search(client, query, delay)
            for url0, pos in ddg:
                url, html = await _fetch(client, url0)
                if url and _is_official(url):
                    conf = score_candidate(url, html, name, city, phone)
                    # BONUS por ranking: si el buscador lo pone arriba Y ya hay
                    # algo de coincidencia real (conf>0), es buena señal.
                    if conf > 0:
                        if pos == 0:
                            conf += 12   # primer resultado: el buscador lo "vota"
                        elif pos == 1:
                            conf += 6
                        conf = min(conf, 100)
                    if conf > best["confidence"]:
                        best = {"website": url, "method": "search", "confidence": conf}
                if best["confidence"] >= 85:
                    break

    # decisión final: solo aceptar si supera el umbral
    if best["confidence"] >= threshold:
        return best
    return {"website": "", "method": "", "confidence": best["confidence"]}