from django.db import models
from accounts.models import TenantModel
from django.utils import timezone
from decimal import Decimal


class Supplier(TenantModel):
    name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    gstin = models.CharField(max_length=15, blank=True, null=True)
    state = models.CharField(max_length=100, default="Maharashtra", help_text="Used for calculating Inter/Intra state GST")

    # SOFT DELETE: If a supplier goes out of business, we set this to False.
    # We NEVER delete the row, otherwise old purchase bills would crash.
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class MedicineMaster(TenantModel):
    name = models.CharField(max_length=255)
    company = models.CharField(max_length=255)
    category = models.CharField(max_length=50)  # e.g., Tablet, Syrup, Injection
    hsn_code = models.CharField(max_length=20, blank=True, null=True)
    # e.g., "1x10", "1x15" — describes how units are packaged on the strip/bottle
    packaging = models.CharField(max_length=50, blank=True, null=True, help_text="e.g., 1x10, 1x15")
    # Number of individual units per strip/pack — used to convert strips → individual unit count
    pack_qty = models.IntegerField(default=1)
    default_gst_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    # Salt / generic name including strength — e.g. "Paracetamol 650mg", "Amoxicillin + Clavulanic Acid 625mg"
    # Not unique: multiple brands can share the same salt_name (Dolo, Crocin both = "Paracetamol 650mg")
    salt_name = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    # EAN-13 or other barcode printed on the strip/box. Product-level identifier (not batch-level).
    # Unique per pharmacy — enforced by constraint below, nulls excluded.
    barcode = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    # SOFT DELETE: If a drug is banned or discontinued, we set this to False.
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['pharmacy', 'barcode'],
                condition=models.Q(barcode__isnull=False),
                name='unique_barcode_per_pharmacy'
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.company})"


class PurchaseBill(TenantModel):
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='purchase_bills')
    invoice_number = models.CharField(max_length=100)
    bill_date = models.DateField(default=timezone.now)

    # Financials — subtotal, total_tax, grand_total are AUTO-CALCULATED by the serializer.
    # Only 'discount' is user-supplied.
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_tax = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_cgst = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_sgst = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_igst = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    grand_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    PAYMENT_CHOICES = [('PENDING', 'Pending'), ('PARTIAL', 'Partial'), ('PAID', 'Paid')]
    payment_status = models.CharField(max_length=20, choices=PAYMENT_CHOICES, default='PENDING')

    class Meta:
        # Safety: One supplier cannot have two bills with the same invoice number for the same pharmacy.
        constraints = [
            models.UniqueConstraint(
                fields=['pharmacy', 'supplier', 'invoice_number'],
                name='unique_invoice_per_supplier_per_pharmacy'
            )
        ]

    def __str__(self):
        return f"Bill {self.invoice_number} - {self.supplier.name}"


# 2. THE LINE ITEM (Historical Audit Trail)
class PurchaseItem(TenantModel):
    purchase_bill = models.ForeignKey(PurchaseBill, on_delete=models.CASCADE, related_name='items')
    medicine = models.ForeignKey(MedicineMaster, on_delete=models.PROTECT)

    batch_number = models.CharField(max_length=100)
    expiry_date = models.DateField()
    # quantity = number of STRIPS received (as on the supplier invoice)
    quantity = models.IntegerField()
    free_quantity = models.IntegerField(default=0)  # Common in pharma (Buy 10, get 1 free)

    # purchase_rate_base = per-STRIP base price BEFORE GST
    purchase_rate_base = models.DecimalField(max_digits=10, decimal_places=2)
    discount_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    
    # Tax fields
    taxable_value = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    gst_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    cgst_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    sgst_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    igst_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    mrp = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.medicine.name} ({self.quantity}) on {self.purchase_bill.invoice_number}"


# ---------------------------------------------------------------------------
# WAREHOUSE / SHELF MANAGEMENT
# ---------------------------------------------------------------------------

class WarehouseBlock(TenantModel):
    """
    A named section of the warehouse rack, identified by a single uppercase letter.
    e.g. Block A, Block B, Block C.

    Each block has its own shelf_count. Shelves within a block are numbered 1..shelf_count.
    Blocks are created by the owner and can be resized independently.
    Never pre-populate ShelfLocation rows — shelves are created on-demand at assignment time.
    """
    block_letter = models.CharField(
        max_length=1,
        help_text="Single uppercase letter identifying this block, e.g. 'A', 'B', 'C'"
    )
    shelf_count = models.IntegerField(
        help_text="Number of shelves in this block"
    )
    label = models.CharField(
        max_length=100, blank=True, null=True,
        help_text="Optional descriptive label, e.g. 'Refrigerated', 'Antibiotics'"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['pharmacy', 'block_letter'],
                name='unique_block_letter_per_pharmacy'
            )
        ]

    def __str__(self):
        return f"Block {self.block_letter}"


