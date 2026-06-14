"""
Módulo de cumplimiento RGPD para datos B2B revendibles.

NO es asesoría legal — es una implementación de buenas prácticas que facilita
cumplir el RGPD. Antes de revender datos personales en España, consulta con un
abogado o gestoría especializada en protección de datos.

Principios implementados:
  1. Distinguir datos DE EMPRESA (más seguros) de datos PERSONALES (restringidos).
  2. Registrar la procedencia de cada dato (trazabilidad).
  3. Respetar una lista de exclusión (opt-out / derecho de supresión).
"""
import re

# Prefijos de email que son claramente GENÉRICOS de empresa (no de una persona).
# Estos son los más seguros para marketing B2B bajo interés legítimo.
BUSINESS_LOCAL_PARTS = {
    "info", "contacto", "contact", "hello", "hola", "admin", "office",
    "mail", "correo", "general", "comercial", "ventas", "sales", "rrhh",
    "support", "soporte", "atencion", "administracion", "no-reply", "noreply",
    "reservas", "booking", "pedidos", "orders", "facturacion", "billing",
    "marketing", "prensa", "press", "hi", "team", "equipo", "empresa",
    "gerencia", "direccion", "secretaria", "recepcion", "citas", "cita",
}

# Patrón que sugiere un email PERSONAL: nombre.apellido@, inicial+apellido@, etc.
PERSONAL_EMAIL_HINT = re.compile(
    r"^[a-z]+[._\-][a-z]+@|^[a-z]\.[a-z]+@|^[a-z]+[0-9]{0,3}@",
    re.IGNORECASE,
)


def classify_email(email: str) -> str:
    """
    Clasifica un email como 'empresa' o 'personal'.
    Los genéricos (info@, ventas@) son 'empresa'.
    Los que parecen nombre.apellido@ son 'personal' (más sensibles).
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return "desconocido"
    local = email.split("@")[0]

    if local in BUSINESS_LOCAL_PARTS:
        return "empresa"
    # nombre.apellido o similar => probablemente personal
    if PERSONAL_EMAIL_HINT.match(email) and local not in BUSINESS_LOCAL_PARTS:
        # heurística: si tiene separador entre dos palabras, parece nombre de persona
        if re.search(r"[._\-]", local):
            return "personal"
    # por defecto, lo tratamos como empresa (genérico) salvo señales claras
    return "empresa"


def split_emails(emails: list) -> dict:
    """Separa una lista de emails en de empresa vs personales."""
    business, personal = [], []
    for e in emails:
        if classify_email(e) == "personal":
            personal.append(e)
        else:
            business.append(e)
    return {"business": business, "personal": personal}


def has_personal_data(emails: list) -> bool:
    """¿La empresa tiene algún dato clasificado como personal?"""
    return any(classify_email(e) == "personal" for e in emails)


def filter_for_resale(company: dict, include_personal: bool = False) -> dict:
    """
    Devuelve una copia de la empresa lista para EXPORTAR/REVENDER.
    Por defecto, EXCLUYE los emails personales (modo seguro RGPD).
    Solo incluye datos personales si include_personal=True (bajo tu responsabilidad).
    """
    out = dict(company)
    emails = company.get("emails", []) or []
    split = split_emails(emails)
    if include_personal:
        out["emails"] = emails
    else:
        out["emails"] = split["business"]  # solo genéricos de empresa
    out["_excluded_personal_count"] = len(split["personal"])
    return out


# --- Lista de exclusión (opt-out) ---

def is_excluded(email: str, opt_out_set: set) -> bool:
    """¿Este email (o su dominio) está en la lista de exclusión?"""
    email = (email or "").strip().lower()
    if email in opt_out_set:
        return True
    if "@" in email:
        domain = email.split("@")[1]
        if domain in opt_out_set:
            return True
    return False


def apply_opt_out(emails: list, opt_out_set: set) -> list:
    """Quita de la lista los emails/dominios excluidos."""
    return [e for e in emails if not is_excluded(e, opt_out_set)]


# Texto de atribución obligatorio por la licencia de OpenStreetMap (ODbL)
ATTRIBUTION_TEXT = "Datos de empresas: © colaboradores de OpenStreetMap (ODbL)."