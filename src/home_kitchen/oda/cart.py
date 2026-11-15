"""Review-bound positive Oda cart deltas; remote writes are never retried."""

import asyncio
import json
import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

from fastmcp import Client
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictBool, StrictInt, field_validator
from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    Table,
    Text,
    create_engine,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from home_kitchen.context import history_context
from home_kitchen.history import HistoryEntryCreate, HistoryRepository

from .models import OdaProduct
from .repository import lifecycles
from .service import RESOURCE, OdaError, OdaService

metadata = MetaData()
HOUSEHOLD_CART_LOCK = int.from_bytes(
    sha256(b"home-kitchen:oda:household-cart").digest()[:8], "big", signed=True
)
reviews = Table(
    "kitchen_oda_cart_reviews",
    metadata,
    Column("id_hash", Text, primary_key=True),
    Column("actor_id", Text, nullable=False, index=True),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("encrypted_payload", Text, nullable=False),
)


class CartAddition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: StrictInt = Field(gt=0)
    quantity: StrictInt = Field(gt=0, le=99)


class CartProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: list[str] = Field(min_length=1, max_length=10)
    additions: list[CartAddition] = Field(min_length=1, max_length=10)


class CartConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewed: StrictBool

    @field_validator("reviewed")
    @classmethod
    def require_review(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Review the exact additions before confirmation")
        return value


def _fingerprint(cart: dict) -> str:
    return sha256(json.dumps(cart, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cart_view(cart: dict) -> tuple[dict, dict[int, Decimal]]:
    """Flatten documented get_cart groups without retaining delivery addresses."""
    quantities: dict[int, Decimal] = {}
    items = []
    try:
        url = HttpUrl(cart["url"])
        if url.scheme != "https" or not (
            url.host == "oda.com" or (url.host or "").endswith(".oda.com")
        ):
            raise ValueError
        for group in cart["groups"]:
            for item in group["items"]:
                product = OdaProduct.model_validate(item["product"])
                quantity = Decimal(str(item["quantity"]))
                if not quantity.is_finite() or quantity < 0:
                    raise ValueError
                quantities[product.id] = quantities.get(product.id, Decimal(0)) + quantity
                items.append(
                    {
                        "product": product.model_dump(mode="json", by_alias=True),
                        "quantity": str(quantity),
                        "totalGrossAmount": str(item["totalGrossAmount"]),
                    }
                )
        return {
            "items": items,
            "totalGrossAmount": str(cart["totalGrossAmount"]),
            "url": str(url),
        }, quantities
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise OdaError("Oda returned an unsupported cart response; review is required") from None


def _validate_tools(tools: list) -> None:
    available = {tool.name: tool for tool in tools}
    if not {"get_cart", "manipulate_cart"}.issubset(available):
        raise OdaError("Oda cart tools are unavailable")
    read = available["get_cart"]
    write = available["manipulate_cart"]
    if read.annotations is None or read.annotations.readOnlyHint is not True:
        raise OdaError("Oda cart read-only annotation changed")
    if (
        write.annotations is None
        or write.annotations.readOnlyHint is not False
        or write.annotations.destructiveHint is not True
    ):
        raise OdaError("Oda cart write annotations changed")
    schema = write.inputSchema
    operations = schema.get("properties", {}).get("operations", {})
    item = operations.get("items", {})
    properties = item.get("properties", {})
    product_schema = properties.get("productId", {})
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or set(schema.get("required", [])) != {"operations"}
        or operations.get("type") != "array"
        or item.get("type") != "object"
        or set(item.get("required", [])) != {"quantity"}
        or properties.get("quantity", {}).get("type") != "integer"
        or product_schema.get("anyOf") != [{"type": "integer"}, {"type": "null"}]
        or set(properties)
        != {
            "quantity",
            "productId",
            "recipeId",
            "productListId",
            "orderNumber",
            "fromRecipePortions",
        }
    ):
        raise OdaError("Oda cart operation schema changed; review is required")
    read_schema = read.inputSchema
    if (
        read_schema.get("type") != "object"
        or read_schema.get("additionalProperties") is not False
        or read_schema.get("required")
    ):
        raise OdaError("Oda cart read schema changed; review is required")


class OdaCartService:
    def __init__(
        self,
        oda: OdaService,
        history_repository: HistoryRepository | None = None,
        *,
        gate_factory: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ):
        self.oda = oda
        self.history_repository = history_repository
        self.gate_factory = gate_factory or self._household_gate

    def _acquire_household_gate(self) -> tuple[Connection, Engine]:
        source = self.oda._repository().engine
        if source.dialect.name != "postgresql":
            raise OdaError("Reviewed Oda writes require the PostgreSQL household gate", 503)
        # Waiting confirmations must not consume the application pool, otherwise
        # the lock holder could be starved of its token/generation connections.
        engine = create_engine(source.url, poolclass=NullPool, connect_args={"connect_timeout": 10})
        connection = None
        try:
            connection = engine.connect()
            connection.execute(text("SET statement_timeout = 45000"))
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": HOUSEHOLD_CART_LOCK})
            connection.commit()
            return connection, engine
        except BaseException:
            if connection is not None:
                connection.close()
            engine.dispose()
            raise

    @staticmethod
    def _release_household_gate(lease: tuple[Connection, Engine]) -> None:
        connection, engine = lease
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": HOUSEHOLD_CART_LOCK}
            )
            connection.commit()
        finally:
            # NullPool physically closes this session, also releasing the lock
            # if an unlock/connection error prevented the explicit release.
            connection.close()
            engine.dispose()

    @asynccontextmanager
    async def _household_gate(self) -> AsyncGenerator[None, None]:
        acquisition = asyncio.create_task(asyncio.to_thread(self._acquire_household_gate))
        try:
            lease = await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            # A cancelled HTTP request cannot strand a later-acquired session lock.
            try:
                lease = await acquisition
            except Exception:
                pass
            else:
                await asyncio.shield(asyncio.to_thread(self._release_household_gate, lease))
            raise
        try:
            yield
        finally:
            await asyncio.shield(asyncio.to_thread(self._release_household_gate, lease))

    def _record(self, connection, actor: str, proposal_id: str, payload: dict, status: str) -> None:
        history = self.history_repository or HistoryRepository(connection.engine)
        history.record(
            HistoryEntryCreate(
                channel=history_context.get().channel,
                kind="domain_event",
                message=f"Oda cart review {status}",
                structured={
                    "event": "oda_cart_review",
                    "proposal_hash": sha256(proposal_id.encode()).hexdigest(),
                    "status": status,
                    "additions": [
                        {"product_id": item["product"]["id"], "package_count": item["quantity"]}
                        for item in payload["additions"]
                    ],
                },
            ),
            actor_id=actor,
            connection=connection,
        )

    def _generation(self, connection, actor: str) -> int:
        generation = connection.execute(
            select(lifecycles.c.generation).where(lifecycles.c.actor_id == actor)
        ).scalar_one_or_none()
        if generation is None:
            raise OdaError("Connect your Oda account first", 409)
        return generation

    async def propose(self, actor: str, request: CartProposalRequest) -> dict:
        repository = self.oda._repository()
        with repository.engine.connect() as connection:
            generation = self._generation(connection, actor)
        if len({item.product_id for item in request.additions}) != len(request.additions):
            raise OdaError("Choose each Oda product once per review", 400)
        searched = await self.oda.search_products(actor, request.queries, size=20)
        products = {
            product["id"]: product for group in searched["result"] for product in group["products"]
        }
        cart = await self.oda.get_cart(actor)
        displayed, quantities = _cart_view(cart)
        additions = []
        for selected in request.additions:
            product = products.get(selected.product_id)
            if product is None:
                raise OdaError("Selected product was not found in the fresh Oda search", 409)
            if (
                not product.get("availability")
                or product["availability"].get("isAvailable") is not True
            ):
                raise OdaError(
                    "Selected Oda product is unavailable or availability is unknown", 409
                )
            current = quantities.get(selected.product_id, Decimal(0))
            additions.append(
                {
                    "product": product,
                    "quantity": selected.quantity,
                    "current_quantity": str(current),
                    "after_quantity": str(current + selected.quantity),
                }
            )
        proposal_id = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(minutes=5)
        payload = {
            "generation": generation,
            "cart_fingerprint": _fingerprint(cart),
            "current_cart": displayed,
            "additions": additions,
            "warnings": [
                "Package-to-shopping-need quantity mapping is unverified; review each quantity.",
                "Only the positive additions shown will be sent. Checkout and payment happen at Oda.",
                "Prices and availability can change; verify them again at Oda before payment.",
            ],
        }
        await asyncio.to_thread(self._save, actor, proposal_id, expires, payload)
        return self.get(actor, proposal_id)

    def _save(self, actor: str, proposal_id: str, expires: datetime, payload: dict) -> None:
        repository = self.oda._repository()
        with repository.engine.begin() as connection:
            if not repository._accept_generation(connection, actor, payload["generation"]):
                raise OdaError("Oda connection changed; create a fresh cart review", 409)
            connection.execute(
                insert(reviews).values(
                    id_hash=sha256(proposal_id.encode()).hexdigest(),
                    actor_id=actor,
                    expires_at=expires,
                    status="pending",
                    encrypted_payload=repository.encrypt(payload),
                )
            )

            self._record(connection, actor, proposal_id, payload, "proposed")

    def get(self, actor: str, proposal_id: str) -> dict:
        repository = self.oda._repository()
        with repository.engine.connect() as connection:
            row = (
                connection.execute(
                    select(reviews).where(
                        reviews.c.id_hash == sha256(proposal_id.encode()).hexdigest(),
                        reviews.c.actor_id == actor,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise OdaError("Oda cart review was not found", 404)
        payload = repository.decrypt(row["encrypted_payload"])
        return {
            "proposal_id": proposal_id,
            "expires_at": row["expires_at"].isoformat(),
            "status": row["status"],
            "current_cart": payload["current_cart"],
            "additions": payload["additions"],
            "warnings": payload["warnings"],
            "detail": payload.get("detail"),
        }

    def _claim(self, actor: str, proposal_id: str) -> dict | None:
        repository = self.oda._repository()
        with repository.engine.begin() as connection:
            # Serialize claims per actor. An interrupted attempted write remains
            # blocked for manual investigation; it is never silently retried.
            connection.execute(
                select(lifecycles.c.actor_id)
                .where(lifecycles.c.actor_id == actor)
                .with_for_update()
            ).first()
            row = (
                connection.execute(
                    select(reviews)
                    .where(
                        reviews.c.id_hash == sha256(proposal_id.encode()).hexdigest(),
                        reviews.c.actor_id == actor,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                raise OdaError("Oda cart review was not found", 404)
            if row["status"] != "pending":
                return None
            expires = (
                row["expires_at"].replace(tzinfo=UTC)
                if row["expires_at"].tzinfo is None
                else row["expires_at"]
            )
            if expires <= datetime.now(UTC):
                raise OdaError("Oda cart review expired; create a fresh review", 409)
            active = connection.execute(
                select(reviews.c.id_hash).where(
                    reviews.c.actor_id == actor,
                    reviews.c.status == "attempted",
                )
            ).first()
            if active is not None:
                raise OdaError(
                    "An earlier Oda cart update is unresolved; inspect it before continuing", 409
                )
            payload = repository.decrypt(row["encrypted_payload"])
            if payload["generation"] != self._generation(connection, actor):
                raise OdaError("Oda connection changed; create a fresh cart review", 409)
            connection.execute(
                update(reviews)
                .where(reviews.c.id_hash == row["id_hash"])
                .values(status="attempted")
            )
            self._record(connection, actor, proposal_id, payload, "attempted")
            return payload

    def _outcome(
        self, actor: str, proposal_id: str, status: str, payload: dict, detail: str
    ) -> None:
        repository = self.oda._repository()
        payload["detail"] = detail
        with repository.engine.begin() as connection:
            connection.execute(
                update(reviews)
                .where(
                    reviews.c.id_hash == sha256(proposal_id.encode()).hexdigest(),
                    reviews.c.actor_id == actor,
                    reviews.c.status == "attempted",
                )
                .values(status=status, encrypted_payload=repository.encrypt(payload))
            )

            self._record(connection, actor, proposal_id, payload, status)

    async def confirm(self, actor: str, proposal_id: str, *, reviewed: bool) -> dict:
        if reviewed is not True:
            raise OdaError("Review the exact Oda additions before confirming", 400)
        payload = await asyncio.to_thread(self._claim, actor, proposal_id)
        if payload is None:
            return self.get(actor, proposal_id)
        write_started = False
        status, detail = "error", "No cart update was sent; create a fresh review."
        try:
            token = await asyncio.to_thread(self.oda.access_token, actor)
            async with self.gate_factory():
                async with Client(RESOURCE, auth=token, timeout=20) as client:
                    _validate_tools(await client.list_tools())
                    current = await client.call_tool("get_cart", {}, raise_on_error=True)
                    cart = current.structured_content
                    if (
                        cart is None
                        and len(current.content) == 1
                        and current.content[0].type == "text"
                    ):
                        cart = json.loads(current.content[0].text)
                    if not isinstance(cart, dict):
                        raise OdaError("Oda returned an unsupported cart response")
                    if _fingerprint(cart) != payload["cart_fingerprint"]:
                        status, detail = "stale", "Your Oda cart changed; create a fresh review."
                    else:
                        repository = self.oda._repository()
                        with repository.engine.connect() as connection:
                            if self._generation(connection, actor) != payload["generation"]:
                                raise OdaError("Oda connection changed; create a fresh review", 409)
                        operations = [
                            {"productId": item["product"]["id"], "quantity": item["quantity"]}
                            for item in payload["additions"]
                        ]
                        write_started = True
                        applied = await client.call_tool(
                            "manipulate_cart", {"operations": operations}, raise_on_error=True
                        )
                        result_cart = applied.structured_content
                        if (
                            result_cart is None
                            and len(applied.content) == 1
                            and applied.content[0].type == "text"
                        ):
                            result_cart = json.loads(applied.content[0].text)
                        if not isinstance(result_cart, dict):
                            raise OdaError("Oda did not return a verifiable updated cart")
                        _, after_quantities = _cart_view(result_cart)
                        if result_cart.get("validationMessages") or any(
                            after_quantities.get(item["product"]["id"], Decimal(0))
                            != Decimal(item["after_quantity"])
                            for item in payload["additions"]
                        ):
                            raise OdaError(
                                "Oda returned cart validation issues or unexpected quantities"
                            )
                        status, detail = (
                            "success",
                            "Reviewed additions appear in the returned Oda cart. Review and pay at Oda.",
                        )
        except Exception:
            if write_started:
                status, detail = (
                    "uncertain",
                    "Oda may have applied the additions. Inspect your cart and create a fresh review; do not retry this update.",
                )
        await asyncio.to_thread(self._outcome, actor, proposal_id, status, payload, detail)
        return self.get(actor, proposal_id)
