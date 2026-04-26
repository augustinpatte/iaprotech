"""
Authentication routes — JWT-based login/token/register/bootstrap.

Security invariants:
  - /bootstrap : ONLY path to create the first admin.
                 Blocked if system already initialized or BOOTSTRAP_ENABLED=false.
  - /register  : ALWAYS creates a "member", never an admin.
                 Requires a valid invite_token if system is initialized.
  - /token     : IP rate limit via get_real_ip() (TRUST_PROXY_HEADERS guard)
                 + per-username lockout.
  - Password policy: >= 12 chars, complexity, no common words.
"""

import json
import base64
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
import re as _re

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from jose.exceptions import JWTClaimsError
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field

from app.config import settings
from app.core.vault import decrypt_and_migrate, decrypt_data, encrypt_data
from app.core.result import Result, ok, err
from app.middleware.rate_limit import limiter
from app.utils.logger import get_logger, hash_id as _h

logger = get_logger(__name__)

USER_STATUS_VALUES = frozenset({"pending", "active", "suspended"})
PROVIDER_API_KEY_NAMES = ("openai", "anthropic", "google", "mistral")
JWT_ISSUER = "privacy-proxy"
JWT_AUDIENCE = "api"
JWT_REQUIRED_CLAIMS = ["exp", "sub", "jti", "iat"]
JWT_DECODE_OPTIONS = {
    "require": JWT_REQUIRED_CLAIMS,
    "require_exp": True,
    "require_sub": True,
    "require_jti": True,
    "require_iat": True,
}


router = APIRouter()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_DUMMY_BCRYPT_HASH = pwd_context.hash("dummy_password_for_timing_protection_only")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")

# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

_redis_client: Optional[aioredis.Redis] = None
_redis_ok: bool = False


async def _get_redis() -> Optional[aioredis.Redis]:
    global _redis_client, _redis_ok
    if _redis_client is None:
        try:
            _redis_client = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
            )
            await _redis_client.ping()
            _redis_ok = True
        except Exception as exc:
            logger.warning("auth: Redis unavailable: %s", exc)
            _redis_client = None
            _redis_ok = False
    return _redis_client if _redis_ok else None


async def _get_user(username: str) -> Optional[dict]:
    r = await _get_redis()
    if r is None:
        return None
    raw = await r.get(f"user:{username}")
    if raw is None:
        return None
    data = json.loads(raw)
    data = await _migrate_legacy_provider_keys(username, data)
    data = await _migrate_provider_api_keys_v2(username, data)
    return _normalize_user_record(username, data)


async def _set_user(username: str, data: dict) -> None:
    r = await _get_redis()
    if r is None:
        raise RuntimeError("Redis unavailable — cannot persist user")
    normalized = _normalize_user_record(username, data)
    payload = {k: v for k, v in normalized.items() if k != "username"}
    pipe = r.pipeline()
    pipe.set(f"user:{username}", json.dumps(payload))
    pipe.sadd("index:users", username)
    await pipe.execute()


def _normalize_provider_api_keys_plain(data: Optional[dict]) -> dict:
    raw = dict(data or {})
    return {
        provider: str(raw.get(provider, "") or "").strip()
        for provider in PROVIDER_API_KEY_NAMES
    }


def _normalize_provider_api_keys(data: Optional[dict], org_id: str) -> str:
    keys = _normalize_provider_api_keys_plain(data)
    plaintext = json.dumps(keys, sort_keys=True, separators=(",", ":")).encode("utf-8")
    blob = encrypt_data(plaintext, settings.VAULT_ENCRYPTION_KEY, org_id)
    return base64.b64encode(blob).decode("ascii")


def decrypt_provider_api_keys(
    encrypted: Optional[str],
    user_id: str = "",
    org_id: str = "",
    *,
    strict: bool = False,
) -> dict:
    if not encrypted:
        return _normalize_provider_api_keys_plain(None)
    try:
        blob = base64.b64decode(str(encrypted).encode("ascii"), validate=True)
        plaintext = decrypt_data(blob, settings.VAULT_ENCRYPTION_KEY, org_id)
        data = json.loads(plaintext.decode("utf-8"))
        return _normalize_provider_api_keys_plain(data)
    except Exception as exc:
        logger.warning(
            "auth.provider_api_keys_decrypt_failed | user=%s | %s",
            _h(user_id) if user_id else "unknown",
            type(exc).__name__,
        )
        if strict:
            raise
        return _normalize_provider_api_keys_plain(None)


