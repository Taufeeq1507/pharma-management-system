import uuid
from django.db import models
from django.utils import timezone
from decimal import Decimal
from accounts.models import TenantModel, CustomUser
from inventory.models import MedicineMaster, InventoryBatch


class SalesBill(TenantModel):
    """
    One customer transaction.

    Two parallel records of truth:
      - SalesItem rows      → relational, used for stock restoration on returns
      - items_snapshot JSON → frozen forever, used for display and printing
    """
    
    # --- 1. UNIFIED CUSTOMER LINK (NEW ERP FIELD) ---
    customer = models.ForeignKey(
        'CustomerParty', on_delete=models.PROTECT, related_name='bills', null=True, blank=True,
        help_text="Strict link to B2B/B2C Ledger. Leave null for guest walk-ins."
    )

    # --- 2. GUEST / WALK-IN DATA (EXISTING) ---
    customer_phone = models.CharField(max_length=15, blank=True, null=True, db_index=True)
    customer_name  = models.CharField(max_length=255, blank=True, null=True)
    # Required when any item in the bill is a Narcotic — mandatory for inspection compliance
    buyer_address  = models.CharField(max_length=500, blank=True, null=True)

    # --- Legal compliance fields (Drugs & Cosmetics Act, 1940) ---
    # Required for Schedule H: prescriber name
    # Required for Schedule H1, X, NARCOTIC: both fields
    prescriber_name   = models.CharField(max_length=255, blank=True, null=True,
                            help_text="Name of prescribing doctor — required for Sch H/H1/X/NARCOTIC")
    prescriber_reg_no = models.CharField(max_length=100, blank=True, null=True,
                            help_text="Doctor/Clinic/Hospital registration number — required for Sch H1/X/NARCOTIC")

    # --- GST Compliance Fields ---
    # Sequential GST invoice number e.g. INV/2025-26/00042 — auto-generated at checkout
    invoice_number  = models.CharField(max_length=30, blank=True, null=True, db_index=True)
    # 2-digit GST state code of the place of supply (derived from buyer GSTIN or pharmacy state)
    place_of_supply = models.CharField(max_length=2, blank=True, null=True,
                            help_text="2-digit GST state code e.g. '27' for Maharashtra")

    bill_date = models.DateTimeField(default=timezone.now)
    billed_by = models.ForeignKey(
        'accounts.CustomUser', on_delete=models.PROTECT, related_name='sales_bills'
    )

    # --- 3. FINANCIALS (EXISTING) ---
    subtotal    = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_tax   = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_cgst  = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_sgst  = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_igst  = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    discount    = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    grand_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    PAYMENT_CHOICES = [('CASH', 'Cash'), ('UPI', 'UPI'), ('CREDIT', 'Credit'), ('SPLIT', 'Split')]
    payment_mode = models.CharField(max_length=20, choices=PAYMENT_CHOICES, default='CASH')
    split_payments = models.JSONField(default=dict, blank=True)

    # --- 4. INVOICE PAYMENT TRACKING (NEW ERP FIELDS) ---
    # Tracks how much has been cleared via the PaymentReceipt + PaymentAllocation tables
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    STATUS_CHOICES = [('UNPAID', 'Unpaid'), ('PARTIAL', 'Partial'), ('PAID', 'Paid')]
    payment_status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='UNPAID')

    # --- 5. THE FROZEN SNAPSHOT (EXISTING) ---
    items_snapshot = models.JSONField(default=list)

    def __str__(self):
        # Gracefully handle the display whether it's a registered party or a guest
        display_name = self.customer.name if self.customer else (self.customer_name or self.customer_phone or 'Guest')
        return f"Bill #{str(self.id)[:8]} | {display_name} | ₹{self.grand_total} | {self.payment_status}"

