# Image base pinned à un tag semver patch.
# Pour update : vérifier les nouvelles patches sur hub.docker.com, mettre à jour le tag, rebuilder, tester.
# Cible de fréquence : trimestrielle.

# ============================================================
# Stage 1 : dependances Python
# ============================================================
FROM python:3.11.15-slim AS deps

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    tesseract-ocr \
    tesseract-ocr-fra \
    libglib2.0-0 \
    libmagic1 \
    libmagic-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --upgrade \
    "pip>=25.0" \
    "setuptools>=80.9.0" \
    "wheel>=0.46.2"
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# ============================================================
# Stage 2 : telechargement des modeles spaCy
# Installation via pip + URLs directes (evite les timeouts GitHub)
# ============================================================
FROM deps AS models

RUN pip install --no-cache-dir --require-hashes \
    "fr_core_news_lg @ https://github.com/explosion/spacy-models/releases/download/fr_core_news_lg-3.7.0/fr_core_news_lg-3.7.0-py3-none-any.whl#sha256=c2d98eb9073ff0f3d865179448e7f1868f27b2643c0e7215acc2438cd9eae554" \
    "en_core_web_lg @ https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.7.1/en_core_web_lg-3.7.1-py3-none-any.whl#sha256=ab70aeb6172cde82508f7739f35ebc9918a3d07debeed637403c8f794ba3d3dc" \
    "de_core_news_lg @ https://github.com/explosion/spacy-models/releases/download/de_core_news_lg-3.7.0/de_core_news_lg-3.7.0-py3-none-any.whl#sha256=1451e06dedb5c3bb096127bf7c2117c253265fdfcad29ee488ff57bec5f78eaa" \
    "es_core_news_lg @ https://github.com/explosion/spacy-models/releases/download/es_core_news_lg-3.7.0/es_core_news_lg-3.7.0-py3-none-any.whl#sha256=08020b83e0c6da1584e567551a5e0de7b15dc0534eaaee21acc1ce908d1be742"

# ============================================================
# Stage 3 : runtime final
# ============================================================
FROM python:3.11.15-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-fra \
    libglib2.0-0 \
    libmagic1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copier les packages Python installes (stage deps + models)
COPY --from=models /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=models /usr/local/bin /usr/local/bin
RUN pip install --no-cache-dir --upgrade \
    "pip>=25.0" \
    "setuptools>=80.9.0" \
    "wheel>=0.46.2"

# Copier le code de l'application
ARG CACHEBUST=1
COPY . .

# Utilisateur non-root pour la securite
RUN groupadd --gid 1001 appgroup \
 && useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser \
 && chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=15s --start-period=300s --retries=3 \
    CMD python healthcheck.py

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "30"]