async def get_user_provider_api_keys(username: str) -> dict:
    user = await _get_user(username)
    if not user:
        return _normalize_provider_api_keys_plain(None)
    return decrypt_provider_api_keys(
        user.get("provider_api_keys_encrypted"),
        username,
        user.get("org_id", ""),
    )


async def _migrate_legacy_provider_keys(username: str, user_record: dict) -> dict:
    if not isinstance(user_record.get("provider_api_keys"), dict):
        return user_record
    migrated = dict(user_record)
    org_id = str(migrated.get("org_id") or "").strip()
    migrated["provider_api_keys_encrypted"] = _normalize_provider_api_keys(
        migrated.get("provider_api_keys"),
        org_id,
    )
    migrated.pop("provider_api_keys", None)
    r = await _get_redis()
    if r is not None:
        await r.set(f"user:{username}", json.dumps(migrated))
        logger.info("auth.provider_api_keys_migrated | user=%s", _h(username))
    return migrated


async def _migrate_provider_api_keys_v2(username: str, user_record: dict) -> dict:
    encrypted = str(user_record.get("provider_api_keys_encrypted") or "").strip()
    org_id = str(user_record.get("org_id") or "").strip()
    if not encrypted or not org_id:
        return user_record
    try:
        blob = base64.b64decode(encrypted.encode("ascii"), validate=True)
        plaintext, migrated_blob = decrypt_and_migrate(
            blob,
            settings.VAULT_ENCRYPTION_KEY,
            org_id,
        )
        json.loads(plaintext.decode("utf-8"))
    except Exception:
        return user_record
    if migrated_blob == blob:
        return user_record
    migrated = dict(user_record)
    migrated["provider_api_keys_encrypted"] = base64.b64encode(migrated_blob).decode("ascii")
    r = await _get_redis()
    if r is not None:
        await r.set(f"user:{username}", json.dumps(migrated))
        logger.info(
            "vault.migrated_v1_to_v2 | user=%s | org=%s",
            _h(username), _h(org_id),
        )
    return migrated


def _normalize_user_record(username: str, data: Optional[dict]) -> dict:
    raw = dict(data or {})
    org_id = str(raw.get("org_id") or "").strip()
    derived_status = "active" if raw.get("is_active", True) else "suspended"
    status_value = str(raw.get("status", derived_status) or derived_status).strip().lower()
    if status_value not in USER_STATUS_VALUES:
        status_value = derived_status
    provider_api_keys_encrypted = str(raw.get("provider_api_keys_encrypted") or "").strip()
    if not provider_api_keys_encrypted:
        if raw.get("provider_api_keys") is not None or org_id:
            provider_api_keys_encrypted = _normalize_provider_api_keys(
                raw.get("provider_api_keys"),
                org_id,
            )

    return {
        "username": username,
        "email": raw.get("email", ""),
        "hashed_password": raw.get("hashed_password", ""),
        "role": raw.get("role", "member"),
        "status": status_value,
        "plan": raw.get("plan"),
        "org_id": org_id,
        "invited_by": raw.get("invited_by"),
        "invitations_used": int(raw.get("invitations_used", 0) or 0),
        "created_at": raw.get("created_at", ""),
        "activated_at": raw.get("activated_at"),
        "last_login": raw.get("last_login"),
        "is_active": status_value == "active",
        "provider_api_keys_encrypted": provider_api_keys_encrypted,
    }


async def _user_exists(username: str) -> bool:
    r = await _get_redis()
    if r is None:
        return False
    return bool(await r.exists(f"user:{username}"))


async def _any_users_exist(redis: Optional[aioredis.Redis] = None) -> bool:
    """Retourne True si au moins une cle user:{*} existe dans Redis."""
    r = redis or await _get_redis()
    if r is None:
        return False
    cursor = 0
    while True:
        cursor, keys = await r.scan(cursor, match="user:*", count=100)
        if any(str(key).count(":") == 1 for key in keys):
            return True
        if cursor == 0:
            return False


