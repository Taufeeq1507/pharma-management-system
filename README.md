# PharmaCare ERP

A full-stack pharmacy management system built with **Django REST Framework** and **React 18**. Handles the complete operational workflow of an independent pharmacy — from purchase to sale, warehouse to GST filing — with a focus on Indian GST compliance.

---

## Features

### Billing & Sales
- Fast counter billing with FEFO (First-Expiry-First-Out) stock deduction
- Payment modes: Cash, UPI, Credit, Split (any combination)
- B2B and B2C customer support with registered party profiles
- Free scheme items (Buy X Get Y) — deduct stock, zero GST impact
- Same-window exchange (return + sale on one bill, capped at ₹500)
- Drug schedule compliance — Narcotic drugs require customer name and address
- FY-aware sequential invoice numbering (INV/2025-26/00001)
- Frozen bill snapshot — reprint-safe even after medicine master changes

### GST Compliance
- Two-pass bill-level discount distribution **pre-GST** (CGST Act §15(3)(a))
- Automatic intra-state (CGST+SGST) vs inter-state (IGST) routing
- HSN code and UQC frozen on each sale line for accurate GSTR-1 Table 12
- Sequential credit note numbering (CN/2025-26/00001)
- GSTR-1 and GSTR-3B report with net-of-returns tax figures

### GST Report Covers
| Report | Tables |
|--------|--------|
| GSTR-3B | Table 3.1 (outward, net of credit notes), Table 4A (ITC), Table 4B1 (ITC reversal — purchase returns), Table 4B2 (ITC reversal — write-offs §17(5)(h)) |
| GSTR-1 | Table 4 (B2B invoices), Table 7 (B2C consolidated), Table 8 (nil-rated), Table 9B (B2B credit notes), Table 12 (HSN summary) |

### Inventory & Purchase
- Supplier management with soft-delete
- Purchase bill entry with two-pass discount distribution
- FEFO batch management — quantities always in individual units (tablets)
- Batch-level MRP and GST% tracking
- Stock search by medicine name, salt name, or barcode

### Warehouse Management
- Named block and shelf system (e.g. Block A, Shelf 3 → "A-3")
- One medicine per batch per shelf constraint (prevents FEFO confusion)
- Physical stock sync — submit actual counts, server computes delta and ITC reversal
- Unassigned batch queue for new stock pending placement
- Immutable adjustment log with GST ITC reversal amounts

### Customer Ledger & Receivables
- Double-entry immutable ledger (Sale → Debit, Payment/Return → Credit)
- Credit limit enforcement under database lock
- FIFO payment allocation across oldest unpaid invoices
- Targeted allocation — pin a payment to a specific invoice
- Full ledger statement with date range filtering

### Supplier Ledger & Payables
- Mirror of customer ledger on the payables side
- FIFO allocation across oldest unpaid purchase bills
- Purchase returns with supplier credit note → ITC reversal
- Purchase returns without credit note → fresh outward supply (adds to GST output tax)

### Returns
- **Sales returns:** Linked to original invoice and exact batch; partial returns supported; refund modes: CASH / UPI / CREDIT_NOTE
- **Purchase returns:** Server-computed refund from original purchase rate; GST treatment depends on whether supplier issues a credit note

### Cash Book
- Aggregates BillPaymentLine (billing collections) + PaymentReceipt (later payments)
- Cash/UPI refunds automatically appear as negative lines
- Grouped by payment mode with net grand total

### Expiry & Return Alerts *(model-ready, Celery task built)*
- Per-supplier configurable return window (e.g. Sun Pharma: 90 days before expiry)
- Alert fires `advance_notice_days` before the return deadline
- GST intelligence: ITC at risk, GST quarter deadline warning
- Sell-vs-return recommendation based on sales velocity

---

## Tech Stack

**Backend**
- Python 3.12 / Django 6.0 / Django REST Framework 3.16
- PostgreSQL
- SimpleJWT for authentication
- Gunicorn + WhiteNoise for production

**Frontend**
- React 19 / Vite 8
- Redux Toolkit + React Router v7
- Recharts for dashboard charts
- Axios for API calls

---

## Architecture