class ShelfLocation(TenantModel):
    """
    One shelf within a WarehouseBlock. Addressed as "{block_letter}-{shelf_number}",
    e.g. A-3 = Block A, Shelf 3.

    Created on-demand via get_or_create at assignment time — NOT pre-populated
    when a WarehouseBlock is created.

    THE MEDICINE CONSTRAINT (enforced at application layer in ShelfAssignmentSerializer):
    A shelf may hold MANY different medicines.
    But each medicine may only appear under ONE batch number on a given shelf.
        Dolo Batch A + Crocin Batch X = ALLOWED
        Dolo Batch A + Dolo Batch B   = HARD BLOCK
    """
    block        = models.ForeignKey(WarehouseBlock, on_delete=models.CASCADE, related_name='shelves')
    shelf_number = models.IntegerField(help_text="Shelf number within the block, 1-indexed")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['pharmacy', 'block', 'shelf_number'],
                name='unique_shelf_per_block_per_pharmacy'
            )
        ]

    def __str__(self):
        return f"Block {self.block.block_letter}-{self.shelf_number}"


# 3. THE LIVE STOCK (Current State — Updated via Upsert Logic)
class InventoryBatch(TenantModel):
    medicine = models.ForeignKey(MedicineMaster, on_delete=models.PROTECT, related_name='live_batches')
    batch_number = models.CharField(max_length=100)
    expiry_date = models.DateField()

    # ALWAYS tracked in individual units (tablets).
    # Conversion on inward: available_quantity = strips_received * medicine.pack_qty
    available_quantity = models.IntegerField()
    gst_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    # MRP of the FULL STRIP/PACK — needed on the billing screen
    mrp = models.DecimalField(max_digits=10, decimal_places=2)

    # WHERE this batch lives on the shelf. None = not yet assigned (pending placement).
    shelf = models.ForeignKey(
        'ShelfLocation',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='batches'
    )

    class Meta:
        # Safety: Prevents a duplicate row for the same batch + MRP combo within a pharmacy.
        # When a second invoice arrives with the same batch/MRP, we UPSERT (increment qty).
        constraints = [
            models.UniqueConstraint(
                fields=['pharmacy', 'medicine', 'batch_number', 'mrp', 'gst_percentage'],
                name='unique_batch_mrp_per_pharmacy'
            )
        ]

    def __str__(self):
        shelf_info = f" @ {self.shelf}" if self.shelf else " (unassigned)"
        return f"{self.medicine.name} - Batch: {self.batch_number} ({self.available_quantity} tabs){shelf_info}"


class StockAdjustment(TenantModel):
    """
    Immutable audit log — one row per quantity change made during a sync or manual adjustment.
    NEVER modified or deleted after creation.

    Hard rules:
    - delta = new_quantity - old_quantity. Computed server-side. Never client-supplied.
    - adjusted_by = request.user. Set from context. Never client-supplied.
    """
    SOURCE_CHOICES = [('SYNC', 'Weekly Sync'), ('MANUAL', 'Manual Adjustment')]

    inventory_batch = models.ForeignKey(
        InventoryBatch, on_delete=models.PROTECT, related_name='adjustments'
    )
    shelf = models.ForeignKey(
        ShelfLocation, on_delete=models.SET_NULL,
        null=True, blank=True
    )
    old_quantity  = models.IntegerField()
    new_quantity  = models.IntegerField()
    delta         = models.IntegerField(help_text="new_quantity - old_quantity. Negative = shrinkage.")
    adjusted_by   = models.ForeignKey('accounts.CustomUser', on_delete=models.PROTECT)
    adjusted_at   = models.DateTimeField(auto_now_add=True)
    reason        = models.CharField(max_length=255, default="Weekly Sync")
    source        = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='SYNC')

    def __str__(self):
        return f"Adjustment {self.delta:+d} on {self.inventory_batch}"