def _user_jti_key(username: str) -> str:
    return f"user:jti:{username}"


def _password_reset_key(token: str) -> str:
    return f"pwd_reset:{token}"


def _email_to_username_key(email: str) -> str:
    return f"email_to_username:{_h(email)}"


async def _track_active_jti(username: str, jti: str, exp_ts: int) -> None:
    r = await _get_redis()
    if r is None:
        return
    member = f"{jti}:{exp_ts}"
    await r.sadd(_user_jti_key(username), member)
    await r.expire(_user_jti_key(username), max(1, exp_ts - int(datetime.now(timezone.utc).timestamp())))


async def _untrack_active_jti(username: str, jti: str) -> None:
    r = await _get_redis()
    if r is None:
        return
    entries = await r.smembers(_user_jti_key(username))
    for entry in entries or []:
        entry_str = str(entry)
        if entry_str.startswith(f"{jti}:"):
            await r.srem(_user_jti_key(username), entry)


async def _find_user_by_email(email: str) -> Optional[str]:
    normalized_email = email.strip().lower()
    if not normalized_email:
        return None
    r = await _get_redis()
    if r is None:
        return None

    index_key = _email_to_username_key(normalized_email)
    indexed_username = await r.get(index_key)
    if indexed_username:
        user = await _get_user(str(indexed_username))
        if user and (
            str(user.get("email", "")).strip().lower() == normalized_email
            or str(indexed_username).strip().lower() == normalized_email
        ):
            return str(indexed_username)
        await r.delete(index_key)

    usernames = sorted(str(member) for member in (await r.smembers("index:users")))
    if not usernames:
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor, match="user:*", count=100)
            usernames.extend(
                str(key)[5:] for key in keys if str(key).count(":") == 1
            )
            if cursor == 0:
                break
        usernames = sorted(set(usernames))
        if usernames:
            await r.sadd("index:users", *usernames)

    for username in usernames:
        user = await _get_user(username)
        if user and (
            str(user.get("email", "")).strip().lower() == normalized_email
            or username.strip().lower() == normalized_email
        ):
            await r.set(index_key, username)
            return username
    return None


def _generate_password_reset_token() -> str:
    return secrets.token_urlsafe(32)


async def _invalidate_all_user_tokens(username: str) -> None:
    r = await _get_redis()
    if r is None:
        return
    # Source de verite: les JTI actifs sont ecrits via _user_jti_key() au login.
    key = _user_jti_key(username)
    entries = await r.smembers(key)
    if not entries:
        return

    now_ts = int(datetime.now(timezone.utc).timestamp())
    pipe = r.pipeline()
    for entry in entries:
        entry_str = str(entry)
        try:
            jti, exp_raw = entry_str.rsplit(":", 1)
            remaining = int(exp_raw) - now_ts
        except ValueError:
            continue
        if remaining > 0:
            pipe.setex(f"jti_bl:{jti}", remaining, "password_reset")
    pipe.delete(key)
    await pipe.execute()


# ---------------------------------------------------------------------------
# Politique de mot de passe
# ---------------------------------------------------------------------------

_COMMON_PASSWORDS = frozenset({
    "password", "password1", "password12", "password123",
    "changeme", "changeme1",
    "123456", "1234567890", "12345678",
    "admin", "admin123",
    "azerty", "azerty123",
    "qwerty", "qwerty123",
    "welcome", "letmein",
})

_MIN_PASSWORD_LENGTH = 12


