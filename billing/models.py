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

    def __str__(self):
        return f"Return ×{self.return_quantity} of {self.sales_item.medicine.name}"




class CustomerParty(TenantModel):
    """Unified master profile for BOTH B2B Wholesale Clients and B2C Retail Patients"""
    
    CUSTOMER_TYPES = [('B2B', 'Wholesale Clinic/Pharma'), ('B2C', 'Retail Patient')]
    customer_type = models.CharField(max_length=3, choices=CUSTOMER_TYPES, default='B2C')
    
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=15) # Unique constraint moved to Meta
    email = models.EmailField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    
    # Only relevant if customer_type == 'B2B'
    gstin = models.CharField(max_length=15, blank=True, null=True)
    
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
    
    PAYMENT_MODES = [('CASH', 'Cash'), ('UPI', 'UPI/NEFT'), ('CHEQUE', 'Cheque')]
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