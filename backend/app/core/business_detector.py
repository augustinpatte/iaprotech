import re


BUSINESS_PATTERNS = {
    "REVENUE": [
        r"(?i)(chiffre.d.affaire|revenue|CA\s*[:=]\s*[\d\s,.]+[€$])",
        r"(?i)(turnover|ventes\s*totales)",
    ],
    "MARGIN": [
        r"(?i)(marge\s*[:=]\s*[\d\s,.]+[%€$]|margin|gross.profit)",
    ],
    "PRICING": [
        r"(?i)(prix\s*client|tarif\s*confidentiel|pricing|price.list)",
        r"(?i)(remise\s*[:=]\s*[\d]+%|discount)",
    ],
    "FORECAST": [
        r"(?i)(prévision|forecast|pipeline|budget\s*\d{4})",
    ],
    "CLIENT_LIST": [
        r"(?i)(liste.clients|client.list|portefeuille.client)",
    ],
    "DEAL": [
        r"(?i)(deal|opportunité|opportunity|closing|signed\s*contract)",
    ],
}


class BusinessDetector:
    def detect(self, text: str) -> list[dict]:
        findings = []
        for category, patterns in BUSINESS_PATTERNS.items():
            for pattern in patterns:
                matches = re.findall(pattern, text)
                if matches:
                    findings.append(
                        {
                            "category": category,
                            "matches": matches[:3],
                            "recommendation": (
                                f"Données {category} détectées — recommande mode STRICT"
                            ),
                        }
                    )
        return findings
