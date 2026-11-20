"""Direct Oda OAuth with PKCE and a deliberately read-only MCP adapter."""

import asyncio
import json
import secrets
import time
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlencode

import httpx
from cryptography.fernet import InvalidToken
from fastmcp import Client
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from sqlalchemy.engine import Engine

from home_kitchen.crypto import build_multi_fernet
from home_kitchen.settings import Settings

from .models import OdaSearchResponse
from .purchase_history import OrdersResponse, TrackingResponse, validate_history_tool
from .repository import OdaRepository

RESOURCE = "https://oda.com/mcp"
ISSUER = "https://oda.com/o"
AUTHORIZE = ISSUER + "/authorize/"
TOKEN = ISSUER + "/token/"
REGISTER = ISSUER + "/register/"
REVOKE = ISSUER + "/revoke_token/"
PRM = "https://oda.com/.well-known/oauth-protected-resource/mcp"
AS_METADATA = "https://oda.com/.well-known/oauth-authorization-server"


class OdaError(Exception):
    """Only fixed, non-sensitive messages may cross the REST boundary."""

    def __init__(self, detail: str, status_code: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class OdaRecipeSearchEntry(BaseModel):
    """The documented Oda recipe_search result; public links stay untrusted strings."""

    model_config = ConfigDict(extra="ignore")

    id: StrictInt
    title: StrictStr
    like_count: StrictInt = Field(alias="likeCount")
    url: StrictStr | None = None
    provider_name: StrictStr | None = Field(default=None, alias="providerName")
    difficulty: StrictStr | None = Field(default=None, alias="difficultyString")
    duration: StrictStr | None = Field(default=None, alias="cookingDurationIso8601")
    is_liked: StrictBool | None = Field(default=None, alias="isLikedByUser")
    image_url: StrictStr | None = Field(default=None, alias="imageUrl")
    deliveries_count: StrictInt | None = Field(default=None, alias="deliveriesCount")


class OdaRecipeSearchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    has_more: StrictBool = Field(alias="hasMore")
    recipes: list[OdaRecipeSearchEntry] = Field(max_length=20)


class OdaRecipeCollectionEntry(BaseModel):
    """A recipe returned by an Oda saved or purchased collection."""

    model_config = ConfigDict(extra="ignore")

    id: StrictInt
    title: StrictStr
    url: StrictStr | None = None
    provider_name: StrictStr | None = Field(default=None, alias="providerName")
    image_url: StrictStr | None = Field(default=None, alias="imageUrl")
    deliveries_count: StrictInt | None = Field(default=None, alias="deliveriesCount")


class OdaRecipeCollectionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    has_more: StrictBool = Field(alias="hasMore")
    recipes: list[OdaRecipeCollectionEntry] = Field(max_length=20)


class OdaService:
    def __init__(
        self, engine: Engine, settings: Settings, *, http_client: httpx.Client | None = None
    ):
        self.settings = settings
        self.http = http_client or httpx.Client(timeout=20, follow_redirects=False)
        self._owns_http = http_client is None
        self.repository = (
            OdaRepository(
                engine, build_multi_fernet(settings.oda_encryption_key.get_secret_value())
            )
            if settings.oda_encryption_key is not None
            else None
        )

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    @property
    def configured(self) -> bool:
        return self.repository is not None and self.settings.public_url is not None

    def _repository(self) -> OdaRepository:
        if not self.configured or self.repository is None:
            raise OdaError("Oda connection is not configured", 503)
        return self.repository

    @property
    def redirect_uri(self) -> str:
        return str(self.settings.public_url).rstrip("/") + "/api/oda/callback"

    def search_cache_context(self, actor_id: str) -> int | None:
        """Fence cached search results across disconnects and new connections."""
        if not self.configured or self.repository is None:
            return None
        return self.repository.connection_generation(actor_id)

    def status(self, actor_id: str) -> dict:
        connected = (
            self.repository.has_connection(actor_id)
            if self.configured and self.repository is not None
            else False
        )
        return {
            "configured": self.configured,
            "connected": connected,
            "detail": "Connected"
            if connected
            else "Not connected"
            if self.configured
            else (
                "Configure the public URL (HTTP loopback is allowed in local development) "
                "and Oda encryption key to connect"
                if self.settings.local_dev
                else "Configure the public HTTPS URL and Oda encryption key to connect"
            ),
        }

    def _json(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = self.http.request(method, url, follow_redirects=False, **kwargs)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (httpx.HTTPError, ValueError):
            raise OdaError("Oda could not complete this request; try again or reconnect") from None

    def _discover(self) -> None:
        resource = self._json("GET", PRM)
        server = self._json("GET", AS_METADATA)
        if (
            resource.get("resource") != RESOURCE
            or ISSUER not in resource.get("authorization_servers", [])
            or server.get("issuer") != ISSUER
            or server.get("authorization_endpoint") != AUTHORIZE
            or server.get("token_endpoint") != TOKEN
            or server.get("registration_endpoint") != REGISTER
            or server.get("revocation_endpoint") != REVOKE
            or "S256" not in server.get("code_challenge_methods_supported", [])
            or "none" not in server.get("token_endpoint_auth_methods_supported", [])
        ):
            raise OdaError("Oda OAuth configuration changed; connection requires review")

    def connect(self, actor_id: str) -> str:
        repository = self._repository()
        generation = repository.begin_connect(actor_id)
        self._discover()
        registered = self._json(
            "POST",
            REGISTER,
            json={
                "client_name": "Home Kitchen",
                "redirect_uris": [self.redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": "mcp",
            },
        )
        client_id = registered.get("client_id")
        if (
            not isinstance(client_id, str)
            or not 1 <= len(client_id) <= 1024
            or registered.get("token_endpoint_auth_method", "none") != "none"
        ):
            raise OdaError("Oda did not register a supported public OAuth client")
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        accepted = repository.save_state(
            state,
            actor_id,
            datetime.now(UTC) + timedelta(minutes=10),
            {
                "client_id": client_id,
                "verifier": verifier,
                "redirect_uri": self.redirect_uri,
                "generation": generation,
            },
            generation,
        )
        if not accepted:
            raise OdaError("Oda authorization was cancelled; connect again", 409)
        challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return (
            AUTHORIZE
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": self.redirect_uri,
                    "scope": "mcp",
                    "resource": RESOURCE,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    @staticmethod
    def _token_payload(response: dict, previous: dict) -> dict:
        token = response.get("access_token")
        refresh = response.get("refresh_token", previous.get("refresh_token"))
        try:
            expires = int(response.get("expires_in", 300))
        except (TypeError, ValueError):
            expires = 0
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 32768
            or str(response.get("token_type", "")).casefold() != "bearer"
            or not 0 < expires <= 31536000
            or (refresh is not None and (not isinstance(refresh, str) or not refresh))
            or not isinstance(response.get("scope", "mcp"), str)
            or "mcp" not in response.get("scope", "mcp").split()
        ):
            raise OdaError("Oda returned an unsupported OAuth token response")
        return {
            "client_id": previous["client_id"],
            "access_token": token,
            "refresh_token": refresh,
            "expires_at": time.time() + expires,
        }

    def callback(self, actor_id: str, state: str, code: str) -> None:
        repository = self._repository()
        if not 20 <= len(state) <= 256 or not 1 <= len(code) <= 4096:
            raise OdaError("Invalid or expired Oda authorization; connect again", 400)
        try:
            pending = repository.consume_state(state, actor_id, datetime.now(UTC))
        except (InvalidToken, ValueError):
            raise OdaError("Oda authorization cannot be read; connect again", 400) from None
        if pending is None or pending["redirect_uri"] != self.redirect_uri:
            raise OdaError("Invalid or expired Oda authorization; connect again", 400)
        response = self._json(
            "POST",
            TOKEN,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": pending["client_id"],
                "code_verifier": pending["verifier"],
                "redirect_uri": pending["redirect_uri"],
                "resource": RESOURCE,
            },
        )
        token = self._token_payload(response, pending)
        if not repository.save_connection(actor_id, token, pending["generation"]):
            self._revoke_tokens(token)
            raise OdaError("Oda authorization was cancelled; connect again", 409)

    def cancel_authorization(self, actor_id: str, state: str) -> None:
        try:
            self._repository().consume_state(state, actor_id, datetime.now(UTC))
        except (InvalidToken, ValueError):
            raise OdaError("Oda authorization cannot be read; connect again", 400) from None

    def access_token(self, actor_id: str, *, force_refresh: bool = False) -> str:
        repository = self._repository()
        try:
            with repository.locked_connection(actor_id) as (connection, token):
                if token is None:
                    raise OdaError("Connect your Oda account first", 409)
                if force_refresh or token["expires_at"] <= time.time() + 30:
                    if not token.get("refresh_token"):
                        raise OdaError("Your Oda connection expired; connect again", 409)
                    response = self._json(
                        "POST",
                        TOKEN,
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": token["refresh_token"],
                            "client_id": token["client_id"],
                            "resource": RESOURCE,
                        },
                    )
                    token = self._token_payload(response, token)
                    repository.update_connection(connection, actor_id, token)
                return token["access_token"]
        except (InvalidToken, ValueError, KeyError):
            raise OdaError("Your Oda connection cannot be read; connect again", 409) from None

    def _revoke_tokens(self, token: dict) -> bool:
        failed = False
        for key in ("refresh_token", "access_token"):
            if token.get(key):
                try:
                    response = self.http.post(
                        REVOKE,
                        data={
                            "token": token[key],
                            "token_type_hint": key,
                            "client_id": token["client_id"],
                        },
                        follow_redirects=False,
                    )
                    response.raise_for_status()
                except httpx.HTTPError:
                    failed = True
        return not failed

    def disconnect(self, actor_id: str) -> None:
        repository = self._repository()
        try:
            token = repository.disconnect(actor_id)
        except (InvalidToken, ValueError, KeyError):
            raise OdaError(
                "Disconnected locally; stored OAuth credentials could not be revoked"
            ) from None
        if token is not None and not self._revoke_tokens(token):
            raise OdaError("Disconnected locally; Oda could not confirm remote token revocation")

    async def _read(self, actor_id: str, name: str, arguments: dict) -> dict:
        if name not in {
            "product_search",
            "get_cart",
            "recipe_search",
            "get_liked_recipes",
            "get_purchased_recipes",
            "get_orders",
            "order_tracking",
        }:
            raise OdaError("This Oda action is not supported", 400)
        token = await asyncio.to_thread(self.access_token, actor_id)
        for attempt in range(2):
            try:
                async with Client(RESOURCE, auth=token, timeout=20) as client:
                    tools = {tool.name: tool for tool in await client.list_tools()}
                    if name not in tools:
                        raise OdaError("The Oda read-only tool is unavailable")
                    if (
                        tools[name].annotations is None
                        or tools[name].annotations.readOnlyHint is not True
                    ):
                        raise OdaError("The Oda tool is no longer advertised as read-only")
                    schema = tools[name].inputSchema
                    properties = schema.get("properties", {})
                    if (
                        schema.get("type") != "object"
                        or schema.get("additionalProperties") is not False
                    ):
                        raise OdaError("The Oda tool schema changed; review is required")
                    if name in {"get_orders", "order_tracking"}:
                        try:
                            validate_history_tool(name, schema, tools[name].outputSchema)
                        except (ValueError, TypeError, KeyError, AttributeError):
                            raise OdaError(
                                "The Oda order schema changed; review is required"
                            ) from None
                    if name == "product_search" and (
                        set(schema.get("required", [])) != {"queries"}
                        or properties.get("queries", {}).get("type") != "array"
                        or properties.get("queries", {}).get("items", {}).get("type") != "string"
                        or properties.get("queries", {}).get("maxItems") != 10
                    ):
                        raise OdaError("The Oda search schema changed; review is required")
                    if name == "get_cart" and schema.get("required"):
                        raise OdaError("The Oda cart schema changed; review is required")
                    if name in {"recipe_search", "get_liked_recipes", "get_purchased_recipes"}:
                        query = properties.get("query", {})
                        page = properties.get("page", {})
                        size = properties.get("size", {})
                        output = tools[name].outputSchema
                        output_properties = output.get("properties", {})
                        recipes = output_properties.get("recipes", {})
                        recipe_properties = recipes.get("items", {}).get("properties", {})
                        recipe_required = set(recipes.get("items", {}).get("required", []))
                        collection_tool = name in {"get_liked_recipes", "get_purchased_recipes"}
                        expected_required = set() if collection_tool else {"query"}

                        def has_types(
                            field: str, expected: set[str], props: dict = recipe_properties
                        ) -> bool:
                            field_schema = props.get(field, {})
                            types = field_schema.get("type", [])
                            if isinstance(types, str):
                                types = [types]
                            if isinstance(types, list) and expected.issubset(set(types)):
                                return True
                            alternatives = field_schema.get("anyOf", [])
                            return expected.issubset(
                                {
                                    item.get("type")
                                    for item in alternatives
                                    if isinstance(item, dict)
                                }
                            )

                        if (
                            set(schema.get("required", [])) != expected_required
                            or (
                                collection_tool
                                and not {"page", "size"}
                                <= set(properties)
                                <= {"page", "size", "intent"}
                            )
                            or (not collection_tool and query.get("type") != "string")
                            or page.get("type") != "integer"
                            or (page.get("minimum"), page.get("maximum")) != (1, 50)
                            or size.get("type") != "integer"
                            or (size.get("minimum"), size.get("maximum")) != (1, 20)
                            or output.get("type") != "object"
                            or set(output.get("required", [])) != {"hasMore", "recipes"}
                            or output_properties.get("hasMore", {}).get("type") != "boolean"
                            or recipes.get("type") != "array"
                            or not {"id", "title"}.issubset(recipe_required)
                            or recipe_properties.get("id", {}).get("type") != "integer"
                            or recipe_properties.get("title", {}).get("type") != "string"
                            or (
                                not collection_tool
                                and (
                                    "likeCount" not in recipe_required
                                    or recipe_properties.get("likeCount", {}).get("type")
                                    != "integer"
                                )
                            )
                            or (
                                collection_tool
                                and any(
                                    not has_types(field, expected_types)
                                    for field, expected_types in {
                                        "url": {"string", "null"},
                                        "providerName": {"string", "null"},
                                        "imageUrl": {"string", "null"},
                                        "deliveriesCount": {"integer"},
                                    }.items()
                                )
                            )
                        ):
                            raise OdaError("The Oda recipe schema changed; review is required")
                    result = await client.call_tool(name, arguments, raise_on_error=True)
                    if result.structured_content is not None:
                        return result.structured_content
                    if len(result.content) == 1 and result.content[0].type == "text":
                        decoded = json.loads(result.content[0].text)
                        if isinstance(decoded, dict):
                            return decoded
                    raise OdaError("Oda returned an unsupported read-only response")
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 401 and attempt == 0:
                    try:
                        token = await asyncio.to_thread(
                            self.access_token, actor_id, force_refresh=True
                        )
                        continue
                    except OdaError:
                        raise
                    except Exception:
                        raise OdaError("Your Oda connection expired; connect again", 409) from None
                if error.response.status_code == 401:
                    raise OdaError("Your Oda connection expired; connect again", 409) from None
                raise OdaError("Oda could not complete the read-only request") from None
            except OdaError:
                raise
            except Exception as error:
                if "401" in str(error) and attempt == 0:
                    try:
                        token = await asyncio.to_thread(
                            self.access_token, actor_id, force_refresh=True
                        )
                        continue
                    except OdaError:
                        raise
                    except Exception:
                        raise OdaError("Your Oda connection expired; connect again", 409) from None
                if "401" in str(error):
                    raise OdaError("Your Oda connection expired; connect again", 409) from None
                raise OdaError("Oda could not complete the read-only request") from None
        raise OdaError("Oda could not complete the read-only request")

    async def search_products(
        self, actor_id: str, queries: list[str], page: int = 1, size: int = 5
    ) -> dict:
        if not 1 <= len(queries) <= 10 or any(
            not query.strip() or len(query) > 300 for query in queries
        ):
            raise OdaError("Provide between one and ten product queries", 400)
        if not 1 <= page <= 50 or not 1 <= size <= 20:
            raise OdaError("Oda search pagination is outside the supported range", 400)
        payload = await self._read(
            actor_id,
            "product_search",
            {
                "queries": [query.strip() for query in queries],
                "page": page,
                "size": size,
            },
        )
        try:
            return OdaSearchResponse.model_validate(payload).model_dump(mode="json", by_alias=True)
        except ValueError:
            raise OdaError("The Oda product response changed; review is required") from None

    async def get_cart(self, actor_id: str) -> dict:
        return await self._read(actor_id, "get_cart", {})

    async def search_recipes(
        self, actor_id: str, query: str, page: int = 1, size: int = 10
    ) -> dict:
        if not query.strip() or len(query) > 200:
            raise OdaError("Recipe search query must contain 1 to 200 characters", 400)
        if not 1 <= page <= 50 or not 1 <= size <= 20:
            raise OdaError("Oda recipe pagination is outside the supported range", 400)
        payload = await self._read(
            actor_id,
            "recipe_search",
            {"query": query.strip(), "page": page, "size": size},
        )
        try:
            return OdaRecipeSearchResponse.model_validate(payload).model_dump(
                mode="json", by_alias=True
            )
        except ValueError:
            raise OdaError("The Oda recipe response changed; review is required") from None

    async def list_liked_recipes(self, actor_id: str, page: int = 1, size: int = 10) -> dict:
        return await self._list_recipe_collection(actor_id, "get_liked_recipes", page, size)

    async def list_purchased_recipes(self, actor_id: str, page: int = 1, size: int = 10) -> dict:
        return await self._list_recipe_collection(actor_id, "get_purchased_recipes", page, size)

    async def _list_recipe_collection(
        self, actor_id: str, tool_name: str, page: int, size: int
    ) -> dict:
        if not 1 <= page <= 50 or not 1 <= size <= 20:
            raise OdaError("Oda recipe pagination is outside the supported range", 400)
        payload = await self._read(actor_id, tool_name, {"page": page, "size": size})
        try:
            return OdaRecipeCollectionResponse.model_validate(payload).model_dump(
                mode="json", by_alias=True
            )
        except ValueError:
            raise OdaError(
                "The Oda recipe collection response changed; review is required"
            ) from None

    async def list_orders(self, actor_id: str, page: int = 1, size: int = 20) -> dict:
        """Read provider summaries; presence and dates do not establish payment."""
        if (
            type(page) is not int
            or type(size) is not int
            or not 1 <= page <= 50
            or not 1 <= size <= 20
        ):
            raise OdaError("Oda order pagination is outside the supported range", 400)
        payload = await self._read(actor_id, "get_orders", {"page": page, "size": size})
        try:
            OrdersResponse.model_validate(payload)
            return payload
        except ValueError:
            raise OdaError("The Oda order response changed; review is required") from None

    async def track_order(self, actor_id: str, order_number: str) -> dict:
        """Read the exact lifecycle status, without asserting purchase completion."""
        if not order_number.strip() or len(order_number) > 512:
            raise OdaError("Provide a supported Oda order number", 400)
        payload = await self._read(actor_id, "order_tracking", {"order_number": order_number})
        try:
            tracking = TrackingResponse.model_validate(payload)
            if tracking.orderNumber != order_number:
                raise ValueError
            return payload
        except ValueError:
            raise OdaError("The Oda tracking response changed; review is required") from None
