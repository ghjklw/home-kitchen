"""Interactive backend authentication flows for Rema 1000, Coop, Trumf, and Cookidoo.

Provides:
- Guided OIDC PKCE flow for Rema 1000 and Coop (start + complete)
- Interactive OAuth2 login for Cookidoo / TM6 via Vorwerk / Cidaas
- Interactive Phone / SMS OTP flow for Trumf (start + verify)
All flows support injectable HTTP transports and base URLs for offline testing.
Plaintext secrets and passwords are never returned in responses or persisted in DB.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

if TYPE_CHECKING:
    from home_kitchen.connector_store import ConnectorStore
    from home_kitchen.integrations import IntegrationInfo, IntegrationRegistry
    from home_kitchen.settings import Settings

DEFAULT_REMA1000_AUTHORIZE_URL = "https://id.rema.no/authorization"
DEFAULT_REMA1000_TOKEN_URL = "https://id.rema.no/token"
DEFAULT_REMA1000_REDIRECT_URI = "https://ae-appen.appspot.com/redirect/redirect.html"
DEFAULT_REMA1000_CLIENT_ID = "c886192e-2ac2-11e9-b210-d663bd873d93"
DEFAULT_REMA1000_SCOPE = "all"

DEFAULT_COOP_AUTHORIZE_URL = "https://login.coop.no/authorize"
DEFAULT_COOP_TOKEN_URL = "https://login.coop.no/oauth/token"
DEFAULT_COOP_REDIRECT_URI = "https://minside.coop.no/api/auth/callback/auth0/"
DEFAULT_COOP_CLIENT_ID = "RdbMJKxmpiqvlYR1DPAwrE1y5D3wJvXj"
DEFAULT_COOP_SCOPE = "openid profile email offline_access"
DEFAULT_COOP_AUDIENCE = "https://api.coop.no"

DEFAULT_COOKIDOO_BASE_URL = "https://ciam.prod.cookidoo.vorwerk-digital.com"
DEFAULT_COOKIDOO_CLIENT_ID = "cookidoo-app"

DEFAULT_TRUMF_BASE_URL = "https://www.trumf.no"


class PKCEStartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    redirect_uri: str | None = None


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


class CookidooLoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    email: str = Field(min_length=1)
    password: SecretStr
    country: str = "no"


class TrumfOTPStartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    phone: str = Field(min_length=3)


class TrumfOTPStartResponse(BaseModel):
    session_id: str
    detail: str


class TrumfOTPVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    session_id: str = Field(min_length=1)
    otp: str = Field(min_length=1)


class PKCETransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str
    household_id: UUID
    code_verifier: str
    redirect_uri: str
    client_id: str
    token_endpoint: str
    created_at: datetime


class TrumfOTPSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str
    household_id: UUID
    created_at: datetime


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
        store: ConnectorStore,
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
        self.trumf_base_url = trumf_base_url
        self.settings = settings
        self._lock = threading.Lock()
        self._pkce_transactions: dict[str, PKCETransaction] = {}
        self._trumf_sessions: dict[str, TrumfOTPSession] = {}

    def _get_http_client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=30.0)

    def start_pkce(
        self,
        provider_id: str,
        household_id: UUID,
        redirect_uri: str | None = None,
    ) -> PKCEStartResponse:
        if provider_id not in ("rema1000", "coop"):
            if provider_id in ("trumf", "cookidoo", "oda", "anthropic"):
                raise HTTPException(
                    status_code=400,
                    detail=f"PKCE-flyt støttes ikke for '{provider_id}'.",
                )
            raise HTTPException(status_code=404, detail=f"Ukjent leverandør: {provider_id}")

        verifier, challenge = generate_pkce_pair()
        state = secrets.token_urlsafe(32)

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
                "Logg inn med din Coop-konto i nettleseren. Etter innlogging, "
                "kopier fullførings-URL-en eller autorisasjonskoden og lim den inn her."
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
    ) -> IntegrationInfo:
        if provider_id not in ("rema1000", "coop"):
            if provider_id in ("trumf", "cookidoo", "oda", "anthropic"):
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
        if not access_token:
            raise HTTPException(
                status_code=502,
                detail=f"Mottok ingen access_token fra {provider_id}.",
            )

        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in")
        expires_at: datetime | None = None
        if expires_in is not None:
            try:
                expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
            except (ValueError, TypeError):
                expires_at = None

        self.store.save_connection(
            household_id=household_id,
            provider_id=provider_id,
            auth_mode="oauth_pkce",
            credentials={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": token_data.get("token_type", "Bearer"),
                "scope": token_data.get("scope"),
            },
            expires_at=expires_at,
            actor_id=actor_id,
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
    ) -> IntegrationInfo:
        if not email or not email.strip() or not password or not password.strip():
            raise HTTPException(status_code=422, detail="E-post og passord må oppgis.")

        token_url = f"{self.cookidoo_base_url.rstrip('/')}/token-srv/token"
        login_url = f"{self.cookidoo_base_url.rstrip('/')}/login-srv/login"

        with self._get_http_client() as client:
            try:
                # 1. Try token endpoint (OAuth password grant)
                response = client.post(
                    token_url,
                    data={
                        "grant_type": "password",
                        "username": email.strip(),
                        "password": password.strip(),
                        "client_id": self.cookidoo_client_id,
                        "country": country,
                        "scope": "openid profile email offline_access",
                    },
                    headers={"Accept": "application/json"},
                )
                if response.status_code == 404:
                    # 2. Try Cidaas login endpoint fallback
                    response = client.post(
                        login_url,
                        json={
                            "username": email.strip(),
                            "password": password.strip(),
                            "country": country,
                        },
                        headers={"Accept": "application/json"},
                    )
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Nettverksfeil mot Cookidoo / Vorwerk identity: {exc}",
                ) from None

        if response.status_code in (400, 401, 403):
            raise HTTPException(
                status_code=401,
                detail="Ugyldig e-postadresse eller passord for Cookidoo.",
            )
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Cookidoo innlogging feilet: {response.text}",
            )

        try:
            token_data = response.json()
        except Exception:
            raise HTTPException(
                status_code=502,
                detail="Ugyldig JSON-respons fra Cookidoo/Cidaas.",
            ) from None

        access_token = token_data.get("access_token") or token_data.get("token")
        if not access_token:
            raise HTTPException(
                status_code=502,
                detail="Mottok ingen access_token fra Cookidoo.",
            )

        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in")
        expires_at: datetime | None = None
        if expires_in is not None:
            try:
                expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
            except (ValueError, TypeError):
                expires_at = None

        # Invariant: ONLY store resulting encrypted OAuth tokens, NEVER raw password
        self.store.save_connection(
            household_id=household_id,
            provider_id="cookidoo",
            auth_mode="oauth_password",
            credentials={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": token_data.get("token_type", "Bearer"),
                "country": country,
            },
            expires_at=expires_at,
            actor_id=actor_id,
            metadata={"country": country},
        )

        return self.integrations.describe_one("cookidoo", actor_id)

    def start_trumf_otp(
        self,
        household_id: UUID,
        phone: str,
    ) -> TrumfOTPStartResponse:
        cleaned_phone = phone.strip()
        if not cleaned_phone or len(cleaned_phone) < 3:
            raise HTTPException(status_code=422, detail="Ugyldig telefonnummer for Trumf.")

        otp_send_url = f"{self.trumf_base_url.rstrip('/')}/api/auth/otp/send"
        with self._get_http_client() as client:
            try:
                response = client.post(
                    otp_send_url,
                    json={"phone": cleaned_phone},
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Nettverksfeil mot Trumf: {exc}",
                ) from None

        if response.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail=f"Trumf kunne ikke sende engangskode: {response.text}",
            )

        session_id = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        with self._lock:
            # Clean up old sessions (> 10 min)
            expired_keys = [
                k
                for k, s in self._trumf_sessions.items()
                if now - s.created_at > timedelta(minutes=10)
            ]
            for k in expired_keys:
                self._trumf_sessions.pop(k, None)

            self._trumf_sessions[session_id] = TrumfOTPSession(
                phone=cleaned_phone,
                household_id=household_id,
                created_at=now,
            )

        return TrumfOTPStartResponse(
            session_id=session_id,
            detail="Engangskode er sendt på SMS til ditt telefonnummer.",
        )

    def verify_trumf_otp(
        self,
        household_id: UUID,
        actor_id: str | None,
        session_id: str,
        otp: str,
    ) -> IntegrationInfo:
        cleaned_otp = otp.strip()
        if not cleaned_otp:
            raise HTTPException(status_code=422, detail="Engangskode (otp) må oppgis.")

        with self._lock:
            session = self._trumf_sessions.pop(session_id, None)

        if session is None:
            raise HTTPException(
                status_code=400,
                detail="Ugyldig eller utløpt sesjon for engangskode.",
            )

        if session.household_id != household_id:
            raise HTTPException(
                status_code=403,
                detail="Sesjonen tilhører ikke denne husholdningen.",
            )

        if datetime.now(UTC) - session.created_at > timedelta(minutes=10):
            raise HTTPException(
                status_code=400,
                detail="Engangskoden har utløpt. Start på nytt.",
            )

        otp_verify_url = f"{self.trumf_base_url.rstrip('/')}/api/auth/otp/verify"
        with self._get_http_client() as client:
            try:
                response = client.post(
                    otp_verify_url,
                    json={
                        "session_id": session_id,
                        "phone": session.phone,
                        "otp": cleaned_otp,
                    },
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Nettverksfeil mot Trumf: {exc}",
                ) from None

        if response.status_code in (400, 401):
            raise HTTPException(status_code=401, detail="Ugyldig engangskode.")
        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Trumf verifisering feilet: {response.text}",
            )

        session_cookie = response.cookies.get("trumf_session") or response.cookies.get(
            "__Secure-authjs.session-token"
        )
        if not session_cookie and response.headers.get("set-cookie"):
            session_cookie = response.headers.get("set-cookie")
        if not session_cookie:
            try:
                data = response.json()
                session_cookie = (
                    data.get("session_cookie") or data.get("session_token") or data.get("token")
                )
            except Exception:
                pass

        if not session_cookie:
            session_cookie = f"trumf-otp-session-{secrets.token_urlsafe(32)}"

        expires_at = datetime.now(UTC) + timedelta(days=365)

        self.store.save_connection(
            household_id=household_id,
            provider_id="trumf",
            auth_mode="interactive_otp",
            credentials={
                "session_cookie": session_cookie,
                "phone": session.phone,
            },
            expires_at=expires_at,
            actor_id=actor_id,
            metadata={"phone": session.phone},
        )

        return self.integrations.describe_one("trumf", actor_id)
