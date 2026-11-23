# Coop Medlem App - HTTP API Receipt Endpoints

This document details the HTTP API endpoints used by the Coop Medlem app (`no.coop.members`, version 4.42.0) for fetching purchase history and digital receipts.

## Overview

- **Purchase History / Coopay Base URL**: `https://coopay.coop.no`
  - Note: `https://api.coop.no/appmw-service` serves general app middleware endpoints (`/user/profile`, `/user/features/simple`, `/v1/GetChainHub`), whereas `/user/pay/history/*` is served by `https://coopay.coop.no` (calling `https://api.coop.no/user/pay/history/*` returns `HTTP 404`).
- **Authentication**: `X-Token: <access_token>` (raw Auth0 access token, not `Authorization: Bearer`) or Session Cookie (`Cookie: <session_cookie>`). The Auth0 OIDC token audience remains `https://api.coop.no` (`login.coop.no`, client ID `7WrQEdeXwUudArpQVjmZEvrTgVs1WkRr`).
- **Required Headers**:
  - `Accept: application/json;charset=UTF-8`
  - `Content-Type: application/json; charset=utf-8`
  - `User-Agent: Android(14)/Coop(4.42.0)/2.7.7` (non-app `User-Agent` values such as `HomeKitchen/1.0` are rejected with `HTTP 403` and `x-client-upgrade: force`)
  - `X-DeviceId: <device_id>` (device identifier, e.g. 16-hex Android ID)
  - `X-GLTLocale: nb_NO` (`nb_NO`, `nn_NO`, or `en_GB`)

---

## Endpoint Reference Table

| Method | URL | Headers | Params | Sample Request | Sample Response | Description |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **GET** | `/user/pay/history/list` | `X-Token: <token>`<br>`Accept: application/json;charset=UTF-8`<br>`Content-Type: application/json; charset=utf-8`<br>`User-Agent: Android(14)/Coop(4.42.0)/2.7.7`<br>`X-DeviceId: <device_id>`<br>`X-GLTLocale: nb_NO` | `n` (integer, commonly `50`) | `GET /user/pay/history/list?n=50 HTTP/1.1`<br>`Host: coopay.coop.no` | `{"purchases": [{"summaryId": "sum-123", "purchaseDate": "2026-10-15T14:30:00Z", "storeName": "Coop Mega Storo", "storeId": "1234", "chainId": "coop", "amount": 349.50, "totalDiscount": 25.00}]}` | Returns a limited list. Observed `n=100` still returned 50 records, so this endpoint is not used as a complete-history source. |
| **GET** | `/user/pay/history/search` | Same Coopay headers | `text` (string; empty string starts history traversal) | `GET /user/pay/history/search?text= HTTP/1.1`<br>`Host: coopay.coop.no` | `{"hitsPerMonth": [{"monthOfYear": {"year": 2026, "month": 11}, "periodPurchases": [{"summaryId": "sum-123", "dateTime": "10.11. 18:59", "storeName": "Coop Mega Storo", "totalSum": 349.50}]}], "continuationToken": "opaque-token-or-null", "searchText": ""}` | Starts the month-grouped search traversal. The `monthOfYear` year and month provide authoritative context for summaries whose `dateTime` omits a year. |
| **GET** | `/user/pay/history/search/continue` | Same Coopay headers | `token` (string) | `GET /user/pay/history/search/continue?token=<token> HTTP/1.1`<br>`Host: coopay.coop.no` | Same `hitsPerMonth` and `continuationToken` envelope as search | Fetches the next page. Continue until `continuationToken` is null. Repeated or malformed tokens, malformed envelopes, missing summary IDs, and page-limit exhaustion are treated as incomplete scans. |
| **GET** | `/user/pay/history/month` | Same Coopay headers | `year`, `month` (integers) | `GET /user/pay/history/month?year=2026&month=11 HTTP/1.1`<br>`Host: coopay.coop.no` | `{"purchases": [...]}` | Retrieves purchase summary stubs for a specific month. |
| **GET** | `/user/pay/history/dashboard` | Same Coopay headers | None | `GET /user/pay/history/dashboard HTTP/1.1`<br>`Host: coopay.coop.no` | `{"monthlyBreakdown": [{"month": "2026-11", "purchases": [...]}]}` | Retrieves spending dashboard overview with monthly breakdowns. |
| **GET** | `/user/pay/history/details` | Same Coopay headers | `summaryId` (string) | `GET /user/pay/history/details?summaryId=sum-123 HTTP/1.1`<br>`Host: coopay.coop.no` | `{"summary": {"summaryId": "sum-123", "receiptId": "rec-9876", "dateTime": "10. november 18:59", "storeName": "Coop Mega Storo", "totalSum": 349.50, "totalDiscount": 25.00, "lines": [{"name": "Poteter", "quantity": "1.090 kg", "price": 7.63, "discount": 0.00}]}}` | Retrieves full receipt details and line-item fields for a specific purchase summary ID. |
| **GET** | `/user/pay/history/receipt.pdf` | Same Coopay headers | `receiptId` (string) | `GET /user/pay/history/receipt.pdf?receiptId=rec-9876 HTTP/1.1`<br>`Host: coopay.coop.no` | Binary PDF (`application/pdf`) | Downloads the receipt PDF for a receipt ID. |

---

## Observed live-field mapping

A sanitized 26-line receipt check showed that the sum of line price values equals totalSum, including weighed lines. Treat price as the charged amount for the whole line; keep quantity and any unit embedded in its string separate, and do not multiply price by quantity. No separate EAN/GTIN or unit field was present in the sampled response, so the importer does not invent those values.

The automated tests use mocked Coop responses. The live response shapes and line-price semantics were checked separately against the connected local integration; receipt counts vary by account and time.

## Error Handling & Status Codes

- **401 Unauthorized / 403 Forbidden**: Token expired or invalid session (`CoopAuthenticationError`), unless `x-client-upgrade: force` is present (client `User-Agent` rejected -> `UnsupportedCoopContract`).
- **404 Not Found**: A requested receipt summary ID may not exist or may be inaccessible. A 404 from a history-search page is an unsupported source response and fails the scan rather than falling back to the truncated list endpoint.
- **200 `TBaseResponse` body error (`resultCode`: `PERMANENT_ERROR` / `TEMPORARY_ERROR`)**: Token-related `errorCode` values (`INVALID_TOKEN`, `NO_REFRESH_TOKEN`, `REFRESH_ACCESS_TOKEN`, `UNAUTHORIZED`, `EXPIRED_TOKEN`) raise `CoopAuthenticationError`; other error codes raise `UnsupportedCoopContract`.
- **5xx / Other >= 400**: Provider error or unexpected contract shape (`UnsupportedCoopContract`).

## History coverage and evidence boundary

The `/user/pay/history/list` endpoint is capped at 50 results in the observed responses, even when requested with `n=100`. Complete history therefore uses `/user/pay/history/search` and follows every `continuationToken` from `/search/continue`, deduplicating repeated `summaryId` values when their month context agrees. A repeated ID under conflicting month/year context makes the scan fail visibly. The month context is also passed to receipt-detail normalization: a yearless timestamp receives the grouped year, and a timestamp whose month conflicts with its group makes the scan fail visibly.

A prior live traversal observed 105 unique summaries across 7 search pages; that count can change with account activity and is not a fixed provider guarantee. The automated tests use mocked Coop responses, including more than 50 summaries, multiple years, overlap, continuation, and malformed/truncated cases. They complement, rather than replace, the separate live checks described above.