```
Browser (React + Redux)
    │  JWT + REST/JSON
    ▼
Django REST Framework
    ├── accounts/   — Auth, multi-tenancy, user roles
    ├── billing/    — Sales, returns, customers, GST report
    └── inventory/  — Medicines, purchases, warehouse, suppliers
    │
    ▼
PostgreSQL  (row-level tenancy via pharmacy_id FK)
```

### Multi-Tenancy
Every data row belongs to a `Pharmacy`. A custom Django manager automatically filters all queries to the current pharmacy from thread-local context — no data leaks between tenants.

Hierarchy: `Organization` → `Pharmacy` (branch) → All records. Supports both standalone pharmacies and multi-branch chains.

### User Roles

| Level | Role | Access |
|-------|------|--------|
| 1 | Clerk | Billing, stock search, customer lookup |
| 2 | Owner | Full access to one pharmacy |
| 4 | Chain Owner | Multi-branch access |
| 5 | SaaS Admin | Unrestricted |

---

## Project Structure

```
├── accounts/          — Auth, Pharmacy, Organization, CustomUser
├── billing/           — SalesBill, SalesReturn, CustomerParty, LedgerEntry, GST report
├── inventory/         — Supplier, MedicineMaster, PurchaseBill, InventoryBatch, Warehouse
├── backend_core/      — Django settings, root URL config
├── Frontend/
│   └── src/
│       ├── pages/     — Billing, History, Inventory, Warehouse, Ledger, GST, Suppliers, Dashboard
│       ├── api/       — Axios API layer
│       └── store/     — Redux slices
├── ERP_MASTER_REFERENCE.md  — Full business logic reference (no code required)
├── requirements.txt
└── docker-compose.yml
```

---

## API Overview

| Module | Base URL | Key Endpoints |
|--------|----------|--------------|
| Auth | `/api/auth/` | `POST /token/`, `POST /token/refresh/` |
| Billing | `/api/billing/` | `checkout/`, `history/`, `return/`, `receipt/`, `gst-report/`, `cash-book/`, `ledger/<id>/` |
| Customers | `/api/billing/customers/` | List, search, create |
| Inventory | `/api/inventory/` | `medicines/`, `purchase/`, `stock/`, `search/`, `return/` |
| Warehouse | `/api/inventory/` | `blocks/`, `shelves/`, `batches/unassigned/`, `sync/` |
| Suppliers | `/api/inventory/suppliers/` | CRUD, `payment/`, `ledger/` |

---

## Local Setup

### Backend

```bash
# Clone and set up virtualenv
git clone https://github.com/your-username/pharma-erp.git
cd pharma-erp
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Environment variables (.env)
SECRET_KEY=your-secret-key
DATABASE_URL=postgres://user:password@localhost:5432/pharmadb
DEBUG=True

# Migrate and run
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

### Frontend

```bash
cd Frontend/temp-pos/Temprorary_PMS
npm install
npm run dev
```

### Docker (optional)

```bash
docker-compose up --build
```

---

## Key Business Logic Decisions

**Why quantities are always in tablets, not strips:**
Storing individual units eliminates conversion bugs. Strips are a UI concept only — the server converts using `pack_qty` at inbound and FEFO deduction time.

**Why GST is calculated before applying bill discount:**
CGST Act §15(3)(a) requires trade discounts given at time of supply to reduce taxable value. Subtracting a bill-level discount post-tax would overstate output tax and ITC.

**Why the ledger is immutable:**
Mutable balances can be corrupted by concurrent updates and leave no audit trail. Every balance change writes an immutable `LedgerEntry` row. A CA can reconstruct any balance for any date range from the ledger alone.

**Why FEFO uses `select_for_update()`:**
Without a database-level lock, two concurrent sales of the same medicine can both read the same `available_quantity` and together oversell — producing negative stock. The lock serializes access to each batch row.

---

## Roadmap

- [ ] Thermal receipt printer (ESC/POS)
- [ ] Barcode scanner integration (field exists, UI pending)
- [ ] Expiry alert dashboard (backend complete)
- [ ] Reorder level alerts
- [ ] Drug scheme management (1+1, brand discounts)
- [ ] Opening balance import for Marg/GoFrugal migration
- [ ] Narcotic register (Form 6/7/8)
- [ ] Offline mode (PWA / local sync)
- [ ] GST portal direct filing API
- [ ] Profit & Loss report
- [ ] Test suite

---


