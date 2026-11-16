"""Original provider-shaped receipt scenarios with separate test-harness facts.

Coop field names reflect the reviewed reverse-engineered gist contract. Trumf
field names reflect the reviewed transaction/line summary. These are not
provider captures and do not claim live API, authentication or revision-time
semantics. Oda order payloads are explicitly internal synthetic examples;
they are not an asserted Oda wire contract.
"""

from dataclasses import dataclass
from typing import Any, Literal

ReceiptLifecycle = Literal["pending", "completed", "corrected", "refund_scenario"]
FailureKind = Literal["authentication", "rate_limit", "unsupported_contract"]


@dataclass(frozen=True)
class SyntheticSourcePage:
    """Raw response plus a harness-only continuation flag.

    ``continuation_observed`` is not part of any provider response payload.
    """

    response: dict[str, Any] | list[dict[str, Any]]
    continuation_observed: bool


@dataclass(frozen=True)
class SyntheticReceiptRevision:
    """Test-side lifecycle/revision annotation kept outside provider payloads."""

    provider: Literal["oda", "coop", "trumf"]
    external_id: str
    revision_id: str
    lifecycle: ReceiptLifecycle
    payload: dict[str, Any]


@dataclass(frozen=True)
class SyntheticHistoryFailure:
    """A fake-reader failure category; it has no provider HTTP/status contract."""

    provider: Literal["oda", "coop", "trumf"]
    kind: FailureKind


def _coop_summary(summary_id: str, receipt_id: str, amount: float) -> dict[str, Any]:
    return {
        "summaryId": summary_id,
        "receiptId": receipt_id,
        "purchaseDate": "2026-04-07T16:40:00+02:00",
        "storeName": "Fictional Coop Test Hall",
        "storeId": "synthetic-coop-store-01",
        "chainId": "synthetic-coop-chain",
        "amount": amount,
        "totalDiscount": 3.25,
    }


def _coop_purchase_stub(summary_id: str, amount: float) -> dict[str, Any]:
    """Return only the documented list-stub fields; receiptId is detail-only."""
    return {
        "summaryId": summary_id,
        "purchaseDate": "2026-04-07T16:40:00+02:00",
        "storeName": "Fictional Coop Test Hall",
        "storeId": "synthetic-coop-store-01",
        "chainId": "synthetic-coop-chain",
        "amount": amount,
        "totalDiscount": 3.25,
    }


def coop_purchase_list_pages() -> tuple[SyntheticSourcePage, ...]:
    """Return overlapping summary pages without inventing a response cursor."""
    first = _coop_purchase_stub("synthetic-coop-summary-001", 86.75)
    second = _coop_purchase_stub("synthetic-coop-summary-002", 31.50)
    return (
        SyntheticSourcePage({"purchases": [first]}, continuation_observed=True),
        SyntheticSourcePage({"purchases": [first, second]}, continuation_observed=False),
    )


def _coop_details(summary_amount: float, carrot_amount: float) -> dict[str, Any]:
    return {
        "summary": {
            **_coop_summary(
                "synthetic-coop-summary-001", "synthetic-coop-receipt-001", summary_amount
            ),
            "lines": [
                {
                    "productName": "Fictional gulrot / carrot ribbons",
                    "ean13": None,
                    "gtin13": None,
                    "barcode": None,
                    "quantity": 2,
                    "amount": carrot_amount,
                    "discount": 1.50,
                    "unitPrice": 9.50,
                },
                {
                    "productName": "Fictional grain parcel",
                    "quantity": None,
                    "amount": 14.00,
                    "discount": None,
                    "unitPrice": 14.00,
                },
            ],
        }
    }


def coop_receipt_detail_revisions() -> tuple[SyntheticReceiptRevision, ...]:
    """Return base, corrected and refund-test snapshots with no source timestamp."""
    return (
        SyntheticReceiptRevision(
            provider="coop",
            external_id="synthetic-coop-summary-001",
            revision_id="fixture-revision-1",
            lifecycle="completed",
            payload=_coop_details(86.75, 18.50),
        ),
        SyntheticReceiptRevision(
            provider="coop",
            external_id="synthetic-coop-summary-001",
            revision_id="fixture-revision-2",
            lifecycle="corrected",
            payload=_coop_details(85.25, 17.00),
        ),
        SyntheticReceiptRevision(
            provider="coop",
            external_id="synthetic-coop-refund-001",
            revision_id="fixture-refund-1",
            lifecycle="refund_scenario",
            payload={
                "summary": {
                    **_coop_summary(
                        "synthetic-coop-refund-001", "synthetic-coop-refund-receipt-001", 4.00
                    ),
                    "lines": [
                        {
                            "productName": "Fictional return adjustment",
                            "quantity": 1,
                            "amount": 4.00,
                            "discount": 0.0,
                            "unitPrice": 4.00,
                        }
                    ],
                }
            },
        ),
    )


