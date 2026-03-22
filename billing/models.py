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
    customer_phone = models.CharField(max_length=15, blank=True, null=True, db_index=True)
    customer_name  = models.CharField(max_length=255, blank=True, null=True)

    bill_date = models.DateTimeField(default=timezone.now)
    billed_by = models.ForeignKey(
        CustomUser, on_delete=models.PROTECT, related_name='sales_bills'
    )

    # Financials — server-calculated only, never accepted from client
    subtotal    = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    total_tax   = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    discount    = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    grand_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))

    PAYMENT_CHOICES = [('CASH', 'Cash'), ('UPI', 'UPI'), ('CREDIT', 'Credit')]
    payment_mode = models.CharField(max_length=20, choices=PAYMENT_CHOICES, default='CASH')

    # THE FROZEN SNAPSHOT
    # Complete picture of the bill at checkout time.
    # MRP changes, supplier changes, medicine deletions — none of it
    # can ever corrupt this record. This is what gets printed.
    items_snapshot = models.JSONField(default=list)

    def __str__(self):
        return f"Bill #{str(self.id)[:8]} | {self.customer_phone or 'Guest'} | ₹{self.grand_total}"


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
    sales_bill      = models.ForeignKey(SalesBill,      on_delete=models.CASCADE,  related_name='items')
    medicine        = models.ForeignKey(MedicineMaster,  on_delete=models.PROTECT)
    inventory_batch = models.ForeignKey(InventoryBatch,  on_delete=models.PROTECT,  related_name='sales_items')

    # Denormalised for fast reads — saves a JOIN on every return lookup
    batch_number = models.CharField(max_length=100)
    quantity     = models.IntegerField()  # tablets deducted from this specific batch

    # Prices frozen at time of sale — independent of future InventoryBatch changes
    mrp_per_strip      = models.DecimalField(max_digits=10, decimal_places=2)
    sale_rate_per_unit = models.DecimalField(max_digits=10, decimal_places=4)  # per tablet
    gst_percentage     = models.DecimalField(max_digits=5,  decimal_places=2)
    line_total         = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.medicine.name} ×{self.quantity} from batch {self.batch_number}"


class SalesReturn(TenantModel):
    """
    Credit note — customer returns tablets.

    Stock is restored to the EXACT InventoryBatch it was sold from,
    identified via the SalesItem FK. No guessing, no wrong batch.

    Partial returns are supported — customer can return 5 of 10 tablets
    across multiple separate return requests.
    Validation checks: already_returned + new_qty <= original_qty
    """
    sales_bill = models.ForeignKey(SalesBill, on_delete=models.PROTECT, related_name='returns')
    sales_item = models.ForeignKey(SalesItem, on_delete=models.PROTECT, related_name='returns')

    return_quantity = models.IntegerField()
    refund_amount   = models.DecimalField(max_digits=10, decimal_places=2)
    return_date     = models.DateField(default=timezone.now)
    reason          = models.CharField(max_length=255, default="Customer Return")

    def __str__(self):
        return f"Return ×{self.return_quantity} of {self.sales_item.medicine.name}"