def validate_password(password: str) -> Result:
    """
    Valide la politique de mot de passe.

    Regles :
      - Minimum 12 caracteres
      - Au moins 1 majuscule (A-Z)
      - Au moins 1 minuscule (a-z)
      - Au moins 1 chiffre (0-9)
      - Au moins 1 caractere special (non alphanumerique)
      - Pas dans la liste des mots de passe courants/triviaux

    Retourne Ok(True) | Err(message, code).
    """
    if len(password) < _MIN_PASSWORD_LENGTH:
        return err(
            f"Le mot de passe doit contenir au moins {_MIN_PASSWORD_LENGTH} "
            f"caracteres (actuel: {len(password)}).",
            "PASSWORD_TOO_SHORT",
        )
    if not _re.search(r"[A-Z]", password):
        return err(
            "Le mot de passe doit contenir au moins une majuscule (A-Z).",
            "PASSWORD_NO_UPPERCASE",
        )
    if not _re.search(r"[a-z]", password):
        return err(
            "Le mot de passe doit contenir au moins une minuscule (a-z).",
            "PASSWORD_NO_LOWERCASE",
        )
    if not _re.search(r"[0-9]", password):
        return err(
            "Le mot de passe doit contenir au moins un chiffre (0-9).",
            "PASSWORD_NO_DIGIT",
        )
    if not _re.search(r"[^a-zA-Z0-9]", password):
        return err(
            "Le mot de passe doit contenir au moins un caractere special "
            "(@, #, !, $, %, ^, &, *, etc.).",
            "PASSWORD_NO_SPECIAL",
        )
    if password.lower() in _COMMON_PASSWORDS:
        return err(
            "Ce mot de passe est trop courant. Choisissez-en un plus unique.",
            "PASSWORD_TOO_COMMON",
        )
    return ok(True)


def _enforce_password(password: str) -> None:
    """Leve HTTP 422 si le mot de passe ne respecte pas la politique."""
    result = validate_password(password)
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": result.code, "message": result.message},
        )


# ---------------------------------------------------------------------------
# IP helper — protege contre le spoofing X-Forwarded-For
# ---------------------------------------------------------------------------

def get_real_ip(request: Request) -> str:
    """
    Extrait l'IP reelle du client.

    Si TRUST_PROXY_HEADERS=true (derriere un reverse proxy de confiance),
    utilise le premier element de X-Forwarded-For.
    Sinon, utilise request.client.host directement — non forgeable par le client.

    IMPORTANT: TRUST_PROXY_HEADERS=true uniquement si un reverse proxy (nginx,
    Traefik...) est devant l'application ET controle ce header.
    En acces direct, X-Forwarded-For peut etre forge par n'importe quel client.
    """
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Rate limiting — /token uniquement
# ---------------------------------------------------------------------------

_LOGIN_IP_LIMIT          = 5    # tentatives max par IP par fenetre
_LOGIN_IP_WINDOW         = 60   # secondes
_LOGIN_LOCKOUT_THRESHOLD = 5    # echecs consecutifs avant lockout username
_LOGIN_LOCKOUT_DURATION  = 900  # 15 minutes en secondes
_PASSWORD_RESET_TTL_SECONDS = 3600
_PASSWORD_RESET_NEUTRAL_MESSAGE = (
    "Si un compte existe pour cet email, un lien de reinitialisation a ete envoye."
)


