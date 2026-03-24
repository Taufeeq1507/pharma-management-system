from rest_framework import serializers
from django.db import transaction
from decimal import Decimal
from .models import SalesBill, SalesItem, SalesReturn
from inventory.models import InventoryBatch, PurchaseItem
from accounts.utils import get_current_pharmacy

# ── Read serializers ───────────────────────────────────────────────────────────

class SalesItemReadSerializer(serializers.ModelSerializer):
    medicine_name = serializers.CharField(source='medicine.name', read_only=True)

    class Meta:
        model  = SalesItem
        fields = [
            'id', 'medicine', 'medicine_name', 'inventory_batch',
            'batch_number', 'quantity',
            'mrp_per_strip', 'sale_rate_per_unit', 'gst_percentage', 'line_total',
        ]


class SalesBillReadSerializer(serializers.ModelSerializer):
    """
    Full bill response for history, detail view, and reprint.

    Returns both:
      items          → live SalesItem rows — use item IDs when processing returns
      items_snapshot → frozen audit JSON   — use this for display and printing
    """
    items           = SalesItemReadSerializer(many=True, read_only=True)
    billed_by_phone = serializers.CharField(source='billed_by.phone_number', read_only=True)

    class Meta:
        model  = SalesBill
        fields = [
            'id', 'customer_phone', 'customer_name',
            'bill_date', 'billed_by', 'billed_by_phone',
            'subtotal', 'total_tax', 'discount', 'grand_total',
            'payment_mode',
            'items',
            'items_snapshot',
        ]


class SalesReturnReadSerializer(serializers.ModelSerializer):
    medicine_name = serializers.CharField(source='sales_item.medicine.name', read_only=True)

    class Meta:
        model  = SalesReturn
        fields = [
            'id', 'sales_bill', 'sales_item', 'medicine_name',
            'return_quantity', 'refund_amount', 'return_date', 'reason',
        ]


# ── Checkout ───────────────────────────────────────────────────────────────────

class CheckoutItemInputSerializer(serializers.Serializer):
    """One medicine line in the POS payload — quantity is in individual tablets."""
    medicine = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1)


