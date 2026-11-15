"""Actor-scoped Oda connection routes; OAuth secrets are never returned to the UI."""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.responses import RedirectResponse

from home_kitchen.oda import OdaError, OdaService
from home_kitchen.oda.cart import CartConfirmation, CartProposalRequest, OdaCartService
from home_kitchen.oda.media import ProductMedia, fetch_product_media


class ProductSearch(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=10)
    page: int = Field(default=1, ge=1, le=50)
    size: int = Field(default=5, ge=1, le=20)


class ProductMediaRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


def oda_router(service: OdaService, cart_service: OdaCartService | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/oda")
    cart_updates = cart_service or OdaCartService(service)
    private_headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

    def actor(request: Request) -> str:
        identity = getattr(request.state, "actor_id", None)
        if not isinstance(identity, str) or not identity:
            raise HTTPException(401, "Household authentication required", headers=private_headers)
        return identity

    def failure(error: OdaError) -> HTTPException:
        return HTTPException(error.status_code, error.detail, headers=private_headers)

    @router.get("/status")
    def status(request: Request) -> dict:
        return service.status(actor(request))

    @router.post("/connect")
    def connect(request: Request) -> dict:
        try:
            return {"authorization_url": service.connect(actor(request))}
        except OdaError as error:
            raise failure(error) from None

    @router.get("/callback")
    def callback(request: Request) -> Response:
        identity = actor(request)
        # Parse manually: framework validation responses echo rejected query
        # inputs, which must never include an OAuth authorization code.
        state = request.query_params.get("state", "")
        code = request.query_params.get("code")
        error = request.query_params.get("error")
        if error is not None or code is None:
            # Consume a denied authorization too, without exposing upstream text.
            try:
                service.cancel_authorization(identity, state)
            except OdaError:
                pass
            return RedirectResponse("/?oda=failed", status_code=303, headers=private_headers)
        try:
            service.callback(identity, state, code)
        except OdaError:
            return RedirectResponse("/?oda=failed", status_code=303, headers=private_headers)
        return RedirectResponse(
            "/?oda=connected",
            status_code=303,
            headers=private_headers,
        )

    @router.delete("/connection", status_code=204)
    def disconnect(request: Request) -> Response:
        try:
            service.disconnect(actor(request))
        except OdaError as error:
            raise failure(error) from None
        return Response(status_code=204)

    @router.post("/products/search")
    async def search(data: ProductSearch, request: Request) -> dict:
        try:
            return await service.search_products(actor(request), data.queries, data.page, data.size)
        except OdaError as error:
            raise failure(error) from None

    @router.get("/cart")
    async def cart(request: Request) -> dict:
        try:
            return await service.get_cart(actor(request))
        except OdaError as error:
            raise failure(error) from None

    @router.post("/products/media")
    async def product_media(data: ProductMediaRequest, request: Request) -> ProductMedia:
        actor(request)
        try:
            return await fetch_product_media(data.url)
        except ValueError:
            raise HTTPException(422, "Use the exact public Oda product page URL") from None

    @router.post("/cart/proposals")
    async def propose_cart(data: CartProposalRequest, request: Request) -> dict:
        try:
            return await cart_updates.propose(actor(request), data)
        except OdaError as error:
            raise failure(error) from None

    @router.get("/cart/proposals/{proposal_id}")
    def cart_proposal(proposal_id: str, request: Request) -> dict:
        try:
            return cart_updates.get(actor(request), proposal_id)
        except OdaError as error:
            raise failure(error) from None

    @router.post("/cart/proposals/{proposal_id}/confirm")
    async def confirm_cart(proposal_id: str, data: CartConfirmation, request: Request) -> dict:
        try:
            return await cart_updates.confirm(actor(request), proposal_id, reviewed=data.reviewed)
        except OdaError as error:
            raise failure(error) from None

    return router