class SalesItem(TenantModel):
    """
    One row per batch deduction.

    FEFO can split a single medicine request across multiple batches.
    Example: clerk asks for 30 tablets of Dolo:
      - 8 tablets from Batch B99 (expires January — soonest)
      - 22 tablets from Batch B47 (expires June)
    That produces TWO SalesItem rows.

    This per-batch granularity enables accurate stock restoration
    on returns — we know exactly which batch to restore tablets to.
    """
    sales_bill      = models.ForeignKey('SalesBill', on_delete=models.CASCADE, related_name='items')
    medicine        = models.ForeignKey('inventory.MedicineMaster', on_delete=models.PROTECT)
    inventory_batch = models.ForeignKey('inventory.InventoryBatch', on_delete=models.PROTECT, related_name='sales_items')

    # Denormalised for fast reads — saves a JOIN on every return lookup
    batch_number = models.CharField(max_length=100)
    
    # --- 1. THE BILLED QUANTITY ---
    quantity     = models.IntegerField(help_text="Tablets deducted from stock AND charged to the customer.") 
    
    # --- 2. THE FREE SCHEME QUANTITY (NEW ERP FIELD) ---
    free_quantity = models.IntegerField(
        default=0, 
        help_text="Free tablets given (Buy X Get Y). Deducts from physical stock, but does NOT affect line_total or taxes."
    )

    # Prices frozen at time of sale — independent of future InventoryBatch changes
    mrp_per_strip      = models.DecimalField(max_digits=10, decimal_places=2)
    sale_rate_per_unit = models.DecimalField(max_digits=10, decimal_places=4)  # per tablet
    discount_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    
    # Taxation breakdown
    taxable_value      = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    gst_percentage     = models.DecimalField(max_digits=5,  decimal_places=2)
    cgst_amount        = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    sgst_amount        = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    igst_amount        = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    line_total         = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        scheme_text = f" (+{self.free_quantity} Free)" if self.free_quantity > 0 else ""
        return f"{self.medicine.name} ×{self.quantity}{scheme_text} from batch {self.batch_number}"

class SalesReturn(TenantModel):
    """
    Credit note — customer returns tablets.

    Stock is restored to the EXACT InventoryBatch it was sold from,
    identified via the SalesItem FK. No guessing, no wrong batch.

    Partial returns are supported — customer can return 5 of 10 tablets
    across multiple separate return requests.
    Validation checks: already_returned + new_qty <= original_qty
    """
    sales_bill = models.ForeignKey(SalesBill, on_delete=models.PROTECT, related_name='returns', null=True, blank=True)
    sales_item = models.ForeignKey(SalesItem, on_delete=models.PROTECT, related_name='returns', null=True, blank=True)

    return_quantity = models.IntegerField()
    refund_amount   = models.DecimalField(max_digits=10, decimal_places=2)
    return_date     = models.DateField(default=timezone.now)
    reason          = models.CharField(max_length=255, default="Customer Return")

    # --- GST Credit Note Fields (for GSTR-1 Table 9B) ---
    # Auto-generated sequential credit note number e.g. CN/2025-26/00001
    credit_note_number = models.CharField(max_length=30, blank=True, null=True, db_index=True)
    cgst_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    sgst_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    igst_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    def __str__(self):
        name = self.sales_item.medicine.name if self.sales_item_id else "Unknown"
        return f"Return ×{self.return_quantity} of {name}"




class CustomerParty(TenantModel):
    """Unified master profile for BOTH B2B Wholesale Clients and B2C Retail Patients"""
    
    CUSTOMER_TYPES = [('B2B', 'Wholesale Clinic/Pharma'), ('B2C', 'Retail Patient')]
    customer_type = models.CharField(max_length=3, choices=CUSTOMER_TYPES, default='B2C')
    
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=15) # Unique constraint moved to Meta
    email = models.EmailField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    
    # Only relevant if customer_type == 'B2B'
    gstin           = models.CharField(max_length=15, blank=True, null=True)
    drug_license_no = models.CharField(
        max_length=50, blank=True, null=True,
        help_text="Drug License No. (Form 20/20B/21/21B) — mandatory for B2B pharmacy/distributor buyers under Drugs & Cosmetics Act, 1940"
    )
    
    # Financials: Handles B2B credit limits and B2C monthly tabs
    credit_limit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    outstanding_balance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    
    class Meta:
        # Ensures phone numbers are unique per pharmacy, but allows two different pharmacies on your SaaS to have the same customer
        constraints = [
            models.UniqueConstraint(fields=['pharmacy', 'phone'], name='unique_customer_phone_per_pharmacy')
        ]

    def __str__(self):
        return f"{self.name} ({self.get_customer_type_display()})"


