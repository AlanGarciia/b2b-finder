"""
Scraper mejorado de webs de empresa: extrae emails, redes sociales y tecnologías.
Mejoras clave para encontrar MÁS emails:
- Desofusca emails escritos como 'hola [arroba] empresa.cat', 'info(at)x(dot)es'
- Sigue los enlaces reales de contacto/aviso legal de la web
- Lee atributos data-email y JSON embebido
- Más rutas típicas de webs españolas/catalanas
- Prioriza el email del dominio propio sobre gmails genéricos
Respeta robots.txt y aplica rate limiting / timeouts.
"""
import re
import asyncio
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from tech_detect import detect_technologies

USER_AGENT = "Mozilla/5.0 (compatible; B2BFinderBot/0.2; +contacto@ejemplo.com)"

CONTACT_PATHS = [
    "", "contacto", "contact", "contacta", "contacte", "contactar", "contactanos",
    "aviso-legal", "avis-legal", "legal", "legal-notice", "politica-privacidad",
    "privacidad", "privacitat", "about", "about-us", "sobre-nosotros", "nosotros",
    "qui-som", "quienes-somos", "empresa", "la-empresa", "equipo", "team",
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# El símbolo @/(at)/(arroba) debe ir entre corchetes/paréntesis O con espacios alrededor,
# nunca pegado como letras dentro de una palabra (evita 'features' -> 'fe@ures').
OBFUSCATED_RE = re.compile(
    r"([a-zA-Z0-9._%+\-]+)\s*"
    r"(?:[\[\(]\s*(?:@|at|arroba|ad)\s*[\]\)]|\s(?:at|arroba|ad)\s|\s@\s)"
    r"\s*([a-zA-Z0-9\-]+)\s*"
    r"(?:[\[\(]\s*(?:\.|dot|punt|punto)\s*[\]\)]|\s(?:dot|punt|punto)\s|\s\.\s|\.)"
    r"\s*([a-zA-Z]{2,})",
    re.IGNORECASE,
)

SOCIAL_PATTERNS = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+"),
    "facebook":  re.compile(r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.\-]+"),
    "linkedin":  re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in)/[A-Za-z0-9_.\-%]+"),
    "twitter":   re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/[A-Za-z0-9_]+"),
    "youtube":   re.compile(r"https?://(?:www\.)?youtube\.com/(?:c/|channel/|@|user/)[A-Za-z0-9_.\-]+"),
    "tiktok":    re.compile(r"https?://(?:www\.)?tiktok\.com/@[A-Za-z0-9_.]+"),
    "whatsapp":  re.compile(r"https?://(?:wa\.me|api\.whatsapp\.com)/[0-9]+"),
}

CONTACT_LINK_WORDS = ("contact", "contacta", "contacte", "aviso legal", "avís legal",
                      "legal", "privacidad", "privacitat", "nosotros", "qui som",
                      "quiénes", "empresa", "escríbenos", "escribenos")

EMAIL_BLOCKLIST = ("example.com", "sentry.io", "wixpress.com", "@2x", ".png",
                   ".jpg", ".jpeg", ".gif", ".webp", ".svg", "@sentry", "domain.com",
                   "email.com", "yourdomain", "tudominio", "tuempresa", "@x.com",
                   ".js", ".css", ".woff", ".ttf", ".module", "githubusercontent",
                   "@ion.", "@ures.", "@dresses.", "@us.com")

# el dominio del email debe tener un TLD alfabético válido tras el último punto
VALID_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

IMG_TAIL_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg)$", re.IGNORECASE)


VALID_TLDS = {"com", "es", "cat", "net", "org", "eu", "info", "biz", "io",
              "co", "es", "gal", "eus", "shop", "online", "store", "pro",
              "com.es", "barcelona", "madrid", "tech", "agency", "studio"}


def _find_obfuscated(text: str) -> set:
    found = set()
    for m in OBFUSCATED_RE.finditer(text):
        local, domain, tld = m.group(1), m.group(2), m.group(3)
        if tld.lower() not in VALID_TLDS:
            continue
        candidate = f"{local}@{domain}.{tld}".lower()
        if EMAIL_RE.match(candidate) and len(local) >= 2 and len(domain) >= 3:
            found.add(candidate)
    return found