def _lockout_exception(ttl: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Compte bloque. Reessayez dans {max(1, ttl // 60)} minute(s).",
    )


async def _check_login_rate_limit(request: Request, username: str) -> None:
    """
    Leve HTTP 429 si :
      - IP > _LOGIN_IP_LIMIT tentatives sur _LOGIN_IP_WINDOW secondes, OU
      - username en lockout (5 echecs consecutifs)
    """
    r = await _get_redis()
    if r is None:
        return  # Sans Redis, le rate limit ne peut pas etre applique

    ip = get_real_ip(request)

    # 1. Limite par IP
    ip_key = f"login:ip:{ip}"
    ip_count = await r.incr(ip_key)
    if ip_count == 1:
        await r.expire(ip_key, _LOGIN_IP_WINDOW)
    if ip_count > _LOGIN_IP_LIMIT:
        logger.warning("auth.ratelimit: IP limit | ip=%s", _h(ip))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Trop de tentatives. Reessayez dans une minute.",
        )

    # 2. Lockout username
    lock_key = f"login:lock:{username}"
    locked = await r.get(lock_key)
    if locked:
        ttl = await r.ttl(lock_key)
        logger.warning(
            "auth.ratelimit: lockout actif | user=%s | ttl=%ds",
            _h(username), ttl,
        )
        raise _lockout_exception(ttl)


async def _on_login_failure(username: str) -> bool:
    """Incremente le compteur d'echecs; lockout username apres seuil."""
    r = await _get_redis()
    if r is None:
        return False
    fail_key = f"login:fail:{username}"
    count = await r.incr(fail_key)
    await r.expire(fail_key, _LOGIN_LOCKOUT_DURATION)
    if count >= _LOGIN_LOCKOUT_THRESHOLD:
        lock_key = f"login:lock:{username}"
        await r.set(lock_key, "1", ex=_LOGIN_LOCKOUT_DURATION)
        logger.warning(
            "auth: lockout apres %d echecs | user=%s", count, _h(username)
        )
        return True
    return False


async def _on_login_success(username: str) -> None:
    """Remet a zero les compteurs d'echec apres connexion reussie."""
    r = await _get_redis()
    if r is None:
        return
    await r.delete(f"login:fail:{username}", f"login:lock:{username}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenData(BaseModel):
    username: Optional[str] = None
    role: Optional[str] = None
    org_id: Optional[str] = ""
    status: Optional[str] = "active"
    plan: Optional[str] = None
    email: Optional[str] = ""


class UserOut(BaseModel):
    username: str
    role: str


class RegisterResponse(BaseModel):
    message: str
    status: Literal["pending"] = "pending"
    username: str


class RegisterRequest(BaseModel):
    username: str = Field(..., pattern=r"^[a-zA-Z0-9_\-.]{3,64}$")
    password: str
    email: Optional[str] = None
    invite_token: Optional[str] = None
    # Note : champ "role" absent intentionnellement — toujours attribue cote serveur


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=20, max_length=64)
    new_password: str = Field(..., min_length=12, max_length=128)


class BootstrapRequest(BaseModel):
    username: str = Field(..., pattern=r"^[a-zA-Z0-9_\-.]{3,64}$")
    password: str


class SystemStatus(BaseModel):
    initialized: bool
    registration_open: bool = True


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (
        expires_delta or timedelta(minutes=settings.TOKEN_EXPIRE_MINUTES)
    )
    exp_min = (
        settings.TOKEN_EXPIRE_MINUTES
        if expires_delta is None
        else int(expires_delta.total_seconds() / 60)
    )
    to_encode.update({
        "exp": expire,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": int(now.timestamp()),
    })
    token = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    jti_short = str(to_encode.get("jti", ""))[:12]
    logger.info(
        "auth.token_issued | user=%s | jti=%s | exp_min=%d",
        _h(data.get("sub", "")),
        _h(jti_short) if jti_short else "none",
        exp_min,
    )
    return token


async def get_current_user(token: str = Depends(oauth2_scheme)) -> TokenData:
    """
    Source de verite unique : Redis.

    1. Decode le JWT pour extraire username (sub) et jti uniquement.
       Le role du JWT est ignore — il peut etre perime.
    2. Verifie que le jti n'est pas blackliste (revocation via /logout).
    3. Recharge l'utilisateur DEPUIS REDIS :
         - utilisateur absent -> HTTP 401 (compte supprime/desactive)
         - role depuis Redis  -> toujours a jour, meme apres promotion/demote
    4. Retourne TokenData(username, role_redis, org_id_redis).
    """
    auth_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # --- etape 1 : decode JWT (signature + claims obligatoires) ---
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options=JWT_DECODE_OPTIONS,
        )
        username: str = payload.get("sub", "")
        if not username:
            raise auth_exc
    except JWTClaimsError as exc:
        message = str(exc).lower()
        if "audience" in message:
            logger.warning("jwt.invalid_claim | claim=%s", "aud")
        elif "issuer" in message:
            logger.warning("jwt.invalid_claim | claim=%s", "iss")
        raise auth_exc
    except JWTError:
        raise auth_exc

    jti: str = payload["jti"]

    # --- etape 2 : JTI blacklist (token revoque via /logout) ---
    try:
        r = await _get_redis()
        if r and await r.exists(f"jti_bl:{jti}"):
            logger.info("get_current_user: token revoque | user=%s", _h(username))
            raise auth_exc
    except HTTPException:
        raise
    except Exception as exc:
        # Redis down : on ne peut pas verifier -> on refuse par defaut (fail-closed)
        logger.warning(
            "get_current_user: jti_check indisponible | %s — refuse par defaut",
            type(exc).__name__,
        )
        raise auth_exc

    # --- etape 3 : rechargement depuis Redis (source de verite) ---
    user_data = await _get_user(username)
    if user_data is None:
        # Compte supprime ou Redis indisponible : refuse dans les deux cas
        logger.warning(
            "get_current_user: utilisateur introuvable | user=%s", _h(username)
        )
        raise auth_exc

    # is_active : flag de desactivation administrative (sans suppression du compte).
    # Par defaut True pour les comptes existants sans ce champ (retrocompatible).
    if not user_data.get("is_active", True):
        logger.info("get_current_user: compte desactive | user=%s", _h(username))
        raise auth_exc

    try:
        r = await _get_redis()
        if r and await r.exists(f"suspended:{username}"):
            logger.info("get_current_user: compte suspendu | user=%s", _h(username))
            raise auth_exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "get_current_user: suspended_check indisponible | %s — refuse par defaut",
            type(exc).__name__,
        )
        raise auth_exc

    return TokenData(
        username=username,
        role=user_data.get("role", "member"),   # role Redis, jamais le role JWT
        org_id=user_data.get("org_id", ""),     # org_id Redis
        status=user_data.get("status", "active"),
        plan=user_data.get("plan"),
        email=user_data.get("email", ""),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status", response_model=SystemStatus)
async def get_system_status():
    """Retourne si le systeme a ete initialise (au moins un utilisateur)."""
    return SystemStatus(
        initialized=await _any_users_exist(),
        registration_open=True,
    )


@router.post("/bootstrap", response_model=UserOut, status_code=201)
@limiter.limit("3/hour")
async def bootstrap(request: Request, body: BootstrapRequest):
    """
    Cree le PREMIER compte admin. Seul chemin vers un role admin.

    Conditions requises (toutes doivent etre vraies) :
      - BOOTSTRAP_ENABLED=true dans la configuration
      - Aucun utilisateur existant dans Redis
      - Politique mot de passe respectee

    HTTP 403 si systeme deja initialise ou bootstrap desactive.
    HTTP 422 si mot de passe invalide.
    HTTP 503 si Redis indisponible.
    """
    # Guard 1 : BOOTSTRAP_ENABLED
    if not settings.BOOTSTRAP_ENABLED:
        logger.warning("auth.bootstrap: tentative avec BOOTSTRAP_ENABLED=false")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bootstrap desactive sur ce serveur.",
        )

    # Guard 2 : systeme vierge
    if await _any_users_exist():
        logger.warning("auth.bootstrap: tentative sur systeme deja initialise")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Systeme deja initialise. "
                "Utilisez POST /register avec un invite_token valide."
            ),
        )

    # Guard 3 : politique mot de passe
    _enforce_password(body.password)

    r = await _get_redis()
    if r is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis indisponible — impossible de creer le compte.",
        )

    from app.core.org_manager import get_org_manager  # noqa: PLC0415

    created_at = datetime.now(timezone.utc).isoformat()
    org = await get_org_manager().create_org(
        name="Organisation Principale",
        owner_id=body.username,
        plan="pro",
    )

    try:
        await _set_user(body.username, {
            "hashed_password": pwd_context.hash(body.password),
            "role": "admin",
            "status": "active",
            "plan": "max",
            "org_id": org.org_id,
            "email": "",
            "invited_by": None,
            "invitations_used": 0,
            "created_at": created_at,
            "activated_at": created_at,
            "last_login": None,
        })
    except Exception:
        pipe = r.pipeline()
        pipe.delete(f"org:data:{org.org_id}")
        pipe.delete(f"org:member:{org.org_id}:{body.username}")
        pipe.delete(f"org:members:{org.org_id}")
        pipe.delete(f"org:settings:{org.org_id}")
        pipe.delete(f"user:org:{body.username}")
        await pipe.execute()
        raise

    logger.info(
        "auth.bootstrap: premier admin cree | user=%s | org=%s | ts=%s",
        _h(body.username),
        _h(org.org_id),
        created_at,
    )
    return UserOut(username=body.username, role="admin")


