# inventory/serializers.py
from decimal import Decimal
from rest_framework import serializers
from django.db import transaction
from accounts.utils import get_current_pharmacy
from .models import (
    Supplier, MedicineMaster, PurchaseBill, PurchaseItem,
    InventoryBatch, PurchaseReturn,
    WarehouseBlock, ShelfLocation, StockAdjustment,
)


# ---------------------------------------------------------------------------
# Master Data Serializers
# ---------------------------------------------------------------------------

class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = '__all__'
        read_only_fields = ['id', 'pharmacy']


class MedicineMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = MedicineMaster
        fields = '__all__'
        read_only_fields = ['id', 'pharmacy']


# ---------------------------------------------------------------------------
# Purchase Bill Serializers
# ---------------------------------------------------------------------------

class PurchaseItemSerializer(serializers.ModelSerializer):
    """
    Handles individual line items on a supplier invoice.
    'purchase_bill' and 'pharmacy' are excluded — set automatically by parent.
    """
    class Meta:
        model = PurchaseItem
        fields = [
            'medicine', 'batch_number', 'expiry_date',
            'quantity', 'free_quantity',
            'purchase_rate_base', 'discount_percentage', 'gst_percentage', 'mrp'
        ]


class PurchaseBillSerializer(serializers.ModelSerializer):
    """
    Handles a full supplier invoice in a single POST payload.
    - subtotal, total_tax, grand_total are READ-ONLY — auto-calculated server-side.
    - discount is the only financial field accepted from the client.
    - Nested 'items' triggers the upsert logic on InventoryBatch.

    CRITICAL UNIT SPLIT:
    - Financial calc uses STRIPS (qty) — rate is per-strip
    - Stock update uses TABLETS — (qty + free_qty) * medicine.pack_qty
    """
    items = PurchaseItemSerializer(many=True)

    class Meta:
        model = PurchaseBill
        fields = [
            'id', 'supplier', 'invoice_number', 'bill_date',
            'subtotal', 'total_tax', 'total_cgst', 'total_sgst', 'total_igst',
            'discount', 'grand_total', 'payment_status', 'items'
        ]
        read_only_fields = [
            'id', 'pharmacy', 'subtotal', 'total_tax', 
            'total_cgst', 'total_sgst', 'total_igst', 'grand_total'
        ]

    def create(self, validated_data):
        items_data = validated_data.pop('items')
        discount = validated_data.get('discount', Decimal('0.00'))
        
        supplier = validated_data['supplier']

        with transaction.atomic():
            bill = PurchaseBill.objects.create(**validated_data)
            pharmacy = bill.pharmacy

            is_inter_state = False
            if supplier.state and pharmacy.state:
                is_inter_state = supplier.state.strip().lower() != pharmacy.state.strip().lower()

            running_subtotal = Decimal('0.00')
            running_tax      = Decimal('0.00')
            running_cgst     = Decimal('0.00')
            running_sgst     = Decimal('0.00')
            running_igst     = Decimal('0.00')

            for item_data in items_data:
                qty      = item_data['quantity']
                free_qty = item_data.get('free_quantity', 0)
                rate     = item_data['purchase_rate_base']
                discount_pct = item_data.get('discount_percentage', Decimal('0.00'))
                gst      = item_data['gst_percentage']

                # FINANCIAL CALC: strips × rate (rate is per-strip)
                line_gross    = rate * qty
                line_discount = line_gross * (discount_pct / Decimal('100'))
                line_base     = line_gross - line_discount
                
                line_tax      = line_base * (gst / Decimal('100'))
                
                # Tax breakdown per item
                cgst_amt = Decimal('0.00')
                sgst_amt = Decimal('0.00')
                igst_amt = Decimal('0.00')

                if is_inter_state:
                    igst_amt = line_tax
                else:
                    cgst_amt = line_tax / Decimal('2')
                    sgst_amt = line_tax / Decimal('2')

                PurchaseItem.objects.create(
                    purchase_bill=bill,
                    taxable_value=line_base,
                    cgst_amount=cgst_amt,
                    sgst_amount=sgst_amt,
                    igst_amount=igst_amt,
                    **item_data
                )

                running_subtotal += line_base
                running_tax      += line_tax
                running_cgst     += cgst_amt
                running_sgst     += sgst_amt
                running_igst     += igst_amt

                # STOCK UPDATE: convert strips → tablets via pack_qty
                pack_qty    = item_data['medicine'].pack_qty
                total_units = (qty + free_qty) * pack_qty

                batch, created = InventoryBatch.objects.get_or_create(
                    medicine=item_data['medicine'],
                    batch_number=item_data['batch_number'],
                    mrp=item_data['mrp'],
                    gst_percentage=item_data['gst_percentage'],
                    defaults={
                        'expiry_date':        item_data['expiry_date'],
                        'available_quantity': total_units,
                        
                    }
                )

                if not created:
                    batch.available_quantity += total_units
                    batch.save()

            bill.subtotal    = running_subtotal
            bill.total_tax   = running_tax
            bill.total_cgst  = running_cgst
            bill.total_sgst  = running_sgst
            bill.total_igst  = running_igst
            bill.grand_total = running_subtotal + running_tax - discount
            bill.save()

        return bill


