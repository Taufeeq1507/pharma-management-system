from rest_framework import serializers
from django.db import transaction
from django.utils import timezone
from decimal import Decimal
from .models import (
    CustomerParty, PaymentReceipt, PaymentAllocation,
    SalesBill, SalesItem, SalesReturn,
    LedgerEntry, BillPaymentLine,
)
from inventory.models import InventoryBatch, MedicineMaster
from accounts.utils import get_current_pharmacy
from accounts.models import Pharmacy


# ── Read serializers ───────────────────────────────────────────────────────────

class SalesItemReadSerializer(serializers.ModelSerializer):
    medicine_name = serializers.CharField(source='medicine.name', read_only=True)

    class Meta:
        model  = SalesItem
        fields = [
            'id', 'medicine', 'medicine_name', 'inventory_batch',
            'batch_number', 'quantity', 'free_quantity',
            'mrp_per_strip', 'sale_rate_per_unit', 'gst_percentage',
            'taxable_value', 'cgst_amount', 'sgst_amount', 'igst_amount',
            'line_total',
        ]


class SalesBillReadSerializer(serializers.ModelSerializer):
    items           = SalesItemReadSerializer(many=True, read_only=True)
    billed_by_phone = serializers.CharField(source='billed_by.phone_number', read_only=True)

    class Meta:
        model  = SalesBill
        fields = [
            'id', 'invoice_number', 'place_of_supply',
            'customer', 'customer_phone', 'customer_name', 'buyer_address',
            'prescriber_name', 'prescriber_reg_no',
            'bill_date', 'billed_by', 'billed_by_phone',
            'subtotal', 'total_tax', 'total_cgst', 'total_sgst', 'total_igst',
            'discount', 'grand_total',
            'payment_mode', 'split_payments',
            'payment_status', 'amount_paid',
            'items', 'items_snapshot',
        ]


class SalesReturnReadSerializer(serializers.ModelSerializer):
    medicine_name = serializers.CharField(source='sales_item.medicine.name', read_only=True)

    class Meta:
        model  = SalesReturn
        fields = [
            'id', 'sales_bill', 'sales_item', 'medicine_name',
            'return_quantity', 'refund_amount', 'return_date', 'reason',
            # CA-required fields (BUG-D / BUG-E)
            'credit_note_number',
            'cgst_amount', 'sgst_amount', 'igst_amount',
        ]


class LedgerEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model  = LedgerEntry
        fields = [
            'id', 'entry_date', 'entry_type',
            'debit', 'credit', 'balance_after',
            'reference_number', 'narration',
            'sales_bill', 'sales_return', 'payment_receipt',
            'created_at',
        ]
        read_only_fields = fields


# ── Customer ───────────────────────────────────────────────────────────────────

class CustomerPartySerializer(serializers.ModelSerializer):
    class Meta:
        model  = CustomerParty
        fields = '__all__'
        read_only_fields = ['id', 'pharmacy', 'outstanding_balance']


# ── Checkout ───────────────────────────────────────────────────────────────────

class CheckoutItemInputSerializer(serializers.Serializer):
    medicine            = serializers.UUIDField()
    quantity            = serializers.IntegerField()
    uom                 = serializers.ChoiceField(choices=['Tabs', 'Strips'], default='Tabs')
    free_quantity       = serializers.IntegerField(min_value=0, default=0)
    discount_percentage = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, default=Decimal('0.00')
    )

    def validate(self, data):
        if data['quantity'] == 0 and data['free_quantity'] == 0:
            raise serializers.ValidationError(
                "Must provide either a billed quantity or a free quantity."
            )
        return data