def trumf_transaction_pages() -> tuple[SyntheticSourcePage, ...]:
    """Return fictional Trumf-shaped transaction and line-detail responses."""
    first = {
        "dato": "2026-04-08T17:10:00+02:00",
        "beskrivelse": "Synthetic pantry collection",
        "kjedeid": "synthetic-chain-01",
        "partnerid": "synthetic-partner-01",
        "batchid": "synthetic-trumf-batch-001",
        "belop": 92.80,
        "trumf": 1.10,
        "ekstratrumf": 0.25,
        "trumfvisa": 0.0,
        "literbensin": 0.0,
        "trumftotal": 1.35,
    }
    second = {
        "dato": "2026-04-09T12:15:00+02:00",
        "beskrivelse": "Synthetic pantry collection, corrected snapshot",
        "kjedeid": "synthetic-chain-01",
        "partnerid": "synthetic-partner-01",
        "batchid": "synthetic-trumf-batch-002",
        "belop": 28.00,
        "trumf": 0.30,
        "ekstratrumf": 0.0,
        "trumfvisa": 0.0,
        "literbensin": 0.0,
        "trumftotal": 0.30,
    }
    details = [
        {
            "varelinjer": [
                {
                    "vareTekst": "Fictional gulrotbånd / carrot ribbons",
                    "ean": "SYNTHETIC-TRUMF-001",
                    "antall": 2,
                    "belop": 18.50,
                },
                {
                    "vareTekst": "Fictional loose grain",
                    "ean": None,
                    "antall": None,
                    "belop": 14.00,
                },
            ]
        },
        {
            "varelinjer": [
                {
                    "vareTekst": "Fictional return adjustment",
                    "ean": None,
                    "antall": 1,
                    "belop": 4.00,
                }
            ]
        },
    ]
    return (
        SyntheticSourcePage([first], continuation_observed=True),
        SyntheticSourcePage([first, second], continuation_observed=False),
        SyntheticSourcePage(details[0], continuation_observed=False),
        SyntheticSourcePage(details[1], continuation_observed=False),
    )


def oda_order_receipt_scenarios() -> tuple[SyntheticReceiptRevision, ...]:
    """Return internal synthetic Oda-order envelopes; not an Oda wire contract."""
    return (
        SyntheticReceiptRevision(
            provider="oda",
            external_id="synthetic-oda-order-001",
            revision_id="fixture-order-revision-1",
            lifecycle="pending",
            payload={
                "orderId": "synthetic-oda-order-001",
                "orderedAt": "2026-04-10T15:20:00+02:00",
                "totalNok": 53.50,
                "lines": [
                    {
                        "sourceName": "Fictional gulrotbånd / carrot ribbons",
                        "quantity": 2,
                        "unit": "stk",
                        "amountNok": 18.50,
                    },
                    {
                        "sourceName": "Fictional grain parcel",
                        "quantity": None,
                        "unit": None,
                        "amountNok": 14.00,
                    },
                ],
            },
        ),
        SyntheticReceiptRevision(
            provider="oda",
            external_id="synthetic-oda-order-001",
            revision_id="fixture-order-revision-2",
            lifecycle="corrected",
            payload={
                "orderId": "synthetic-oda-order-001",
                "orderedAt": "2026-04-10T15:20:00+02:00",
                "totalNok": 49.50,
                "lines": [
                    {
                        "sourceName": "Fictional gulrotbånd / carrot ribbons",
                        "quantity": 1,
                        "unit": "stk",
                        "amountNok": 14.50,
                    },
                    {
                        "sourceName": "Fictional grain parcel",
                        "quantity": None,
                        "unit": None,
                        "amountNok": 14.00,
                    },
                ],
            },
        ),
        SyntheticReceiptRevision(
            provider="oda",
            external_id="synthetic-oda-refund-001",
            revision_id="fixture-refund-oda-001",
            lifecycle="refund_scenario",
            payload={
                "orderId": "synthetic-oda-refund-001",
                "orderedAt": "2026-04-11T10:00:00+02:00",
                "totalNok": 4.00,
                "lines": [
                    {
                        "sourceName": "Fictional return adjustment",
                        "quantity": 1,
                        "unit": "stk",
                        "amountNok": 4.00,
                    }
                ],
            },
        ),
    )


def history_failure_scenarios() -> tuple[SyntheticHistoryFailure, ...]:
    """Return failure classes for fake-reader tests, without invented HTTP codes."""
    return (
        SyntheticHistoryFailure(provider="coop", kind="authentication"),
        SyntheticHistoryFailure(provider="trumf", kind="rate_limit"),
        SyntheticHistoryFailure(provider="coop", kind="unsupported_contract"),
    )
