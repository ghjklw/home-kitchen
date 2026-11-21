"""Adapters connecting basket matching ports to catalogue services."""

from __future__ import annotations

from uuid import UUID

from home_kitchen.basket.ports import (
    IdentityCorrectionPort,
    IdentityLookupPort,
    SkuPreferencePort,
)
from home_kitchen.catalogue.identity import IdentityCorrectionCitation, IdentityCorrectionEvent
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.catalogue.overview_service import ProductOverviewService


class ProductOverviewSkuPreferenceAdapter(SkuPreferencePort):
    """Adapts ProductOverviewService preferences to SkuPreferencePort."""

    def __init__(self, product_overview: ProductOverviewService) -> None:
        self.product_overview = product_overview

    def get_sku_preferences(self, product_id: UUID) -> tuple[list[str], list[str]]:
        try:
            pref, exc = self.product_overview.get_preferences(product_id)
            return ([p.oda_product_id for p in pref], [e.oda_product_id for e in exc])
        except KeyError:
            return ([], [])


class CanonicalIdentityLookupAdapter(IdentityLookupPort):
    """Adapts CanonicalIdentityService resolution to IdentityLookupPort."""

    def __init__(
        self,
        identities: CanonicalIdentityService,
        *,
        household_id: UUID | None = None,
    ) -> None:
        self.identities = identities
        self.household_id = household_id

    def resolve_product_id(self, name: str) -> UUID | None:
        for loc in ("nb", "en", "fr"):
            for ctx in ("recipe", "receipt", ""):
                try:
                    res = self.identities.resolve(
                        "product", loc, ctx, name, household_id=self.household_id
                    )
                except TypeError:
                    res = self.identities.resolve("product", loc, ctx, name)
                if res.status == "resolved" and len(res.candidates) == 1:
                    return res.candidates[0].id
        return None


class CanonicalIdentityCorrectionAdapter(IdentityCorrectionPort):
    """Adapts CanonicalIdentityService correction audit events to IdentityCorrectionPort."""

    def __init__(
        self,
        identities: CanonicalIdentityService,
        *,
        household_id: UUID | None = None,
    ) -> None:
        self.identities = identities
        self.household_id = household_id

    def get_identity_corrections(
        self, need_name: str, product_id: UUID | None
    ) -> list[IdentityCorrectionCitation]:
        events = self.identities.list_correction_events(
            kind="product",
            alias=need_name,
            household_id=self.household_id,
            limit=10,
        )
        citations: list[IdentityCorrectionCitation] = []
        if isinstance(events, list):
            for ev in events:
                if not isinstance(ev, IdentityCorrectionEvent):
                    continue
                if product_id is not None and ev.new_identity_id != product_id:
                    continue
                citations.append(IdentityCorrectionCitation.from_event(ev))
        if not citations and product_id is not None:
            recent_events = self.identities.list_correction_events(
                kind="product",
                household_id=self.household_id,
                limit=50,
            )
            if isinstance(recent_events, list):
                for ev in recent_events:
                    if isinstance(ev, IdentityCorrectionEvent) and ev.new_identity_id == product_id:
                        citations.append(IdentityCorrectionCitation.from_event(ev))
        return citations
