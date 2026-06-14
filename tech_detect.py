"""
Detección de tecnologías sin APIs de pago.
Replica la idea de Wappalyzer/BuiltWith con fingerprints sobre HTML y headers.
Amplía FINGERPRINTS añadiendo más patrones según necesites.
"""
import re

# Cada tech: lista de señales. Si UNA coincide, se marca como detectada.
# tipos de señal: ("html", regex) | ("header", "Nombre-Header", regex) | ("script_src", regex)
FINGERPRINTS = {
    # --- CMS ---
    "WordPress":   [("html", r"wp-content|wp-includes"), ("header", "Link", r"wp\.me")],
    "Joomla":      [("html", r"/media/jui/|Joomla!")],
    "Drupal":      [("html", r"Drupal\.settings|/sites/default/files"), ("header", "X-Generator", r"Drupal")],
    "Wix":         [("html", r"wix\.com|_wixCssStates|static\.wixstatic")],
    "Squarespace": [("html", r"static1\.squarespace|Squarespace")],
    "Webflow":     [("html", r"webflow\.js|wf-page|data-wf-")],

    # --- E-commerce ---
    "Shopify":     [("html", r"cdn\.shopify\.com|Shopify\.theme|shopify-section")],
    "WooCommerce": [("html", r"woocommerce|wc-block")],
    "PrestaShop":  [("html", r"prestashop|/themes/.+?/assets")],
    "Magento":     [("html", r"Magento|mage/cookies|/static/version")],

    # --- Frameworks JS ---
    "Next.js":     [("html", r"__NEXT_DATA__|/_next/static"), ("header", "X-Powered-By", r"Next\.js")],
    "Nuxt.js":     [("html", r"__NUXT__|/_nuxt/")],
    "React":       [("html", r"data-reactroot|react\.production\.min|_reactListening")],
    "Vue.js":      [("html", r"data-v-[0-9a-f]{8}|vue\.runtime")],
    "Angular":     [("html", r"ng-version|ng-app|angular\.js")],
    "Svelte":      [("html", r"svelte-[0-9a-z]+")],

    # --- Backend / servidores ---
    "PHP":         [("header", "X-Powered-By", r"PHP"), ("header", "Set-Cookie", r"PHPSESSID")],
    "Laravel":     [("header", "Set-Cookie", r"laravel_session|XSRF-TOKEN")],
    "ASP.NET":     [("header", "X-Powered-By", r"ASP\.NET"), ("header", "X-AspNet-Version", r".")],
    "Express":     [("header", "X-Powered-By", r"Express")],
    "Nginx":       [("header", "Server", r"nginx")],
    "Apache":      [("header", "Server", r"Apache")],
    "Cloudflare":  [("header", "Server", r"cloudflare"), ("header", "CF-RAY", r".")],

    # --- Analytics / marketing ---
    "Google Analytics": [("html", r"google-analytics\.com|gtag\(|googletagmanager\.com/gtag")],
    "Google Tag Manager":[("html", r"googletagmanager\.com/gtm")],
    "Meta Pixel":  [("html", r"connect\.facebook\.net/.+?/fbevents|fbq\(")],
    "HubSpot":     [("html", r"js\.hs-scripts\.com|hsforms")],
    "Hotjar":      [("html", r"static\.hotjar\.com|hjSetting")],

    # --- Otros ---
    "jQuery":      [("html", r"jquery[.-][0-9]|jquery\.min\.js")],
    "Bootstrap":   [("html", r"bootstrap(\.min)?\.css|bootstrap(\.min)?\.js")],
    "Tailwind CSS":[("html", r"tailwind|--tw-")],
    "Font Awesome":[("html", r"font-?awesome|fa-[a-z]+")],
    "reCAPTCHA":   [("html", r"google\.com/recaptcha|grecaptcha")],
}


def detect_technologies(html: str, headers: dict) -> list[str]:
    """Devuelve lista ordenada de tecnologías detectadas."""
    html = html or ""
    # normaliza headers a lower-case keys
    h = {k.lower(): v for k, v in (headers or {}).items()}
    found = set()

    for tech, signals in FINGERPRINTS.items():
        for sig in signals:
            try:
                if sig[0] == "html":
                    if re.search(sig[1], html, re.IGNORECASE):
                        found.add(tech)
                        break
                elif sig[0] == "header":
                    val = h.get(sig[1].lower(), "")
                    if val and re.search(sig[2], val, re.IGNORECASE):
                        found.add(tech)
                        break
            except re.error:
                continue
    return sorted(found)
