from datetime import UTC, datetime

import httpx
from sqlalchemy import create_engine, func, select

from home_kitchen.coop_history import CoopHttpSource, CoopPurchaseHistoryAdapter
from home_kitchen.purchase_sync import (
    ProviderOrderRecord,
    ProviderPage,
    PurchaseHistorySyncService,
    metadata,
    provider_order_sources,
    sync_account,
)


def test_coop_resync_keeps_old_snapshot_and_uses_corrected_historic_revision():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)

    def receipt(revision_id: str, purchase_date: str) -> ProviderOrderRecord:
        return ProviderOrderRecord(
            external_id="coop-historic-receipt",
            revision_id=revision_id,
            source_updated_at=None,
            payload={
                "summary": {
                    "summaryId": "coop-historic-receipt",
                    "purchaseDate": purchase_date,
                    "amount": "42.00",
                    "lines": [
                        {
                            "productName": "Synthetic oats",
                            "quantity": 1,
                            "amount": "42.00",
                        }
                    ],
                }
            },
        )

    snapshots = [
        receipt("sha256:normalizer-v1", "2026-11-10T18:59:00Z"),
        receipt("sha256:normalizer-v2", "2023-12-10T18:59:00Z"),
    ]

    class ChangingAdapter:
        def fetch_page(self, account_id, cursor):
            snapshot = snapshots.pop(0)
            return ProviderPage(
                records=[snapshot],
                next_cursor=f"poll-{len(snapshots)}",
                has_more=False,
            )

    adapter = ChangingAdapter()
    try:
        assert sync_account(engine, "coop", "synthetic-account", adapter) == 1
        assert sync_account(engine, "coop", "synthetic-account", adapter) == 1

        with engine.connect() as connection:
            rows = (
                connection.execute(
                    select(provider_order_sources).where(
                        provider_order_sources.c.external_id == "coop-historic-receipt"
                    )
                )
                .mappings()
                .all()
            )

        assert len(rows) == 2
        by_revision = {row["revision_id"]: row for row in rows}
        assert by_revision["sha256:normalizer-v1"]["payload"]["summary"]["purchaseDate"] == (
            "2026-11-10T18:59:00Z"
        )
        assert by_revision["sha256:normalizer-v2"]["payload"]["summary"]["purchaseDate"] == (
            "2023-12-10T18:59:00Z"
        )

        effective = PurchaseHistorySyncService(engine=engine).get_effective_orders("coop")
        assert len(effective) == 1
        assert effective[0].revision_id == "sha256:normalizer-v2"
        assert effective[0].revision_count == 2
        assert effective[0].superseded_revision_ids == ["sha256:normalizer-v1"]
        assert effective[0].transaction_at == datetime(2023, 12, 10, 18, 59, tzinfo=UTC)
    finally:
        engine.dispose()


def test_coop_search_sync_imports_more_than_the_legacy_50_item_limit():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    current_purchases = [
        {"summaryId": f"coop-current-{index:02}", "dateTime": "18.11. 12:00"} for index in range(51)
    ]
    historic_purchases = [
        {"summaryId": "coop-current-50", "dateTime": "18.11. 12:00"},
        *[
            {"summaryId": f"coop-historic-{index:02}", "dateTime": "07.04. 16:40"}
            for index in range(9)
        ],
    ]
    search_counts = {"start": 0, "continue": 0, "details": 0}

    def history_group(year: int, month: int, purchases: list[dict]) -> dict:
        return {
            "monthOfYear": {"year": year, "month": month},
            "periodPurchases": purchases,
        }

    def search_response(*groups: dict, token: str | None = None) -> dict:
        return {"hitsPerMonth": list(groups), "continuationToken": token}

    def handler(request):
        if request.url.path == "/user/pay/history/search":
            search_counts["start"] += 1
            return httpx.Response(
                200,
                json=search_response(
                    history_group(2026, 11, current_purchases), token="historic-page"
                ),
            )
        if request.url.path == "/user/pay/history/search/continue":
            search_counts["continue"] += 1
            assert request.url.params.get("token") == "historic-page"
            return httpx.Response(
                200,
                json=search_response(
                    history_group(2026, 11, historic_purchases[:1]),
                    history_group(2023, 4, historic_purchases[1:]),
                ),
            )
        if request.url.path == "/user/pay/history/details":
            search_counts["details"] += 1
            summary_id = request.url.params["summaryId"]
            return httpx.Response(
                200,
                json={
                    "summary": {
                        "summaryId": summary_id,
                        "dateTime": (
                            "07.04. 16:40"
                            if summary_id.startswith("coop-historic-")
                            else "18.11. 12:00"
                        ),
                        "storeName": "Synthetic Coop",
                        "amount": "10.00",
                        "lines": [
                            {
                                "productName": "Synthetic oats",
                                "quantity": 1,
                                "amount": "10.00",
                            }
                        ],
                    }
                },
            )
        raise AssertionError(f"unexpected Coop request path {request.url.path}")

    source = CoopHttpSource(access_token="synthetic-token", transport=httpx.MockTransport(handler))
    adapter = CoopPurchaseHistoryAdapter(source, "synthetic-account", page_size=20)

    try:
        assert sync_account(engine, "coop", "synthetic-account", adapter) == 60
        with engine.connect() as connection:
            persisted_count = connection.execute(
                select(func.count())
                .select_from(provider_order_sources)
                .where(provider_order_sources.c.provider == "coop")
            ).scalar_one()
        assert persisted_count == 60
        effective = PurchaseHistorySyncService(engine=engine).get_effective_orders("coop")
        assert len(effective) == 60
        historic = next(order for order in effective if order.external_id == "coop-historic-00")
        assert historic.transaction_at is not None
        assert historic.transaction_at.year == 2023
        assert search_counts["start"] == 1
        assert search_counts["continue"] == 1
        assert search_counts["details"] == 60
    finally:
        engine.dispose()