@router.post("/forgot-password", status_code=200)
@limiter.limit("3/hour")
async def forgot_password(request: Request, body: ForgotPasswordRequest):
    response = {"message": _PASSWORD_RESET_NEUTRAL_MESSAGE}
    username: Optional[str] = None
    token_was_stored = False
    try:
        username = await _find_user_by_email(str(body.email))
        if username:
            r = await _get_redis()
            if r is None:
                logger.warning("auth.password_reset_unavailable | redis=down")
            else:
                token = _generate_password_reset_token()
                created_at = datetime.now(timezone.utc).isoformat()
                await r.setex(
                    _password_reset_key(token),
                    _PASSWORD_RESET_TTL_SECONDS,
                    json.dumps({"username": username, "created_at": created_at}),
                )
                token_was_stored = True
                reset_link = f"{request.url_for('reset_password')}?token={token}"
                # TODO INTEGRATION: brancher le mailer (Postmark/SES) à cet endroit pour envoyer le lien.
                # Voir docs/INCIDENT_RESPONSE.md pour le template d'email.
                # Pour le développement, le lien complet est loggé en DEBUG pour les tests E2E.
                logger.debug(
                    "auth.password_reset_link | user=%s | url=%s",
                    _h(username),
                    reset_link,
                )
                if settings.DEBUG:
                    response["token"] = token
    except Exception as exc:
        logger.warning("auth.password_reset_request_failed | %s", type(exc).__name__)
    logger.info(
        "auth.password_reset_requested | user=%s | persisted=%s",
        _h(username) if username else "none",
        "true" if token_was_stored else "false",
    )
    return response


