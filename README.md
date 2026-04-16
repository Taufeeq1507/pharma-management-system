# PharmaCare ERP — Backend

A production-grade pharmacy management REST API built with **Django REST Framework** and **PostgreSQL**. Covers the complete operational workflow of an independent Indian pharmacy — purchase, billing, warehouse, accounting, and GST compliance — with a focus on correctness under concurrent load.

---

## Live Demo

🔗 **Frontend:** https://temprorary-pms.vercel.app/login

📱 **Demo credentials** — Phone: `9999999999` | Password: `demo1234`

🖥️ **Frontend repo:** https://github.com/Taufeeq1507/pharma-management-system

> This repo covers the backend only. The frontend is a separate React + Redux Toolkit SPA consuming these APIs.

---

## Tech Stack

- **Python 3.12** / **Django 6.0** / **Django REST Framework 3.16**
- **PostgreSQL** — row-level multi-tenancy
- **SimpleJWT** — phone-number-based JWT authentication
- **Gunicorn + WhiteNoise** — production serving

---

## Architecture

### Multi-Tenancy

Every data row belongs to a `Pharmacy`. A custom Django manager (`PharmacyManager`) automatically appends `.filter(pharmacy=current_pharmacy)` to every queryset using thread-local context — no cross-tenant data leaks at the ORM level.

```
Organization  (pharmacy chain / group)
    └── Pharmacy  (one branch or standalone store)
            └── All records (medicines, bills, customers …)
```

### User Roles

| Level | Role | Scope |
|-------|------|-------|
| 1 | Clerk | Billing, stock search |
| 2 | Owner | Full access to one pharmacy |
| 4 | Chain Owner | Across all branches in their organization |
| 5 | SaaS Admin | Unrestricted |

---

## Modules

### Billing & Sales

- FEFO stock deduction under `select_for_update()` — prevents concurrent oversell
- Payment modes: Cash, UPI, Credit, Split (any combination)
- Two-pass bill-level discount distribution **pre-GST** per CGST Act §15(3)(a)
- GST back-calculated from MRP; intra/inter-state routed via GSTIN prefix comparison
- Free scheme quantities (Buy X Get Y) — deduct stock, zero revenue impact
- FY-aware sequential invoice numbering (`INV/2025-26/00001`) with auto-reset on April 1
- Frozen JSON snapshot on every bill — reprint-safe even after master data changes
- Same-window exchange (negative-quantity items on a bill, ≤₹500 cap)
- Drug schedule compliance — Narcotic items enforce customer name + address at server level

### Inventory & Purchase

- Supplier master with soft-delete and running payable balance
- Purchase bill with identical two-pass discount distribution and automatic batch upsert
- Stock always stored in **individual units (tablets)**; strips↔tablets conversion via `pack_qty`
- Atomic stock increment using `F()` expressions — safe under concurrent purchase bills

### Warehouse Management

- Block → Shelf → Batch hierarchy (e.g. Block A, Shelf 3)
- One medicine per batch per shelf constraint — enforced at application layer
- Physical stock sync: submit actual counts, server computes delta, writes immutable `StockAdjustment`, triggers GST ITC reversal calculation for shrinkage (§17(5)(h))

### Sales Returns & Credit Notes

- Linked to the original invoice and exact source batch — no guessing on stock restoration
- Partial returns supported across multiple requests
- `refund_mode` field: CASH / UPI / CREDIT_NOTE — each drives different cash-book and ledger entries
- Auto-detected for single-mode bills; required from clerk for SPLIT bills
- Sequential credit note numbering (`CN/2025-26/00001`)
- Proportional GST breakdown preserved on every credit note

### Purchase Returns

- Refund amount computed server-side from original `PurchaseItem.purchase_rate_base` — client cannot supply an arbitrary figure
- Two GST paths: with supplier credit note → ITC reversal (GSTR-3B Table 4B1); without → treated as fresh outward supply (adds to output tax)

### Customer Ledger & Receivables

- Immutable double-entry `LedgerEntry` on every balance change (Sale → Debit, Payment/Return → Credit)
- Credit limit enforced under `select_for_update()` before any credit sale
- FIFO payment allocation across oldest unpaid invoices; targeted allocation to a specific invoice also supported
- Full ledger statement reconstructible for any date range from the entry log alone

