"""
Detección de "señales de venta" (sales signals).
Esto es lo que convierte datos en bruto en LEADS VENDIBLES.

La idea: una agencia/freelance no quiere "todas las empresas", quiere
"empresas que necesitan mi servicio AHORA". Estas señales identifican eso.
"""

# Tecnologías que indican una web anticuada / mejorable (oportunidad de rediseño)
OUTDATED_TECH = {"jQuery", "Bootstrap", "PHP", "WordPress"}
MODERN_TECH = {"Next.js", "Nuxt.js", "React", "Vue.js", "Svelte", "Tailwind CSS"}


def detect_signals(company: dict) -> dict:
    """
    Analiza una empresa y devuelve señales de oportunidad comercial.
    `company` debe tener: website, emails, social, technologies.
    """
    website = (company.get("website") or "").strip()
    emails = company.get("emails") or []
    social = company.get("social") or {}
    techs = set(company.get("technologies") or [])

    signals = []
    priority = 0  # mayor = lead más caliente para servicios web/marketing

    # --- SIN WEB: el lead estrella para vender diseño web ---
    if not website:
        signals.append("sin_web")
        priority += 50

    # --- WEB ANTICUADA: oportunidad de rediseño ---
    if website and techs:
        if "WordPress" in techs and not (techs & MODERN_TECH):
            signals.append("wordpress_clasico")
            priority += 20
        if techs.isdisjoint(MODERN_TECH) and (techs & OUTDATED_TECH):
            signals.append("stack_anticuado")
            priority += 15
        if "Wix" in techs or "Squarespace" in techs:
            signals.append("web_plantilla")  # web de plantilla = margen de mejora
            priority += 10

    # --- SIN PRESENCIA EN REDES: oportunidad de marketing/social ---
    if not social:
        signals.append("sin_redes")
        priority += 15
    elif len(social) == 1:
        signals.append("pocas_redes")
        priority += 8

    # --- SIN EMAIL DE CONTACTO: difícil de contactar (pero hay teléfono) ---
    if not emails and company.get("phone"):
        signals.append("solo_telefono")
        priority += 5

    # --- TIENE ECOMMERCE: cliente con presupuesto (vende online) ---
    if techs & {"Shopify", "WooCommerce", "PrestaShop", "Magento"}:
        signals.append("tiene_ecommerce")
        priority += 10

    # --- NO MIDE NADA: sin analytics = oportunidad de consultoría datos ---
    if website and not (techs & {"Google Analytics", "Google Tag Manager", "Meta Pixel"}):
        signals.append("sin_analitica")
        priority += 5

    # clasificación legible
    if priority >= 50:
        heat = "muy_caliente"
    elif priority >= 25:
        heat = "caliente"
    elif priority >= 10:
        heat = "tibio"
    else:
        heat = "frio"

    return {
        "signals": signals,
        "priority": priority,
        "heat": heat,
    }


# Descripción legible de cada señal (para mostrar en la UI / al vender)
SIGNAL_LABELS = {
    "sin_web":          "No tiene web — candidato a diseño web",
    "wordpress_clasico":"WordPress clásico — candidato a rediseño",
    "stack_anticuado":  "Tecnología anticuada — candidato a modernización",
    "web_plantilla":    "Web de plantilla (Wix/Squarespace) — mejorable",
    "sin_redes":        "Sin redes sociales — candidato a social media",
    "pocas_redes":      "Poca presencia en redes",
    "solo_telefono":    "Solo teléfono, sin email",
    "tiene_ecommerce":  "Tiene tienda online — con presupuesto",
    "sin_analitica":    "No mide tráfico — candidato a analítica",
}
