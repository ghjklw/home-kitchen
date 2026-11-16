from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select, text

from home_kitchen.purchase_sync import (
    CheckpointConflict,
    ProviderOrderRecord,
    ProviderPage,
    RevisionConflict,
    checkpoint,
    metadata,
    persist_page,
    provider_order_sources,
    sync_account,
)


@pytest.fixture
def engine():
    value = create_engine("sqlite://")
    metadata.create_all(value)
    yield value
    value.dispose()


def record(external_id: str, revision: str, payload: dict | None = None):
    return ProviderOrderRecord(
        external_id=external_id,
        revision_id=revision,
        source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=payload or {"state": "provider-reported"},
    )


class PagedAdapter:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch_page(self, account_id, cursor):
        self.calls.append((account_id, cursor))
        return self.pages[cursor]


def page(records, cursor, has_more):
    return ProviderPage(records=records, next_cursor=cursor, has_more=has_more)


def count_sources(engine):
    with engine.connect() as connection:
        return connection.execute(
            select(func.count()).select_from(provider_order_sources)
        ).scalar_one()


def test_backfill_overlap_dedupe_and_next_poll_resumes_terminal_cursor(engine):
    backfill = PagedAdapter(
        {
            None: page([record("o1", "r1")], "page-2", True),
            "page-2": page([record("o1", "r1"), record("o2", "r1")], "watermark-2", False),
        }
    )

    assert sync_account(engine, "provider-a", "account-a", backfill) == 3
    assert backfill.calls == [("account-a", None), ("account-a", "page-2")]
    assert checkpoint(engine, "provider-a", "account-a") == "watermark-2"
    assert count_sources(engine) == 2
    with engine.connect() as connection:
        row = connection.execute(select(provider_order_sources)).mappings().first()
        assert row["observed_at"] is not None
        assert row["source_updated_at"] != row["observed_at"]

    incremental = PagedAdapter({"watermark-2": page([record("o3", "r1")], "watermark-3", False)})
    assert sync_account(engine, "provider-a", "account-a", incremental) == 1
    assert incremental.calls == [("account-a", "watermark-2")]
    assert checkpoint(engine, "provider-a", "account-a") == "watermark-3"
    assert count_sources(engine) == 3

    # Same revision replay is immutable and idempotent.
    persist_page(
        engine,
        "provider-a",
        "account-a",
        page([record("o3", "r1")], "watermark-3", False),
        expected_cursor="watermark-3",
    )
    assert count_sources(engine) == 3


def test_page_failure_rolls_back_source_and_checkpoint(engine):
    persist_page(
        engine,
        "provider-a",
        "account-a",
        page([], "page-2", True),
        expected_cursor=None,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_record BEFORE INSERT ON provider_order_sources "
                "BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END"
            )
        )

    with pytest.raises(Exception, match="injected persistence failure"):
        persist_page(
            engine,
            "provider-a",
            "account-a",
            page([record("o1", "r1")], "page-3", True),
            expected_cursor="page-2",
        )

    assert checkpoint(engine, "provider-a", "account-a") == "page-2"
    assert count_sources(engine) == 0


def test_revisions_are_immutable_and_corrections_require_new_revision(engine):
    persist_page(
        engine,
        "provider-a",
        "account-a",
        page([record("o1", "r1")], "c1", True),
        expected_cursor=None,
    )
    with pytest.raises(RevisionConflict, match="new revision_id"):
        persist_page(
            engine,
            "provider-a",
            "account-a",
            page([record("o1", "r1", {"status": "returned"})], "c2", True),
            expected_cursor="c1",
        )
    assert checkpoint(engine, "provider-a", "account-a") == "c1"

    persist_page(
        engine,
        "provider-a",
        "account-a",
        page([record("o1", "r2", {"status": "returned"})], "watermark", False),
        expected_cursor="c1",
    )
    assert count_sources(engine) == 2


def test_provider_account_isolation(engine):
    persist_page(engine, "provider-a", "account-a", page([], "a1", False), expected_cursor=None)
    persist_page(engine, "provider-a", "account-b", page([], "b1", False), expected_cursor=None)
    persist_page(engine, "provider-b", "account-a", page([], "p1", False), expected_cursor=None)

    assert checkpoint(engine, "provider-a", "account-a") == "a1"
    assert checkpoint(engine, "provider-a", "account-b") == "b1"
    assert checkpoint(engine, "provider-b", "account-a") == "p1"


def test_stale_page_cannot_regress_newer_checkpoint(engine):
    persist_page(engine, "provider-a", "account-a", page([], "c1", True), expected_cursor=None)
    stale_page = page([record("o1", "r1")], "stale", True)
    persist_page(engine, "provider-a", "account-a", page([], "c2", True), expected_cursor="c1")

    with pytest.raises(CheckpointConflict):
        persist_page(engine, "provider-a", "account-a", stale_page, expected_cursor="c1")
    assert checkpoint(engine, "provider-a", "account-a") == "c2"
    assert count_sources(engine) == 0


@pytest.mark.parametrize("external_id", ["", "   "])
def test_rejects_malformed_external_identity(external_id):
    with pytest.raises(ValidationError):
        record(external_id, "r1")


def test_rejects_naive_source_time():
    with pytest.raises(ValidationError):
        ProviderOrderRecord(
            external_id="o1",
            revision_id="r1",
            source_updated_at=datetime(2026, 1, 1),
            payload={},
        )


@pytest.mark.parametrize(("provider", "account_id"), [(" ", "acct"), ("provider", "")])
def test_rejects_malformed_provider_account_identity(engine, provider, account_id):
    with pytest.raises(ValueError, match="provider and account_id"):
        persist_page(
            engine,
            provider,
            account_id,
            page([], "cursor", False),
            expected_cursor=None,
        )
