#!/bin/bash
# ==============================================================
# deploy.sh — Déploiement production Privacy Proxy (iaprotech.com)
# Usage : bash deploy.sh [--skip-pull] [--skip-build]
# ==============================================================
set -euo pipefail

COMPOSE_FILE="docker-compose.prod.yml"
NGINX_URL="https://iaprotech.com/health"

# Couleurs
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# --- Vérifications préalables -----------------------------------------
info "=== Déploiement IaProTech Privacy Proxy ==="
info "Date : $(date '+%Y-%m-%d %H:%M:%S')"

if [ ! -f ".env.prod" ]; then
    error ".env.prod manquant. Créez-le depuis .env.prod.example"
    exit 1
fi

if ! command -v docker &>/dev/null; then
    error "Docker non installé"
    exit 1
fi

if ! docker compose version &>/dev/null; then
    error "Docker Compose plugin non installé"
    exit 1
fi

if [ ! -f "$COMPOSE_FILE" ]; then
    error "$COMPOSE_FILE introuvable"
    exit 1
fi

# Vérifier certificats SSL
if [ ! -d "/etc/letsencrypt/live/iaprotech.com" ]; then
    warn "Certificats SSL non trouvés dans /etc/letsencrypt/live/iaprotech.com"
    warn "Lancez d'abord : sudo certbot certonly --standalone -d iaprotech.com -d www.iaprotech.com -d api.iaprotech.com"
fi

# --- Git pull -----------------------------------------------------
SKIP_PULL=false
SKIP_BUILD=false
for arg in "$@"; do
    case $arg in
        --skip-pull)  SKIP_PULL=true ;;
        --skip-build) SKIP_BUILD=true ;;
    esac
done

if [ "$SKIP_PULL" = false ]; then
    info "Récupération des dernières modifications..."
    git pull origin main
else
    warn "git pull ignoré (--skip-pull)"
fi

# --- Build Docker --------------------------------------------------
if [ "$SKIP_BUILD" = false ]; then
    info "Build des images Docker (--no-cache)..."
    docker compose -f "$COMPOSE_FILE" build --no-cache
else
    warn "Build ignoré (--skip-build)"
fi

# --- Sauvegarde Redis avant restart --------------------------------
info "Sauvegarde Redis (BGSAVE)..."
if docker compose -f "$COMPOSE_FILE" ps redis | grep -q "running"; then
    REDIS_PASS=$(grep '^REDIS_PASSWORD=' .env.prod | cut -d'=' -f2- | tr -d '"')
    REDISCLI_AUTH="$REDIS_PASS" docker compose -f "$COMPOSE_FILE" exec -T -e REDISCLI_AUTH redis redis-cli BGSAVE > /dev/null 2>&1 || warn "BGSAVE ignoré (Redis non démarré ou auth échouée)"
fi

# --- Démarrage sans downtime (recreate) ----------------------------
info "Redémarrage des conteneurs..."
docker compose -f "$COMPOSE_FILE" up -d --force-recreate

# --- Healthcheck ---------------------------------------------------
info "Attente du démarrage (30s)..."
sleep 30

MAX_RETRIES=10
RETRY_INTERVAL=10

check_backend_health() {
    local retries=0
    while [ $retries -lt $MAX_RETRIES ]; do
        if docker compose -f "$COMPOSE_FILE" exec -T backend python -c "import urllib.request; r = urllib.request.urlopen('http://localhost:8000/health', timeout=5); raise SystemExit(0 if r.status == 200 else 1)" > /dev/null 2>&1; then
            info "Backend : OK"
            return 0
        fi
        retries=$((retries + 1))
        warn "Backend non prêt ($retries/$MAX_RETRIES), nouvel essai dans ${RETRY_INTERVAL}s..."
        sleep $RETRY_INTERVAL
    done
    error "Backend KO après $MAX_RETRIES tentatives"
    return 1
}

check_url_health() {
    local url="$1"
    local name="$2"
    local retries=0
    while [ $retries -lt $MAX_RETRIES ]; do
        if curl -sf --max-time 5 "$url" > /dev/null 2>&1; then
            info "$name : OK"
            return 0
        fi
        retries=$((retries + 1))
        warn "$name non prêt ($retries/$MAX_RETRIES), nouvel essai dans ${RETRY_INTERVAL}s..."
        sleep $RETRY_INTERVAL
    done
    error "$name KO après $MAX_RETRIES tentatives"
    return 1
}

HEALTH_OK=true
check_backend_health || HEALTH_OK=false
check_url_health "$NGINX_URL" "Nginx" || HEALTH_OK=false

# --- Nettoyage images obsolètes ------------------------------------
info "Nettoyage des images Docker orphelines..."
docker image prune -f --filter "until=24h" > /dev/null 2>&1 || true

# --- Résultat final ------------------------------------------------
echo ""
if [ "$HEALTH_OK" = true ]; then
    info "=== Déploiement réussi ! ==="
    info "Frontend : https://iaprotech.com"
    info "API      : https://api.iaprotech.com"
    info "Health   : https://api.iaprotech.com/health"
    echo ""
    docker compose -f "$COMPOSE_FILE" ps
else
    error "=== Déploiement avec erreurs — vérifiez les logs ==="
    echo ""
    docker compose -f "$COMPOSE_FILE" logs --tail=50 backend
    exit 1
fi
