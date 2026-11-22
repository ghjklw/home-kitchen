"""OAuth broker for household members connecting cloud MCP clients."""

import json
import math
import secrets
import time
from hashlib import sha256
from urllib.parse import urlencode, urlsplit

from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.oauth_proxy.models import (
    ClientCode,
    JTIMapping,
    ProxyDCRClient,
    RefreshTokenMetadata,
    UpstreamTokenSet,
)
from fastmcp.server.auth.providers.azure import AzureProvider
from fastmcp.server.auth.redirect_validation import validate_redirect_uri
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from mcp.server.auth.provider import AuthorizationCode, RefreshToken, RegistrationError, TokenError
from mcp.server.transport_security import RequestBodyLimitMiddleware
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from sqlalchemy.engine import Engine
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from home_kitchen.context import HistoryContext, history_context
from home_kitchen.mcp_oauth_storage import OAuthRegistrationLimitError, PostgresOAuthStore
from home_kitchen.settings import Settings


class PublicClientMetadata:
    """Preserve SDK metadata/CORS while declaring its public-client method."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        start = None
        parts = []

        async def metadata_send(message):
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                if start is None:
                    raise RuntimeError("Metadata response is missing its status")
                parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    body = b"".join(parts)
                    if start["status"] == 200 and scope["method"] != "OPTIONS":
                        document = json.loads(body)
                        document["token_endpoint_auth_methods_supported"] = ["none"]
                        body = json.dumps(document).encode()
                    start["headers"] = [
                        (key, value) for key, value in start["headers"] if key != b"content-length"
                    ] + [(b"content-length", str(len(body)).encode())]
                    await send(start)
                    await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, metadata_send)


class HouseholdAzureProvider(AzureProvider):
    """Validate the upstream identity on every broker-token use, failing closed."""

    def __init__(self, settings: Settings, engine: Engine):
        # Settings validates this complete production-only configuration first.
        assert settings.mcp_public_url is not None
        assert settings.mcp_client_secret is not None
        assert settings.mcp_signing_key is not None
        assert settings.mcp_encryption_key is not None
        self.household_group = str(settings.entra_allowed_group)
        self.household_tenant = str(settings.entra_tenant_id)
        self.household_audience = str(settings.entra_audience)
        self.household_scope = settings.entra_scope
        self.client_redirects = settings.mcp_allowed_redirect_uris
        self.storage = PostgresOAuthStore(engine, settings.mcp_encryption_key.get_secret_value())
        super().__init__(
            client_id=self.household_audience,
            tenant_id=self.household_tenant,
            client_secret=settings.mcp_client_secret.get_secret_value(),
            base_url=str(settings.mcp_public_url).rstrip("/"),
            # Keep issuer_url equal to base_url: FastMCP's root discovery routes
            # support path-aware issuers without inconsistent issuer metadata.
            required_scopes=[self.household_scope],
            additional_authorize_scopes=["openid", "profile", "email", "offline_access"],
            allowed_client_redirect_uris=settings.mcp_allowed_redirect_uris,
            client_storage=self.storage,
            jwt_signing_key=settings.mcp_signing_key.get_secret_value(),
            require_authorization_consent=True,
        )

    def _required_upstream_client_secret(self) -> str:
        # Azure is configured as a confidential client; fail closed if that
        # invariant changes rather than silently sending an unauthenticated request.
        if self._upstream_client_secret is None:
            raise ValueError("Upstream client secret is required")
        return self._upstream_client_secret.get_secret_value()

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # FastMCP otherwise defers this allowlist check until authorization and
        # persists arbitrary registrations first. Reject them at registration.
        if client_info.token_endpoint_auth_method not in (
            None,
            "none",
            "client_secret_post",
            "client_secret_basic",
        ):
            raise RegistrationError(
                "invalid_client_metadata", "Unsupported token authentication method"
            )
        if not client_info.redirect_uris or any(
            not validate_redirect_uri(uri, self.client_redirects)
            for uri in client_info.redirect_uris
        ):
            raise RegistrationError("invalid_redirect_uri", "Client redirect is not allowed")
        # The SDK proxy stores downstream clients as public PKCE clients. Return
        # that actual contract rather than advertising an unused generated secret.
        client_info.token_endpoint_auth_method = "none"
        client_info.client_secret = None
        client_info.client_secret_expires_at = None
        try:
            await super().register_client(client_info)
        except OAuthRegistrationLimitError as error:
            raise RegistrationError("invalid_client_metadata", str(error)) from None

    def get_routes(self, mcp_path=None):
        routes = super().get_routes(mcp_path)
        result = []
        for route in routes:
            if isinstance(route, Route) and route.path == "/.well-known/oauth-authorization-server":
                result.append(
                    Route(
                        route.path, endpoint=PublicClientMetadata(route.app), methods=route.methods
                    )
                )
            elif isinstance(route, Route) and route.path in {
                "/authorize",
                "/token",
                "/register",
                "/consent",
            }:
                # FastMCP replaces some SDK routes without its body limiter.
                # Keep CORS outermost so oversized browser requests receive
                # the same CORS headers, and leave actual MCP payloads alone.
                endpoint = route.app
                if isinstance(endpoint, CORSMiddleware):
                    endpoint.app = RequestBodyLimitMiddleware(endpoint.app, 64 * 1024)
                else:
                    endpoint = RequestBodyLimitMiddleware(endpoint, 64 * 1024)
                result.append(Route(route.path, endpoint=endpoint, methods=route.methods))
            else:
                result.append(route)
        return result

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = await super().get_client(client_id)
        if client is not None:
            # Apply the current allowlist to clients registered before a policy
            # change as well as new clients. Preserve the SDK's redirect checks.
            if isinstance(client, ProxyDCRClient):
                client.allowed_redirect_uri_patterns = self.client_redirects
        return client

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Reject refresh JWTs before the upstream token lookup.
        try:
            payload = self.jwt_issuer.verify_token(token)
            if payload.get("token_use") not in (None, "access"):
                return None
        except Exception:
            return None
        # The SDK validates the broker signature/audience and swaps the reference
        # for its stored Entra token, verifying its signature/issuer/aud/scopes.
        verified = await super().load_access_token(token)
        return self.household_identity(verified)

    async def authorize(self, client, params):
        # An omitted scope uses the advertised required scope. Keep explicit
        # requests intact so the normal client scope validation remains binding.
        if params.scopes is None:
            params = params.model_copy(update={"scopes": [self.household_scope]})
        return await super().authorize(client, params)

    async def _submit_consent(self, request: Request):
        # The SDK checks the form token against the transaction, but does not
        # bind it to the browser that viewed consent. An attacker can otherwise
        # copy their own form/token into an auto-submitting victim browser.
        public = urlsplit(str(self.base_url))
        expected_origin = f"{public.scheme}://{public.netloc}"
        form = await request.form()
        csrf = str(form.get("csrf_token", ""))
        cookie_name = self._cookie_name("MCP_CONSENT_STATE")
        browser_tokens = (
            self._decode_list_cookie(request, "MCP_CONSENT_STATE")
            if request.cookies.get(cookie_name)
            else []
        )
        if request.headers.get("origin") != expected_origin or not any(
            secrets.compare_digest(csrf.encode(), value.encode()) for value in browser_tokens
        ):
            return JSONResponse({"detail": "Invalid consent request"}, status_code=400)
        return await super()._submit_consent(request)

    def household_identity(self, verified) -> AccessToken | None:
        """Require complete signed upstream claims, including overage fail-closed."""
        if not isinstance(verified, AccessToken) or not isinstance(verified.claims, dict):
            return None
        claims = verified.claims
        groups = claims.get("groups")
        now = time.time()
        for name in ("iat", "exp"):
            value = claims.get(name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                return None
        nbf = claims.get("nbf", claims["iat"])
        if type(nbf) not in (int, float) or not math.isfinite(nbf):
            return None
        if (
            claims.get("tid") != self.household_tenant
            or claims.get("aud") != self.household_audience
            or claims.get("iss")
            != f"https://login.microsoftonline.com/{self.household_tenant}/v2.0"
            or not isinstance(claims.get("sub"), str)
            or not claims["sub"].strip()
            or not isinstance(groups, list)
            or self.household_group not in groups
            or not isinstance(claims.get("scp"), str)
            or self.household_scope not in claims["scp"].split()
            or claims["iat"] > now
            or nbf > now
            or claims["exp"] <= now
            or claims["exp"] <= claims["iat"]
        ):
            return None
        return verified

    async def _verified_upstream(self, token: str) -> AccessToken | None:
        return self.household_identity(await self._token_validator.verify_token(token))

    async def _handle_idp_callback(self, request: Request):
        """Exchange with a closed HTTP client and never log codes or IdP errors.

        Keep FastMCP's transaction/code models and PKCE protocol handler; replace
        its callback implementation which logs code-bearing redirects at DEBUG.
        """
        code, state = request.query_params.get("code"), request.query_params.get("state")
        if request.query_params.get("error") or not code or not state:
            return JSONResponse({"detail": "MCP sign-in failed"}, status_code=400)
        try:
            transaction = await self.storage.get(state, collection="mcp-oauth-transactions")
            if transaction is None or not transaction.get("proxy_code_verifier"):
                return JSONResponse({"detail": "MCP sign-in expired"}, status_code=400)
            # Preserve FastMCP 3's callback binding: only the browser that
            # approved consent may complete the upstream authorization.
            consent_token = transaction.get("consent_token")
            if not consent_token or not self._verify_consent_binding_cookie(
                request, state, consent_token
            ):
                return JSONResponse({"detail": "Invalid consent session"}, status_code=403)
            transaction = await self.storage.take(state, collection="mcp-oauth-transactions")
            if transaction is None:
                return JSONResponse({"detail": "MCP sign-in expired"}, status_code=400)
            # Recheck current redirect policy before delivering a code, including
            # for transactions started before an operator tightened the policy.
            if not validate_redirect_uri(transaction["client_redirect_uri"], self.client_redirects):
                return JSONResponse({"detail": "MCP client is not allowed"}, status_code=400)
            async with AsyncOAuth2Client(
                client_id=self._upstream_client_id,
                client_secret=self._required_upstream_client_secret(),
                token_endpoint_auth_method=self._token_endpoint_auth_method,
                timeout=30,
            ) as client:
                tokens = await client.fetch_token(
                    url=self._upstream_token_endpoint,
                    code=code,
                    redirect_uri=f"{str(self.base_url).rstrip('/')}{self._redirect_path}",
                    code_verifier=transaction["proxy_code_verifier"],
                )
            if await self._verified_upstream(tokens.get("access_token", "")) is None:
                return JSONResponse(
                    {"detail": "Household authentication required"}, status_code=403
                )
            client_code = secrets.token_urlsafe(32)
            now = time.time()
            await self._code_store.put(
                client_code,
                ClientCode(
                    code=client_code,
                    client_id=transaction["client_id"],
                    redirect_uri=transaction["client_redirect_uri"],
                    code_challenge=transaction["code_challenge"],
                    code_challenge_method="S256",
                    scopes=transaction["scopes"],
                    idp_tokens=tokens,
                    expires_at=now + 300,
                    created_at=now,
                ),
                ttl=300,
            )
            response = RedirectResponse(
                transaction["client_redirect_uri"]
                + "?"
                + urlencode(
                    {
                        "code": client_code,
                        "state": transaction["client_state"],
                    }
                ),
                status_code=302,
            )
            self._clear_consent_binding_cookie(request, response, state)
            return response
        except Exception:
            # Provider exception strings can contain credentials or code-bearing
            # request URLs. Never echo or log them, including chained tracebacks.
            return JSONResponse({"detail": "MCP sign-in could not be completed"}, status_code=400)

    async def load_authorization_code(self, client, authorization_code):
        # The inherited SDK logs missing/expired raw codes at DEBUG.
        code = await self._code_store.get(authorization_code)
        if code is None or code.expires_at <= time.time() or code.client_id != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            client_id=code.client_id,
            scopes=code.scopes,
            expires_at=code.expires_at,
            redirect_uri=AnyUrl(code.redirect_uri),
            redirect_uri_provided_explicitly=True,
            code_challenge=code.code_challenge or "",
            resource=str(self._resource_url),
        )

    async def exchange_authorization_code(self, client, authorization_code):
        # SDK token handler has checked PKCE. Consume atomically to prevent two
        # pods exchanging the same code concurrently after both validate PKCE.
        try:
            payload = await self.storage.take(
                authorization_code.code, collection="mcp-authorization-codes"
            )
            if payload is None:
                raise ValueError("Code already consumed")
            code = ClientCode.model_validate(payload)
            if code.client_id != client.client_id or code.expires_at <= time.time():
                raise ValueError("Code binding failed")
            return await self._issue_token_pair(client, code.scopes, code.idp_tokens)
        except Exception:
            raise TokenError("invalid_grant", "MCP authorization could not be completed") from None

    async def _issue_token_pair(
        self, client, scopes, tokens, prior: UpstreamTokenSet | None = None
    ):
        verified = await self._verified_upstream(tokens.get("access_token", ""))
        if verified is None or not client.client_id or self.household_scope not in scopes:
            raise ValueError("Invalid household token")
        now = time.time()
        expires_in = int(min(verified.claims["exp"] - now, int(tokens.get("expires_in", 3600))))
        if expires_in < 1:
            raise ValueError("Token expired")
        refresh_value = tokens.get("refresh_token") or (prior.refresh_token if prior else None)
        refresh_expiry = (
            now + int(tokens["refresh_expires_in"])
            if "refresh_expires_in" in tokens
            else prior.refresh_token_expires_at
            if prior
            else now + 30 * 24 * 3600
        )
        refresh_ttl = max(0, int((refresh_expiry or now) - now)) if refresh_value else 0
        upstream_id, access_jti = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        await self._upstream_token_store.put(
            upstream_id,
            UpstreamTokenSet(
                upstream_token_id=upstream_id,
                access_token=tokens["access_token"],
                refresh_token=refresh_value,
                refresh_token_expires_at=refresh_expiry,
                expires_at=now + expires_in,
                token_type="Bearer",
                scope=" ".join(scopes),
                client_id=client.client_id,
                created_at=now,
                raw_token_data=tokens,
            ),
            ttl=max(expires_in, refresh_ttl),
        )
        await self._jti_mapping_store.put(
            access_jti,
            JTIMapping(jti=access_jti, upstream_token_id=upstream_id, created_at=now),
            ttl=expires_in,
        )
        access = self.jwt_issuer.issue_access_token(
            client.client_id, scopes, access_jti, expires_in=expires_in
        )
        refresh = None
        if refresh_value and refresh_ttl > 0:
            refresh_jti = secrets.token_urlsafe(32)
            refresh = self.jwt_issuer.issue_refresh_token(
                client.client_id, scopes, refresh_jti, expires_in=refresh_ttl
            )
            await self._jti_mapping_store.put(
                refresh_jti,
                JTIMapping(jti=refresh_jti, upstream_token_id=upstream_id, created_at=now),
                ttl=refresh_ttl,
            )
            await self._refresh_token_store.put(
                sha256(refresh.encode()).hexdigest(),
                RefreshTokenMetadata(
                    client_id=client.client_id,
                    scopes=scopes,
                    expires_at=int(now) + refresh_ttl,
                    created_at=now,
                ),
                ttl=refresh_ttl,
            )
        # Pending registrations expire after one day, while an authenticated
        # client's record must remain available for every token it was issued.
        await self._client_store.put(
            client.client_id,
            client,
            ttl=max(self.storage.PENDING_CLIENT_TTL, expires_in, refresh_ttl),
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=expires_in,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes):
        try:
            payload = self.jwt_issuer.verify_token(
                refresh_token.token, expected_token_use="refresh"
            )
            if payload.get("token_use") != "refresh":
                raise ValueError("Invalid token type")
            if not set(scopes).issubset(refresh_token.scopes):
                raise ValueError("Scope escalation")
            mapping = await self._jti_mapping_store.get(payload["jti"])
            if mapping is None:
                raise ValueError("Refresh expired")
            upstream = await self._upstream_token_store.get(mapping.upstream_token_id)
            if (
                upstream is None
                or upstream.client_id != client.client_id
                or not upstream.refresh_token
            ):
                raise ValueError("Refresh binding failed")
            # Reserve before the network call; a failed exchange requires login
            # again rather than replaying a possibly consumed upstream refresh.
            metadata = await self.storage.take(
                sha256(refresh_token.token.encode()).hexdigest(), collection="mcp-refresh-tokens"
            )
            if metadata is None or metadata["client_id"] != client.client_id:
                raise ValueError("Refresh already consumed")
            await self._jti_mapping_store.delete(payload["jti"])
            async with AsyncOAuth2Client(
                client_id=self._upstream_client_id,
                client_secret=self._required_upstream_client_secret(),
                token_endpoint_auth_method=self._token_endpoint_auth_method,
                timeout=30,
            ) as session:
                tokens = await session.refresh_token(
                    url=self._upstream_token_endpoint,
                    refresh_token=upstream.refresh_token,
                    scope=" ".join(self._prepare_scopes_for_upstream_refresh(scopes)),
                )
            return await self._issue_token_pair(client, scopes, tokens, prior=upstream)
        except Exception:
            raise TokenError(
                "invalid_grant", "MCP refresh could not be completed; sign in again"
            ) from None


class MCPActorMiddleware(Middleware):
    """Carry the verified Entra subject into sync tools and durable history."""

    async def on_call_tool(self, context, call_next):
        # Pydantic AI uses the exact same FastMCP instance in-process for calls
        # made during an already authenticated REST request. ContextVars are
        # propagated by asyncio into that nested MCP session; accept that trusted
        # request identity without inventing a loopback token or weakening the
        # external MCP bearer-token check below.
        current = history_context.get()
        if current.actor_id and current.channel in {"app", "api"}:
            return await call_next(context)
        verified = get_access_token()
        if verified is None or not verified.claims.get("sub"):
            raise ValueError("Household authentication required")
        token = history_context.set(HistoryContext(actor_id=verified.claims["sub"], channel="mcp"))
        try:
            return await call_next(context)
        finally:
            history_context.reset(token)
