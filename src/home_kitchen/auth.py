"""One access boundary for REST, static files and mounted MCP."""

from ipaddress import ip_address, ip_network

import jwt
from jwt import PyJWKClient
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from home_kitchen.context import HistoryContext, history_context
from home_kitchen.settings import Settings


class AccessControl:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.issuer = f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0"
        self.jwks = PyJWKClient(
            f"https://login.microsoftonline.com/{settings.entra_tenant_id}/discovery/v2.0/keys",
            timeout=5,
        )
        self.proxy_networks = [ip_network(cidr) for cidr in settings.trusted_proxy_cidrs]

    def decode(self, token: str) -> dict | None:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.settings.entra_audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub", "aud", "iss", "tid"]},
            )
            valid = (
                claims["tid"] == str(self.settings.entra_tenant_id)
                and self.settings.entra_scope in claims.get("scp", "").split()
                and str(self.settings.entra_allowed_group) in claims.get("groups", [])
            )
            return claims if valid else None
        except (jwt.PyJWTError, ValueError, TypeError):
            return None

    def verify(self, token: str) -> bool:
        return self.decode(token) is not None

    async def accepted(self, request: Request, call_next, actor_id: str):
        channel = (
            "mcp"
            if request.url.path.startswith("/mcp")
            else ("app" if request.headers.get("sec-fetch-site") == "same-origin" else "api")
        )
        request.state.actor_id = actor_id
        token = history_context.set(HistoryContext(actor_id=actor_id, channel=channel))
        try:
            response = await call_next(request)
            if request.url.path.startswith("/api"):
                response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            history_context.reset(token)

    async def middleware(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        try:
            peer = ip_address(request.client.host) if request.client else None
        except ValueError:
            peer = None
        if self.settings.local_dev:
            # Host headers and X-Forwarded-For do not establish local trust.
            if peer and peer.is_loopback:
                return await self.accepted(request, call_next, "local-development")
        else:
            authorization = request.headers.get("authorization", "")
            if authorization.startswith("Bearer "):
                claims = await run_in_threadpool(self.decode, authorization.removeprefix("Bearer "))
                if claims is not None:
                    return await self.accepted(request, call_next, str(claims["sub"]))
            # Browser session is enforced by the existing Entra proxy. MCP always
            # requires its own bearer token; it cannot borrow a browser identity.
            if (
                not request.url.path.startswith("/mcp")
                and peer
                and any(peer in network for network in self.proxy_networks)
                and request.headers.get("x-auth-request-user")
                and str(self.settings.entra_allowed_group)
                in request.headers.get("x-auth-request-groups", "").split(",")
                and request.method in {"GET", "HEAD", "OPTIONS"}
            ):
                return await self.accepted(
                    request, call_next, request.headers["x-auth-request-user"]
                )
            # Browser mutations must also be same-origin to prevent cookie CSRF.
            if (
                not request.url.path.startswith("/mcp")
                and peer
                and any(peer in network for network in self.proxy_networks)
                and request.headers.get("x-auth-request-user")
                and str(self.settings.entra_allowed_group)
                in request.headers.get("x-auth-request-groups", "").split(",")
                and request.headers.get("origin") == f"https://{request.url.hostname}"
            ):
                return await self.accepted(
                    request, call_next, request.headers["x-auth-request-user"]
                )
        return JSONResponse(
            {"detail": "Household authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="kitchen"'},
        )