def _clean_emails(emails: set, site_domain: str = "") -> list:
    out = set()
    for e in emails:
        el = e.lower().strip(".,;:")
        if any(b in el for b in EMAIL_BLOCKLIST):
            continue
        if IMG_TAIL_RE.search(el):
            continue
        if len(el) > 100 or el.count("@") != 1:
            continue
        if not VALID_EMAIL_RE.match(el):
            continue
        # el TLD final debe ser solo letras (descarta foo@bar.123 y restos de código)
        tld = el.rsplit(".", 1)[-1]
        if not tld.isalpha() or len(tld) > 12:
            continue
        out.add(el)

    site_domain = site_domain.lower().replace("www.", "")

    def rank(email):
        dom = email.split("@")[1]
        if site_domain and site_domain in dom:
            return 0
        if dom in ("gmail.com", "hotmail.com", "yahoo.es", "outlook.com"):
            return 2
        return 1

    return sorted(out, key=lambda e: (rank(e), e))


def _robots_allowed(base_url: str, path_url: str) -> bool:
    try:
        parsed = urlparse(base_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(USER_AGENT, path_url)
    except Exception:
        return True


async def _fetch(client: httpx.AsyncClient, url: str):
    try:
        r = await client.get(url, timeout=12.0, follow_redirects=True)
        ctype = r.headers.get("content-type", "")
        if "text/html" not in ctype and "application/xhtml" not in ctype:
            return None, dict(r.headers)
        return r.text, dict(r.headers)
    except Exception:
        return None, {}


def _extract_from_html(html: str, soup: BeautifulSoup) -> set:
    emails = set()
    for a in soup.select('a[href^="mailto:"]'):
        addr = a.get("href", "")[7:].split("?")[0].strip()
        if addr:
            emails.add(addr)
    for el in soup.find_all(attrs={"data-email": True}):
        emails.add(el["data-email"])
    for el in soup.find_all(attrs={"data-mail": True}):
        emails.add(el["data-mail"])
    for m in EMAIL_RE.findall(html):
        emails.add(m)
    text = soup.get_text(" ")
    emails |= _find_obfuscated(text)
    return emails


def _collect_social(soup, html, social):
    hrefs = " ".join(a.get("href", "") for a in soup.find_all("a"))
    haystack = hrefs + " " + html
    for net, pat in SOCIAL_PATTERNS.items():
        if net not in social:
            f = pat.search(haystack)
            if f:
                social[net] = f.group(0)


async def scrape_company(website: str, delay: float = 0.8, max_pages: int = 8) -> dict:
    if not website:
        return {"emails": [], "social": {}, "technologies": [], "error": "no_website"}

    if not website.startswith("http"):
        website = "https://" + website

    base = website.rstrip("/")
    site_domain = urlparse(base).netloc
    emails = set()
    social = {}
    technologies = set()
    pages_ok = 0
    visited = set()

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "es,ca,en",
               "Accept": "text/html,application/xhtml+xml"}

    async with httpx.AsyncClient(headers=headers) as client:
        home_html, home_headers = await _fetch(client, base)
        discovered = []
        if home_html:
            visited.add(base)
            pages_ok += 1
            soup = BeautifulSoup(home_html, "html.parser")
            emails |= _extract_from_html(home_html, soup)
            for t in detect_technologies(home_html, home_headers):
                technologies.add(t)
            _collect_social(soup, home_html, social)
            for a in soup.find_all("a", href=True):
                txt = (a.get_text() or "").lower().strip()
                href = a["href"]
                if any(w in txt for w in CONTACT_LINK_WORDS):
                    full = urldefrag(urljoin(base + "/", href))[0]
                    if full.startswith(base) and full not in visited:
                        discovered.append(full)

        candidates = discovered + [
            urldefrag(urljoin(base + "/", p))[0] for p in CONTACT_PATHS if p
        ]
        seen = set()
        candidates = [u for u in candidates if not (u in seen or seen.add(u))]

        for url in candidates:
            if pages_ok >= max_pages:
                break
            if url in visited:
                continue
            if not _robots_allowed(base, url):
                continue
            html, resp_headers = await _fetch(client, url)
            await asyncio.sleep(delay)
            visited.add(url)
            if html is None:
                continue
            pages_ok += 1
            soup = BeautifulSoup(html, "html.parser")
            emails |= _extract_from_html(html, soup)
            for t in detect_technologies(html, resp_headers):
                technologies.add(t)
            _collect_social(soup, html, social)
            clean = _clean_emails(emails, site_domain)
            if clean and site_domain.replace("www.", "") in clean[0].split("@")[1]:
                if len(social) >= 1:
                    break

    return {
        "emails": _clean_emails(emails, site_domain),
        "social": social,
        "technologies": sorted(technologies),
        "pages_scraped": pages_ok,
        "error": None if pages_ok else "unreachable",
    }