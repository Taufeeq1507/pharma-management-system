from rest_framework import serializers
from django.db import transaction
from decimal import Decimal
from .models import CustomerParty, PaymentReceipt, PaymentAllocation
from .models import SalesBill, SalesItem, SalesReturn
from inventory.models import InventoryBatch, MedicineMaster
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
            'id', 'customer_phone', 'customer_name', 'buyer_address',
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


# Ensure you import CustomerParty along with your other models
# from billing.models import SalesBill, SalesItem, CustomerParty 
# from inventory.models import InventoryBatch, MedicineMaster, PurchaseItem

class CheckoutItemInputSerializer(serializers.Serializer):
    """One medicine line in the POS payload."""
    medicine = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=0) # Billed tablets
    free_quantity = serializers.IntegerField(min_value=0, default=0) # Free tablets (Buy X Get Y)
    discount_percentage = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, default=Decimal('0.00')
    )

    def validate(self, data):
        if data['quantity'] == 0 and data['free_quantity'] == 0:
            raise serializers.ValidationError("Must provide either a billed quantity or a free quantity.")
        return data


class CheckoutSerializer(serializers.Serializer):
    """
    Accepts the full POS payload:
    {
        "customer_id":    "<uuid>",       // REQUIRED for CREDIT sales
        "customer_phone": "9876543210",   // Optional walk-in guest
        "customer_name":  "Ramesh Kumar", // Optional walk-in guest
        "discount":       "10.00",        
        "payment_mode":   "CREDIT",
        "items": [
            { "medicine": "<uuid>", "quantity": 30, "free_quantity": 5 }
        ]
    }
    """
    customer_id    = serializers.UUIDField(required=False, allow_null=True)
    customer_phone = serializers.CharField(max_length=15,  required=False, allow_blank=True)
    customer_name  = serializers.CharField(max_length=255, required=False, allow_blank=True)
    buyer_address  = serializers.CharField(max_length=500, required=False, allow_blank=True)

    # Obsolete fields removed: is_b2b, customer_gstin

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

    def validate(self, data):
        # Strict B2B Rule: You cannot give credit to an unregistered guest
        if data.get('payment_mode') == 'CREDIT' and not data.get('customer_id'):
            raise serializers.ValidationError("Credit sales require a registered Customer/Patient profile.")

        # Narcotic Rule: buyer name + address are legally mandatory
        medicine_ids = [item['medicine'] for item in data.get('items', [])]
        has_narcotic = MedicineMaster.objects.filter(
            id__in=medicine_ids, drug_schedule='NARCOTIC'
        ).exists()
        if has_narcotic:
            if not data.get('customer_name', '').strip():
                raise serializers.ValidationError(
                    "Customer name is required when selling Narcotic drugs."
                )
            if not data.get('buyer_address', '').strip():
                raise serializers.ValidationError(
                    "Buyer address is required when selling Narcotic drugs."
                )

        return data

    @transaction.atomic
    def create(self, validated_data):
        request        = self.context['request']
        items          = validated_data.pop('items')
        discount       = validated_data.get('discount', Decimal('0.00'))
        customer_id    = validated_data.get('customer_id')

        # ── Step 0: Fetch Customer & Determine Interstate Logic ───────────────
        pharmacy = request.user.pharmacy # Assuming get_current_pharmacy() equivalent
        customer_obj = None
        is_inter_state = False
        
        if customer_id:
            # We don't need to lock the customer yet, just reading info for GST math
            customer_obj = CustomerParty.objects.get(id=customer_id)
            if customer_obj.customer_type == 'B2B' and customer_obj.gstin and pharmacy.gstin:
                if len(customer_obj.gstin) >= 2 and len(pharmacy.gstin) >= 2:
                    is_inter_state = customer_obj.gstin[:2] != pharmacy.gstin[:2]

        # ── Step 1: FEFO resolution under lock (Billed + Free) ────────────────
        deduction_plan = []
        errors         = {}

        for item in items:
            medicine_id   = item['medicine']
            qty_needed    = item['quantity']
            free_needed   = item['free_quantity']
            discount_pct  = item.get('discount_percentage', Decimal('0.00'))
            
            qty_remaining  = qty_needed
            free_remaining = free_needed

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

            if not batches and (qty_needed > 0 or free_needed > 0):
                errors[str(medicine_id)] = "No stock available for this medicine."
                continue

            for batch in batches:
                if qty_remaining <= 0 and free_remaining <= 0:
                    break
                
                # Deduct Billed Qty first
                deduct_qty = min(batch.available_quantity, qty_remaining)
                qty_remaining -= deduct_qty
                batch.available_quantity -= deduct_qty
                
                # Deduct Free Qty with whatever stock is left in this batch
                deduct_free = min(batch.available_quantity, free_remaining)
                free_remaining -= deduct_free
                batch.available_quantity -= deduct_free

                if deduct_qty > 0 or deduct_free > 0:
                    deduction_plan.append((batch, deduct_qty, deduct_free, discount_pct))

            if qty_remaining > 0 or free_remaining > 0:
                errors[str(medicine_id)] = (
                    f"Insufficient stock. Requested {qty_needed} billed + {free_needed} free. "
                    f"Shortfall: {qty_remaining} billed, {free_remaining} free."
                )

        if errors:
            raise serializers.ValidationError(errors)

        # ── Step 2: Execute deductions + build snapshot ───────────────────────
        sales_items_to_create = []
        items_snapshot        = []
        subtotal              = Decimal('0.00')
        total_tax             = Decimal('0.00')
        total_cgst            = Decimal('0.00')
        total_sgst            = Decimal('0.00')
        total_igst            = Decimal('0.00')

        for batch, qty, free_qty, discount_pct in deduction_plan:
            pack_qty           = batch.medicine.pack_qty or 1
            mrp_per_strip      = batch.mrp
            
            # The Law Change: Tax MUST come from the active Medicine Master, not the historical batch
            gst_pct            = batch.medicine.default_gst_percentage 

            # Financial math STRICTLY uses `qty` (billed), completely ignoring `free_qty`
            if qty > 0:
                sale_rate_per_unit = (mrp_per_strip / Decimal(pack_qty)).quantize(Decimal('0.0001'))
                line_gross = (sale_rate_per_unit * qty).quantize(Decimal('0.01'))
                line_discount = (line_gross * discount_pct / Decimal('100')).quantize(Decimal('0.01'))
                line_base  = line_gross - line_discount
                line_tax   = (line_base * gst_pct / Decimal('100')).quantize(Decimal('0.01'))
                line_total = line_base + line_tax
            else:
                sale_rate_per_unit = Decimal('0.0000')
                line_base = Decimal('0.00')
                line_tax = Decimal('0.00')
                line_total = Decimal('0.00')

            cgst_amt = Decimal('0.00')
            sgst_amt = Decimal('0.00')
            igst_amt = Decimal('0.00')

            if line_tax > 0:
                if is_inter_state:
                    igst_amt = line_tax
                else:
                    cgst_amt = line_tax / Decimal('2')
                    sgst_amt = line_tax / Decimal('2')

            subtotal  += line_base
            total_tax += line_tax
            total_cgst += cgst_amt
            total_sgst += sgst_amt
            total_igst += igst_amt

            # Deduct from shelf (batch is already locked via select_for_update)
            # We deduct BOTH billed and free quantities
            batch.available_quantity -= (qty + free_qty)
            batch.save()

            # ... (Keep your existing Supplier Info query logic here) ...
            supplier_name, supplier_gstin, supplier_invoice = None, None, None # Placeholder for brevity

            items_snapshot.append({
                "medicine_id":        str(batch.medicine.id),
                "medicine_name":      batch.medicine.name,
                "batch_number":       batch.batch_number,
                "expiry_date":        str(batch.expiry_date),
                "quantity":           qty,
                "free_quantity":      free_qty,
                "mrp_per_strip":      str(mrp_per_strip),
                "sale_rate_per_unit": str(sale_rate_per_unit),
                "discount_percentage":str(discount_pct),
                "taxable_value":      str(line_base),
                "gst_percentage":     str(gst_pct),
                "cgst_amount":        str(cgst_amt),
                "sgst_amount":        str(sgst_amt),
                "igst_amount":        str(igst_amt),
                "drug_schedule":      batch.medicine.drug_schedule,
                "line_total":         str(line_total),
            })

            sales_items_to_create.append({
                'medicine':           batch.medicine,
                'inventory_batch':    batch,
                'batch_number':       batch.batch_number,
                'quantity':           qty,
                'free_quantity':      free_qty, # Added to model
                'mrp_per_strip':      mrp_per_strip,
                'sale_rate_per_unit': sale_rate_per_unit,
                'discount_percentage':discount_pct,
                'taxable_value':      line_base,
                'gst_percentage':     gst_pct,
                'cgst_amount':        cgst_amt,
                'sgst_amount':        sgst_amt,
                'igst_amount':        igst_amt,
                'line_total':         line_total,
            })

        # ── Step 3: Create the SalesBill ──────────────────────────────────────
        payment_mode = validated_data.get('payment_mode', 'CASH')
        grand_total  = (subtotal + total_tax - discount).quantize(Decimal('0.01'))

        bill = SalesBill.objects.create(
            customer       = customer_obj,
            customer_phone = validated_data.get('customer_phone') or None,
            customer_name  = validated_data.get('customer_name')  or None,
            buyer_address  = validated_data.get('buyer_address')  or None,
            billed_by      = request.user,
            subtotal       = subtotal.quantize(Decimal('0.01')),
            total_tax      = total_tax.quantize(Decimal('0.01')),
            total_cgst     = total_cgst.quantize(Decimal('0.01')),
            total_sgst     = total_sgst.quantize(Decimal('0.01')),
            total_igst     = total_igst.quantize(Decimal('0.01')),
            discount       = discount,
            grand_total    = grand_total,
            items_snapshot = items_snapshot,
            payment_mode   = payment_mode,
            # payment_status defaults to 'UNPAID' implicitly 
        )

        # ── Step 4: Bulk create all SalesItem rows ────────────────────────────
        SalesItem.objects.bulk_create([
            SalesItem(sales_bill=bill, pharmacy=pharmacy, **item_data)
            for item_data in sales_items_to_create
        ])

        # ── Step 5: Update the Ledger for Credit Sales (NEW) ──────────────────
        if payment_mode == 'CREDIT' and customer_obj:
            # Re-fetch customer under a strict database lock to prevent race conditions
            locked_customer = CustomerParty.objects.select_for_update().get(id=customer_obj.id)
            locked_customer.outstanding_balance += grand_total
            locked_customer.save()

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




class PaymentReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentReceipt
        fields = ['id', 'customer', 'amount', 'payment_mode', 'reference_number', 'receipt_date', 'notes', 'amount_allocated']
        read_only_fields = ['id', 'amount_allocated']

    def validate_amount(self, value):
        if value <= Decimal('0.00'):
            raise serializers.ValidationError("Payment amount must be greater than zero.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        amount = validated_data['amount']
        
        # 1. LOCK THE CUSTOMER ROW
        # Prevents race conditions if two clerks log payments for the same clinic simultaneously
        customer = CustomerParty.objects.select_for_update().get(id=validated_data['customer'].id)
        
        # 2. UPDATE OUTSTANDING BALANCE
        customer.outstanding_balance -= amount
        customer.save()

        # 3. CREATE THE RECEIPT
        receipt = PaymentReceipt.objects.create(**validated_data)

        # 4. AUTO-ALLOCATION (FIFO)
        # Fetch all unpaid bills for this customer, oldest first. Lock them.
        unpaid_bills = SalesBill.objects.select_for_update().filter(
            customer=customer
        ).exclude(payment_status='PAID').order_by('bill_date')

        remaining_amount_to_allocate = amount

        for bill in unpaid_bills:
            if remaining_amount_to_allocate <= Decimal('0.00'):
                break

            # How much does this specific bill still need?
            bill_due = bill.grand_total - bill.amount_paid

            # Apply either the full bill due, or whatever money we have left
            apply_amount = min(remaining_amount_to_allocate, bill_due)

            # Create the Bridge Record
            PaymentAllocation.objects.create(
                receipt=receipt,
                bill=bill,
                amount_applied=apply_amount
            )

            # Update the Bill
            bill.amount_paid += apply_amount
            if bill.amount_paid >= bill.grand_total:
                bill.payment_status = 'PAID'
            elif bill.amount_paid > Decimal('0.00'):
                bill.payment_status = 'PARTIAL'
            bill.save()

            # Deduct from our running total
            remaining_amount_to_allocate -= apply_amount

        # 5. UPDATE RECEIPT ALLOCATION TOTAL
        receipt.amount_allocated = amount - remaining_amount_to_allocate
        receipt.save()

        return receipt