@router.post("/reset-password", status_code=200)
@limiter.limit("5/hour")
async def reset_password(request: Request, body: ResetPasswordRequest):
    r = await _get_redis()
    if r is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporairement indisponible.",
        )

    raw = await r.execute_command("GETDEL", _password_reset_key(body.token))
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lien invalide ou expire.",
        )

    try:
        reset_data = json.loads(raw)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lien invalide ou expire.",
        ) from exc

    username = str(reset_data.get("username") or "")
    user = await _get_user(username)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lien invalide ou expire.",
        )

    _enforce_password(body.new_password)
    user["hashed_password"] = pwd_context.hash(body.new_password)
    await _set_user(username, user)
    await _invalidate_all_user_tokens(username)
    logger.info("auth.password_reset_completed | user=%s", _h(username))
    return {"detail": "Mot de passe reinitialise avec succes."}


@router.post("/token", response_model=Token)
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    """Echange credentials contre un token JWT."""
    await _check_login_rate_limit(request, form_data.username)

    user = await _get_user(form_data.username)
    password_ok = False
    if user:
        password_ok = verify_password(form_data.password, user["hashed_password"])
    else:
        pwd_context.verify(form_data.password, _DUMMY_BCRYPT_HASH)
    if not user or not password_ok:
        logger.warning("auth.login: echec | user=%s", _h(form_data.username))
        locked = await _on_login_failure(form_data.username)
        if locked:
            raise _lockout_exception(_LOGIN_LOCKOUT_DURATION)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Identifiants incorrects.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.get("status") == "pending":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "detail": "Votre compte est en attente de validation par l'administrateur.",
                "status": "pending",
            },
        )

    if user.get("status") == "suspended":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "detail": "Votre compte a été suspendu.",
                "status": "suspended",
            },
        )

    await _on_login_success(form_data.username)
    user["last_login"] = datetime.now(timezone.utc).isoformat()
    await _set_user(user["username"], user)
    jti = str(uuid.uuid4())
    expires_delta = timedelta(minutes=settings.TOKEN_EXPIRE_MINUTES)
    expire_at = datetime.now(timezone.utc) + expires_delta
    token = create_access_token(
        data={
            "sub":  user["username"],
            "role": user["role"],
            "status": user.get("status", "active"),
            "plan": user.get("plan"),
            "jti":  jti,
        },
        expires_delta=expires_delta,
    )
    try:
        await _track_active_jti(user["username"], jti, int(expire_at.timestamp()))
    except Exception as exc:
        logger.warning("auth.login: jti_track failed | %s", type(exc).__name__)
    logger.info("auth.login: succes | user=%s", _h(user["username"]))
    return Token(access_token=token, expires_in=settings.TOKEN_EXPIRE_MINUTES * 60)