# 4. THE DEBIT NOTE (Returns)
class PurchaseReturn(TenantModel):
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='returns')
    # Optional link to the original bill
    original_bill = models.ForeignKey(PurchaseBill, on_delete=models.SET_NULL, null=True, blank=True)

    medicine = models.ForeignKey(MedicineMaster, on_delete=models.PROTECT)
    batch_number = models.CharField(max_length=100)
    mrp = models.DecimalField(max_digits=10, decimal_places=2)
    # return_quantity is in individual units (tablets), consistent with InventoryBatch
    return_quantity = models.IntegerField()
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2)
    return_date = models.DateField(default=timezone.now)

    reason = models.CharField(max_length=255, default="Expired")  # e.g., Expired, Damaged, Recall

    def __str__(self):
        return f"Return {self.medicine.name} ({self.return_quantity}) to {self.supplier.name}"
    
class SupplierReturnPolicy(TenantModel):
    """
    Each supplier has their own return acceptance window.
    e.g. Sun Pharma accepts returns until 90 days before expiry.
    Cipla accepts until 60 days before expiry.
    """
    supplier = models.OneToOneField(
        Supplier,
        on_delete=models.CASCADE,
        related_name='return_policy'
    )
    return_window_days = models.IntegerField(
        default=90,
        help_text="Days before expiry date by which returns must be initiated"
    )
    gst_credit_eligible = models.BooleanField(
        default=True,
        help_text="Does this supplier issue GST credit notes on returns?"
    )
    advance_notice_days = models.IntegerField(
        default=14,
        help_text="Alert fires this many days before the return window closes"
    )
    notes = models.TextField(blank=True, null=True)

    def save(self, *args, **kwargs):
        # Bug 4 fix: derive pharmacy from supplier so Celery tasks (which have
        # no request context) don't raise ValueError from TenantModel.save().
        if self._state.adding and getattr(self, 'pharmacy_id', None) is None:
            self.pharmacy = self.supplier.pharmacy
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.supplier.name} — return by {self.return_window_days}d before expiry"


class ReturnAlert(TenantModel):
    """
    Auto-generated alert for each InventoryBatch approaching
    its supplier's return deadline.

    Generated nightly by a Celery task.
    Status lifecycle: PENDING → RETURN | SELL | IGNORED
    """
    STATUS_CHOICES = [
        ('PENDING',  'Pending Decision'),
        ('RETURN',   'Marked for Return'),
        ('SELL',     'Decision: Sell Before Expiry'),
        ('IGNORED',  'Ignored'),
    ]

    RECOMMENDATION_CHOICES = [
        ('SELL',    'Sell before expiry'),
        ('RETURN',  'Return to supplier'),
        ('PARTIAL', 'Sell some, return rest'),
    ]

    inventory_batch = models.ForeignKey(
        InventoryBatch,
        on_delete=models.CASCADE,
        related_name='return_alerts'
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name='return_alerts'
    )

    # The hard deadline — must return before this date or window closes
    return_deadline = models.DateField(
        help_text="Last date supplier accepts this return"
    )
    # Alert fires this many days before the deadline
    alert_date = models.DateField(
        help_text="Date this alert was surfaced to the owner"
    )

    # GST intelligence
    gst_quarter_deadline = models.DateField(
        null=True, blank=True,
        help_text="GST filing deadline for the quarter this return falls in"
    )
    gst_credit_at_risk = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('0.00'),
        help_text="Estimated GST input credit lost if return is missed"
    )
    # True if return_deadline falls AFTER the GST filing deadline
    # i.e. you must return BEFORE the quarter closes to claim input credit
    gst_warning = models.BooleanField(default=False)

    # Sell vs Return intelligence
    estimated_sell_quantity = models.IntegerField(
        default=0,
        help_text="Predicted units sellable before expiry based on velocity"
    )
    recommendation = models.CharField(
        max_length=10,
        choices=RECOMMENDATION_CHOICES,
        default='RETURN'
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='PENDING'
    )
    actioned_by = models.ForeignKey(
        'accounts.CustomUser',
        on_delete=models.SET_NULL,
        null=True, blank=True
    )
    actioned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # Bug 4 fix: derive pharmacy from the linked InventoryBatch's pharmacy
        # so Celery alert-generation tasks (no request context) don't crash.
        if self._state.adding and getattr(self, 'pharmacy_id', None) is None:
            self.pharmacy = self.inventory_batch.pharmacy
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"Alert: {self.inventory_batch.medicine.name} "
            f"— return by {self.return_deadline}"
        )