### Supplier Ledger & Payables

- Mirror of customer ledger on the payables side
- FIFO allocation across oldest unpaid purchase bills on every supplier payment

### Cash Book

- Aggregates `BillPaymentLine` (billing-time collections) and `PaymentReceipt` (later payments)
- `CASH_REFUND` / `UPI_REFUND` lines carry negative amounts — cash book net is correct without any application-layer arithmetic
- Grouped by payment mode; grand total = net cash in till

### GST Report (GSTR-1 & GSTR-3B)

| Return | Tables Produced |
|--------|----------------|
| GSTR-3B | Table 3.1 outward taxable (net of credit notes, intra/inter split); Table 4A ITC from purchases; Table 4B1 ITC reversal (purchase returns with CN); Table 4B2 ITC reversal (stock write-offs §17(5)(h)) |
| GSTR-1 | Table 4 B2B invoices; Table 7 B2C consolidated; Table 8 nil-rated/exempt; Table 9B B2B credit notes; Table 12 HSN summary with UQC quantity conversion |

**Key correctness points:**

- Outward taxable aggregated at `SalesItem` level (not bill level) — excludes 0% items on mixed bills
- Credit notes netted from both taxable value **and** tax in GSTR-3B Table 3.1
- Unregistered B2B (no GSTIN) correctly classified as B2C in all tables
- HSN quantities converted to UQC units using frozen `pack_qty` from sale time

---

## API Reference

### Billing — `/api/billing/`

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/checkout/` | Create a sale bill |
| GET | `/history/` | All bills, newest first (`?customer_phone=`) |
| GET | `/history/<uuid>/` | Full bill detail |
| GET | `/customer/<phone>/` | All bills for a phone number |
| GET/POST | `/customers/` | Search / register customers |
| POST | `/return/` | Process a sales return |
| POST | `/receipt/` | Record customer payment |
| GET | `/gst-report/` | GSTR-1 + GSTR-3B (`?from=`, `?to=`) |
| GET | `/ledger/<customer_id>/` | Customer ledger statement |
| GET | `/cash-book/` | Cash book by mode (`?from_date=`, `?to_date=`) |

### Inventory — `/api/inventory/`

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET/POST | `/medicines/` | Medicine master |
| GET/POST | `/purchase/` | Purchase bills |
| GET | `/stock/` | Live stock with batch detail |
| GET | `/search/` | Medicine search (`?q=`) |
| POST | `/return/` | Purchase return to supplier |
| GET/POST | `/suppliers/` | Supplier master |
| POST | `/suppliers/<id>/payment/` | Record supplier payment |
| GET | `/suppliers/<id>/ledger/` | Supplier ledger statement |
| GET/POST | `/blocks/` | Warehouse blocks |
| GET | `/blocks/<letter>/shelves/` | Shelves with assigned batches |
| GET | `/batches/unassigned/` | Batches pending shelf placement |
| POST | `/batches/<id>/assign/` | Assign batch to shelf |
| POST | `/sync/` | Physical stock count sync |
| GET | `/adjustments/` | Stock adjustment history |

---

## Data Integrity Highlights

- All multi-step writes are wrapped in `transaction.atomic()`
- Consistent lock acquisition order prevents deadlocks across concurrent requests
- `LedgerEntry`, `SupplierLedgerEntry`, `StockAdjustment`, `SalesItem`, `PaymentAllocation` — immutable after creation
- Concurrent stock deduction uses `select_for_update()` on `InventoryBatch` rows
- Concurrent invoice numbering uses `select_for_update()` on `Pharmacy` row with FY-rollover detection
- Stock increments on purchase use `F('available_quantity') + n` — atomic at DB level

---

## Roadmap

- [ ] Thermal receipt printer support (ESC/POS)
- [ ] Barcode scanner integration (`barcode` field exists on medicine master)
- [ ] Expiry / return alert dashboard (models + Celery task built, API pending)
- [ ] Opening balance import for Marg/GoFrugal migration
- [ ] Narcotic register (Form 6/7/8 compliance)
- [ ] GST portal direct filing API
- [ ] Test suite

---

