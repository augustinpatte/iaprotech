# Privacy Proxy

Proxy RGPD pour APIs LLM. Pseudonymise automatiquement les données personnelles avant leur envoi à un fournisseur d'IA, puis restaure les données réelles dans la réponse — le LLM ne voit jamais les PII.

---

## Description

Privacy Proxy est un service intermédiaire conforme au **RGPD Art. 5** qui s'intercale entre vos utilisateurs et les APIs LLM (Anthropic, OpenAI). Il implémente la **pseudonymisation réversible** : chaque entité personnelle détectée est remplacée par un jeton opaque (`[PERSONNE_1]`, `[EMAIL_2]`…), le texte pseudonymisé est envoyé au LLM, et les jetons sont remplacés par les données réelles dans la réponse retournée à l'utilisateur.

**Ce que le fournisseur LLM reçoit :**
```
"Bonjour, je suis [PERSONNE_1]. Mon email est [EMAIL_1] et mon IBAN [IBAN_1]."
```

**Ce que l'utilisateur voit :**
```
"Bonjour, je suis Jean Dupont. Mon email est jean@exemple.fr et mon IBAN FR76…"
```

Le mapping `[PERSONNE_1] ↔ Jean Dupont` est chiffré AES-256 et stocké dans Redis avec une TTL configurable. Il n'est jamais transmis au LLM.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Privacy Proxy Pipeline                       │
│                                                                     │
│  Utilisateur                                                        │
│      │                                                              │
│      │ POST /api/chat/stream  (texte avec PII)                      │
│      ▼                                                              │
│  ┌──────────┐    ┌────────────────────────────────────────────┐     │
│  │ Frontend │───►│             Backend FastAPI                 │     │
│  │  React   │    │                                            │     │
│  └──────────┘    │  1. AuthMiddleware — valide JWT            │     │
│                  │  2. RateLimit     — 60 req/min par user    │     │
│                  │  3. Redactor      — détecte les PII        │     │
│                  │       │ (Presidio + spaCy fr/en/de/es)     │     │
│                  │       ▼                                    │     │
│                  │  4. Vault.store   — chiffre le mapping     │     │
│                  │       │ (AES-256-GCM → Redis + TTL)        │     │
│                  │       ▼                                    │     │
│                  │  5. LLM API       — prompt pseudonymisé    │     │
│                  │       │ (Anthropic / OpenAI)               │     │
│                  │       ▼                                    │     │
│                  │  6. Reidentifier  — restaure les PII       │     │
│                  │       │ (buffer intelligent, SSE)          │     │
│                  │       ▼                                    │     │
│                  │  7. Réponse       — texte complet à l'user │     │
│                  └────────────────────────────────────────────┘     │
│                                                                     │
│  Vault (Redis)  ──  mappings chiffrés, TTL 1h, AOF activé          │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Installation rapide

### Prérequis