@router.post("/logout", status_code=200)
async def logout(
    current_user: TokenData = Depends(get_current_user),
    token: str = Depends(oauth2_scheme),
):
    """Revoque le token courant (JTI ajoutee a la blacklist Redis, TTL residuel)."""
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options=JWT_DECODE_OPTIONS,
        )
        jti = payload.get("jti")
        exp = payload.get("exp")
        if jti and exp:
            remaining = max(1, int(exp - datetime.now(timezone.utc).timestamp()))
            r = await _get_redis()
            if r:
                await r.setex(f"jti_bl:{jti}", remaining, "1")
                await _untrack_active_jti(current_user.username, jti)
    except JWTClaimsError as exc:
        message = str(exc).lower()
        if "audience" in message:
            logger.warning("jwt.invalid_claim | claim=%s", "aud")
        elif "issuer" in message:
            logger.warning("jwt.invalid_claim | claim=%s", "iss")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as exc:
        logger.warning("auth.logout: erreur revocation | %s", type(exc).__name__)
    logger.info("auth.logout | user=%s", _h(current_user.username))
    return {"detail": "Deconnecte avec succes."}


@router.get("/me", response_model=UserOut)
async def get_me(current_user: TokenData = Depends(get_current_user)):
    """Retourne les informations de l'utilisateur connecte."""
    return UserOut(username=current_user.username, role=current_user.role)


async def _mark_invite_status(token: str, **updates) -> None:
    r = await _get_redis()
    if r is None:
        return
    key = f"invite:meta:{token}"
    raw = await r.get(key)
    if not raw:
        return
    try:
        data = json.loads(raw)
    except Exception:
        data = {}
    data.update(updates)
    await r.set(key, json.dumps(data))


@router.post("/register", response_model=RegisterResponse, status_code=201)
@limiter.limit("5/minute")
async def register(request: Request, body: RegisterRequest):
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="L'email est obligatoire.",
        )

    # SECURITY: enforce invite-only model when system is already bootstrapped
    if await _any_users_exist() and not body.invite_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Un invite_token est requis pour creer un compte. "
                   "Demandez une invitation a un administrateur.",
        )

    _enforce_password(body.password)

    if await _user_exists(body.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ce nom d'utilisateur est deja utilise.",
        )

    invited_by = None
    consumed_invite = None
    if body.invite_token:
        from app.core.org_manager import get_org_manager  # noqa: PLC0415

        mgr = get_org_manager()
        invite = await mgr.get_invite(body.invite_token)
        if not invite:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token d'invitation invalide ou expiré.",
            )
        if invite.get("email", "").lower() != email:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Email ne correspond pas à l'invitation.",
            )
        consumed_invite = await mgr.consume_invite(body.invite_token, email)
        invited_by = consumed_invite.get("invited_by")

    created_at = datetime.now(timezone.utc).isoformat()
    try:
        await _set_user(body.username, {
            "email": email,
            "hashed_password": pwd_context.hash(body.password),
            "role": "member",
            "status": "pending",
            "plan": None,
            "org_id": "",
            "invited_by": invited_by,
            "invitations_used": 0,
            "created_at": created_at,
            "activated_at": None,
            "last_login": None,
        })
    except Exception as exc:
        if consumed_invite and body.invite_token:
            try:
                from app.core.org_manager import get_org_manager  # noqa: PLC0415

                await get_org_manager().restore_invite(body.invite_token, consumed_invite)
            except Exception:
                logger.warning("auth.register: invite_restore_failed | user=%s", _h(body.username))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erreur lors de la creation du compte.",
        ) from exc

    if body.invite_token:
        await _mark_invite_status(
            body.invite_token,
            status="accepted",
            accepted_by=body.username,
            accepted_at=created_at,
        )

    logger.info(
        "auth.register.pending | user=%s | invited_by=%s",
        _h(body.username),
        _h(invited_by) if invited_by else "none",
    )
    return RegisterResponse(
        username=body.username,
        message="Compte créé. En attente de validation par l'administrateur.",
    )
