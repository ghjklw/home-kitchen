# Coop Medlem App - HTTP API Receipt Endpoints

This document details the HTTP API endpoints used by the Coop Medlem app (`no.coop.members`, version 4.42.0) for fetching purchase history and digital receipts.

## Overview

- **Base URL**: `https://api.coop.no`
- **Authentication**: Bearer token (`Authorization: Bearer <access_token>`) or Session Cookie (`Cookie: <session_cookie>`)
- **Required Headers**:
  - `Accept: application/json`
  - `User-Agent: HomeKitchen/1.0` (or Coop App User-Agent)

---

## Endpoint Reference Table

| Method | URL | Headers | Params | Sample Request | Sample Response | Description |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **GET** | `/user/pay/history/list` | `Authorization: Bearer <token>`<br>`Accept: application/json` | None | `GET /user/pay/history/list HTTP/1.1`<br>`Host: api.coop.no` | `{"purchases": [{"summaryId": "sum-123", "purchaseDate": "2026-10-15T14:30:00Z", "storeName": "Coop Mega Storo", "storeId": "1234", "chainId": "coop", "amount": 349.50, "totalDiscount": 25.00}]}` | Retrieves the list of purchase summary stubs for the authenticated member. |
| **GET** | `/user/pay/history/details` | `Authorization: Bearer <token>`<br>`Accept: application/json` | `summaryId` (string) | `GET /user/pay/history/details?summaryId=sum-123 HTTP/1.1`<br>`Host: api.coop.no` | `{"summary": {"summaryId": "sum-123", "receiptId": "rec-9876", "purchaseDate": "2026-10-15T14:30:00Z", "storeName": "Coop Mega Storo", "storeId": "1234", "chainId": "coop", "amount": 349.50, "totalDiscount": 25.00, "memberBonus": 10.50, "lines": [{"productName": "Tine Melk 1L", "ean13": "7038010000101", "barcode": "7038010000101", "quantity": 2, "amount": 42.00, "discount": 0.00, "unitPrice": 21.00}]}}` | Retrieves full receipt details including line items, barcodes (EAN-13), discounts, and member bonus for a specific purchase summary ID. |

---

## Error Handling & Status Codes

- **401 Unauthorized / 403 Forbidden**: Token expired or invalid session. Requires re-authentication via OIDC / login flow.
- **404 Not Found**: The requested receipt summary ID does not exist or is inaccessible.
- **5xx / Other >= 400**: Provider error or unexpected contract shape (`UnsupportedCoopContract`).