- [Docker](https://docs.docker.com/get-docker/) et Docker Compose
- Une clé API Anthropic ou OpenAI

### 1. Cloner le dépôt

```bash
git clone <url-du-depot>
cd privacy-proxy
```

### 2. Configurer l'environnement

```bash
cp .env.example .env
```

Générer les clés secrètes obligatoires :

```bash
# SECRET_KEY (JWT signing)
python -c "import secrets; print(secrets.token_hex(32))"

# VAULT_ENCRYPTION_KEY (AES-256 via Fernet)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Remplir `.env` avec vos valeurs (voir section [Variables d'environnement](#variables-denvironnement)).

### 3. Lancer la stack

```bash
docker compose up --build
```

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| Backend API | http://localhost:8000 |
| Swagger (debug uniquement) | http://localhost:8000/docs |

Identifiants par défaut : `admin` / `changeme` — **à changer immédiatement en production**.

---

## Variables d'environnement

| Variable | Description | Exemple |
|---|---|---|
| `SECRET_KEY` | Clé de signature JWT (min. 32 chars, aléatoire) | `a3f8...` |
| `VAULT_ENCRYPTION_KEY` | Clé Fernet AES-256 pour chiffrement des mappings PII | `gAAAAAB...` |
| `LLM_PROVIDER` | Fournisseur LLM actif | `anthropic` ou `openai` |
| `DEFAULT_MODEL` | Modèle utilisé par défaut | `claude-3-5-sonnet-20241022` |
| `ANTHROPIC_API_KEY` | Clé API Anthropic (obligatoire si provider=anthropic) | `sk-ant-api03-...` |
| `OPENAI_API_KEY` | Clé API OpenAI (obligatoire si provider=openai) | `sk-proj-...` |
| `REDIS_URL` | URL de connexion Redis | `redis://localhost:6379/0` |
| `REDIS_TTL_SECONDS` | TTL des mappings PII en secondes | `3600` |
| `REDIS_PASSWORD` | Mot de passe Redis | `changeme` |
| `TOKEN_EXPIRE_MINUTES` | Durée de validité des tokens JWT | `60` |
| `MAX_REQUESTS_PER_MINUTE` | Limite de requêtes par utilisateur | `60` |
| `MAX_FILE_SIZE_MB` | Taille maximale des fichiers uploadés | `5` |
| `ALLOWED_FILE_TYPES` | Extensions derivees de la matrice `SUPPORTED_FILE_FORMATS` | `["pdf","docx","txt","xlsx","xls","csv","pptx","jpg","jpeg","png","eml"]` |
| `REDACTION_ENGINE` | Moteur de pseudonymisation | `presidio` |
| `SUPPORTED_LANGUAGES` | Langues analysées par Presidio | `["fr","en","de","es"]` |
| `ALLOWED_ORIGINS` | Origines CORS autorisées | `["http://localhost:3000"]` |
| `DEBUG` | Active le mode debug et Swagger | `false` |
| `LOG_LEVEL` | Niveau de verbosité des logs | `INFO` |

---

## Matrice des formats supportes

| Format | Preview | Pseudonymisation | Contexte chat | Export | Controles granulaires |
|---|---|---|---|---|---|
| `pdf` | Oui | Oui | Oui | `pdf` | Non |
| `docx` | Oui | Oui | Oui | `docx` | Non |
| `txt` | Oui | Oui | Oui | `txt` | Non |
| `xlsx` | Oui | Oui | Oui | `xlsx` | Oui |
| `xls` | Oui | Oui | Oui | `xlsx` | Non |
| `csv` | Oui | Oui | Oui | `csv` | Non |
| `pptx` | Oui | Oui | Oui | Non | Non |
| `jpg` | Non | Oui | Oui | Non | Non |
| `jpeg` | Non | Oui | Oui | Non | Non |
| `png` | Non | Oui | Oui | Non | Non |
| `eml` | Oui | Oui | Oui | Non | Non |

Notes :
- Les fichiers `xls` sont convertis automatiquement en `xlsx` avant traitement.
- Les exclusions par feuille, colonne et plage ne s'appliquent qu'aux vrais fichiers `xlsx`.
- Les fichiers image (`jpg`, `jpeg`, `png`) passent par OCR et n'ont pas de preview structuree cote upload.

---
## Langues supportées

| Code | Langue | Modèle spaCy |
|---|---|---|
| `fr` | Français | `fr_core_news_lg` |
| `en` | Anglais | `en_core_web_lg` |
| `de` | Allemand | `de_core_news_lg` |
| `es` | Espagnol | `es_core_news_lg` |
| `it` | Italien | *(règles regex uniquement)* |
| `pt` | Portugais | *(règles regex uniquement)* |
| `nl` | Néerlandais | *(règles regex uniquement)* |

La langue est détectée automatiquement via `lingua-language-detector` et `langdetect`.

---

## Endpoints API

Toutes les routes (sauf `/health` et `/api/auth/token`) nécessitent un header `Authorization: Bearer <token>`.

| Méthode | Endpoint | Description |
|---|---|---|
| `POST` | `/api/auth/token` | Obtenir un token JWT (form: username, password) |
| `GET` | `/api/auth/me` | Informations sur l'utilisateur connecté |
| `POST` | `/api/chat` | Chat synchrone — pseudonymise, appelle le LLM, restaure |
| `POST` | `/api/chat/stream` | Chat SSE streaming — mêmes garanties, réponse en temps réel |
| `POST` | `/api/upload` | Upload PDF/DOCX/TXT — extraction, chunking, pseudonymisation |
| `GET` | `/api/session/{id}` | Statut d'une session (existe, nb tokens) |
| `DELETE` | `/api/session/{id}` | Suppression RGPD Art. 17 — efface le mapping du vault |
| `GET` | `/health` | Liveness probe — retourne `{status, version, timestamp}` |

### Exemple — Chat SSE

```bash
curl -X POST http://localhost:8000/api/chat/stream \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Résume le contrat de Jean Dupont (jean@acme.fr)"}]
  }'
```

Réponse (SSE) :
```
data: {"event":"start","session_id":"uuid...","redacted_entities":2}

data: {"event":"delta","data":"Voici un résumé du contrat de Jean Dupont"}

data: {"event":"done"}
```

### Exemple — Upload de fichier

```bash
curl -X POST http://localhost:8000/api/upload \
  -H "Authorization: Bearer <token>" \
  -F "file=@contrat.pdf"
```

Réponse :
```json
{
  "session_id": "uuid...",
  "filename": "contrat.pdf",
  "file_type": "pdf",
  "original_size_chars": 8420,
  "chunks_count": 5,
  "total_entities": 14,
  "token_estimate": 2105,
  "redacted_text": "[Page 1]\nContrat entre [PERSONNE_1] et [ORGANISATION_1]..."
}
```

---

## Conformité RGPD

| Article | Mesure implémentée |
|---|---|
| **Art. 5(1)(c)** — minimisation | Seules les données strictement nécessaires sont traitées. Les PII ne transitent jamais vers le LLM. |
| **Art. 5(1)(f)** — intégrité | Mappings chiffrés AES-256-GCM en transit et au repos. Redis protégé par mot de passe. |
| **Art. 17** — droit à l'effacement | `DELETE /api/session/{id}` supprime immédiatement le mapping du vault Redis. |
| **Art. 25** — privacy by design | Pseudonymisation automatique activée par défaut, non configurable à la baisse par l'utilisateur. |
| **Audit logs** | Logger JSON structuré zero-content : les logs ne contiennent jamais de texte ou PII. Les `session_id` et `user_id` sont hashés SHA-256 avant écriture. |
| **Rétention limitée** | Le mapping de ré-identification expire après 1h (TTL Redis configurable). Les données pseudonymisées sont conservées pour la durée du projet. Les données ré-identifiées sont purgées à l'archivage du projet. |
| **Isolation réseau** | Les services backend et Redis communiquent sur un réseau Docker privé (`proxy_net`). Redis n'est pas exposé publiquement en production. |

---

## Développement sans Docker

```bash
# Backend
cd backend
python -m venv .venv
source .venv/bin/activate       # Windows : .venv\Scripts\activate
pip install -r requirements.txt
python -m spacy download fr_core_news_lg
python -m spacy download en_core_web_lg
python -m spacy download de_core_news_lg
python -m spacy download es_core_news_lg
uvicorn app.main:app --reload --port 8000

# Frontend (terminal séparé)
cd frontend
npm install
npm run dev
```

Redis doit être accessible sur `REDIS_URL` (Docker ou installation locale).

---

---

## Déploiement Production

### Prérequis

| Élément | Détail |
|---|---|
| Serveur | Ubuntu 22.04 LTS, 4 GB RAM minimum |
| Docker | >= 24.0 avec plugin Compose v2 |
| Domaine | `iaprotech.com` pointant sur l'IP du serveur (A record + CNAME `api.` et `www.`) |
| Ports ouverts | 80 (HTTP), 443 (HTTPS) |

Installer Docker sur Ubuntu :

```bash
curl -fsSL https://get.docker.com | bash
sudo usermod -aG docker $USER && newgrp docker
```

---

### Étapes de déploiement

#### 1. Cloner le dépôt

```bash
git clone <url-du-depot>
cd privacy-proxy
```

#### 2. Configurer les variables d'environnement

```bash
cp .env.prod.example .env.prod
```

Générer les clés secrètes :

```bash
# SECRET_KEY (JWT signing)
python3 -c "import secrets; print(secrets.token_hex(32))"

# VAULT_ENCRYPTION_KEY (AES-256-GCM)
python3 -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"

# REDIS_PASSWORD
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Remplir `.env.prod` avec les valeurs générées, la clé API LLM et le mot de passe admin.

> **Important** — ne jamais committer `.env.prod`. Le fichier est dans `.gitignore`.

#### 3. Obtenir les certificats SSL (Let's Encrypt)

```bash
sudo apt install certbot -y

# Obtenir les certificats (le port 80 doit être libre)
sudo certbot certonly --standalone \
  -d iaprotech.com \
  -d www.iaprotech.com \
  -d api.iaprotech.com \
  --email votre@email.com \
  --agree-tos \
  --non-interactive
```

Renouvellement automatique (cron) :

```bash
echo "0 3 * * * root certbot renew --quiet && docker compose -f /chemin/docker-compose.prod.yml restart nginx" | sudo tee /etc/cron.d/certbot-renew
```

#### 4. Lancer le déploiement

```bash
bash deploy.sh
```

Options disponibles :

| Option | Effet |
|---|---|
| *(aucune)* | git pull + build + restart + healthcheck |
| `--skip-pull` | Pas de git pull (déploiement du code local) |
| `--skip-build` | Pas de rebuild Docker (mise à jour config seulement) |

---

### Commandes utiles

```bash
# Voir les logs en temps réel
docker compose -f docker-compose.prod.yml logs -f

# Logs d'un service spécifique
docker compose -f docker-compose.prod.yml logs -f backend

# Statut des conteneurs
docker compose -f docker-compose.prod.yml ps

# Redémarrer un service sans rebuild
docker compose -f docker-compose.prod.yml restart backend

# Redémarrer toute la stack
docker compose -f docker-compose.prod.yml restart

# Arrêt complet (données Redis préservées dans le volume)
docker compose -f docker-compose.prod.yml down

# Arrêt + suppression des volumes (DESTRUCTIF — perte de toutes les données)
docker compose -f docker-compose.prod.yml down -v
```

#### Backup Redis

```bash
# Déclencher une sauvegarde manuelle
docker compose -f docker-compose.prod.yml exec redis \
  redis-cli -a "$REDIS_PASSWORD" BGSAVE

# Copier le dump hors du conteneur
docker compose -f docker-compose.prod.yml cp \
  redis:/data/dump.rdb ./backup/redis_$(date +%Y%m%d_%H%M%S).rdb
```

Backup automatique quotidien :

```bash
echo "0 2 * * * root docker compose -f /chemin/docker-compose.prod.yml exec -T redis redis-cli -a \"\$REDIS_PASSWORD\" BGSAVE && docker compose -f /chemin/docker-compose.prod.yml cp redis:/data/dump.rdb /backups/redis_\$(date +\%Y\%m\%d).rdb" | sudo tee /etc/cron.d/redis-backup
```

#### Mise à jour de l'application

```bash
# Déploiement standard avec rebuild complet
bash deploy.sh

# Déploiement rapide (pas de rebuild, simple mise à jour config)
bash deploy.sh --skip-build
```

---

### Monitoring

```bash
# Santé du backend
curl https://api.iaprotech.com/health

# Utilisation des ressources Docker
docker stats

# Espace disque volumes
docker system df
```


## Licence

MIT — voir [LICENSE](LICENSE).
