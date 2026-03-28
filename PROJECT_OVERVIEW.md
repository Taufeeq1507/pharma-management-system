# Pharma Management System — Project Overview

## Tech Stack

**Backend:** Django 6.0.3, Django REST Framework 3.16.1, PostgreSQL, SimpleJWT
**Frontend:** React 19.2.4, Vite 8.0.1, React Router 7, Axios, Recharts
**Deployment:** Gunicorn + WhiteNoise, Railway/any PostgreSQL host


---

## Architecture

### Multi-Tenancy Model

Every data model extends `TenantModel` (abstract base class), which:
- Automatically scopes all queries to the currently authenticated pharmacy via `PharmacyManager`
- Uses thread-local storage (set by `PharmacyMiddleware` on each JWT-authenticated request) to inject the pharmacy context
- Prevents any cross-pharmacy data leakage at the ORM level without requiring manual `.filter(pharmacy=...)` on every query

This means adding a new model is as simple as extending `TenantModel` — multi-tenancy is handled automatically.

### Privilege Levels

| Level | Role         | Access                                      |
|-------|--------------|---------------------------------------------|
| 1     | Clerk        | Billing, sales history, medicine search     |
| 2     | Branch Owner | Full access to one pharmacy branch          |
| 3     | Support      | Read/write pharmacy data, no ownership ops  |
| 4     | Chain Owner  | Manages multiple branches under one org     |
| 5     | SaaS Admin   | Full platform access, all pharmacies        |

### Organization > Pharmacy > User hierarchy