# ---------------------------------------------------------------------------
# Inventory Batch Serializer (read-only stock view)
# ---------------------------------------------------------------------------

class InventoryBatchSerializer(serializers.ModelSerializer):
    medicine_name    = serializers.CharField(source='medicine.name',          read_only=True)
    medicine_company = serializers.CharField(source='medicine.company',       read_only=True)
    packaging        = serializers.CharField(source='medicine.packaging',     read_only=True)
    drug_schedule    = serializers.CharField(source='medicine.drug_schedule', read_only=True)
    pack_qty         = serializers.IntegerField(source='medicine.pack_qty',   read_only=True)

    class Meta:
        model = InventoryBatch
        fields = [
            'id', 'medicine', 'medicine_name', 'medicine_company',
            'packaging', 'drug_schedule', 'pack_qty',
            'batch_number', 'expiry_date', 'available_quantity',
            'gst_percentage', 'mrp', 'shelf'
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Medicine Search Serializer
# ---------------------------------------------------------------------------

class MedicineSearchSerializer(serializers.ModelSerializer):
    live_batches = InventoryBatchSerializer(many=True, read_only=True)

    class Meta:
        model = MedicineMaster
        fields = [
            'id', 'name', 'company', 'category',
            'packaging', 'pack_qty', 'default_gst_percentage',
            'salt_name', 'barcode', 'drug_schedule', 'is_active',
            'live_batches'
        ]


# ---------------------------------------------------------------------------
# Purchase Return Serializer
# ---------------------------------------------------------------------------

class PurchaseReturnSerializer(serializers.ModelSerializer):
    inventory_batch = serializers.PrimaryKeyRelatedField(
        queryset=InventoryBatch.objects.all()
    )
    medicine_name = serializers.CharField(
        source='medicine.name', read_only=True
    )

    class Meta:
        model = PurchaseReturn
        fields = [
            'id', 'supplier', 'original_bill',
            'inventory_batch',
            'medicine','medicine_name',
            'batch_number', 'mrp',
            'return_quantity', 'refund_amount', 'return_date', 'reason'
        ]
        read_only_fields = ['id', 'pharmacy', 'medicine_name', 'batch_number', 'mrp']

    def validate(self, data):
        batch      = data['inventory_batch']
        return_qty = data['return_quantity']
        pack_qty   = batch.medicine.pack_qty

        if return_qty % pack_qty != 0:
            raise serializers.ValidationError(
                f"Return quantity must be a multiple of {pack_qty} "
                f"(one strip of {batch.medicine.name} contains {pack_qty} tablets). "
                f"You entered {return_qty}."
            )

        if return_qty > batch.available_quantity:
            raise serializers.ValidationError(
                f"Return quantity ({return_qty} tablets) exceeds available "
                f"stock ({batch.available_quantity} tablets) for batch "
                f"'{batch.batch_number}'."
            )

        self._batch = batch
        return data

    def create(self, validated_data):
        batch = validated_data.pop('inventory_batch')

        with transaction.atomic():
            batch.available_quantity -= validated_data['return_quantity']
            batch.save()

            return PurchaseReturn.objects.create(
                medicine     = batch.medicine,
                batch_number = batch.batch_number,
                mrp          = batch.mrp,
                **validated_data
            )


# ---------------------------------------------------------------------------
# WAREHOUSE BLOCK SERIALIZERS
# ---------------------------------------------------------------------------

class WarehouseBlockSerializer(serializers.ModelSerializer):
    """
    Represents a named rack section (e.g. Block A).
    'shelves' returns the configured shelf_count (not live rows).
    'occupied_shelves' counts distinct shelf rows that have live stock.
    block_letter is always normalised to uppercase.
    """
    shelves          = serializers.SerializerMethodField()
    occupied_shelves = serializers.SerializerMethodField()

    def get_shelves(self, obj):
        return obj.shelf_count

    def get_occupied_shelves(self, obj):
        return obj.shelves.filter(batches__available_quantity__gt=0).distinct().count()

    class Meta:
        model  = WarehouseBlock
        fields = ['id', 'block_letter', 'shelf_count', 'label', 'shelves', 'occupied_shelves']
        read_only_fields = ['id', 'pharmacy']

    def validate_block_letter(self, value):
        value = value.upper()
        if not value.isalpha() or len(value) != 1:
            raise serializers.ValidationError(
                "block_letter must be a single uppercase letter (A-Z)."
            )
        return value

    def validate_shelf_count(self, value):
        if value < 1:
            raise serializers.ValidationError("shelf_count must be at least 1.")
        return value

    def validate(self, data):
        """
        Guard against reducing shelf_count below shelves that already have
        batches assigned. Only runs on updates (PATCH/PUT) — self.instance
        is None on creation so the check is skipped automatically.
        """
        new_shelf_count = data.get('shelf_count')

        # Only relevant when shelf_count is being changed on an existing block
        if self.instance is None or new_shelf_count is None:
            return data

        # If count is staying the same or growing, nothing to check
        if new_shelf_count >= self.instance.shelf_count:
            return data

        # Find shelves that would be cut off AND still have batches assigned
        affected_shelves = (
            ShelfLocation.objects
            .filter(
                block=self.instance,
                shelf_number__gt=new_shelf_count,
                batches__isnull=False,
            )
            .distinct()
            .order_by('shelf_number')
        )

        if affected_shelves.exists():
            shelf_list = ', '.join(
                f"{self.instance.block_letter}-{s.shelf_number}"
                for s in affected_shelves
            )
            raise serializers.ValidationError(
                f"Cannot reduce Block {self.instance.block_letter} to {new_shelf_count} shelves. "
                f"The following shelves still have stock assigned to them: {shelf_list}. "
                f"Reassign or clear those batches before shrinking the block."
            )

        return data


class ShelfLocationSerializer(serializers.ModelSerializer):
    """
    Represents one shelf within a block. Address format: "{block_letter}-{shelf_number}".
    e.g. A-3 = Block A, Shelf 3.
    """
    block_letter = serializers.CharField(source='block.block_letter', read_only=True)
    address      = serializers.SerializerMethodField()
    batches      = InventoryBatchSerializer(many=True, read_only=True)

    def get_address(self, obj):
        return f"{obj.block.block_letter}-{obj.shelf_number}"

    class Meta:
        model  = ShelfLocation
        fields = ['id', 'block', 'block_letter', 'shelf_number', 'address', 'batches']
        read_only_fields = ['id', 'pharmacy', 'batches']


# ---------------------------------------------------------------------------
# SHELF ASSIGNMENT SERIALIZER
# ---------------------------------------------------------------------------

class ShelfAssignmentSerializer(serializers.Serializer):
    """
    Assigns (or moves) an InventoryBatch to a shelf address.
    Input: block_letter + shelf_number (e.g. "A" + 3 → A-3).

    THE SHELF CONSTRAINT (hard block — no override, no exception):
        A shelf may hold many different medicines.
        But each medicine may only appear under ONE batch number per shelf.
        Same medicine, different batches = HARD BLOCK.

    block_letter is normalised to uppercase on input.
    Shelf rows are created on-demand via get_or_create — never pre-populated.
    """
    block_letter = serializers.CharField(max_length=1)
    shelf_number = serializers.IntegerField(min_value=1)

    def validate(self, data):
        block_letter = data['block_letter'].upper()
        shelf_number = data['shelf_number']
        pharmacy     = get_current_pharmacy()

        # 1. Look up the WarehouseBlock — must already exist (owner creates it)
        try:
            block = WarehouseBlock.objects.get(pharmacy=pharmacy, block_letter=block_letter)
        except WarehouseBlock.DoesNotExist:
            raise serializers.ValidationError(
                f"Block '{block_letter}' does not exist for this pharmacy."
            )

        # 2. Validate shelf_number is within the block's configured range
        if shelf_number > block.shelf_count:
            raise serializers.ValidationError(
                f"Block {block_letter} only has {block.shelf_count} shelves. "
                f"Shelf {shelf_number} does not exist."
            )

        # 3. Get the batch being assigned (injected by the view)
        batch = self.context['batch']

        # 4. Get or create the ShelfLocation (shelves are virtual until first assignment)
        shelf, _ = ShelfLocation.objects.get_or_create(
            pharmacy=pharmacy,
            block=block,
            shelf_number=shelf_number,
        )

        # 5. Enforce the shelf constraint: same medicine, different batch = hard block
        conflicting_batch = (
            InventoryBatch.objects
            .filter(medicine=batch.medicine, shelf=shelf)
            .exclude(pk=batch.pk)  # Allow re-assigning a batch to its own current shelf
            .first()
        )

        if conflicting_batch:
            raise serializers.ValidationError(
                f"Shelf {block_letter}-{shelf_number} already contains {batch.medicine.name} "
                f"from batch {conflicting_batch.batch_number}. "
                f"A shelf cannot hold the same medicine in two different batches. "
                f"Assign this batch to a different shelf."
            )

        # 6. Stash the resolved shelf for save()
        self._shelf = shelf
        return data

    def save(self):
        batch       = self.context['batch']
        batch.shelf = self._shelf
        batch.save()
        return batch


# ---------------------------------------------------------------------------
# STOCK SYNC SERIALIZERS
# ---------------------------------------------------------------------------

class SyncItemSerializer(serializers.Serializer):
    """One line in a sync payload — the clerk's physical count for one batch."""
    inventory_batch_id = serializers.UUIDField()
    actual_quantity    = serializers.IntegerField(min_value=0)


class StockSyncSerializer(serializers.Serializer):
    """
    Accepts the full physical-count results for all batches on ONE shelf.
    Addressed by block_letter + shelf_number (e.g. Block A Shelf 3).

    Validation rules (ALL checked BEFORE any write):
    - WarehouseBlock must exist.
    - ShelfLocation must exist (no shelf creation during sync).
    - Every batch_id must be assigned to this exact shelf.
    - Any mismatch is rejected with a clear error before writes begin.

    Save rules (ALL inside transaction.atomic()):
    - Skip batches where count matches current quantity.
    - Create StockAdjustment then update batch for every change.
    - delta = new - old (server-computed, never client-supplied).
    - adjusted_by = request.user (server-set, never client-supplied).
    """
    block_letter = serializers.CharField(max_length=1)
    shelf_number = serializers.IntegerField(min_value=1)
    items        = SyncItemSerializer(many=True)

    def validate(self, data):
        block_letter = data['block_letter'].upper()
        shelf_number = data['shelf_number']
        items        = data['items']
        pharmacy     = get_current_pharmacy()

        # 1. Look up the block
        try:
            block = WarehouseBlock.objects.get(pharmacy=pharmacy, block_letter=block_letter)
        except WarehouseBlock.DoesNotExist:
            raise serializers.ValidationError(
                f"Block '{block_letter}' does not exist for this pharmacy."
            )

        # 2. Look up the shelf — must already exist (never create during sync)
        try:
            shelf = ShelfLocation.objects.get(pharmacy=pharmacy, block=block, shelf_number=shelf_number)
        except ShelfLocation.DoesNotExist:
            raise serializers.ValidationError(
                f"Shelf {block_letter}-{shelf_number} has never been assigned any stock."
            )

        # 3. Resolve and validate every batch before writing anything
        sync_pairs = []
        for item in items:
            batch_id   = item['inventory_batch_id']
            actual_qty = item['actual_quantity']

            try:
                batch = InventoryBatch.objects.get(pk=batch_id)
            except InventoryBatch.DoesNotExist:
                raise serializers.ValidationError(
                    f"InventoryBatch '{batch_id}' does not exist or does not belong to this pharmacy."
                )

            if batch.shelf_id != shelf.id:
                raise serializers.ValidationError(
                    f"Batch {batch.batch_number} of {batch.medicine.name} is not assigned to "
                    f"shelf {block_letter}-{shelf_number}. Sync payload mismatch."
                )

            sync_pairs.append((batch, actual_qty))

        # 4. Stash for save()
        self._sync_pairs = sync_pairs
        self._shelf      = shelf
        return data

    def save(self):
        request     = self.context['request']
        adjustments = []

        with transaction.atomic():
            for batch, actual_quantity in self._sync_pairs:
                if actual_quantity == batch.available_quantity:
                    continue  # No change — skip silently

                StockAdjustment.objects.create(
                    inventory_batch = batch,
                    shelf           = self._shelf,
                    old_quantity    = batch.available_quantity,
                    new_quantity    = actual_quantity,
                    delta           = actual_quantity - batch.available_quantity,
                    adjusted_by     = request.user,
                    reason          = "Weekly Sync",
                    source          = 'SYNC',
                )
                adjustments.append(True)

                batch.available_quantity = actual_quantity
                batch.save()

        return {
            "shelf":            f"{self._shelf.block.block_letter}-{self._shelf.shelf_number}",
            "items_checked":    len(self._sync_pairs),
            "adjustments_made": len(adjustments),
        }


# ---------------------------------------------------------------------------
# STOCK ADJUSTMENT HISTORY SERIALIZER (read-only)
# ---------------------------------------------------------------------------

class StockAdjustmentSerializer(serializers.ModelSerializer):
    medicine_name     = serializers.CharField(source='inventory_batch.medicine.name', read_only=True)
    batch_number      = serializers.CharField(source='inventory_batch.batch_number',  read_only=True)
    adjusted_by_phone = serializers.CharField(source='adjusted_by.phone_number',       read_only=True)
    shelf_address     = serializers.SerializerMethodField()

    def get_shelf_address(self, obj):
        if obj.shelf:
            return f"{obj.shelf.block.block_letter}-{obj.shelf.shelf_number}"
        return None

    class Meta:
        model  = StockAdjustment
        fields = [
            'id', 'medicine_name', 'batch_number',
            'shelf_address', 'old_quantity', 'new_quantity', 'delta',
            'adjusted_by_phone', 'adjusted_at', 'reason', 'source',
        ]