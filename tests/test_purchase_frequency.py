"""Unit tests for recency-weighted, per-provider, coverage-aware purchase frequency."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, text

from home_kitchen.catalogue.identity import CanonicalProduct
from home_kitchen.catalogue.overview_models import PurchaseEvidenceRecord
from home_kitchen.catalogue.overview_repository import MemoryProductOverviewRepository
from home_kitchen.catalogue.overview_service import (
    ProductOverviewService,
    compute_purchase_frequency,
)
from home_kitchen.context import build_purchase_history_context
from home_kitchen.history import HistoryRepository
from home_kitchen.purchase_frequency import (
    ProviderObservationWindow,
    compute_recency_weighted_frequency,
    merge_observation_intervals,
    recency_weight,
)
from home_kitchen.purchase_sync import resolve_effective_orders
from home_kitchen.recommendations import build_planner
from home_kitchen.settings import Settings


def test_recency_weight_follows_half_life_decay():
    ref = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    assert recency_weight(ref, reference_time=ref, half_life_days=90.0) == 1.0
    assert (
        abs(recency_weight(ref - timedelta(days=90), reference_time=ref, half_life_days=90.0) - 0.5)
        < 1e-6
    )
    assert (
        abs(
            recency_weight(ref - timedelta(days=180), reference_time=ref, half_life_days=90.0)
            - 0.25
        )
        < 1e-6
    )


def test_merge_observation_intervals_handles_overlaps_without_double_counting():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    intervals = [
        (base, base + timedelta(days=100)),
        (base + timedelta(days=50), base + timedelta(days=150)),
        (base + timedelta(days=200), base + timedelta(days=230)),
    ]
    merged = merge_observation_intervals(intervals)
    assert len(merged) == 2
    assert merged[0] == (base, base + timedelta(days=150))
    assert merged[1] == (base + timedelta(days=200), base + timedelta(days=230))


def test_recency_weighted_frequency_adapts_to_changing_habits():
    """Recent purchases weigh substantially more than purchases from years ago."""
    # Old habit in 2024: bought every 7 days
    old_events = [
        PurchaseEvidenceRecord(
            purchased_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC) + timedelta(days=i * 7),
            provenance="provider_verified",
            evidence_source="oda",
            provider="oda",
        )
        for i in range(6)
    ]
    # New habit in late 2026: bought every 28 days
    recent_events = [
        PurchaseEvidenceRecord(
            purchased_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC) + timedelta(days=i * 28),
            provenance="provider_verified",
            evidence_source="oda",
            provider="oda",
        )
        for i in range(4)
    ]
    all_events = old_events + recent_events
    ref = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    # Naive calculation would give (1058 days / 9 intervals) ~= 117.6 days!
    analysis = compute_recency_weighted_frequency(all_events, as_of=ref, half_life_days=90.0)
    assert analysis.status == "known"
    assert analysis.average_interval_days is not None
    assert 25.0 <= analysis.average_interval_days <= 32.0
    assert analysis.is_lower_bound is True
    assert analysis.weighted_purchases_per_30_days is not None


def test_recency_weighted_frequency_speeds_up_when_recent_purchases_accelerate():
    """Habit shifting from monthly (2024) to every 5 days (2026) reflects the recent 5-day cadence."""
    old_events = [
        PurchaseEvidenceRecord(
            purchased_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC) + timedelta(days=i * 30),
            provenance="provider_verified",
            evidence_source="oda",
            provider="oda",
        )
        for i in range(4)
    ]
    recent_events = [
        PurchaseEvidenceRecord(
            purchased_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC) + timedelta(days=i * 5),
            provenance="provider_verified",
            evidence_source="oda",
            provider="oda",
        )
        for i in range(5)
    ]
    ref = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    analysis = compute_recency_weighted_frequency(old_events + recent_events, as_of=ref)
    assert analysis.status == "known"
    assert analysis.average_interval_days is not None
    assert 4.5 <= analysis.average_interval_days <= 6.5


def test_per_provider_observation_windows_prevent_cross_retailer_dilution():
    """A Trumf-only item (12m retention) is evaluated on Trumf's window, not Oda's 3-year window."""
    # Oda has 3 years of history, Trumf only retains 12 months
    provider_windows = {
        "oda": ProviderObservationWindow(
            provider="oda",
            start=datetime(2023, 1, 1, tzinfo=UTC),
            end=datetime(2026, 11, 20, tzinfo=UTC),
            total_records=120,
            incomplete_history=True,
        ),
        "trumf": ProviderObservationWindow(
            provider="trumf",
            start=datetime(2025, 11, 20, tzinfo=UTC),
            end=datetime(2026, 11, 20, tzinfo=UTC),
            total_records=24,
            retained_months=12,
            incomplete_history=True,
        ),
    }
    trumf_butter = [
        PurchaseEvidenceRecord(
            purchased_at=datetime(2025, 11, 25, 12, 0, tzinfo=UTC) + timedelta(days=i * 30),
            provenance="provider_verified",
            evidence_source="trumf",
            provider="trumf",
        )
        for i in range(12)
    ]
    freq = compute_purchase_frequency(
        trumf_butter,
        provider_windows=provider_windows,
        as_of=datetime(2026, 11, 20, tzinfo=UTC),
    )
    assert freq.status == "known"
    assert freq.average_interval_days == 30.0
    assert freq.coverage_days == 365
    assert freq.is_lower_bound is True
    assert len(freq.provider_windows) == 1
    assert freq.provider_windows[0].provider == "trumf"
    assert freq.provider_windows[0].retained_months == 12
    assert freq.provider_windows[0].incomplete_history is True


def test_trailing_silence_in_active_provider_window_decays_stale_weekly_item():
    """An item bought weekly in 2025 and never again during 2026 active receipts must not look weekly."""
    provider_windows = {
        "oda": ProviderObservationWindow(
            provider="oda",
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2026, 11, 20, tzinfo=UTC),
            total_records=80,
            incomplete_history=True,
        )
    }
    old_weekly = [
        PurchaseEvidenceRecord(
            purchased_at=datetime(2025, 1, 5, 12, 0, tzinfo=UTC) + timedelta(days=i * 7),
            provenance="provider_verified",
            evidence_source="oda",
            provider="oda",
        )
        for i in range(5)
    ]
    freq = compute_purchase_frequency(
        old_weekly,
        provider_windows=provider_windows,
        as_of=datetime(2026, 11, 20, tzinfo=UTC),
    )
    assert freq.status == "known"
    assert freq.average_interval_days is not None
    assert freq.average_interval_days > 100.0
    assert freq.weighted_purchases_per_30_days is not None
    assert freq.weighted_purchases_per_30_days < 0.5


def test_product_overview_service_passes_repository_provider_windows():
    prod_id = uuid4()
    repo = MemoryProductOverviewRepository(
        products=[CanonicalProduct(id=prod_id, canonical_name="smør")]
    )
    repo.provider_windows = {
        "coop": ProviderObservationWindow(
            provider="coop",
            start=datetime(2026, 6, 1, tzinfo=UTC),
            end=datetime(2026, 11, 20, tzinfo=UTC),
            total_records=20,
            incomplete_history=True,
        )
    }
    for i in range(4):
        repo.purchase_evidence[prod_id].append(
            PurchaseEvidenceRecord(
                purchased_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC) + timedelta(days=i * 14),
                provenance="provider_verified",
                evidence_source="coop",
                provider="coop",
            )
        )

    svc = ProductOverviewService(repository=repo)
    overview = svc.get_product_overview(prod_id)
    assert overview.purchase_frequency.status == "known"
    assert overview.purchase_frequency.average_interval_days == 14.0
    assert overview.purchase_frequency.coverage_days == 172
    assert overview.purchase_frequency.is_lower_bound is True
    assert overview.purchase_frequency.confidence == "medium"


def test_build_purchase_history_context_ranks_by_recency_and_includes_frequency_metadata():
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    rows = []
    # Old product bought 5 times in 2024 (higher raw count, stale habit)
    for idx in range(5):
        tx_dt = datetime(2024, 2, 1, 12, 0, tzinfo=UTC) + timedelta(days=idx * 7)
        rows.append(
            {
                "id": f"old-{idx}",
                "provider": "oda",
                "account_id": "malo",
                "external_id": f"oda-old-{idx}",
                "revision_id": "r1",
                "source_updated_at": None,
                "observed_at": tx_dt,
                "payload": {
                    "order": {
                        "orderNumber": f"oda-old-{idx}",
                        "deliveryDate": tx_dt.date().isoformat(),
                        "storeName": "Oda",
                        "products": [
                            {
                                "product": {"id": 1, "name": "Gammel Vare", "unitName": "stk"},
                                "quantity": 1,
                            }
                        ],
                    },
                    "tracking": {"orderNumber": f"oda-old-{idx}", "status": "delivered"},
                },
            }
        )
    # Recent product bought 3 times in Nov 2026 (lower raw count, active recent habit)
    for idx in range(3):
        tx_dt = datetime(2026, 11, 4, 12, 0, tzinfo=UTC) + timedelta(days=idx * 7)
        rows.append(
            {
                "id": f"new-{idx}",
                "provider": "oda",
                "account_id": "malo",
                "external_id": f"oda-new-{idx}",
                "revision_id": "r1",
                "source_updated_at": None,
                "observed_at": tx_dt,
                "payload": {
                    "order": {
                        "orderNumber": f"oda-new-{idx}",
                        "deliveryDate": tx_dt.date().isoformat(),
                        "storeName": "Oda",
                        "products": [
                            {
                                "product": {"id": 2, "name": "Havremelk", "unitName": "l"},
                                "quantity": 2,
                            }
                        ],
                    },
                    "tracking": {"orderNumber": f"oda-new-{idx}", "status": "delivered"},
                },
            }
        )

    effective = resolve_effective_orders(rows)
    ctx = build_purchase_history_context(effective, as_of=now)

    assert len(ctx.bought_before) == 2
    assert ctx.bought_before[0].product_name == "Havremelk"
    assert ctx.bought_before[0].recency_score is not None
    assert ctx.bought_before[1].recency_score is not None
    assert ctx.bought_before[0].recency_score > ctx.bought_before[1].recency_score
    assert ctx.bought_before[0].average_interval_days == 7.0
    assert ctx.bought_before[0].is_lower_bound is True
    assert "recency-weighted lower bounds" in ctx.boundary_note.lower()

    # Planner instructions reference recency-weighted lower bounds and keep stock boundary
    from pydantic_ai.models.test import TestModel

    planner = build_planner(Settings(local_dev=True), TestModel())
    instructions = " ".join(str(x) for x in planner._instructions)
    assert "recency-weighted lower bounds" in instructions
    assert "never to assert current stock" in instructions


def test_bought_before_context_uses_observed_at_fallback_only_for_non_coop_orders():
    observed_at = datetime.now(UTC).replace(microsecond=0)
    rows = [
        {
            "id": "coop-undated",
            "provider": "coop",
            "account_id": "synthetic-account",
            "external_id": "coop-undated",
            "revision_id": "r1",
            "source_updated_at": None,
            "observed_at": observed_at,
            "payload": {
                "summary": {
                    "summaryId": "coop-undated",
                    "lines": [{"productName": "Coop synthetic item", "quantity": 1, "amount": 5}],
                }
            },
        },
        {
            "id": "oda-undated",
            "provider": "oda",
            "account_id": "synthetic-account",
            "external_id": "oda-undated",
            "revision_id": "r1",
            "source_updated_at": None,
            "observed_at": observed_at,
            "payload": {
                "order": {
                    "orderNumber": "oda-undated",
                    "products": [
                        {
                            "product": {"id": 321, "name": "Oda synthetic item"},
                            "quantity": 1,
                        }
                    ],
                },
                "tracking": {"status": "delivered"},
            },
        },
    ]

    effective = resolve_effective_orders(rows)
    context = build_purchase_history_context(effective, as_of=observed_at)
    items = {item.provider: item for item in context.bought_before}

    assert items["coop"].last_purchased_at is None
    assert items["coop"].recency_score is None
    assert items["coop"].provider_coverage_days is None
    assert items["oda"].last_purchased_at == observed_at
    assert items["oda"].recency_score is not None
    assert set(context.source_providers) == {"coop", "oda"}


def test_history_repository_compute_item_purchase_frequency_uses_recency_weighting():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE kitchen_history (
                    id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    structured TEXT NOT NULL,
                    recipe_id TEXT,
                    session_id TEXT,
                    actor_id TEXT
                )
                """
            )
        )
        # Old 2024 purchases every 14 days
        for i in range(3):
            dt = datetime(2024, 1, 1, 12, 0, tzinfo=UTC) + timedelta(days=i * 14)
            conn.execute(
                text(
                    "INSERT INTO kitchen_history (id, occurred_at, channel, kind, message, structured) "
                    "VALUES (:id, :occurred_at, 'app', 'purchase', 'Bought Melk', :structured)"
                ),
                {
                    "id": str(uuid4()),
                    "occurred_at": dt.isoformat(),
                    "structured": json.dumps({"need": {"name": "Melk"}}),
                },
            )
        # Recent 2026 purchases every 7 days
        for i in range(3):
            dt = datetime(2026, 11, 1, 12, 0, tzinfo=UTC) + timedelta(days=i * 7)
            conn.execute(
                text(
                    "INSERT INTO kitchen_history (id, occurred_at, channel, kind, message, structured) "
                    "VALUES (:id, :occurred_at, 'app', 'purchase', 'Bought Melk', :structured)"
                ),
                {
                    "id": str(uuid4()),
                    "occurred_at": dt.isoformat(),
                    "structured": json.dumps({"need": {"name": "Melk"}}),
                },
            )

    repo = HistoryRepository(engine)
    analysis = repo.compute_item_purchase_frequency(
        "Melk",
        as_of=datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
    )
    assert analysis.status == "known"
    assert analysis.sample_size == 6
    assert analysis.average_interval_days is not None
    assert 6.5 <= analysis.average_interval_days <= 8.0
    assert analysis.is_lower_bound is True
    engine.dispose()
    assert SimpleNamespace is not None