- `Organization`: Top-level entity (a pharmacy chain or standalone owner's umbrella)
- `Pharmacy`: One branch. Can belong to an organization.
- `CustomUser`: Linked to both a `Pharmacy` (their home branch) and an `Organization`

---

## Backend Features

### Accounts App

- JWT authentication via SimpleJWT (60-min access, 7-day rotating refresh, blacklist on logout)
- Phone number as the login identifier (no username)
- `RegisterPharmacyView`: Creates Organization + Pharmacy + Owner in a single atomic transaction
- `PharmacyMiddleware`: Extracts JWT on every request, populates thread-local pharmacy/org context
- Staff creation endpoint: Owner can create Clerk accounts for their pharmacy
- `UpdatePharmacyView`: Owner can update pharmacy GSTIN, drug license, etc.

### Inventory App

**Master Data**
- `MedicineMaster`: Drug catalog with name, company, category, HSN code, packaging (e.g. 1x10), pack qty (strips to tablets conversion), default GST%, soft-delete
- `Supplier`: Vendor master with state (for GST calc), GSTIN, contact info, soft-delete

**Purchase Flow**
- `PurchaseBill`: Supplier invoice — subtotal, total_tax (CGST/SGST/IGST breakdown), discount, grand_total all computed server-side
- `PurchaseItem`: Line items on a bill — rate, discount%, GST%, batch, expiry, free qty
- Bills are immutable once posted (no PATCH/DELETE)
- On bill creation: atomically upserts `InventoryBatch` — if same batch+MRP already exists, increments quantity; otherwise creates new row
- Unit conversion: financial calc uses strips, stock is stored in individual tablets (`qty * pack_qty`)

**GST Logic**
- Intra-state sale: CGST + SGST (50/50 split)
- Inter-state sale: IGST (full amount)
- Determined by comparing `supplier.state` vs `pharmacy.state` on purchase, and `customer.gstin[:2]` vs `pharmacy.gstin[:2]` on billing
- Billing tax rate sourced from active `MedicineMaster.default_gst_percentage` (not historical batch value — legally correct)

**Live Stock**
- `InventoryBatch`: Current available quantity per batch, tracked in individual tablets, includes MRP, GST%, shelf location FK
- FEFO ordering: `order_by('expiry_date')` ensures oldest expiry is always sold first
- `StockAdjustment`: Immutable audit log — every quantity change creates a record with old_qty, new_qty, delta, who adjusted, when, and source (SYNC or MANUAL)

**Warehouse**
- `WarehouseBlock`: Named rack sections (Block A, B, C...) with configurable shelf count and optional label
- `ShelfLocation`: Individual shelves, addressed as `{block_letter}-{shelf_number}` (e.g. A-3), created on-demand at first assignment
- Shelf constraint: One medicine may only appear under ONE batch number per shelf. Same medicine, different batch = hard block
- `ShelfAssignView`: Assigns/moves a batch to a shelf address with full constraint validation
- `StockSyncView`: Physical count sync — accepts counted quantities for all batches on one shelf, creates adjustment records, updates stock atomically

**Returns to Supplier**
- `PurchaseReturn`: Debit note to supplier, deducts from `InventoryBatch` atomically
- Validates return quantity is a multiple of `pack_qty` (strip-level returns)
- `SupplierReturnPolicy`: Per-supplier return window (days before expiry) and GST credit eligibility
- `ReturnAlert`: Auto-generated alerts for batches approaching their return deadline, with GST credit-at-risk calculation and sell-vs-return recommendation

### Billing App

**Checkout (POS)**
- Full FEFO resolution: for each medicine, iterates batches ordered by expiry date, deducts billed + free quantities
- Concurrent protection: `select_for_update()` locks all relevant batches before deduction — no race conditions
- Free scheme support: `free_quantity` field on each line item; deducted from stock but excluded from financial calculations
- Per-line discount percentage
- Bill-level discount (flat amount)
- Payment modes: CASH, UPI, CREDIT
- Creates frozen `items_snapshot` JSON on every bill (used for display/printing; never changes even if stock data changes later)
- `SalesItem` rows store per-batch granularity — enabling accurate per-batch stock restoration on returns

**Customer & Payment Tracking (ERP Layer)**
- `CustomerParty`: Unified B2B (clinics, pharmacies) and B2C (retail patients) profile
  - B2B customers have GSTIN, enabling interstate GST calculation on sales
  - Credit limit and outstanding balance tracking
- Credit sales: `outstanding_balance` on customer is incremented atomically under `select_for_update()` lock
- `PaymentReceipt`: Logs money received from a customer
- `PaymentAllocation`: Maps receipt amount to specific invoices (FIFO auto-allocation — oldest unpaid bills cleared first)
- Bill `payment_status` auto-updates: UNPAID → PARTIAL → PAID as allocations are applied

**Sales Returns**
- Partial returns supported: validates `already_returned + new_return_qty <= original_qty`
- Stock restored to the exact `InventoryBatch` it was sold from (identified via `SalesItem.inventory_batch` FK)
- `select_for_update()` on batch prevents concurrent return race conditions

**Sales History**
- Filter by customer phone or name
- Full bill detail with line items
- Per-item return button (triggers return modal)

---

## Frontend Features

### Auth Flow
- Tokens stored in `sessionStorage` (cleared on tab close)
- Auto-refresh on 401 via Axios response interceptor — retries the original request with new token transparently
- `AuthContext` hydrates user state from `/api/accounts/me/` on page load
- Role-based routing: Clerks go to `/billing`, Owners go to `/dashboard`

### Pages

| Page       | Access  | Description                                                      |
|------------|---------|------------------------------------------------------------------|
| Login      | Public  | Phone + password, redirects by role                             |
| Register   | Public  | Creates pharmacy+owner; toggle for Standalone vs Chain          |
| Dashboard  | Owner+  | Revenue metrics, expiry watch, stock summary, 7-day sales chart |
| Billing    | Clerk+  | POS with medicine search, cart, discount, payment mode, receipt |
| Inventory  | Owner+  | 3 tabs: Stock (with filters), Medicines master, Purchase bills   |
| Warehouse  | Owner+  | Blocks, unassigned batches queue, shelf contents view           |
| Suppliers  | Owner+  | Supplier list, add form                                         |
| History    | Clerk+  | Bill list with search, bill detail panel, return modal          |
| Staff      | Owner+  | List all staff, create clerk accounts                           |

### Responsive Layout
- Desktop (≥1024px): Full sidebar with labels
- Tablet (768–1023px): Collapsible icon-only sidebar
- Mobile (<768px): Top bar + bottom navigation bar (5 items max)
- `useWindowSize` hook drives all breakpoint logic

### Key UX Patterns
- Billing: Debounced medicine search (300ms), click-to-add batches, inline quantity +/- controls, real-time total calculation
- Inventory: Inline medicine and supplier creation while filling a purchase bill (no need to leave the form)
- Warehouse: Blocks → click → shelves drill-down, inline assign controls on unassigned batch rows
- Dashboard: Recharts BarChart for 7-day revenue, expiry progress bars with urgency coloring

---

## API Summary

Base path: `/api/`

### Accounts `/api/accounts/`
- `POST register/` — Create pharmacy + owner
- `POST login/` — Get JWT tokens
- `POST logout/` — Blacklist refresh token
- `POST token/refresh/` — Refresh access token
- `GET me/` — Current user + pharmacy
- `GET/PATCH pharmacy/` — Pharmacy settings
- `GET/POST staff/` — List/create staff

### Inventory `/api/inventory/`
- `GET/POST suppliers/` — Supplier master
- `GET/POST medicines/` — Medicine master
- `GET/POST purchase/` — Purchase bills
- `GET purchase/{id}/` — Bill detail with items
- `GET stock/` — Live batches (filter: `?medicine=uuid`)
- `GET search/?q=` — Medicine search for POS
- `POST return/` — Debit note + stock deduction
- `GET/POST blocks/` — Warehouse blocks
- `GET/PATCH blocks/{id}/` — Block detail/resize
- `GET blocks/{letter}/shelves/` — Shelves in a block
- `GET shelves/{id}/` — Single shelf with batches
- `GET batches/unassigned/` — Unplaced stock queue
- `POST batches/{id}/assign/` — Assign batch to shelf
- `POST sync/` — Physical stock count sync
- `GET adjustments/` — Adjustment history

### Billing `/api/billing/`
- `POST checkout/` — Full POS transaction
- `GET history/` — Sales bills (filter: `?customer_phone=`)
- `GET history/{id}/` — Bill detail
- `GET customer/{phone}/` — Customer bill history
- `POST return/` — Customer return (credit note)

### Docs
- `GET /api/docs/` — Swagger UI (drf-spectacular)
- `GET /api/redoc/` — ReDoc
- `GET /api/schema/` — OpenAPI 3.0 JSON

---

## Data Integrity Guarantees

- All writes that touch multiple tables are wrapped in `transaction.atomic()`
- All concurrent writes (checkout, returns, payments) use `select_for_update()` row locking
- Soft deletes on Suppliers and Medicines (historical bills/batches remain valid)
- Bills are immutable once posted (purchase bills, sales bills)
- `items_snapshot` JSON frozen at sale time — audit trail independent of future data changes
- `StockAdjustment` rows are never modified or deleted after creation
- Unique constraints: invoice number per supplier per pharmacy, batch+MRP per pharmacy, shelf per block per pharmacy, customer phone per pharmacy

---

## Scaling Potential

### Current Architecture Limitations
- Single-server Django (Gunicorn workers share thread-local state — safe per request but no async)
- Thread-locals for pharmacy context work correctly per-request but need care if switching to async views
- No caching layer — every request hits PostgreSQL

### Horizontal Scaling Path

**Short term (0–1k pharmacies)**
- Deploy as-is on Railway/Render with a managed PostgreSQL instance
- Add `DATABASE_URL` pooler (PgBouncer) to handle connection limits
- Add Redis for JWT token blacklist (replace DB blacklist table) — reduces write load significantly

**Medium term (1k–10k pharmacies)**
- Add Redis + `django-cachalot` or manual cache for medicine master lookups (medicine data changes rarely)
- Move `StockAdjustment` inserts to a Celery task queue — fire-and-forget audit trail, unblocking the checkout critical path
- Add read replicas; route `GET` requests to replica (stock views, history, search)
- Celery workers for `ReturnAlert` generation (nightly batch job) and any future scheduled tasks

**Long term (10k+ pharmacies)**
- Partition `SalesBill`, `SalesItem`, `StockAdjustment` tables by `pharmacy_id` — natural partition key already exists on every row
- Consider sharding by organization (all branches of one chain on same shard = no cross-shard joins)
- Replace medicine search with Elasticsearch/Typesense for sub-10ms autocomplete at scale
- Extract billing into a separate Django app/service (it already has clean boundaries: no imports from billing in inventory)
- Move `items_snapshot` JSON to a document store (MongoDB/DynamoDB) for cheaper reads on bill history

### SaaS Billing Integration Points
- `Organization.subscription_plan` field already exists
- `Pharmacy.subscription_plan` exists for per-branch overrides
- Feature flags can be stored in `Pharmacy.settings` (JSONField) — no schema migration needed to add per-pharmacy flags
- ChainOwner (level 4) already segregated for enterprise-tier feature gates

### What Scales Well Today
- Multi-tenancy is zero-overhead: PharmacyManager filters are SQL `WHERE pharmacy_id = ?` — PostgreSQL index on `pharmacy` FK on every table handles this efficiently
- No N+1 queries on critical paths: `prefetch_related` and `select_related` used throughout viewsets
- Atomic checkout is bottlenecked per-pharmacy, not globally — two different pharmacies never contend for the same rows
- UUID primary keys: safe for future distributed ID generation (no auto-increment coordination needed)

---

## Environment Variables

| Variable              | Required | Description                                    |
|-----------------------|----------|------------------------------------------------|
| `SECRET_KEY`          | Yes      | Django secret key                              |
| `DEBUG`               | Yes      | True for dev, False for prod                   |
| `ALLOWED_HOSTS`       | Yes      | Comma-separated hostnames                      |
| `DATABASE_URL`        | No       | Full DB URL (Railway auto-provides)            |
| `DB_NAME/USER/PASSWORD/HOST/PORT` | No | Individual DB params (local dev)    |
| `CORS_ALLOWED_ORIGINS`| Yes      | Comma-separated frontend origins               |
| `CORS_ALLOW_ALL_ORIGINS` | No    | Set True only in local dev                     |
| `VITE_API_BASE_URL`   | Yes      | Backend URL for frontend Axios client          |

---

## Known Gaps / Future Work

- No Celery/beat setup yet — `ReturnAlert` generation is modeled but not scheduled
- No email/SMS notifications
- No print-ready bill template (receipt data is available via `items_snapshot`)
- Chain Owner dashboard (cross-branch analytics) not yet built — data model supports it
- Payment allocation currently FIFO-only — no manual allocation UI
- No rate limiting on public endpoints (register, login)
- No audit log for user actions beyond stock adjustments
