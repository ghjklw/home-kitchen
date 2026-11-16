from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select, text

from home_kitchen.purchase_sync import (
    ProviderOrderRecord,
    ProviderPage,
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


def test_backfill_pages_and_retry_overlap_dedupe(engine):
    adapter = PagedAdapter(
        {
            None: ProviderPage(records=[record("o1", "r1")], next_cursor="page-2"),
            "page-2": ProviderPage(records=[record("o1", "r1"), record("o2", "r1")], next_cursor=None),
        }
    )

    assert sync_account(engine, "provider-a", "account-a", adapter) == 3
    assert adapter.calls == [("account-a", None), ("account-a", "page-2")]
    assert checkpoint(engine, "provider-a", "account-a") is None
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(provider_order_sources)).scalar_one() == 2

    persist_page(engine, "provider-a", "account-a", ProviderPage(records=[record("o2", "r1")], next_cursor=None))
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(provider_order_sources)).scalar_one() == 2


def test_page_failure_rolls_back_records_and_checkpoint(engine):
    persist_page(engine, "provider-a", "account-a", ProviderPage(records=[], next_cursor="page-2"))
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TRIGGER reject_record BEFORE INSERT ON provider_order_sources "
                 "BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END")
        )

    with pytest.raises(Exception, match="injected persistence failure"):
        persist_page(
            engine,
            "provider-a",
            "account-a",
            ProviderPage(records=[record("o1", "r1")], next_cursor="page-2"),
        )

    assert checkpoint(engine, "provider-a", "account-a") == "page-2"
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(provider_order_sources)).scalar_one() == 0


def test_revisions_are_preserved_and_provider_accounts_are_isolated(engine):
    # Status and return fields stay opaque; a correction/return is another source revision.
    persist_page(engine, "provider-a", "account-a", ProviderPage(records=[record("o1", "r1")], next_cursor="c"))
    persist_page(
        engine,
        "provider-a",
        "account-a",
        ProviderPage(records=[record("o1", "r2", {"status": "returned"})], next_cursor=None),
    )
    persist_page(engine, "provider-a", "account-b", ProviderPage(records=[record("o1", "r1")], next_cursor="b"))
    persist_page(engine, "provider-b", "account-a", ProviderPage(records=[record("o1", "r1")], next_cursor="p"))

    with engine.connect() as connection:
        rows = connection.execute(select(provider_order_sources)).mappings().all()
        assert len(rows) == 4
        assert {(row["provider"], row["account_id"]) for row in rows} == {
            ("provider-a", "account-a"),
            ("provider-a", "account-b"),
            ("provider-b", "account-a"),
        }
    assert checkpoint(engine, "provider-a", "account-a") is None
    assert checkpoint(engine, "provider-a", "account-b") == "b"
    assert checkpoint(engine, "provider-b", "account-a") == "p"


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
        persist_page(engine, provider, account_id, ProviderPage(records=[], next_cursor=None))