class CheckoutSerializer(serializers.Serializer):
    """
    Accepts the full POS payload:

    {
        "customer_phone": "9876543210",   // optional
        "customer_name":  "Ramesh Kumar", // optional
        "discount":       "10.00",        // optional
        "payment_mode":   "CASH",
        "items": [
            { "medicine": "<uuid>", "quantity": 30 },
            { "medicine": "<uuid>", "quantity": 10 }
        ]
    }

    validate() — input format checks only (no DB writes, no locks)
    create()   — atomic FEFO resolution + lock + deductions + bill + snapshot
    """
    customer_phone = serializers.CharField(max_length=15,  required=False, allow_blank=True)
    customer_name  = serializers.CharField(max_length=255, required=False, allow_blank=True)
    discount       = serializers.DecimalField(
                         max_digits=10, decimal_places=2,
                         required=False, default=Decimal('0.00'))
    payment_mode   = serializers.ChoiceField(
                         choices=['CASH', 'UPI', 'CREDIT'], default='CASH')
    items          = CheckoutItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("A sale must have at least one item.")
        return value

    # Bug 2 fix: validate() does format checks only — NO DB queries, NO locks.
    # FEFO resolution and select_for_update() live entirely inside create()'s
    # @transaction.atomic block so that row locks are held for the full duration.
    def validate(self, data):
        return data

    @transaction.atomic
    def create(self, validated_data):
        """
        FEFO Resolution + atomic deductions.

        For each (medicine, qty) pair:
        1. select_for_update() inside the open transaction — holds row locks
        2. Order by expiry_date ASC — First Expire First Out
        3. Walk batches greedily until requested qty is satisfied
        4. Re-check available_quantity under the lock (prevents double-sell races)
        5. If total available stock < requested qty → ValidationError rolls back all
        """
        request        = self.context['request']
        items          = validated_data.pop('items')
        discount       = validated_data.get('discount', Decimal('0.00'))

        # ── Step 1: FEFO resolution under lock ────────────────────────────────
        deduction_plan = []
        errors         = {}

        for item in items:
            medicine_id   = item['medicine']
            qty_needed    = item['quantity']
            qty_remaining = qty_needed

            # select_for_update inside @transaction.atomic — locks rows for real
            batches = list(
                InventoryBatch.objects
                .select_for_update()
                .select_related('medicine')
                .filter(
                    medicine_id            = medicine_id,
                    available_quantity__gt = 0,
                    medicine__is_active    = True,
                )
                .order_by('expiry_date')  # FEFO
            )

            if not batches:
                errors[str(medicine_id)] = "No stock available for this medicine."
                continue

            for batch in batches:
                if qty_remaining <= 0:
                    break
                deduct = min(batch.available_quantity, qty_remaining)
                deduction_plan.append((batch, deduct))
                qty_remaining -= deduct

            if qty_remaining > 0:
                errors[str(medicine_id)] = (
                    f"Insufficient stock. Requested {qty_needed}, "
                    f"only {qty_needed - qty_remaining} available."
                )

        if errors:
            raise serializers.ValidationError(errors)

        # ── Step 2: Execute deductions + build snapshot ───────────────────────
        sales_items_to_create = []
        items_snapshot        = []
        subtotal              = Decimal('0.00')
        total_tax             = Decimal('0.00')

        for batch, qty in deduction_plan:
            pack_qty           = batch.medicine.pack_qty or 1
            mrp_per_strip      = batch.mrp
            sale_rate_per_unit = (mrp_per_strip / Decimal(pack_qty)).quantize(Decimal('0.0001'))
            gst_pct            = batch.gst_percentage

            line_base  = (sale_rate_per_unit * qty).quantize(Decimal('0.01'))
            line_tax   = (line_base * gst_pct / Decimal('100')).quantize(Decimal('0.01'))
            line_total = line_base + line_tax

            subtotal  += line_base
            total_tax += line_tax

            # Deduct from shelf (batch is already locked via select_for_update)
            batch.available_quantity -= qty
            batch.save()

            # Fetch supplier info for the audit snapshot
            purchase_item = (
                PurchaseItem.objects
                .filter(medicine=batch.medicine, batch_number=batch.batch_number, mrp=batch.mrp)
                .select_related('purchase_bill__supplier')
                .order_by('-purchase_bill__bill_date')
                .first()
            )
            supplier_name    = None
            supplier_gstin   = None
            supplier_invoice = None
            if purchase_item:
                supplier_name    = purchase_item.purchase_bill.supplier.name
                supplier_gstin   = purchase_item.purchase_bill.supplier.gstin
                supplier_invoice = purchase_item.purchase_bill.invoice_number

            items_snapshot.append({
                "medicine_id":        str(batch.medicine.id),
                "medicine_name":      batch.medicine.name,
                "company":            batch.medicine.company,
                "hsn_code":           batch.medicine.hsn_code,
                "batch_number":       batch.batch_number,
                "expiry_date":        str(batch.expiry_date),
                "quantity":           qty,
                "pack_qty":           pack_qty,
                "mrp_per_strip":      str(mrp_per_strip),
                "sale_rate_per_unit": str(sale_rate_per_unit),
                "gst_percentage":     str(gst_pct),
                "line_total":         str(line_total),
                "supplier_name":      supplier_name,
                "supplier_gstin":     supplier_gstin,
                "purchase_invoice":   supplier_invoice,
            })

            sales_items_to_create.append({
                'medicine':           batch.medicine,
                'inventory_batch':    batch,
                'batch_number':       batch.batch_number,
                'quantity':           qty,
                'mrp_per_strip':      mrp_per_strip,
                'sale_rate_per_unit': sale_rate_per_unit,
                'gst_percentage':     gst_pct,
                'line_total':         line_total,
            })

        # ── Step 3: Create the SalesBill ──────────────────────────────────────
        bill = SalesBill.objects.create(
            billed_by      = request.user,
            subtotal       = subtotal.quantize(Decimal('0.01')),
            total_tax      = total_tax.quantize(Decimal('0.01')),
            discount       = discount,
            grand_total    = (subtotal + total_tax - discount).quantize(Decimal('0.01')),
            items_snapshot = items_snapshot,
            customer_phone = validated_data.get('customer_phone') or None,
            customer_name  = validated_data.get('customer_name')  or None,
            payment_mode   = validated_data.get('payment_mode', 'CASH'),
        )

        # ── Step 4: Bulk create all SalesItem rows ────────────────────────────
        pharmacy = get_current_pharmacy()
        SalesItem.objects.bulk_create([
            SalesItem(sales_bill=bill, pharmacy=pharmacy, **item_data)
            for item_data in sales_items_to_create
        ])

        return bill


# ── Sales Return ───────────────────────────────────────────────────────────────

class SalesReturnSerializer(serializers.ModelSerializer):
    """
    Accepts: sales_bill, sales_item, return_quantity, refund_amount, reason

    Validation:
    1. Confirm sales_item belongs to sales_bill
    2. Calculate already_returned for this sales_item across all prior returns
    3. Check already_returned + return_quantity <= sales_item.quantity
    4. Stash self._batch = sales_item.inventory_batch

    Create (atomic):
    1. Restore stock to the exact batch it was sold from
    2. Create SalesReturn record
    """
    class Meta:
        model  = SalesReturn
        fields = [
            'id', 'sales_bill', 'sales_item',
            'return_quantity', 'refund_amount', 'return_date', 'reason',
        ]
        read_only_fields = ['id', 'pharmacy']

    def validate(self, data):
        sales_bill  = data['sales_bill']
        sales_item  = data['sales_item']
        return_qty  = data['return_quantity']

        # Confirm the item belongs to the bill
        if sales_item.sales_bill_id != sales_bill.id:
            raise serializers.ValidationError(
                "This sales item does not belong to the provided sales bill."
            )

        # Calculate how much has already been returned for this item
        already_returned = sum(
            r.return_quantity
            for r in sales_item.returns.all()
        )

        returnable = sales_item.quantity - already_returned

        if return_qty > returnable:
            raise serializers.ValidationError(
                f"Cannot return {return_qty} tablets. "
                f"Original quantity: {sales_item.quantity}, "
                f"already returned: {already_returned}, "
                f"remaining returnable: {returnable}."
            )

        self._batch = sales_item.inventory_batch
        return data

    @transaction.atomic
    def create(self, validated_data):
        # Bug 3 fix: re-fetch the batch WITH a lock inside this atomic block
        # so concurrent returns on the same sales_item cannot both restore stock
        # from the same stale in-memory object.
        batch = InventoryBatch.objects.select_for_update().get(pk=self._batch.pk)
        batch.available_quantity += validated_data['return_quantity']
        batch.save()

        return SalesReturn.objects.create(**validated_data)