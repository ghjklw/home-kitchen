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
| **GET** | `/user/pay/history/list` | `X-Token: <token>`<br>`Accept: application/json;charset=UTF-8`<br>`Content-Type: application/json; charset=utf-8`<br>`User-Agent: Android(14)/Coop(4.42.0)/2.7.7`<br>`X-DeviceId: <device_id>`<br>`X-GLTLocale: nb_NO` | `n` (integer, e.g. `50`) | `GET /user/pay/history/list?n=50 HTTP/1.1`<br>`Host: coopay.coop.no` | `{"purchases": [{"summaryId": "sum-123", "purchaseDate": "2026-10-15T14:30:00Z", "storeName": "Coop Mega Storo", "storeId": "1234", "chainId": "coop", "amount": 349.50, "totalDiscount": 25.00}]}` | Retrieves the list of purchase summary stubs for the authenticated member. |
| **GET** | `/user/pay/history/search` | Same Coopay headers | `text` (string) | `GET /user/pay/history/search?text= HTTP/1.1`<br>`Host: coopay.coop.no` | `{"purchases": [...]}` | Searches purchase summary stubs by text. |
| **GET** | `/user/pay/history/search/continue` | Same Coopay headers | `token` (string) | `GET /user/pay/history/search/continue?token=<token> HTTP/1.1`<br>`Host: coopay.coop.no` | `{"purchases": [...]}` | Fetches the next search page using a continuation token. |
| **GET** | `/user/pay/history/month` | Same Coopay headers | `year`, `month` (integers) | `GET /user/pay/history/month?year=2026&month=11 HTTP/1.1`<br>`Host: coopay.coop.no` | `{"purchases": [...]}` | Retrieves purchase summary stubs for a specific month. |
| **GET** | `/user/pay/history/dashboard` | Same Coopay headers | None | `GET /user/pay/history/dashboard HTTP/1.1`<br>`Host: coopay.coop.no` | `{"monthlyBreakdown": [{"month": "2026-11", "purchases": [...]}]}` | Retrieves spending dashboard overview with monthly breakdowns. |
| **GET** | `/user/pay/history/details` | Same Coopay headers | `summaryId` (string) | `GET /user/pay/history/details?summaryId=sum-123 HTTP/1.1`<br>`Host: coopay.coop.no` | `{"summary": {"summaryId": "sum-123", "receiptId": "rec-9876", "purchaseDate": "2026-10-15T14:30:00Z", "storeName": "Coop Mega Storo", "storeId": "1234", "chainId": "coop", "amount": 349.50, "totalDiscount": 25.00, "memberBonus": 10.50, "lines": [{"productName": "Tine Melk 1L", "ean13": "7038010000101", "barcode": "7038010000101", "quantity": 2, "amount": 42.00, "discount": 0.00, "unitPrice": 21.00}]}}` | Retrieves full receipt details including line items, barcodes (EAN-13), discounts, and member bonus for a specific purchase summary ID. |
| **GET** | `/user/pay/history/receipt.pdf` | Same Coopay headers | `receiptId` (string) | `GET /user/pay/history/receipt.pdf?receiptId=rec-9876 HTTP/1.1`<br>`Host: coopay.coop.no` | Binary PDF (`application/pdf`) | Downloads the receipt PDF for a receipt ID. |

---

## Error Handling & Status Codes

- **401 Unauthorized / 403 Forbidden**: Token expired or invalid session (`CoopAuthenticationError`), unless `x-client-upgrade: force` is present (client `User-Agent` rejected -> `UnsupportedCoopContract`).
- **404 Not Found**: The requested receipt summary ID does not exist or is inaccessible.
- **200 `TBaseResponse` body error (`resultCode`: `PERMANENT_ERROR` / `TEMPORARY_ERROR`)**: Token-related `errorCode` values (`INVALID_TOKEN`, `NO_REFRESH_TOKEN`, `REFRESH_ACCESS_TOKEN`, `UNAUTHORIZED`, `EXPIRED_TOKEN`) raise `CoopAuthenticationError`; other error codes raise `UnsupportedCoopContract`.
- **5xx / Other >= 400**: Provider error or unexpected contract shape (`UnsupportedCoopContract`).