class PaymentReceipt(TenantModel):
    """Logs money received to clear credit (Works for B2B Cheques and B2C Cash tabs)"""
    customer = models.ForeignKey(CustomerParty, on_delete=models.PROTECT, related_name='receipts')
    
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    
    PAYMENT_MODES = [('CASH', 'Cash'), ('UPI', 'UPI/NEFT'), ('CHEQUE', 'Cheque'), ('BANK_TRANSFER', 'Bank Transfer')]
    payment_mode = models.CharField(max_length=20, choices=PAYMENT_MODES, default='CASH')
    
    reference_number = models.CharField(max_length=100, blank=True, null=True, help_text="Cheque No. or UTR")
    receipt_date = models.DateField(default=timezone.now)
    notes = models.TextField(blank=True, null=True)
    
    # Tracks how much of this receipt has been applied to specific invoices
    amount_allocated = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))


class PaymentAllocation(TenantModel):
    """The ERP Bridge: Maps ₹X from a specific Receipt to Invoice #Y"""
    receipt = models.ForeignKey(PaymentReceipt, on_delete=models.CASCADE, related_name='allocations')
    bill = models.ForeignKey('SalesBill', on_delete=models.CASCADE, related_name='allocations')

    amount_applied = models.DecimalField(max_digits=10, decimal_places=2)
    allocated_at = models.DateTimeField(auto_now_add=True)


class LedgerEntry(TenantModel):
    """
    Immutable double-entry ledger for every change to a customer's outstanding balance.

    Replaces the mutable outstanding_balance-only approach.
    A CA can now reconstruct the full ledger for any date range:
      Opening balance + Debits (sales) - Credits (payments, returns) = Closing balance.

    Hard rules:
    - Created atomically alongside the transaction that changes outstanding_balance.
    - Never modified or deleted after creation.
    - balance_after = running outstanding_balance AFTER this entry is applied.
    """
    ENTRY_TYPES = [
        ('SALE',       'Credit Sale'),
        ('PAYMENT',    'Payment Received'),
        ('RETURN',     'Sales Return / Credit Note'),
        ('OPENING',    'Opening Balance'),
        ('ADJUSTMENT', 'Manual Adjustment'),
    ]

    customer        = models.ForeignKey('CustomerParty', on_delete=models.PROTECT, related_name='ledger_entries')
    entry_date      = models.DateField(default=timezone.now)
    entry_type      = models.CharField(max_length=20, choices=ENTRY_TYPES)

    # Debit  = customer owes more  (credit sale)
    # Credit = customer owes less  (payment received, credit note issued)
    debit           = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    credit          = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    balance_after   = models.DecimalField(max_digits=10, decimal_places=2)

    reference_number = models.CharField(max_length=50, blank=True, null=True,
                           help_text="Invoice number (INV/…), credit note number (CN/…), or receipt ID")

    # At most one of these is set — whichever triggered this entry
    sales_bill      = models.ForeignKey('SalesBill',      null=True, blank=True,
                           on_delete=models.SET_NULL, related_name='ledger_entries')
    sales_return    = models.ForeignKey('SalesReturn',    null=True, blank=True,
                           on_delete=models.SET_NULL, related_name='ledger_entries')
    payment_receipt = models.ForeignKey('PaymentReceipt', null=True, blank=True,
                           on_delete=models.SET_NULL, related_name='ledger_entries')

    narration   = models.CharField(max_length=255, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['entry_date', 'created_at']

    def __str__(self):
        return f"{self.entry_type} | {self.reference_number} | Dr {self.debit} Cr {self.credit} | Bal {self.balance_after}"


class BillPaymentLine(TenantModel):
    """
    Normalised payment-mode split — one row per mode per bill.

    Solves the cash-book aggregation problem for SPLIT bills.
    Instead of parsing split_payments JSON in the application layer,
    any cash-book query is a simple GROUP BY mode + SUM(amount).

    For single-mode bills (CASH, UPI, CREDIT) a single row is created.
    For SPLIT bills, one row per mode in the split.
    """
    bill   = models.ForeignKey('SalesBill', on_delete=models.CASCADE, related_name='payment_lines')
    mode   = models.CharField(max_length=20)   # CASH | UPI | CREDIT | CHEQUE | BANK_TRANSFER
    amount = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['pharmacy', 'bill', 'mode'],
                name='unique_payment_mode_per_bill'
            )
        ]

    def __str__(self):
        return f"{self.bill_id} — {self.mode} ₹{self.amount}"