"""
Verificación de emails sin APIs de pago.
- Valida el formato.
- Comprueba que el dominio tiene registros MX (puede recibir correo).
- Marca emails genéricos (info@, contacto@) vs personales.

No garantiza que el buzón exista (eso requiere SMTP probing, arriesgado y
a menudo bloqueado), pero filtra >80% de la basura: dominios muertos,
typos de dominio, y direcciones de imágenes/placeholders.
"""
import re
import socket
import asyncio
from functools import lru_cache

try:
    import dns.resolver  # dnspython
    HAS_DNS = True
except ImportError:
    HAS_DNS = False

EMAIL_FORMAT = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

GENERIC_LOCAL_PARTS = {
    "info", "contacto", "contact", "hello", "hola", "admin", "office",
    "mail", "correo", "general", "comercial", "ventas", "sales", "rrhh",
    "support", "soporte", "atencion", "administracion", "no-reply", "noreply",
}

# dominios de email desechables / proveedores que no son la empresa
FREE_PROVIDERS = {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com",
                  "yahoo.es", "hotmail.es", "live.com", "icloud.com"}


@lru_cache(maxsize=2048)
def _has_mx(domain: str) -> bool:
    """¿El dominio acepta correo? (cache para no repetir consultas DNS)."""
    if HAS_DNS:
        try:
            records = dns.resolver.resolve(domain, "MX", lifetime=5.0)
            return len(records) > 0
        except Exception:
            # fallback: ¿al menos resuelve a una IP? (algunos dominios usan A para mail)
            try:
                socket.gethostbyname(domain)
                return True
            except Exception:
                return False
    else:
        # sin dnspython: comprobamos solo que el dominio resuelve
        try:
            socket.gethostbyname(domain)
            return True
        except Exception:
            return False


def verify_email(email: str) -> dict:
    """Devuelve dict con el estado de validación de un email."""
    email = (email or "").strip().lower()
    result = {
        "email": email,
        "valid_format": False,
        "domain_ok": False,
        "is_generic": False,
        "is_free_provider": False,
        "score": 0,  # 0-100, calidad estimada del lead
    }

    if not EMAIL_FORMAT.match(email):
        return result
    result["valid_format"] = True

    local, _, domain = email.partition("@")
    result["is_generic"] = local in GENERIC_LOCAL_PARTS
    result["is_free_provider"] = domain in FREE_PROVIDERS
    result["domain_ok"] = _has_mx(domain)

    # scoring: dominio propio + formato válido + correo personal = mejor lead
    score = 0
    if result["valid_format"]:
        score += 30
    if result["domain_ok"]:
        score += 40
    if not result["is_free_provider"]:
        score += 20  # email corporativo vale más que gmail
    if not result["is_generic"]:
        score += 10  # email personal > genérico
    result["score"] = score
    return result


async def verify_emails(emails: list[str]) -> list[dict]:
    """Verifica una lista de emails en paralelo (las consultas DNS son bloqueantes,
    las lanzamos en un threadpool)."""
    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(None, verify_email, e) for e in emails]
    return await asyncio.gather(*tasks)


def best_emails(verified: list[dict], min_score: int = 50) -> list[str]:
    """Devuelve los emails que superan el umbral, ordenados por calidad."""
    good = [v for v in verified if v["score"] >= min_score]
    good.sort(key=lambda v: v["score"], reverse=True)
    return [v["email"] for v in good]
