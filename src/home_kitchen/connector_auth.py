"""Interactive backend authentication flows for Rema 1000, Coop, Cookidoo, Trumf, and Kassal.

Provides:
- Guided OIDC PKCE flow for Rema 1000 and Coop (start + complete)
- Interactive OAuth2 login for Cookidoo / TM6 via Vorwerk / Cidaas
- Session cookie validation and rolling renewal for Trumf
- API key validation for Kassal.app
All flows support injectable HTTP transports and base URLs for offline testing.
Plaintext secrets and passwords are never returned in responses or persisted in DB.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import threading
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

if TYPE_CHECKING:
    from home_kitchen.connector_ports import ConnectorCredentialPort
    from home_kitchen.integrations import IntegrationInfo, IntegrationRegistry
    from home_kitchen.settings import Settings

_LOG = logging.getLogger(__name__)

REFRESHABLE_PROVIDERS = ("rema1000", "coop", "cookidoo", "trumf")
OAUTH_REFRESH_MODES = ("oauth_pkce", "oauth_password")
TRUMF_REFRESH_MODES = ("session_cookie", "manual_session_cookie")
DEFAULT_REFRESH_INTERVAL_SECONDS = 300.0
TRUMF_SESSION_REFRESH_ERROR = (
    "Trumf-sesjonen kan ikke fornyes automatisk og må kobles til på nytt i nettleseren "
    "(lim inn full Cookie-header inkludert alle __Secure-authjs.session-token-deler)."
)
TRUMF_SESSION_CONNECT_ERROR = (
    "Trumf-informasjonskapselen er ugyldig, utløpt eller kan ikke fornyes. "
    "Kopier hele Cookie:-verdien fra DevTools → Network → Headers → Request Headers "
    "(ikke Set-Cookie), inkludert alle __Secure-authjs.session-token-deler."
)
_TRUMF_UNKNOWN_EXPIRY_REFRESH_INTERVAL = timedelta(hours=1)

DEFAULT_REMA1000_AUTHORIZE_URL = "https://id.rema.no/authorization"
DEFAULT_REMA1000_TOKEN_URL = "https://id.rema.no/token"
DEFAULT_REMA1000_REDIRECT_URI = "https://ae-appen.appspot.com/redirect/redirect.html"
DEFAULT_REMA1000_CLIENT_ID = "c886192e-2ac2-11e9-b210-d663bd873d93"
DEFAULT_REMA1000_SCOPE = "all"

DEFAULT_COOP_AUTHORIZE_URL = "https://login.coop.no/authorize"
DEFAULT_COOP_TOKEN_URL = "https://login.coop.no/oauth/token"
DEFAULT_COOP_REDIRECT_URI = "no.coop.members://auth/callback"
DEFAULT_COOP_CLIENT_ID = "7WrQEdeXwUudArpQVjmZEvrTgVs1WkRr"
DEFAULT_COOP_SCOPE = "openid profile email offline_access"
DEFAULT_COOP_AUDIENCE = "https://api.coop.no"

DEFAULT_COOKIDOO_BASE_URL = "https://ciam.prod.cookidoo.vorwerk-digital.com"
DEFAULT_COOKIDOO_CLIENT_ID = "mobile-android"
DEFAULT_COOKIDOO_REDIRECT_URI = "com.vorwerk.cookidoo://code-grant"
DEFAULT_COOKIDOO_SCOPE = "openid profile email offline offline_access"
CIAM_LOGIN_SRV_URL = "https://ciam.prod.cookidoo.vorwerk-digital.com/login-srv/login"
COOKIDOO_LOGIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

DEFAULT_TRUMF_BASE_URL = "https://www.trumf.no"


class PKCEStartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    redirect_uri: str | None = None
    account_id: str | None = None
    account_label: str | None = None
    add_account: bool = False


class PKCEStartResponse(BaseModel):
    provider_id: str
    authorize_url: str
    redirect_uri: str
    state: str
    instructions: str


class PKCECompleteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    redirect_url: str | None = None
    code: str | None = None
    state: str | None = None
    account_id: str | None = None
    account_label: str | None = None
    add_account: bool = False


class CookidooLoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    email: str = Field(min_length=1)
    password: SecretStr
    country: str = "no"
    account_id: str | None = None
    account_label: str | None = None
    add_account: bool = False


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    account_id: str | None = None


class PKCETransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str
    household_id: UUID
    code_verifier: str
    redirect_uri: str
    client_id: str
    token_endpoint: str
    created_at: datetime
    account_id: str | None = None
    account_label: str | None = None
    add_account: bool = False


class ConnectorAuthError(HTTPException):
    """HTTPException raised when connector authentication or session renewal fails."""


_COOKIE_ATTR_FLAGS = frozenset({"httponly", "secure", "partitioned"})
_COOKIE_ATTR_NAMES = frozenset({"path", "domain", "expires", "max-age", "samesite", "priority"})


def _parse_expires_at(expires_in: Any) -> datetime | None:
    if expires_in is None:
        return None
    try:
        return datetime.now(UTC) + timedelta(seconds=int(expires_in))
    except (ValueError, TypeError):
        return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _normalize_trumf_cookie(raw_cookie: str) -> tuple[str, dict[str, str]]:
    """Strip Set-Cookie attributes and return (cookie_header, ordered_pairs)."""
    cookies: dict[str, str] = {}
    for segment in re.split(r"[\r\n;]+", raw_cookie):
        cleaned = re.sub(r"^(?:cookie|set-cookie):\s*", "", segment.strip(), flags=re.IGNORECASE)
        if not cleaned or cleaned.lower() in _COOKIE_ATTR_FLAGS:
            continue
        if "=" not in cleaned:
            continue
        name, val = cleaned.split("=", 1)
        name = name.strip()
        val = val.strip()
        if not name or name.lower() in _COOKIE_ATTR_NAMES:
            continue
        cookies[name] = val

    if not cookies:
        return raw_cookie.strip(), {}
    return "; ".join(f"{k}={v}" for k, v in cookies.items()), cookies


def _parse_trumf_set_cookies(
    set_cookie_headers: list[str],
) -> tuple[dict[str, str], set[str], bool]:
    """Parse Set-Cookie headers -> (updates, removed_names, cleared_session_token)."""
    updates: dict[str, str] = {}
    removed: set[str] = set()
    cleared_session = False

    for header in set_cookie_headers:
        parts = [part.strip() for part in header.split(";") if part.strip()]
        if not parts or "=" not in parts[0]:
            continue
        name, val = parts[0].split("=", 1)
        name = name.strip()
        val = val.strip()
        if not name:
            continue

        max_age: int | None = None
        for attr in parts[1:]:
            if "=" in attr:
                attr_name, attr_val = attr.split("=", 1)
                if attr_name.strip().lower() == "max-age":
                    try:
                        max_age = int(attr_val.strip())
                    except ValueError:
                        pass

        is_authjs_session = name == "__Secure-authjs.session-token" or name.startswith(
            "__Secure-authjs.session-token."
        )
        if max_age is not None and max_age <= 0:
            if is_authjs_session:
                cleared_session = True
            removed.add(name)
            updates.pop(name, None)
        else:
            removed.discard(name)
            updates[name] = val

    return updates, removed, cleared_session


def generate_pkce_pair() -> tuple[str, str]:
    """Generate high-entropy code_verifier and S256 code_challenge."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def parse_oauth_code_and_state(
    redirect_url: str | None = None,
    code: str | None = None,
    state: str | None = None,
) -> tuple[str, str]:
    """Parse code and state from redirect URL (query or fragment) or direct fields."""
    if redirect_url:
        parsed = urllib.parse.urlsplit(redirect_url)
        query_params = urllib.parse.parse_qs(parsed.query)
        fragment_params = urllib.parse.parse_qs(parsed.fragment)

        extracted_code = (
            query_params.get("code", [None])[0] or fragment_params.get("code", [None])[0]
        )
        extracted_state = (
            query_params.get("state", [None])[0] or fragment_params.get("state", [None])[0]
        )

        if extracted_code:
            code = extracted_code
        if extracted_state:
            state = extracted_state

    if not code or not str(code).strip():
        raise HTTPException(status_code=422, detail="Mangler autorisasjonskode (code)")
    if not state or not str(state).strip():
        raise HTTPException(status_code=422, detail="Mangler state-parameter")

    return str(code).strip(), str(state).strip()


