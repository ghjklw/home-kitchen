"""Provider interface and explicit registry for product catalogue integrations."""

from typing import Protocol

from .models import ProductSnapshot


class ProductProvider(Protocol):
    """Adapter implemented by an explicitly configured product provider."""

    provider_id: str

    async def search(self, query: str) -> list[ProductSnapshot]: ...

    async def verify(self, product_id: str) -> ProductSnapshot: ...


class ProviderRegistry:
    """Small in-memory registry; providers are never discovered or enabled implicitly."""

    def __init__(self) -> None:
        self._providers: dict[str, ProductProvider] = {}

    def register(self, provider: ProductProvider) -> None:
        provider_id = provider.provider_id.strip().casefold()
        if not provider_id:
            raise ValueError("provider_id cannot be blank")
        if provider_id in self._providers:
            raise ValueError(f"provider already registered: {provider_id}")
        self._providers[provider_id] = provider

    def get(self, provider_id: str) -> ProductProvider:
        try:
            return self._providers[provider_id.casefold()]
        except KeyError:
            raise KeyError(f"provider is not configured: {provider_id}") from None

    def catalog(self) -> list[str]:
        return sorted(self._providers)
