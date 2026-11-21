"""Protocol ports for Home Assistant integration."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol


class HomeAssistantClientPort(Protocol):
    """Port for inspecting and updating Home Assistant To-Do lists."""

    def list_items(self) -> list[dict[str, Any]]: ...

    def export_remaining_items(
        self, items: list[str], *, dry_run: bool = False
    ) -> dict[str, Any]: ...

    def preview_quick_captures(self, existing_items: Iterable[str] = ()) -> dict[str, Any]: ...

    def import_quick_captures(
        self, existing_items: Iterable[str] = (), *, reviewed: bool = False
    ) -> dict[str, Any]: ...
