"""Bootstrap wiring for kitchen features using Protocol ports."""

from __future__ import annotations

from typing import Any

from home_kitchen.purchase_sync import ProviderAdapter


def _build_purchase_adapter(
    provider: str, account_id: str, creds: dict[str, Any]
) -> ProviderAdapter | None:
    if provider == "trumf":
        cookie = creds.get("session_cookie") or creds.get("cookie")
        if not isinstance(cookie, str) or not cookie.strip():
            return None
        from home_kitchen.trumf_history import TrumfHttpSource, TrumfPurchaseHistoryAdapter

        return TrumfPurchaseHistoryAdapter(TrumfHttpSource(cookie), account_id=account_id)
    if provider == "coop":
        token = creds.get("access_token") or creds.get("session_token")
        cookie = creds.get("session_cookie") or creds.get("cookie")
        token_str = token.strip() if isinstance(token, str) and token.strip() else None
        cookie_str = cookie.strip() if isinstance(cookie, str) and cookie.strip() else None
        if not token_str and not cookie_str:
            return None
        from home_kitchen.coop_history import CoopHttpSource, CoopPurchaseHistoryAdapter

        return CoopPurchaseHistoryAdapter(
            CoopHttpSource(access_token=token_str, session_cookie=cookie_str),
            account_id=account_id,
        )
    if provider == "rema1000":
        token = creds.get("access_token") or creds.get("session_token")
        if not isinstance(token, str) or not token.strip():
            return None
        from home_kitchen.rema1000_history import (
            Rema1000HttpSource,
            Rema1000PurchaseHistoryAdapter,
        )

        return Rema1000PurchaseHistoryAdapter(Rema1000HttpSource(token), account_id=account_id)
    return None