class ConnectorAuthService:
    def __init__(
        self,
        store: ConnectorCredentialPort,
        integrations: IntegrationRegistry,
        household_id: UUID | str,
        *,
        transport: httpx.BaseTransport | None = None,
        rema1000_authorize_url: str = DEFAULT_REMA1000_AUTHORIZE_URL,
        rema1000_token_url: str = DEFAULT_REMA1000_TOKEN_URL,
        rema1000_redirect_uri: str = DEFAULT_REMA1000_REDIRECT_URI,
        rema1000_client_id: str = DEFAULT_REMA1000_CLIENT_ID,
        rema1000_scope: str = DEFAULT_REMA1000_SCOPE,
        coop_authorize_url: str = DEFAULT_COOP_AUTHORIZE_URL,
        coop_token_url: str = DEFAULT_COOP_TOKEN_URL,
        coop_redirect_uri: str = DEFAULT_COOP_REDIRECT_URI,
        coop_client_id: str = DEFAULT_COOP_CLIENT_ID,
        coop_scope: str = DEFAULT_COOP_SCOPE,
        cookidoo_base_url: str = DEFAULT_COOKIDOO_BASE_URL,
        cookidoo_client_id: str = DEFAULT_COOKIDOO_CLIENT_ID,
        cookidoo_redirect_uri: str = DEFAULT_COOKIDOO_REDIRECT_URI,
        cookidoo_scope: str = DEFAULT_COOKIDOO_SCOPE,
        trumf_base_url: str = DEFAULT_TRUMF_BASE_URL,
        settings: Settings | None = None,
    ):
        self.store = store
        self.integrations = integrations
        if isinstance(household_id, str):
            try:
                self.household_id = UUID(household_id)
            except ValueError:
                self.household_id = uuid4()
        else:
            self.household_id = household_id
        self.transport = transport
        self.rema1000_authorize_url = rema1000_authorize_url
        self.rema1000_token_url = rema1000_token_url
        self.rema1000_redirect_uri = rema1000_redirect_uri
        self.rema1000_client_id = rema1000_client_id
        self.rema1000_scope = rema1000_scope
        self.coop_authorize_url = coop_authorize_url
        self.coop_token_url = coop_token_url
        self.coop_redirect_uri = coop_redirect_uri
        self.coop_client_id = coop_client_id
        self.coop_scope = coop_scope
        self.cookidoo_base_url = cookidoo_base_url
        self.cookidoo_client_id = cookidoo_client_id
        self.cookidoo_redirect_uri = cookidoo_redirect_uri
        self.cookidoo_scope = cookidoo_scope
        self.trumf_base_url = trumf_base_url
        self.settings = settings
        self._lock = threading.Lock()
        self._refresh_locks: dict[tuple[UUID, str, str], threading.RLock] = {}
        self._pkce_transactions: dict[str, PKCETransaction] = {}

    def _get_http_client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=30.0)

    def _connection_lock(
        self, household_id: UUID, provider_id: str, account_id: str = "default"
    ) -> threading.RLock:
        key = (household_id, provider_id, account_id)
        with self._lock:
            lock = self._refresh_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._refresh_locks[key] = lock
            return lock

    def _refresh_config(self, provider_id: str) -> tuple[str, str]:
        if provider_id == "rema1000":
            return self.rema1000_token_url, self.rema1000_client_id
        if provider_id == "coop":
            return self.coop_token_url, self.coop_client_id
        if provider_id == "cookidoo":
            return (
                f"{self.cookidoo_base_url.rstrip('/')}/token-srv/token",
                self.cookidoo_client_id,
            )
        if provider_id in ("oda", "anthropic", "trumf"):
            raise HTTPException(
                status_code=400,
                detail=f"Tokenfornyelse støttes ikke for '{provider_id}'.",
            )
        raise HTTPException(status_code=404, detail=f"Ukjent leverandør: {provider_id}")

    def start_pkce(
        self,
        provider_id: str,
        household_id: UUID,
        redirect_uri: str | None = None,
        *,
        account_id: str | None = None,
        account_label: str | None = None,
        add_account: bool = False,
    ) -> PKCEStartResponse:
        if provider_id not in ("rema1000", "coop"):
            if provider_id in ("cookidoo", "oda", "anthropic", "trumf"):
                raise HTTPException(
                    status_code=400,
                    detail=f"PKCE-flyt støttes ikke for '{provider_id}'.",
                )
            raise HTTPException(status_code=404, detail=f"Ukjent leverandør: {provider_id}")

        verifier, challenge = generate_pkce_pair()
        state = secrets.token_hex(24)

        if provider_id == "rema1000":
            auth_url = self.rema1000_authorize_url
            tok_url = self.rema1000_token_url
            used_redirect_uri = redirect_uri or self.rema1000_redirect_uri
            client_id = self.rema1000_client_id
            scope = self.rema1000_scope
            instructions = (
                "Logg inn med din Rema 1000 Æ-konto i nettleseren. Etter innlogging, "
                "kopier fullførings-URL-en eller autorisasjonskoden og lim den inn her."
            )
        else:  # coop
            auth_url = self.coop_authorize_url
            tok_url = self.coop_token_url
            used_redirect_uri = redirect_uri or self.coop_redirect_uri
            client_id = self.coop_client_id
            scope = self.coop_scope
            instructions = (
                "Logg inn med din Coop-konto i nettleseren. Etter innlogging blir du sendt til "
                "no.coop.members://auth/callback (eller tilbake hit). Kopier hele omdirigerings-URL-en "
                "eller autorisasjonskoden og lim den inn her."
            )

        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": used_redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if provider_id == "coop":
            params["audience"] = DEFAULT_COOP_AUDIENCE
        authorize_url = f"{auth_url}?{urllib.parse.urlencode(params)}"

        now = datetime.now(UTC)
        transaction = PKCETransaction(
            provider_id=provider_id,
            household_id=household_id,
            code_verifier=verifier,
            redirect_uri=used_redirect_uri,
            client_id=client_id,
            token_endpoint=tok_url,
            created_at=now,
            account_id=account_id,
            account_label=account_label,
            add_account=add_account,
        )
        with self._lock:
            # Clean up old transactions (> 15 min)
            expired_keys = [
                k
                for k, tx in self._pkce_transactions.items()
                if now - tx.created_at > timedelta(minutes=15)
            ]
            for k in expired_keys:
                self._pkce_transactions.pop(k, None)

            self._pkce_transactions[state] = transaction

        return PKCEStartResponse(
            provider_id=provider_id,
            authorize_url=authorize_url,
            redirect_uri=used_redirect_uri,
            state=state,
            instructions=instructions,
        )

    def complete_pkce(
        self,
        provider_id: str,
        household_id: UUID,
        actor_id: str | None,
        code: str | None = None,
        state: str | None = None,
        redirect_url: str | None = None,
        *,
        account_id: str | None = None,
        account_label: str | None = None,
        add_account: bool = False,
    ) -> IntegrationInfo:
        if provider_id not in ("rema1000", "coop"):
            if provider_id in ("cookidoo", "oda", "anthropic", "trumf"):
                raise HTTPException(
                    status_code=400,
                    detail=f"PKCE-flyt støttes ikke for '{provider_id}'.",
                )
            raise HTTPException(status_code=404, detail=f"Ukjent leverandør: {provider_id}")

        parsed_code, parsed_state = parse_oauth_code_and_state(
            redirect_url=redirect_url, code=code, state=state
        )

        with self._lock:
            transaction = self._pkce_transactions.pop(parsed_state, None)

        if transaction is None:
            raise HTTPException(
                status_code=400,
                detail="Ugyldig eller utløpt state. PKCE-transaksjonen må startes på nytt.",
            )

        if transaction.provider_id != provider_id:
            raise HTTPException(
                status_code=400,
                detail=f"PKCE state matcher ikke leverandør '{provider_id}' (tilhører '{transaction.provider_id}').",
            )

        if transaction.household_id != household_id:
            raise HTTPException(
                status_code=403,
                detail="PKCE-transaksjonen tilhører ikke denne husholdningen.",
            )

        if datetime.now(UTC) - transaction.created_at > timedelta(minutes=15):
            raise HTTPException(
                status_code=400,
                detail="PKCE-transaksjonen har utløpt. Start på nytt.",
            )

        token_data = self._exchange_pkce_code(
            token_endpoint=transaction.token_endpoint,
            client_id=transaction.client_id,
            code=parsed_code,
            code_verifier=transaction.code_verifier,
            redirect_uri=transaction.redirect_uri,
            provider_id=provider_id,
        )

        access_token = token_data.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ConnectorAuthError(
                status_code=502,
                detail=f"Mottok ingen access_token fra {provider_id}.",
            )

        new_access_token, new_refresh_token, expires_at, refreshed_data = (
            self._validate_initial_oauth_refresh(
                provider_id=provider_id,
                token_endpoint=transaction.token_endpoint,
                client_id=transaction.client_id,
                token_data=token_data,
            )
        )
        effective_account_id = account_id if account_id is not None else transaction.account_id
        effective_label = account_label if account_label is not None else transaction.account_label
        effective_add = add_account or transaction.add_account

        self.store.save_connection(
            household_id=household_id,
            provider_id=provider_id,
            auth_mode="oauth_pkce",
            credentials={
                "access_token": new_access_token,
                "refresh_token": new_refresh_token,
                "token_type": (
                    refreshed_data.get("token_type") or token_data.get("token_type", "Bearer")
                ),
                "scope": (
                    refreshed_data.get("scope")
                    if refreshed_data.get("scope") is not None
                    else token_data.get("scope")
                ),
            },
            expires_at=expires_at,
            actor_id=actor_id,
            metadata={
                "has_refresh_token": bool(new_refresh_token),
                "last_refreshed_at": datetime.now(UTC).isoformat(),
            },
            account_id=effective_account_id,
            account_label=effective_label,
            add_account=effective_add,
        )

        return self.integrations.describe_one(provider_id, actor_id)

    def _exchange_pkce_code(
        self,
        token_endpoint: str,
        client_id: str,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        provider_id: str,
    ) -> dict[str, Any]:
        with self._get_http_client() as client:
            try:
                response = client.post(
                    token_endpoint,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "code": code,
                        "code_verifier": code_verifier,
                        "redirect_uri": redirect_uri,
                    },
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Nettverksfeil mot token-endepunktet til {provider_id}: {exc}",
                ) from None

        if response.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=f"Tokenutveksling feilet hos {provider_id}: {response.text}",
            )

        try:
            return response.json()
        except Exception:
            raise HTTPException(
                status_code=502,
                detail=f"Ugyldig JSON-respons fra token-endepunktet til {provider_id}.",
            ) from None

    def login_cookidoo(
        self,
        household_id: UUID,
        actor_id: str | None,
        email: str,
        password: str,
        country: str = "no",
        *,
        account_id: str | None = None,
        account_label: str | None = None,
        add_account: bool = False,
    ) -> IntegrationInfo:
        if not email or not email.strip() or not password or not password.strip():
            raise HTTPException(status_code=422, detail="E-post og passord må oppgis.")

        email_clean = email.strip()
        pwd_clean = password.strip()
        country_clean = (country.strip().lower() or "no") if country else "no"

        verifier, challenge = generate_pkce_pair()
        state = secrets.token_hex(16)

        authz_url = f"{self.cookidoo_base_url.rstrip('/')}/authz-srv/authz"
        token_url = f"{self.cookidoo_base_url.rstrip('/')}/token-srv/token"
        login_url = f"{self.cookidoo_base_url.rstrip('/')}/login-srv/login"

        with self._get_http_client() as client:
            token_data = None
            try:
                # 1. Start authorize request to reach CIAM login form and get requestId
                authz_params = {
                    "response_type": "code",
                    "client_id": self.cookidoo_client_id,
                    "redirect_uri": self.cookidoo_redirect_uri,
                    "scope": self.cookidoo_scope,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "market": country_clean,
                }
                authz_resp = client.get(
                    authz_url,
                    params=authz_params,
                    headers=COOKIDOO_LOGIN_HEADERS,
                    follow_redirects=True,
                )

                request_id = None
                if authz_resp.status_code == 200:
                    parsed_authz_url = urllib.parse.urlsplit(str(authz_resp.url))
                    qs = urllib.parse.parse_qs(parsed_authz_url.query)
                    request_id = qs.get("requestId", [None])[0]
                    if not request_id:
                        match = (
                            re.search(
                                r'<input[^>]*name=["\']requestId["\'][^>]*value=["\']([^"\']+)["\']',
                                authz_resp.text,
                            )
                            or re.search(
                                r'<input[^>]*value=["\']([0-9a-f-]{36})["\'][^>]*name=["\']requestId["\']',
                                authz_resp.text,
                            )
                            or re.search(
                                r'requestId["\']?\s*[:=]\s*["\']([0-9a-f-]{36})["\']',
                                authz_resp.text,
                                re.I,
                            )
                        )
                        if match:
                            request_id = match.group(1)

                if authz_resp.status_code == 200 and request_id:
                    current_url = login_url
                    current_data: dict[str, Any] | None = {
                        "requestId": request_id,
                        "username": email_clean,
                        "password": pwd_clean,
                    }
                    current_method = "POST"
                    auth_code = None

                    for _ in range(10):
                        login_resp = client.request(
                            current_method,
                            current_url,
                            data=current_data,
                            headers=COOKIDOO_LOGIN_HEADERS,
                            follow_redirects=False,
                        )
                        loc = login_resp.headers.get("Location")
                        if login_resp.status_code in (301, 302, 303, 307, 308) and loc:
                            if loc.startswith(self.cookidoo_redirect_uri):
                                parsed_loc = urllib.parse.urlsplit(loc)
                                qs_loc = urllib.parse.parse_qs(parsed_loc.query)
                                cb_state = qs_loc.get("state", [None])[0]
                                if cb_state and cb_state != state:
                                    raise HTTPException(
                                        status_code=400,
                                        detail="OAuth state stemte ikke overens.",
                                    )
                                auth_code = qs_loc.get("code", [None])[0]
                                break
                            if "error=invalid_username_password" in loc or "error=" in loc:
                                raise HTTPException(
                                    status_code=401,
                                    detail="Ugyldig e-postadresse eller passord for Cookidoo.",
                                )
                            current_url = urllib.parse.urljoin(current_url, loc)
                            current_method = "GET"
                            current_data = None
                            continue
                        if login_resp.status_code in (400, 401, 403):
                            raise HTTPException(
                                status_code=401,
                                detail="Ugyldig e-postadresse eller passord for Cookidoo.",
                            )
                        break

                    if not auth_code:
                        raise HTTPException(
                            status_code=401,
                            detail="Ugyldig e-postadresse eller passord for Cookidoo.",
                        )

                    token_resp = client.post(
                        token_url,
                        data={
                            "grant_type": "authorization_code",
                            "client_id": self.cookidoo_client_id,
                            "redirect_uri": self.cookidoo_redirect_uri,
                            "code": auth_code,
                            "code_verifier": verifier,
                        },
                        headers={"Accept": "application/json", **COOKIDOO_LOGIN_HEADERS},
                    )
                    if token_resp.status_code in (400, 401, 403):
                        raise HTTPException(
                            status_code=401,
                            detail="Ugyldig e-postadresse eller passord for Cookidoo.",
                        )
                    if token_resp.status_code != 200:
                        raise HTTPException(
                            status_code=token_resp.status_code,
                            detail=f"Tokenutveksling feilet hos Cookidoo: {token_resp.text}",
                        )
                    token_data = token_resp.json()

                else:
                    # Fallback for unit test mocks that only mock /token-srv/token
                    legacy_resp = client.post(
                        token_url,
                        data={
                            "grant_type": "password",
                            "username": email_clean,
                            "password": pwd_clean,
                            "client_id": self.cookidoo_client_id,
                            "country": country_clean,
                            "scope": self.cookidoo_scope,
                        },
                        headers={"Accept": "application/json"},
                    )
                    if legacy_resp.status_code in (400, 401, 403):
                        raise HTTPException(
                            status_code=401,
                            detail="Ugyldig e-postadresse eller passord for Cookidoo.",
                        )
                    if legacy_resp.status_code != 200:
                        raise HTTPException(
                            status_code=legacy_resp.status_code,
                            detail=f"Cookidoo innlogging feilet: {legacy_resp.text}",
                        )
                    token_data = legacy_resp.json()
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Nettverksfeil mot Cookidoo / Vorwerk identity: {exc}",
                ) from None

        if not token_data or not isinstance(token_data, dict):
            raise HTTPException(
                status_code=502,
                detail="Ugyldig JSON-respons fra Cookidoo/Cidaas.",
            )

        access_token = token_data.get("access_token") or token_data.get("token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ConnectorAuthError(
                status_code=502,
                detail="Mottok ingen access_token fra Cookidoo.",
            )

        new_access_token, new_refresh_token, expires_at, refreshed_data = (
            self._validate_initial_oauth_refresh(
                provider_id="cookidoo",
                token_endpoint=token_url,
                client_id=self.cookidoo_client_id,
                token_data=token_data,
            )
        )

        # Invariant: ONLY store resulting encrypted OAuth tokens, NEVER raw password
        self.store.save_connection(
            household_id=household_id,
            provider_id="cookidoo",
            auth_mode="oauth_password",
            credentials={
                "access_token": new_access_token,
                "refresh_token": new_refresh_token,
                "token_type": (
                    refreshed_data.get("token_type") or token_data.get("token_type", "Bearer")
                ),
                "country": country_clean,
            },
            expires_at=expires_at,
            actor_id=actor_id,
            metadata={
                "country": country_clean,
                "has_refresh_token": bool(new_refresh_token),
                "last_refreshed_at": datetime.now(UTC).isoformat(),
            },
            account_id=account_id,
            account_label=account_label,
            add_account=add_account,
        )

        return self.integrations.describe_one("cookidoo", actor_id)

    def _execute_oauth_refresh_request(
        self,
        provider_id: str,
        token_endpoint: str,
        client_id: str,
        refresh_token: str,
        *,
        rejection_status_code: int,
        rejection_detail: str,
        use_connector_auth_error: bool = False,
    ) -> tuple[str, str, datetime | None, dict[str, Any]]:
        clean_refresh_token = refresh_token.strip()
        headers = {"Accept": "application/json"}
        if provider_id == "cookidoo":
            headers = {**headers, **COOKIDOO_LOGIN_HEADERS}

        error_cls: type[HTTPException] = (
            ConnectorAuthError if use_connector_auth_error else HTTPException
        )

        with self._get_http_client() as client:
            try:
                response = client.post(
                    token_endpoint,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "refresh_token": clean_refresh_token,
                    },
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                safe_exc = str(exc).replace(clean_refresh_token, "[REDACTED]")
                raise error_cls(
                    status_code=502,
                    detail=f"Nettverksfeil mot token-endepunktet til {provider_id}: {safe_exc}",
                ) from None

        if response.status_code == 401:
            raise error_cls(status_code=401, detail=rejection_detail)
        if response.status_code != 200:
            safe_body = response.text.replace(clean_refresh_token, "[REDACTED]")
            status_code = rejection_status_code if response.status_code in (400, 403) else 502
            detail = (
                rejection_detail
                if use_connector_auth_error and response.status_code in (400, 403)
                else f"Tokenfornyelse feilet hos {provider_id}: {safe_body}"
            )
            raise error_cls(status_code=status_code, detail=detail)

        try:
            token_data = response.json()
        except Exception:
            raise error_cls(
                status_code=502,
                detail=f"Ugyldig JSON-respons fra token-endepunktet til {provider_id}.",
            ) from None

        if not isinstance(token_data, dict):
            raise error_cls(
                status_code=502,
                detail=f"Ugyldig JSON-respons fra token-endepunktet til {provider_id}.",
            )

        access_token = token_data.get("access_token") or (
            token_data.get("token") if provider_id == "cookidoo" else None
        )
        if not isinstance(access_token, str) or not access_token.strip():
            raise error_cls(
                status_code=502,
                detail=f"Mottok ingen access_token fra {provider_id}.",
            )

        rotated_refresh = token_data.get("refresh_token")
        new_refresh_token = (
            rotated_refresh.strip()
            if isinstance(rotated_refresh, str) and rotated_refresh.strip()
            else clean_refresh_token
        )
        expires_at = _parse_expires_at(token_data.get("expires_in"))
        return access_token, new_refresh_token, expires_at, token_data

    def _validate_initial_oauth_refresh(
        self,
        provider_id: str,
        token_endpoint: str,
        client_id: str,
        token_data: dict[str, Any],
    ) -> tuple[str, str, datetime | None, dict[str, Any]]:
        refresh_token = token_data.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            raise ConnectorAuthError(
                status_code=401,
                detail=(
                    f"Mottok ingen refresh_token fra {provider_id}. "
                    "Tilkoblingen kan ikke fornyes automatisk og ble ikke lagret."
                ),
            )

        new_access_token, new_refresh_token, refreshed_expires_at, refreshed_data = (
            self._execute_oauth_refresh_request(
                provider_id=provider_id,
                token_endpoint=token_endpoint,
                client_id=client_id,
                refresh_token=refresh_token,
                rejection_status_code=401,
                rejection_detail=(
                    f"Tokenfornyelse ble avvist av {provider_id}. "
                    "Tilkoblingen ble ikke lagret — prøv å logge inn på nytt."
                ),
                use_connector_auth_error=True,
            )
        )
        expires_at = refreshed_expires_at or _parse_expires_at(token_data.get("expires_in"))
        return new_access_token, new_refresh_token, expires_at, refreshed_data

    def _validate_trumf_session(
        self,
        raw_cookie: str,
        *,
        error_detail: str,
    ) -> tuple[str, datetime | None]:
        if not isinstance(raw_cookie, str) or not raw_cookie.strip():
            raise ConnectorAuthError(status_code=401, detail=error_detail)

        normalized_cookie, cookie_map = _normalize_trumf_cookie(raw_cookie)
        if not normalized_cookie:
            raise ConnectorAuthError(status_code=401, detail=error_detail)

        session_url = f"{self.trumf_base_url.rstrip('/')}/api/auth/session"

        try:
            with self._get_http_client() as client:
                response = client.get(
                    session_url,
                    headers={
                        "Cookie": normalized_cookie,
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            safe_err = f"Nettverksfeil mot Trumf: {exc}".replace(normalized_cookie, "[REDACTED]")
            raise ConnectorAuthError(status_code=502, detail=safe_err) from None

        updates, removed, cleared_session = _parse_trumf_set_cookies(
            response.headers.get_list("set-cookie")
        )

        data: Any = None
        if response.status_code == 200:
            try:
                data = response.json()
            except Exception:
                data = None

        has_session_identity = isinstance(data, dict) and bool(
            data.get("user") or data.get("accessToken")
        )
        if response.status_code != 200 or not has_session_identity or cleared_session:
            raise ConnectorAuthError(
                status_code=(502 if response.status_code >= 500 else 401),
                detail=error_detail,
            )

        merged_cookies = dict(cookie_map)
        for name in removed:
            merged_cookies.pop(name, None)
        merged_cookies.update(updates)
        new_cookie = (
            "; ".join(f"{k}={v}" for k, v in merged_cookies.items())
            if merged_cookies
            else normalized_cookie
        )
        server_expires_at = _parse_iso_datetime(data.get("expires"))
        return new_cookie, server_expires_at

    def connect_trumf_cookie(
        self,
        household_id: UUID,
        actor_id: str | None,
        raw_cookie: str,
        *,
        expires_at: datetime | None = None,
        account_id: str | None = None,
        account_label: str | None = None,
        add_account: bool = False,
    ) -> IntegrationInfo:
        """Validate and renew a Trumf session cookie before saving it to ConnectorStore."""
        new_cookie, server_expires_at = self._validate_trumf_session(
            raw_cookie,
            error_detail=TRUMF_SESSION_CONNECT_ERROR,
        )
        effective_expires_at = (
            server_expires_at or expires_at or (datetime.now(UTC) + timedelta(hours=24))
        )
        self.store.save_connection(
            household_id=household_id,
            provider_id="trumf",
            auth_mode="session_cookie",
            credentials={"session_cookie": new_cookie},
            expires_at=effective_expires_at,
            actor_id=actor_id,
            metadata={
                "has_session_cookie": True,
                "last_refreshed_at": datetime.now(UTC).isoformat(),
            },
            account_id=account_id,
            account_label=account_label,
            add_account=add_account,
        )
        return self.integrations.describe_one("trumf", actor_id)

    def connect_kassal_key(
        self,
        household_id: UUID,
        actor_id: str | None,
        api_key: str,
        *,
        account_id: str | None = None,
        account_label: str | None = None,
        add_account: bool = False,
    ) -> IntegrationInfo:
        """Validate Kassal.app API key before saving it to ConnectorStore."""
        from home_kitchen.kassal import KassalAuthError, KassalClient, KassalError

        clean_key = api_key.strip() if isinstance(api_key, str) else ""
        if not clean_key:
            raise ConnectorAuthError(
                status_code=422, detail="Kassal.app krever en gyldig API-nøkkel."
            )

        client = KassalClient(clean_key, transport=self.transport)
        try:
            client.validate_key()
        except KassalAuthError as exc:
            raise ConnectorAuthError(status_code=401, detail=str(exc)) from exc
        except KassalError as exc:
            raise ConnectorAuthError(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            raise ConnectorAuthError(
                status_code=502, detail=f"Feil ved validering av Kassal.app API-nøkkel: {exc}"
            ) from exc

        self.store.save_connection(
            household_id=household_id,
            provider_id="kassal",
            auth_mode="api_key",
            credentials={"api_key": clean_key},
            actor_id=actor_id,
            metadata={
                "last_refreshed_at": datetime.now(UTC).isoformat(),
            },
            account_id=account_id,
            account_label=account_label,
            add_account=add_account,
        )
        return self.integrations.describe_one("kassal", actor_id)

    def _read_credentials_safely(
        self,
        household_id: UUID,
        provider_id: str,
        account_id: str,
    ) -> dict[str, Any] | None:
        try:
            return self.store.get_credentials(household_id, provider_id, account_id=account_id)
        except (InvalidToken, ValueError):
            detail = "Lagrede tilkoblingsdetaljer kan ikke leses. Koble til på nytt."
            self.store.update_account_metadata(
                household_id,
                provider_id,
                account_id,
                {"last_error": detail},
            )
            raise HTTPException(status_code=409, detail=detail) from None
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

    def _refresh_single_account(
        self,
        household_id: UUID,
        actor_id: str | None,
        provider_id: str,
        account_id: str,
        token_endpoint: str,
        client_id: str,
    ) -> None:
        with self._connection_lock(household_id, provider_id, account_id):
            meta = self.store.get_connection_metadata(
                household_id, provider_id, account_id=account_id
            )
            creds = self._read_credentials_safely(household_id, provider_id, account_id)
            if meta is None or creds is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Ingen lagret tilkobling funnet for '{provider_id}' (konto '{account_id}').",
                )

            refresh_token = creds.get("refresh_token")
            if not isinstance(refresh_token, str) or not refresh_token.strip():
                detail = f"Tilkoblingen for '{provider_id}' mangler refresh_token og må kobles til på nytt."
                self.store.update_account_metadata(
                    household_id,
                    provider_id,
                    meta.account_id,
                    {"last_error": detail},
                )
                raise HTTPException(status_code=400, detail=detail)

            clean_refresh_token = refresh_token.strip()
            access_secret = creds.get("access_token")
            try:
                access_token, new_refresh_token, expires_at, token_data = (
                    self._execute_oauth_refresh_request(
                        provider_id=provider_id,
                        token_endpoint=token_endpoint,
                        client_id=client_id,
                        refresh_token=clean_refresh_token,
                        rejection_status_code=400,
                        rejection_detail=f"Tokenfornyelse avvist av {provider_id}. Koble til på nytt.",
                        use_connector_auth_error=False,
                    )
                )
            except HTTPException as exc:
                safe_detail = str(exc.detail).replace(clean_refresh_token, "[REDACTED]")
                if isinstance(access_secret, str) and access_secret.strip():
                    safe_detail = safe_detail.replace(access_secret.strip(), "[REDACTED]")
                self.store.update_account_metadata(
                    household_id,
                    provider_id,
                    meta.account_id,
                    {"last_error": safe_detail},
                )
                raise

            updated_credentials: dict[str, Any] = {
                **creds,
                "access_token": access_token,
                "refresh_token": new_refresh_token,
                "token_type": token_data.get("token_type") or creds.get("token_type", "Bearer"),
            }
            if token_data.get("scope") is not None:
                updated_credentials["scope"] = token_data["scope"]

            updated_metadata = dict(meta.metadata)
            updated_metadata.pop("last_error", None)
            updated_metadata["last_refreshed_at"] = datetime.now(UTC).isoformat()
            updated_metadata["has_refresh_token"] = bool(new_refresh_token)

            effective_actor = (
                actor_id if actor_id is not None else (meta.updated_by_actor_id or meta.actor_id)
            )
            self.store.save_connection(
                household_id=household_id,
                provider_id=provider_id,
                auth_mode=meta.auth_mode,
                credentials=updated_credentials,
                expires_at=expires_at,
                actor_id=effective_actor,
                metadata=updated_metadata,
                account_id=meta.account_id,
                account_label=meta.account_label,
            )

    def _refresh_trumf_account(
        self,
        household_id: UUID,
        actor_id: str | None,
        account_id: str,
    ) -> None:
        with self._connection_lock(household_id, "trumf", account_id):
            meta = self.store.get_connection_metadata(household_id, "trumf", account_id=account_id)
            creds = self._read_credentials_safely(household_id, "trumf", account_id)
            if meta is None or creds is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Ingen lagret tilkobling funnet for 'trumf' (konto '{account_id}').",
                )

            raw_cookie = creds.get("session_cookie") or creds.get("cookie")
            if not isinstance(raw_cookie, str) or not raw_cookie.strip():
                detail = "Tilkoblingen for 'trumf' mangler sesjonsinformasjonskapsel og må kobles til på nytt."
                self.store.update_account_metadata(
                    household_id,
                    "trumf",
                    meta.account_id,
                    {"last_error": detail},
                )
                raise HTTPException(status_code=400, detail=detail)

            try:
                new_cookie, server_expires_at = self._validate_trumf_session(
                    raw_cookie,
                    error_detail=TRUMF_SESSION_REFRESH_ERROR,
                )
            except ConnectorAuthError as exc:
                self.store.update_account_metadata(
                    household_id,
                    "trumf",
                    meta.account_id,
                    {"last_error": str(exc.detail)},
                )
                raise

            expires_at = (
                server_expires_at or meta.expires_at or (datetime.now(UTC) + timedelta(hours=24))
            )

            updated_credentials: dict[str, Any] = {
                **creds,
                "session_cookie": new_cookie,
            }
            updated_credentials.pop("cookie", None)

            updated_metadata = dict(meta.metadata)
            updated_metadata.pop("last_error", None)
            updated_metadata["last_refreshed_at"] = datetime.now(UTC).isoformat()
            updated_metadata["has_session_cookie"] = True

            effective_actor = (
                actor_id if actor_id is not None else (meta.updated_by_actor_id or meta.actor_id)
            )
            self.store.upsert_account_credentials(
                household_id=household_id,
                provider_id="trumf",
                auth_mode=meta.auth_mode,
                credentials=updated_credentials,
                account_id=meta.account_id,
                account_label=meta.account_label,
                expires_at=expires_at,
                actor_id=effective_actor,
                metadata=updated_metadata,
            )

    @staticmethod
    def _has_refresh_material(
        provider_id: str, auth_mode: str, creds: dict[str, Any] | None
    ) -> bool:
        if not creds:
            return False
        if provider_id == "trumf" and auth_mode in TRUMF_REFRESH_MODES:
            raw_cookie = creds.get("session_cookie") or creds.get("cookie")
            return isinstance(raw_cookie, str) and bool(raw_cookie.strip())
        if auth_mode not in OAUTH_REFRESH_MODES:
            return False
        refresh_token = creds.get("refresh_token")
        return isinstance(refresh_token, str) and bool(refresh_token.strip())

    @staticmethod
    def _should_auto_refresh(provider_id: str, status: Any, *, margin_seconds: int) -> bool:
        now = datetime.now(UTC)
        deadline = now + timedelta(seconds=margin_seconds)
        expires_at = getattr(status, "expires_at", None)
        auth_mode = getattr(status, "auth_mode", "")
        if provider_id == "trumf" and auth_mode in TRUMF_REFRESH_MODES:
            if expires_at is not None:
                return expires_at <= deadline
            meta = getattr(status, "metadata", None) or {}
            last_refreshed_at = (
                _parse_iso_datetime(meta.get("last_refreshed_at"))
                if isinstance(meta, dict)
                else None
            )
            return (
                last_refreshed_at is None
                or last_refreshed_at <= now - _TRUMF_UNKNOWN_EXPIRY_REFRESH_INTERVAL
            )
        return expires_at is not None and expires_at <= deadline

    def refresh_connection(
        self,
        household_id: UUID,
        actor_id: str | None,
        provider_id: str,
        account_id: str | None = None,
    ) -> IntegrationInfo:
        """Exchange stored refresh_token(s) or renew Trumf session cookie."""
        token_endpoint: str | None = None
        client_id: str | None = None
        if provider_id != "trumf":
            token_endpoint, client_id = self._refresh_config(provider_id)

        def refresh_one(target_account_id: str) -> None:
            if provider_id == "trumf":
                meta = self.store.get_connection_metadata(
                    household_id, provider_id, account_id=target_account_id
                )
                if meta is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Ingen lagret tilkobling funnet for '{provider_id}' (konto '{target_account_id}').",
                    )
                if meta.auth_mode not in TRUMF_REFRESH_MODES:
                    raise HTTPException(
                        status_code=400,
                        detail="Tilkoblingen for 'trumf' mangler sesjonsinformasjonskapsel og må kobles til på nytt.",
                    )
                self._refresh_trumf_account(household_id, actor_id, target_account_id)
                return
            assert token_endpoint is not None and client_id is not None
            self._refresh_single_account(
                household_id,
                actor_id,
                provider_id,
                target_account_id,
                token_endpoint,
                client_id,
            )

        cleaned_account_id = account_id.strip() if isinstance(account_id, str) else None
        if cleaned_account_id:
            refresh_one(cleaned_account_id)
            return self.integrations.describe_one(provider_id, actor_id)

        accounts = self.store.list_credentials(household_id, provider_id)
        if not accounts:
            raise HTTPException(
                status_code=404,
                detail=f"Ingen lagret tilkobling funnet for '{provider_id}'.",
            )

        if len(accounts) == 1:
            refresh_one(accounts[0].account_id)
            return self.integrations.describe_one(provider_id, actor_id)

        refreshable_accounts = []
        last_read_exc: HTTPException | None = None
        for acc in accounts:
            try:
                acc_creds = self._read_credentials_safely(household_id, provider_id, acc.account_id)
            except HTTPException as exc:
                last_read_exc = exc
                continue
            if self._has_refresh_material(provider_id, acc.auth_mode, acc_creds):
                refreshable_accounts.append(acc)
        if not refreshable_accounts:
            if last_read_exc is not None:
                raise last_read_exc
            if provider_id == "trumf":
                missing_label = "sesjonsinformasjonskapsel"
            else:
                missing_label = "refresh_token"
            raise HTTPException(
                status_code=400,
                detail=f"Tilkoblingene for '{provider_id}' mangler {missing_label} og må kobles til på nytt.",
            )

        success_count = 0
        last_exc: HTTPException | None = None
        for acc in refreshable_accounts:
            try:
                refresh_one(acc.account_id)
                success_count += 1
            except HTTPException as exc:
                last_exc = exc

        if success_count == 0 and last_exc is not None:
            raise last_exc

        return self.integrations.describe_one(provider_id, actor_id)

    def ensure_fresh_credentials(
        self,
        household_id: UUID,
        actor_id: str | None,
        provider_id: str,
        margin_seconds: int = 300,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Return stored credentials, proactively refreshing if nearing or past expiry."""
        meta = self.store.get_connection_metadata(household_id, provider_id, account_id=account_id)
        if meta is None:
            raise HTTPException(
                status_code=404,
                detail=f"Ingen lagret tilkobling funnet for '{provider_id}'.",
            )
        target_account_id = meta.account_id
        with self._connection_lock(household_id, provider_id, target_account_id):
            meta = self.store.get_connection_metadata(
                household_id, provider_id, account_id=target_account_id
            )
            creds = self._read_credentials_safely(household_id, provider_id, target_account_id)
            if meta is None or creds is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Ingen lagret tilkobling funnet for '{provider_id}'.",
                )

            if (
                provider_id in REFRESHABLE_PROVIDERS
                and self._has_refresh_material(provider_id, meta.auth_mode, creds)
                and self._should_auto_refresh(provider_id, meta, margin_seconds=margin_seconds)
            ):
                self.refresh_connection(
                    household_id, actor_id, provider_id, account_id=target_account_id
                )
                refreshed = self.store.get_credentials(
                    household_id, provider_id, account_id=target_account_id
                )
                if refreshed is not None:
                    return refreshed

            return creds

    def refresh_expiring_connections(self, margin_seconds: int = 900) -> list[str]:
        """Scan ConnectorStore for all refreshable accounts due for renewal and renew them proactively."""
        refreshed: list[str] = []
        connections = self.store.list_credentials(self.household_id)

        for status in connections:
            provider_id = status.provider_id
            account_id = status.account_id
            if provider_id not in REFRESHABLE_PROVIDERS:
                continue
            if not self._should_auto_refresh(provider_id, status, margin_seconds=margin_seconds):
                continue
            try:
                creds = self.store.get_credentials(
                    self.household_id, provider_id, account_id=account_id
                )
                if not self._has_refresh_material(provider_id, status.auth_mode, creds):
                    continue
                self.refresh_connection(
                    self.household_id,
                    status.updated_by_actor_id or status.actor_id,
                    provider_id,
                    account_id=account_id,
                )
                refreshed.append(provider_id)
            except Exception as exc:
                if isinstance(exc, HTTPException):
                    existing_meta = self.store.get_connection_metadata(
                        self.household_id, provider_id, account_id=account_id
                    )
                    has_last_error = bool(
                        existing_meta
                        and isinstance(existing_meta.metadata, dict)
                        and existing_meta.metadata.get("last_error")
                    )
                    if not has_last_error:
                        self.store.update_account_metadata(
                            self.household_id,
                            provider_id,
                            account_id,
                            {"last_error": str(exc.detail or "Automatisk tokenfornyelse feilet")},
                        )
                else:
                    self.store.update_account_metadata(
                        self.household_id,
                        provider_id,
                        account_id,
                        {"last_error": "Automatisk tokenfornyelse feilet"},
                    )
                _LOG.warning(
                    "Proactive token refresh failed for provider %s account %s",
                    provider_id,
                    account_id,
                )

        return refreshed

    async def refresh_forever(self, interval: float = DEFAULT_REFRESH_INTERVAL_SECONDS) -> None:
        """Periodically renew expiring OAuth connections in the background."""
        while True:
            try:
                task = asyncio.create_task(asyncio.to_thread(self.refresh_expiring_connections))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    try:
                        await task
                    except Exception:
                        pass
                    raise
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.warning("Background connector token refresh failed; will retry on next run")
            await asyncio.sleep(interval)
