"""
Healthcheck script — appelé par Docker HEALTHCHECK.
Fait un GET /health et sort avec le code 0 (ok) ou 1 (erreur).
Usage : python healthcheck.py
"""

import sys
from urllib.request import urlopen
from urllib.error import URLError

try:
    with urlopen("http://localhost:8000/health", timeout=5) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except (URLError, OSError):
    sys.exit(1)
