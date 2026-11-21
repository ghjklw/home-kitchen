"""Kassal.app client for barcode (EAN/GTIN) enrichment and product metadata."""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

_LOG = logging.getLogger(__name__)

DEFAULT_KASSAL_BASE_URL = "https://kassal.app"
TEST_VALIDATION_EAN = "7038010000188"
KASSAL_MISSING_KEY_ERROR = (
    "Kassal.app API-nøkkel mangler; koble til Kassal.app i Innstillinger for å "
    "berike strekkoder (EAN) på kvitteringer."
)


class KassalProduct(BaseModel):
    """Enriched grocery product facts retrieved from Kassal.app by barcode (EAN)."""

    model_config = ConfigDict(extra="ignore")

    ean: str
    name: str
    brand: str | None = None
    vendor: str | None = None
    weight: float | None = None
    weight_unit: str | None = None
    category: Any = None
    image: str | None = None
    raw_data: dict[str, Any] = Field(default_factory=dict)


class KassalError(Exception):
    """Base exception for Kassal.app API errors."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class KassalAuthError(KassalError):
    """Authentication failure (401/403) from Kassal.app."""


class KassalRateLimitError(KassalError):
    """Rate limit (HTTP 429) response from Kassal.app."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after = retry_after


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = float(value.strip())
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


class KassalClient:
    """Thread-safe, caching HTTP client for the Kassal.app v1 products API."""

    def __init__(
        self,
        api_key: str | SecretStr,
        base_url: str = DEFAULT_KASSAL_BASE_URL,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if isinstance(api_key, SecretStr):
            clean_key = api_key.get_secret_value().strip()
        elif isinstance(api_key, str):
            clean_key = api_key.strip()
        else:
            clean_key = ""

        if not clean_key:
            raise ValueError("Kassal.app API-nøkkel må oppgis.")

        self.api_key = clean_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self._cache: dict[str, KassalProduct | None] = {}
        self._lock = threading.Lock()

    def _redact_secret(self, text: str) -> str:
        if self.api_key and self.api_key in text:
            return text.replace(self.api_key, "[REDACTED]")
        return text

    def _get_client(self) -> httpx.Client:
        return httpx.Client(
            transport=self.transport,
            timeout=20.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": "HomeKitchen/1.0",
            },
        )

    def lookup_ean(self, ean: str | None) -> KassalProduct | None:
        """Fetch product facts by barcode (EAN), with in-memory instance caching."""
        if not ean:
            return None
        cleaned_ean = re.sub(r"\s+", "", str(ean).strip())
        if not cleaned_ean:
            return None

        with self._lock:
            if cleaned_ean in self._cache:
                return self._cache[cleaned_ean]

        url = f"{self.base_url}/api/v1/products/ean/{cleaned_ean}"
        try:
            with self._get_client() as client:
                resp = client.get(url)
        except httpx.HTTPError as exc:
            safe_exc = self._redact_secret(str(exc))
            _LOG.warning("Kassal.app network failure for EAN %s: %s", cleaned_ean, safe_exc)
            raise KassalError(
                f"Nettverksfeil mot Kassal.app for EAN {cleaned_ean}: {safe_exc}"
            ) from exc

        if resp.status_code in (401, 403):
            raise KassalAuthError(
                "Ugyldig eller utløpt Kassal.app API-nøkkel. Vennligst sjekk API-nøkkelen i Innstillinger.",
                status_code=resp.status_code,
            )
        if resp.status_code == 404:
            with self._lock:
                self._cache[cleaned_ean] = None
            return None
        if resp.status_code == 429:
            safe_text = self._redact_secret(resp.text)
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            raise KassalRateLimitError(
                f"Kassal.app svarte med HTTP 429 for EAN {cleaned_ean}: {safe_text}",
                retry_after=retry_after,
            )
        if resp.status_code >= 400:
            safe_text = self._redact_secret(resp.text)
            raise KassalError(
                f"Kassal.app svarte med HTTP {resp.status_code} for EAN {cleaned_ean}: {safe_text}",
                status_code=resp.status_code,
            )

        try:
            payload = resp.json()
        except Exception as exc:
            raise KassalError(f"Ugyldig JSON-respons fra Kassal.app: {exc}") from exc

        product_dict = self._extract_product_dict(payload)
        if not product_dict:
            with self._lock:
                self._cache[cleaned_ean] = None
            return None

        name = (
            product_dict.get("name")
            or product_dict.get("title")
            or product_dict.get("product_name")
            or ""
        ).strip()
        if not name:
            with self._lock:
                self._cache[cleaned_ean] = None
            return None

        brand = product_dict.get("brand") or product_dict.get("brand_name")
        vendor = product_dict.get("vendor") or product_dict.get("supplier")
        weight = product_dict.get("weight")
        weight_unit = product_dict.get("weight_unit") or product_dict.get("unit")
        category = product_dict.get("category") or product_dict.get("category_name")
        image = (
            product_dict.get("image")
            or product_dict.get("image_url")
            or product_dict.get("thumbnail")
        )

        parsed_weight: float | None = None
        if weight is not None:
            try:
                parsed_weight = float(weight)
            except (ValueError, TypeError):
                parsed_weight = None

        product = KassalProduct(
            ean=cleaned_ean,
            name=name,
            brand=str(brand).strip() if brand else None,
            vendor=str(vendor).strip() if vendor else None,
            weight=parsed_weight,
            weight_unit=str(weight_unit).strip() if weight_unit else None,
            category=category,
            image=str(image).strip() if image else None,
            raw_data=product_dict,
        )

        with self._lock:
            self._cache[cleaned_ean] = product
        return product

    def validate_key(self, test_ean: str = TEST_VALIDATION_EAN) -> bool:
        """Validate API key by performing an authenticated request against a known EAN."""
        self.lookup_ean(test_ean)
        return True

    @staticmethod
    def _extract_product_dict(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if isinstance(data, dict):
            products = data.get("products")
            if isinstance(products, list) and products and isinstance(products[0], dict):
                return products[0]
            if "name" in data or "title" in data:
                return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        products = payload.get("products")
        if isinstance(products, list) and products and isinstance(products[0], dict):
            return products[0]
        if "name" in payload or "title" in payload:
            return payload
        return None
