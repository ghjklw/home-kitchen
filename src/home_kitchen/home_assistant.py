"""Optional one-way Home Assistant To-Do adapter for remaining store-trip items."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, HttpUrl, SecretStr

_LOG = logging.getLogger(__name__)

DEFAULT_HOME_ASSISTANT_TODO_ENTITY = "todo.shopping_list"
HOME_ASSISTANT_NOT_CONFIGURED_ERROR = "Home Assistant er ikke konfigurert; legg til URL og tilgangstoken i Innstillinger eller miljøvariabler."


class HomeAssistantError(Exception):
    """Base exception for Home Assistant API errors."""


class HomeAssistantAuthError(HomeAssistantError):
    """Authentication failure (401/403) from Home Assistant."""


class HomeAssistantNotConfiguredError(HomeAssistantError):
    """Home Assistant integration is not configured."""


class HomeAssistantTodoExportResult(BaseModel):
    """Conflict-safe result of exporting remaining store-trip items to Home Assistant."""

    model_config = ConfigDict(extra="forbid")

    added: list[str]
    skipped_existing: list[str]
    total_requested: int
    entity_id: str


def format_store_trip_export_item(name: str, quantity: Decimal, unit: str) -> str:
    """Format a remaining store-trip item into a concise, deterministic to-do summary."""
    cleaned_name = name.strip()
    cleaned_unit = unit.strip()
    qty_text = f"{quantity:f}"
    if "." in qty_text:
        qty_text = qty_text.rstrip("0").rstrip(".")
    if not cleaned_unit or (cleaned_unit == "item" and quantity == Decimal(1)):
        return cleaned_name
    if cleaned_unit == "item":
        return f"{cleaned_name} ({qty_text})"
    return f"{cleaned_name} ({qty_text} {cleaned_unit})"


def _normalize_item_list(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []
    normalized: list[dict[str, Any]] = []
    for entry in raw_items:
        if isinstance(entry, dict):
            normalized.append(entry)
        elif isinstance(entry, str) and entry.strip():
            normalized.append({"summary": entry.strip(), "status": "needs_action"})
    return normalized


def _extract_todo_items(payload: Any, entity_id: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return _normalize_item_list(payload)
    if not isinstance(payload, dict):
        return []

    service_response = payload.get("service_response")
    if isinstance(service_response, dict):
        entity_data = service_response.get(entity_id)
        if isinstance(entity_data, dict) and "items" in entity_data:
            return _normalize_item_list(entity_data["items"])
        if "items" in service_response:
            return _normalize_item_list(service_response["items"])
        if len(service_response) == 1:
            only_value = next(iter(service_response.values()))
            if isinstance(only_value, dict) and "items" in only_value:
                return _normalize_item_list(only_value["items"])

    entity_block = payload.get(entity_id)
    if isinstance(entity_block, dict) and "items" in entity_block:
        return _normalize_item_list(entity_block["items"])

    if "items" in payload:
        return _normalize_item_list(payload["items"])

    attributes = payload.get("attributes")
    if isinstance(attributes, dict):
        if "items" in attributes:
            return _normalize_item_list(attributes["items"])
        if "todo_items" in attributes:
            return _normalize_item_list(attributes["todo_items"])

    return []


def _is_active_todo_item(item: dict[str, Any]) -> bool:
    if item.get("complete") is True:
        return False
    status = item.get("status")
    if status is None:
        return True
    return str(status).strip().lower() != "completed"


def _item_summary(item: dict[str, Any]) -> str | None:
    raw = item.get("summary") or item.get("name") or item.get("item")
    if isinstance(raw, str):
        cleaned = raw.strip()
        return cleaned or None
    return None


class HomeAssistantTodoClient:
    """One-way HTTP client for Home Assistant To-Do list inspection and item addition."""

    def __init__(
        self,
        base_url: str | HttpUrl,
        token: str | SecretStr,
        entity_id: str = DEFAULT_HOME_ASSISTANT_TODO_ENTITY,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        clean_url = str(base_url).strip().rstrip("/") if base_url is not None else ""
        if not clean_url:
            raise ValueError("Home Assistant URL må oppgis.")

        if isinstance(token, SecretStr):
            clean_token = token.get_secret_value().strip()
        elif isinstance(token, str):
            clean_token = token.strip()
        else:
            clean_token = ""
        if not clean_token:
            raise ValueError("Home Assistant-token må oppgis.")

        clean_entity = entity_id.strip() if isinstance(entity_id, str) else ""
        if not clean_entity:
            raise ValueError("Home Assistant to-do entity_id må oppgis.")

        self.base_url = clean_url
        self.token = clean_token
        self.entity_id = clean_entity
        self.transport = transport

    def _get_client(self) -> httpx.Client:
        return httpx.Client(
            transport=self.transport,
            timeout=20.0,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "HomeKitchen/1.0",
            },
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response, context: str) -> None:
        if resp.status_code in (401, 403):
            raise HomeAssistantAuthError(
                "Ugyldig eller utløpt Home Assistant-token. Vennligst sjekk tilkoblingen i Innstillinger."
            )
        if resp.status_code >= 400:
            raise HomeAssistantError(
                f"Home Assistant svarte med HTTP {resp.status_code} ved {context}: {resp.text}"
            )

    @staticmethod
    def _parse_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except Exception as exc:
            raise HomeAssistantError(f"Ugyldig JSON-respons fra Home Assistant: {exc}") from exc

    def _list_items_with_client(self, client: httpx.Client) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/services/todo/get_items?return_response"
        try:
            resp = client.post(url, json={"entity_id": self.entity_id})
        except httpx.HTTPError as exc:
            _LOG.warning("Home Assistant network failure during todo/get_items: %s", exc)
            raise HomeAssistantError(f"Nettverksfeil mot Home Assistant: {exc}") from exc

        if resp.status_code in (401, 403):
            self._raise_for_status(resp, "todo/get_items")

        if resp.status_code in (400, 404, 405):
            state_url = f"{self.base_url}/api/states/{self.entity_id}"
            try:
                state_resp = client.get(state_url)
            except httpx.HTTPError as exc:
                _LOG.warning("Home Assistant network failure during state fallback: %s", exc)
                raise HomeAssistantError(f"Nettverksfeil mot Home Assistant: {exc}") from exc
            self._raise_for_status(state_resp, f"states/{self.entity_id}")
            return _extract_todo_items(self._parse_json(state_resp), self.entity_id)

        self._raise_for_status(resp, "todo/get_items")
        return _extract_todo_items(self._parse_json(resp), self.entity_id)

    def list_items(self) -> list[dict[str, Any]]:
        """List current items on the configured Home Assistant to-do entity."""
        with self._get_client() as client:
            return self._list_items_with_client(client)

    def export_remaining_items(self, items: list[str], *, dry_run: bool = False) -> dict[str, Any]:
        """Export missing items one-way to Home Assistant, skipping existing active items."""
        deduped: list[str] = []
        seen_input: set[str] = set()
        for raw in items:
            cleaned = raw.strip() if isinstance(raw, str) else ""
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key in seen_input:
                continue
            seen_input.add(key)
            deduped.append(cleaned)

        if not deduped:
            return {
                "added": [],
                "skipped_existing": [],
                "total_requested": 0,
                "entity_id": self.entity_id,
            }

        with self._get_client() as client:
            existing_items = self._list_items_with_client(client)
            existing_keys: set[str] = set()
            for item in existing_items:
                if not _is_active_todo_item(item):
                    continue
                summary = _item_summary(item)
                if summary is not None:
                    existing_keys.add(summary.casefold())

            added: list[str] = []
            skipped_existing: list[str] = []
            add_url = f"{self.base_url}/api/services/todo/add_item"

            for item_text in deduped:
                if item_text.casefold() in existing_keys:
                    skipped_existing.append(item_text)
                    continue
                if not dry_run:
                    try:
                        resp = client.post(
                            add_url,
                            json={"entity_id": self.entity_id, "item": item_text},
                        )
                    except httpx.HTTPError as exc:
                        _LOG.warning(
                            "Home Assistant network failure during todo/add_item for %s: %s",
                            item_text,
                            exc,
                        )
                        raise HomeAssistantError(
                            f"Nettverksfeil mot Home Assistant ved eksport av '{item_text}': {exc}"
                        ) from exc
                    self._raise_for_status(resp, "todo/add_item")
                existing_keys.add(item_text.casefold())
                added.append(item_text)

        return {
            "added": added,
            "skipped_existing": skipped_existing,
            "total_requested": len(deduped),
            "entity_id": self.entity_id,
        }