class CheckoutSerializer(serializers.Serializer):
    customer_id    = serializers.UUIDField(required=False, allow_null=True)
    customer_phone = serializers.CharField(max_length=15,  required=False, allow_blank=True)
    customer_name  = serializers.CharField(max_length=255, required=False, allow_blank=True)
    buyer_address  = serializers.CharField(max_length=500, required=False, allow_blank=True)
    prescriber_name   = serializers.CharField(max_length=255, required=False, allow_blank=True)
    prescriber_reg_no = serializers.CharField(max_length=100, required=False, allow_blank=True)
    discount       = serializers.DecimalField(
                         max_digits=10, decimal_places=2,
                         required=False, default=Decimal('0.00'))
    payment_mode   = serializers.ChoiceField(
                         choices=['CASH', 'UPI', 'CREDIT', 'SPLIT'], default='CASH')
    split_payments = serializers.DictField(
                         child=serializers.DecimalField(max_digits=10, decimal_places=2),
                         required=False, default=dict)
    items          = CheckoutItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("A sale must have at least one item.")
        return value

    def validate(self, data):
        if data.get('payment_mode') == 'CREDIT' and not data.get('customer_id'):
            raise serializers.ValidationError(
                "Credit sales require a registered Customer/Patient profile."
            )

        items = data.get('items', [])
        negative_items = [item for item in items if item.get('quantity', 0) < 0]

        if negative_items:
            payment_mode = data.get('payment_mode')
            customer_id  = data.get('customer_id')
            is_b2b_customer = False
            if customer_id:
                try:
                    cust = CustomerParty.objects.get(id=customer_id)
                    is_b2b_customer = (cust.customer_type == 'B2B')
                except CustomerParty.DoesNotExist:
                    pass

            split_payments = data.get('split_payments', {})
            uses_credit = (
                payment_mode == 'CREDIT' or
                (payment_mode == 'SPLIT' and
                 Decimal(str(split_payments.get('CREDIT', '0.00'))) > 0)
            )
            if uses_credit or is_b2b_customer:
                raise serializers.ValidationError(
                    "B2B and Credit returns must be processed via the formal "
                    "Sales Return menu linked to the original invoice."
                )

            total_negative_value = Decimal('0.00')
            for item in negative_items:
                medicine_id  = item['medicine']
                qty          = item['quantity']
                discount_pct = item.get('discount_percentage', Decimal('0.00'))
                batch = InventoryBatch.objects.filter(
                    medicine_id=medicine_id, medicine__is_active=True
                ).first()
                if not batch:
                    raise serializers.ValidationError(
                        f"Cannot process return: no batch found for medicine {medicine_id}"
                    )
                pack_qty  = batch.medicine.pack_qty or 1
                gst_pct   = batch.medicine.default_gst_percentage
                mrp_rate  = batch.mrp / Decimal(pack_qty)
                sale_rate = (mrp_rate / (Decimal('1') + gst_pct / Decimal('100'))).quantize(Decimal('0.0001'))
                abs_qty   = abs(qty)
                gross     = (sale_rate * abs_qty).quantize(Decimal('0.01'))
                disc      = (gross * discount_pct / Decimal('100')).quantize(Decimal('0.01'))
                base      = gross - disc
                tax       = (base * gst_pct / Decimal('100')).quantize(Decimal('0.01'))
                total_negative_value += (base + tax)

            if total_negative_value > Decimal('500.00'):
                raise serializers.ValidationError(
                    "Unlinked returns cannot exceed ₹500. "
                    "Please link this to the original bill via the Returns menu."
                )

        medicine_ids  = [item['medicine'] for item in items]
        has_narcotic  = MedicineMaster.objects.filter(
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
        split_payments = validated_data.get('split_payments', {})

        pharmacy     = request.user.pharmacy
        customer_obj = None
        is_inter_state = False

        if customer_id:
            customer_obj = CustomerParty.objects.get(id=customer_id)
            if (customer_obj.customer_type == 'B2B' and
                    customer_obj.gstin and pharmacy.gstin and
                    len(customer_obj.gstin) >= 2 and len(pharmacy.gstin) >= 2):
                is_inter_state = customer_obj.gstin[:2] != pharmacy.gstin[:2]

        # ── BUG-B FIX: FY-aware sequential invoice counter ────────────────────
        # Lock Pharmacy row first (consistent lock order prevents deadlocks).
        # Detect FY rollover and reset BOTH counters on the first sale of the year.
        today    = timezone.localdate()
        fy_year  = today.year if today.month >= 4 else today.year - 1
        fy_label = f"{fy_year}-{str(fy_year + 1)[2:]}"

        ph_locked = Pharmacy.objects.select_for_update().get(id=pharmacy.id)
        if ph_locked.current_fy != fy_label:
            # New financial year — reset both series to zero
            ph_locked.invoice_counter = 0
            ph_locked.cn_counter      = 0
            ph_locked.current_fy      = fy_label
        ph_locked.invoice_counter += 1
        ph_locked.save(update_fields=['invoice_counter', 'cn_counter', 'current_fy'])
        invoice_number = f"INV/{fy_label}/{ph_locked.invoice_counter:05d}"

        # ── Derive place_of_supply ─────────────────────────────────────────────
        GST_STATE_CODES = {
            'Jammu and Kashmir': '01', 'Himachal Pradesh': '02', 'Punjab': '03',
            'Chandigarh': '04', 'Uttarakhand': '05', 'Haryana': '06', 'Delhi': '07',
            'Rajasthan': '08', 'Uttar Pradesh': '09', 'Bihar': '10',
            'Sikkim': '11', 'Arunachal Pradesh': '12', 'Nagaland': '13',
            'Manipur': '14', 'Mizoram': '15', 'Tripura': '16', 'Meghalaya': '17',
            'Assam': '18', 'West Bengal': '19', 'Jharkhand': '20', 'Odisha': '21',
            'Chhattisgarh': '22', 'Madhya Pradesh': '23', 'Gujarat': '24',
            'Dadra and Nagar Haveli and Daman and Diu': '26', 'Maharashtra': '27',
            'Andhra Pradesh': '28', 'Karnataka': '29', 'Goa': '30',
            'Lakshadweep': '31', 'Kerala': '32', 'Tamil Nadu': '33',
            'Puducherry': '34', 'Andaman and Nicobar Islands': '35',
            'Telangana': '36', 'Ladakh': '38',
        }
        if customer_obj and customer_obj.gstin and len(customer_obj.gstin) >= 2:
            place_of_supply = customer_obj.gstin[:2]
        elif pharmacy.gstin and len(pharmacy.gstin) >= 2:
            place_of_supply = pharmacy.gstin[:2]
        else:
            place_of_supply = GST_STATE_CODES.get(pharmacy.state, '27')

        # ── Step 1: FEFO resolution under lock ────────────────────────────────
        deduction_plan = []
        errors         = {}

        for item in items:
            medicine_id  = item['medicine']
            uom          = item.get('uom', 'Tabs')
            raw_qty      = item['quantity']
            raw_free_qty = item['free_quantity']
            discount_pct = item.get('discount_percentage', Decimal('0.00'))

            medicine_obj = MedicineMaster.objects.get(id=medicine_id)
            pack_qty = medicine_obj.pack_qty or 1

            qty_needed  = raw_qty  * pack_qty if uom == 'Strips' else raw_qty
            free_needed = raw_free_qty * pack_qty if uom == 'Strips' else raw_free_qty

            qty_remaining  = qty_needed
            free_remaining = free_needed

            if qty_needed < 0:
                # Same-window exchange
                return_batch = InventoryBatch.objects.select_for_update().filter(
                    medicine_id=medicine_id, medicine__is_active=True
                ).order_by('expiry_date').first()
                if not return_batch:
                    errors[str(medicine_id)] = "Cannot return: No batch found in stock."
                    continue
                deduction_plan.append(
                    (return_batch, qty_needed, 0, discount_pct, uom, pack_qty)
                )
                continue

            batches = list(
                InventoryBatch.objects
                .select_for_update()
                .select_related('medicine')
                .filter(
                    medicine_id=medicine_id,
                    available_quantity__gt=0,
                    medicine__is_active=True,
                )
                .order_by('expiry_date')
            )

            if not batches and (qty_needed > 0 or free_needed > 0):
                errors[str(medicine_id)] = "No stock available for this medicine."
                continue

            for batch in batches:
                if qty_remaining <= 0 and free_remaining <= 0:
                    break
                batch_remaining = batch.available_quantity
                deduct_qty  = min(batch_remaining, qty_remaining)
                qty_remaining  -= deduct_qty
                batch_remaining -= deduct_qty
                deduct_free = min(batch_remaining, free_remaining)
                free_remaining -= deduct_free
                if deduct_qty > 0 or deduct_free > 0:
                    deduction_plan.append(
                        (batch, deduct_qty, deduct_free, discount_pct, uom, pack_qty)
                    )

            if qty_remaining > 0 or free_remaining > 0:
                errors[str(medicine_id)] = (
                    f"Insufficient stock. Shortfall: {qty_remaining} billed, "
                    f"{free_remaining} free."
                )

        if errors:
            raise serializers.ValidationError(errors)

        # ── Step 2a: First-pass item base computation (item discounts only) ───
        # We compute each item's pre-bill-discount taxable base here so we can
        # distribute the bill-level discount in the next step.
        item_pass1 = []
        for batch, qty, free_qty, discount_pct, uom, pack_qty in deduction_plan:
            gst_pct      = batch.medicine.default_gst_percentage
            mrp_per_strip = batch.mrp

            if qty != 0:
                mrp_inclusive_rate = mrp_per_strip / Decimal(str(pack_qty))
                sale_rate_per_unit = (
                    mrp_inclusive_rate / (Decimal('1') + gst_pct / Decimal('100'))
                ).quantize(Decimal('0.0001'))
                line_gross    = (sale_rate_per_unit * Decimal(str(qty))).quantize(Decimal('0.01'))
                line_item_disc = (line_gross * discount_pct / Decimal('100')).quantize(Decimal('0.01'))
                line_base     = line_gross - line_item_disc
            else:
                sale_rate_per_unit = Decimal('0.0000')
                line_base          = Decimal('0.00')

            item_pass1.append({
                'batch': batch, 'qty': qty, 'free_qty': free_qty,
                'discount_pct': discount_pct, 'uom': uom, 'pack_qty': pack_qty,
                'mrp_per_strip': mrp_per_strip, 'gst_pct': gst_pct,
                'sale_rate_per_unit': sale_rate_per_unit,
                'line_base': line_base,
                'bill_discount_share': Decimal('0.00'),
            })

        # ── Step 2b: Distribute bill-level trade discount proportionally ───────
        # BUG-C FIX: Under CGST Act Section 15(3)(a), a trade discount given at
        # the time of supply and recorded on the invoice reduces the taxable value.
        # Subtracting it post-GST (old behaviour) overstated tax liability.
        # We now distribute it proportionally across positive-base items BEFORE
        # computing GST, so every tax figure in the snapshot is GST-compliant.
        positive_indices  = [i for i, d in enumerate(item_pass1) if d['line_base'] > 0]
        positive_subtotal = sum(item_pass1[i]['line_base'] for i in positive_indices)

        if discount > 0 and positive_subtotal > 0:
            allocated = Decimal('0.00')
            for i in positive_indices[:-1]:
                share = (
                    item_pass1[i]['line_base'] / positive_subtotal * discount
                ).quantize(Decimal('0.01'))
                item_pass1[i]['bill_discount_share'] = share
                allocated += share
            if positive_indices:
                # Last positive item absorbs any rounding residual
                item_pass1[positive_indices[-1]]['bill_discount_share'] = discount - allocated

        # ── Step 2c: Final per-item compute + stock deduction ─────────────────
        sales_items_to_create = []
        items_snapshot        = []
        subtotal              = Decimal('0.00')
        total_tax             = Decimal('0.00')
        total_cgst            = Decimal('0.00')
        total_sgst            = Decimal('0.00')
        total_igst            = Decimal('0.00')

        for d in item_pass1:
            batch         = d['batch']
            qty           = d['qty']
            free_qty      = d['free_qty']
            pack_qty      = d['pack_qty']
            uom           = d['uom']
            gst_pct       = d['gst_pct']
            mrp_per_strip = d['mrp_per_strip']
            sale_rate_per_unit = d['sale_rate_per_unit']
            discount_pct  = d['discount_pct']

            adjusted_taxable = d['line_base'] - d['bill_discount_share']

            if qty != 0:
                line_tax  = (adjusted_taxable * gst_pct / Decimal('100')).quantize(Decimal('0.01'))
                line_total = adjusted_taxable + line_tax
            else:
                line_tax   = Decimal('0.00')
                line_total = Decimal('0.00')

            cgst_amt = sgst_amt = igst_amt = Decimal('0.00')
            if line_tax > 0:
                if is_inter_state:
                    igst_amt = line_tax
                else:
                    cgst_amt = (line_tax / Decimal('2')).quantize(Decimal('0.01'))
                    sgst_amt = line_tax - cgst_amt   # absorbs rounding

            subtotal   += adjusted_taxable
            total_tax  += line_tax
            total_cgst += cgst_amt
            total_sgst += sgst_amt
            total_igst += igst_amt

            # Deduct stock (batch already locked via select_for_update in Step 1)
            batch.available_quantity -= (qty + free_qty)
            batch.save()

            # Snapshot quantity in the UOM the clerk used
            if uom == 'Strips':
                snap_qty  = float(qty)  / float(pack_qty)
                snap_free = float(free_qty) / float(pack_qty)
                if float(snap_qty).is_integer():  snap_qty  = int(snap_qty)
                if float(snap_free).is_integer(): snap_free = int(snap_free)
            else:
                snap_qty  = qty
                snap_free = free_qty

            items_snapshot.append({
                "medicine_id":         str(batch.medicine.id),
                "medicine_name":       batch.medicine.name,
                # BUG-A FIX: HSN code frozen in snapshot for GSTR-1 Table 12
                "hsn_code":            batch.medicine.hsn_code or "",
                "uqc":                 batch.medicine.uqc,
                "batch_number":        batch.batch_number,
                "expiry_date":         str(batch.expiry_date),
                "quantity":            snap_qty,
                "uom":                 uom,
                "free_quantity":       snap_free,
                "mrp_per_strip":       str(mrp_per_strip),
                "sale_rate_per_unit":  str(sale_rate_per_unit),
                "discount_percentage": str(discount_pct),
                # BUG-C FIX: taxable_value is post-bill-discount, fully GST-compliant
                "taxable_value":       str(adjusted_taxable),
                "gst_percentage":      str(gst_pct),
                "cgst_amount":         str(cgst_amt),
                "sgst_amount":         str(sgst_amt),
                "igst_amount":         str(igst_amt),
                "drug_schedule":       batch.medicine.drug_schedule,
                "line_total":          str(line_total),
            })

            sales_items_to_create.append({
                'medicine':            batch.medicine,
                'inventory_batch':     batch,
                'batch_number':        batch.batch_number,
                'quantity':            qty,
                'free_quantity':       free_qty,
                'mrp_per_strip':       mrp_per_strip,
                'sale_rate_per_unit':  sale_rate_per_unit,
                'discount_percentage': discount_pct,
                'taxable_value':       adjusted_taxable,
                'gst_percentage':      gst_pct,
                'cgst_amount':         cgst_amt,
                'sgst_amount':         sgst_amt,
                'igst_amount':         igst_amt,
                'line_total':          line_total,
            })

        # ── Step 3: Create SalesBill ───────────────────────────────────────────
        payment_mode = validated_data.get('payment_mode', 'CASH')

        # BUG-C FIX: grand_total = subtotal + tax.
        # 'discount' is already embedded in subtotal (distributed pre-GST);
        # it's stored on the bill for audit/display, NOT subtracted again here.
        grand_total = (subtotal + total_tax).quantize(Decimal('0.01'))

        if payment_mode == 'SPLIT':
            split_sum = sum(
                (Decimal(str(v)) for v in split_payments.values()), Decimal('0.00')
            )
            if split_sum != grand_total:
                raise serializers.ValidationError({
                    "split_payments":
                        f"Split total (₹{split_sum}) must equal grand total (₹{grand_total})."
                })

        bill = SalesBill.objects.create(
            customer          = customer_obj,
            customer_phone    = validated_data.get('customer_phone') or None,
            customer_name     = validated_data.get('customer_name')  or None,
            buyer_address     = validated_data.get('buyer_address')  or None,
            prescriber_name   = validated_data.get('prescriber_name')   or None,
            prescriber_reg_no = validated_data.get('prescriber_reg_no') or None,
            invoice_number    = invoice_number,
            place_of_supply   = place_of_supply,
            billed_by         = request.user,
            subtotal          = subtotal.quantize(Decimal('0.01')),
            total_tax         = total_tax.quantize(Decimal('0.01')),
            total_cgst        = total_cgst.quantize(Decimal('0.01')),
            total_sgst        = total_sgst.quantize(Decimal('0.01')),
            total_igst        = total_igst.quantize(Decimal('0.01')),
            discount          = discount,       # stored for audit/display
            grand_total       = grand_total,
            items_snapshot    = items_snapshot,
            payment_mode      = payment_mode,
            split_payments    = split_payments,
        )

        # ── Step 4: Bulk-create SalesItem rows ────────────────────────────────
        SalesItem.objects.bulk_create([
            SalesItem(sales_bill=bill, pharmacy=pharmacy, **item_data)
            for item_data in sales_items_to_create
        ])

        # ── Step 5: Ledger update for credit component ────────────────────────
        credit_amount = Decimal('0.00')
        if payment_mode == 'CREDIT':
            credit_amount = grand_total
        elif payment_mode == 'SPLIT':
            credit_amount = Decimal(str(split_payments.get('CREDIT', '0.00')))

        if credit_amount > 0 and customer_obj:
            locked_customer = CustomerParty.objects.select_for_update().get(
                id=customer_obj.id
            )
            if locked_customer.credit_limit > 0:
                new_balance = locked_customer.outstanding_balance + credit_amount
                if new_balance > locked_customer.credit_limit:
                    raise serializers.ValidationError(
                        f"This sale of ₹{credit_amount} would exceed "
                        f"{locked_customer.name}'s credit limit of "
                        f"₹{locked_customer.credit_limit}. "
                        f"Current outstanding: ₹{locked_customer.outstanding_balance}."
                    )
            locked_customer.outstanding_balance += credit_amount
            locked_customer.save()

            # BUG-G FIX: Immutable ledger entry for every balance change
            LedgerEntry.objects.create(
                customer         = locked_customer,
                entry_date       = timezone.localdate(),
                entry_type       = 'SALE',
                debit            = credit_amount,
                credit           = Decimal('0.00'),
                balance_after    = locked_customer.outstanding_balance,
                reference_number = invoice_number,
                sales_bill       = bill,
                narration        = f"Credit Sale – {invoice_number}",
            )

        # ── Step 6: Payment-status bookkeeping ────────────────────────────────
        if payment_mode == 'SPLIT':
            if credit_amount > 0 and credit_amount < grand_total:
                bill.payment_status = 'PARTIAL'
                bill.amount_paid    = grand_total - credit_amount
                bill.save(update_fields=['payment_status', 'amount_paid'])
            elif credit_amount == 0:
                bill.payment_status = 'PAID'
                bill.amount_paid    = grand_total
                bill.save(update_fields=['payment_status', 'amount_paid'])
        elif payment_mode in ['CASH', 'UPI']:
            bill.payment_status = 'PAID'
            bill.amount_paid    = grand_total
            bill.save(update_fields=['payment_status', 'amount_paid'])

        # ── Step 7: BillPaymentLine — normalised cash-book rows (BUG-H FIX) ──
        # One row per payment mode. Cash-book queries are now a simple
        # GROUP BY mode + SUM(amount) — no JSON parsing required.
        if payment_mode == 'SPLIT':
            payment_lines = [
                BillPaymentLine(
                    bill=bill, pharmacy=pharmacy,
                    mode=mode, amount=Decimal(str(amt)),
                )
                for mode, amt in split_payments.items()
            ]
        else:
            payment_lines = [
                BillPaymentLine(
                    bill=bill, pharmacy=pharmacy,
                    mode=payment_mode, amount=grand_total,
                )
            ]
        BillPaymentLine.objects.bulk_create(payment_lines)

        return bill


# ── Sales Return ───────────────────────────────────────────────────────────────

class SalesReturnSerializer(serializers.ModelSerializer):
    """
    Accepts: sales_bill, sales_item, return_quantity, reason

    Server computes:
      - refund_amount    (proportional share of original line_total)
      - credit_note_number  (sequential CN/FY/NNNNN — BUG-D fix)
      - cgst/sgst/igst   (proportional to original SalesItem — BUG-E fix)

    Atomic create:
      1. Lock Pharmacy → generate credit note number
      2. Restore stock to exact source batch
      3. Reduce outstanding_balance (capped at credit portion for SPLIT bills)
      4. Write immutable LedgerEntry  (BUG-G fix)
    """
    class Meta:
        model  = SalesReturn
        fields = [
            'id', 'sales_bill', 'sales_item',
            'return_quantity', 'refund_amount', 'return_date', 'reason',
        ]
        read_only_fields = [
            'id', 'pharmacy', 'refund_amount',
        ]

    def validate(self, data):
        sales_bill = data['sales_bill']
        sales_item = data['sales_item']
        return_qty = data['return_quantity']

        if sales_item.sales_bill_id != sales_bill.id:
            raise serializers.ValidationError(
                "This sales item does not belong to the provided sales bill."
            )

        already_returned = sum(r.return_quantity for r in sales_item.returns.all())
        returnable = sales_item.quantity - already_returned
        if return_qty > returnable:
            raise serializers.ValidationError(
                f"Cannot return {return_qty} tablets. "
                f"Original: {sales_item.quantity}, "
                f"already returned: {already_returned}, "
                f"remaining: {returnable}."
            )

        # Proportional refund (tax-inclusive, matches original payment)
        refund_amount = (
            (Decimal(str(return_qty)) / Decimal(str(sales_item.quantity)))
            * sales_item.line_total
        ).quantize(Decimal('0.01'))
        self._refund_amount = refund_amount

        # BUG-E FIX: Compute CGST/SGST/IGST split for the credit note.
        # Derive proportional taxable value → back-calculate tax component →
        # split using original bill's intra/inter-state indicator.
        refund_taxable = (
            (Decimal(str(return_qty)) / Decimal(str(sales_item.quantity)))
            * sales_item.taxable_value
        ).quantize(Decimal('0.01'))
        refund_tax = (refund_amount - refund_taxable).quantize(Decimal('0.01'))

        if sales_bill.total_igst > 0:
            self._cn_igst = refund_tax
            self._cn_cgst = Decimal('0.00')
            self._cn_sgst = Decimal('0.00')
        else:
            self._cn_cgst = (refund_tax / Decimal('2')).quantize(Decimal('0.01'))
            self._cn_sgst = refund_tax - self._cn_cgst
            self._cn_igst = Decimal('0.00')

        self._batch = sales_item.inventory_batch
        return data

    @transaction.atomic
    def create(self, validated_data):
        sales_bill = validated_data['sales_bill']

        # ── Lock Pharmacy FIRST (consistent lock order) ───────────────────────
        # BUG-D FIX: Generate sequential credit note number per financial year.
        # Same FY-rollover reset logic as the invoice counter.
        today    = timezone.localdate()
        fy_year  = today.year if today.month >= 4 else today.year - 1
        fy_label = f"{fy_year}-{str(fy_year + 1)[2:]}"

        pharmacy = Pharmacy.objects.select_for_update().get(
            id=self._batch.pharmacy_id
        )
        if pharmacy.current_fy != fy_label:
            pharmacy.invoice_counter = 0
            pharmacy.cn_counter      = 0
            pharmacy.current_fy      = fy_label
        pharmacy.cn_counter += 1
        pharmacy.save(update_fields=['cn_counter', 'invoice_counter', 'current_fy'])
        credit_note_number = f"CN/{fy_label}/{pharmacy.cn_counter:05d}"

        # ── Restore stock to exact source batch ───────────────────────────────
        batch = InventoryBatch.objects.select_for_update().get(pk=self._batch.pk)
        batch.available_quantity += validated_data['return_quantity']
        batch.save()

        # ── Reduce outstanding balance (capped at credit portion) ─────────────
        refund_amount     = self._refund_amount
        balance_reduction = Decimal('0.00')
        locked_customer   = None

        if refund_amount > Decimal('0.00') and sales_bill.payment_mode in ['CREDIT', 'SPLIT']:
            if sales_bill.customer:
                locked_customer = CustomerParty.objects.select_for_update().get(
                    id=sales_bill.customer.id
                )
                if sales_bill.payment_mode == 'SPLIT':
                    credit_portion    = Decimal(str(sales_bill.split_payments.get('CREDIT', '0.00')))
                    balance_reduction = min(refund_amount, credit_portion)
                else:
                    balance_reduction = refund_amount

                if balance_reduction > Decimal('0.00'):
                    locked_customer.outstanding_balance -= balance_reduction
                    locked_customer.save()

        # ── Persist the SalesReturn with full GST breakdown ───────────────────
        validated_data['refund_amount'] = refund_amount
        sales_return = SalesReturn.objects.create(
            credit_note_number = credit_note_number,
            cgst_amount        = self._cn_cgst,
            sgst_amount        = self._cn_sgst,
            igst_amount        = self._cn_igst,
            **validated_data
        )

        # ── BUG-G FIX: Immutable ledger entry ─────────────────────────────────
        if balance_reduction > Decimal('0.00') and locked_customer:
            LedgerEntry.objects.create(
                customer         = locked_customer,
                entry_date       = timezone.localdate(),
                entry_type       = 'RETURN',
                debit            = Decimal('0.00'),
                credit           = balance_reduction,
                balance_after    = locked_customer.outstanding_balance,
                reference_number = credit_note_number,
                sales_return     = sales_return,
                narration        = (
                    f"Sales Return – {credit_note_number} "
                    f"against Invoice {sales_bill.invoice_number or str(sales_bill.id)[:8]}"
                ),
            )

        return sales_return


# ── Payment Receipt ────────────────────────────────────────────────────────────

class PaymentReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PaymentReceipt
        fields = [
            'id', 'customer', 'amount', 'payment_mode',
            'reference_number', 'receipt_date', 'notes', 'amount_allocated',
        ]
        read_only_fields = ['id', 'amount_allocated']

    def validate_amount(self, value):
        if value <= Decimal('0.00'):
            raise serializers.ValidationError(
                "Payment amount must be greater than zero."
            )
        return value

    def validate(self, data):
        customer = data.get('customer')
        amount   = data.get('amount', Decimal('0.00'))
        if customer and amount > customer.outstanding_balance:
            raise serializers.ValidationError(
                f"Payment (₹{amount}) exceeds outstanding balance "
                f"(₹{customer.outstanding_balance})."
            )
        return data

    @transaction.atomic
    def create(self, validated_data):
        amount = validated_data['amount']

        # Lock customer row first (consistent order)
        customer = CustomerParty.objects.select_for_update().get(
            id=validated_data['customer'].id
        )
        customer.outstanding_balance -= amount
        customer.save()

        receipt = PaymentReceipt.objects.create(**validated_data)

        # Auto-allocate FIFO across unpaid bills
        unpaid_bills = SalesBill.objects.select_for_update().filter(
            customer=customer
        ).exclude(payment_status='PAID').order_by('bill_date')

        remaining = amount
        for bill in unpaid_bills:
            if remaining <= Decimal('0.00'):
                break
            bill_due     = bill.grand_total - bill.amount_paid
            apply_amount = min(remaining, bill_due)
            PaymentAllocation.objects.create(
                receipt=receipt, bill=bill, amount_applied=apply_amount
            )
            bill.amount_paid += apply_amount
            if bill.amount_paid >= bill.grand_total:
                bill.payment_status = 'PAID'
            elif bill.amount_paid > Decimal('0.00'):
                bill.payment_status = 'PARTIAL'
            bill.save()
            remaining -= apply_amount

        receipt.amount_allocated = amount - remaining
        receipt.save()

        # BUG-G FIX: Immutable ledger entry for every payment
        LedgerEntry.objects.create(
            customer         = customer,
            entry_date       = validated_data.get('receipt_date', timezone.localdate()),
            entry_type       = 'PAYMENT',
            debit            = Decimal('0.00'),
            credit           = amount,
            balance_after    = customer.outstanding_balance,
            reference_number = (
                validated_data.get('reference_number') or
                str(receipt.id)[:8].upper()
            ),
            payment_receipt  = receipt,
            narration        = (
                f"Payment Received – {validated_data.get('payment_mode', '')}"
            ),
        )

        return receipt
