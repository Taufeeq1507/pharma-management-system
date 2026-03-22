# inventory/views.py
from rest_framework import viewsets, generics, status
from rest_framework.response import Response
from rest_framework.generics import get_object_or_404
from accounts.permissions import IsClerkOrHigher, IsOwnerOrHigher
from django.db.models import Prefetch
from .models import (
    Supplier, MedicineMaster, PurchaseBill,
    InventoryBatch, WarehouseBlock, ShelfLocation, StockAdjustment,
)
from .serializers import (
    SupplierSerializer, MedicineMasterSerializer,
    PurchaseBillSerializer,
    InventoryBatchSerializer,
    MedicineSearchSerializer,
    PurchaseReturnSerializer,
    WarehouseBlockSerializer,
    ShelfLocationSerializer,
    ShelfAssignmentSerializer,
    StockSyncSerializer,
    StockAdjustmentSerializer,
)


# ---------------------------------------------------------------------------
# Master Data ViewSets
# ---------------------------------------------------------------------------

class SupplierViewSet(viewsets.ModelViewSet):
    """List, create, and update suppliers. Soft-delete via is_active=False — no hard deletes."""
    serializer_class = SupplierSerializer
    permission_classes = [IsClerkOrHigher]
    http_method_names = ['get', 'post', 'put', 'patch']
    def get_queryset(self):
        return Supplier.objects.filter(is_active=True)


class MedicineMasterViewSet(viewsets.ModelViewSet):
    """List, create, and update medicines. Soft-delete via is_active=False — no hard deletes."""
    serializer_class = MedicineMasterSerializer
    permission_classes = [IsClerkOrHigher]
    http_method_names = ['get', 'post', 'put', 'patch']
    def get_queryset(self):
        return MedicineMaster.objects.filter(is_active=True)


# ---------------------------------------------------------------------------
# Purchase Bill ViewSet
# ---------------------------------------------------------------------------

class PurchaseBillViewSet(viewsets.ModelViewSet):
    """
    POST /api/inventory/purchase/ — Submits a full supplier invoice in one JSON payload.
    GET  /api/inventory/purchase/  — Lists all purchase bills for the current pharmacy.
    GET  /api/inventory/purchase/{id}/ — Retrieves a single bill with all line items.

    Bills are IMMUTABLE once posted — no PATCH/PUT/DELETE.
    """
    serializer_class = PurchaseBillSerializer
    permission_classes = [IsOwnerOrHigher]
    http_method_names = ['get', 'post']
    def get_queryset(self):
        return PurchaseBill.objects.all().prefetch_related('items')


# ---------------------------------------------------------------------------
# Stock List & Search Views
# ---------------------------------------------------------------------------

class StockListView(generics.ListAPIView):
    """GET /api/inventory/stock/ — Live batches with stock. Optional ?medicine=<uuid>"""
    serializer_class = InventoryBatchSerializer
    permission_classes = [IsClerkOrHigher]

    def get_queryset(self):
        qs = InventoryBatch.objects.filter(available_quantity__gt=0).select_related('medicine')
        medicine_id = self.request.query_params.get('medicine')
        if medicine_id:
            qs = qs.filter(medicine__id=medicine_id)
        return qs


class MedicineSearchView(generics.ListAPIView):
    """GET /api/inventory/search/?q=dolo — Medicine search with live batches for billing screen."""
    serializer_class = MedicineSearchSerializer
    permission_classes = [IsClerkOrHigher]

    def get_queryset(self):
        q = self.request.query_params.get('q', '').strip()
        if not q:
            return MedicineMaster.objects.none()
        return (
            MedicineMaster.objects
            .filter(name__icontains=q, is_active=True)
            .prefetch_related(
                Prefetch(
                    'live_batches',
                    queryset=InventoryBatch.objects.filter(
                        available_quantity__gt=0
                    )
                )
            )
        )


# ---------------------------------------------------------------------------
# Purchase Return View
# ---------------------------------------------------------------------------

class PurchaseReturnView(generics.CreateAPIView):
    """POST /api/inventory/return/ — Debit note + atomic stock deduction."""
    serializer_class = PurchaseReturnSerializer
    permission_classes = [IsOwnerOrHigher]


# ---------------------------------------------------------------------------
# WAREHOUSE BLOCK VIEWS
# ---------------------------------------------------------------------------

class WarehouseBlockViewSet(viewsets.ModelViewSet):
    """
    GET/POST  /api/inventory/blocks/        — List blocks or create a new block.
    GET/PATCH /api/inventory/blocks/{id}/   — View or resize a block (no hard delete).

    Blocks are owned entities — one per letter per pharmacy (e.g. Block A, Block B).
    shelf_count can be updated (PATCH) when the owner reorganises the warehouse.
    Deleting blocks is disabled — shelves and adjustment history would be orphaned.
    """
    serializer_class = WarehouseBlockSerializer
    permission_classes = [IsOwnerOrHigher]
    http_method_names = ['get', 'post', 'put', 'patch']  # No hard delete

    def get_queryset(self):
        return WarehouseBlock.objects.prefetch_related(
            'shelves__batches__medicine'
        ).order_by('block_letter')


class BlockShelvesView(generics.ListAPIView):
    """
    GET /api/inventory/blocks/<block_letter>/shelves/
    Lists all ShelfLocation rows that have been created within a block
    (i.e. shelves that have had at least one batch assigned to them).
    """
    serializer_class = ShelfLocationSerializer
    permission_classes = [IsClerkOrHigher]

    def get_queryset(self):
        letter = self.kwargs['block_letter'].upper()
        return (
            ShelfLocation.objects
            .filter(block__block_letter=letter)
            .prefetch_related('batches__medicine')
            .select_related('block')
            .order_by('shelf_number')
        )


class ShelfDetailView(generics.RetrieveAPIView):
    """
    GET /api/inventory/shelves/<uuid:pk>/
    Returns a single shelf showing all batches currently on it.
    """
    serializer_class = ShelfLocationSerializer
    permission_classes = [IsClerkOrHigher]

    def get_queryset(self):
        return (
            ShelfLocation.objects
            .prefetch_related('batches__medicine')
            .select_related('block')
        )


class UnassignedBatchListView(generics.ListAPIView):
    """
    GET /api/inventory/batches/unassigned/
    Lists batches that have stock but have not yet been assigned to a shelf.
    This is the "pending placement" queue clerks work through after new stock arrives.
    """
    serializer_class = InventoryBatchSerializer
    permission_classes = [IsClerkOrHigher]

    def get_queryset(self):
        return (
            InventoryBatch.objects
            .filter(shelf__isnull=True, available_quantity__gt=0)
            .select_related('medicine')
        )


class ShelfAssignView(generics.GenericAPIView):
    """
    POST /api/inventory/batches/<uuid:batch_id>/assign/
    Assigns or moves a batch to a shelf address (block_letter + shelf_number).

    Enforces the hard shelf constraint:
        One medicine = one batch number per shelf, no exceptions.
    Moving a batch to its own current shelf is idempotent (handled via .exclude(pk=batch.pk)).
    """
    serializer_class = ShelfAssignmentSerializer
    permission_classes = [IsClerkOrHigher]

    def post(self, request, batch_id):
        batch = get_object_or_404(InventoryBatch, pk=batch_id)

        serializer = ShelfAssignmentSerializer(
            data=request.data,
            context={'request': request, 'batch': batch}
        )
        serializer.is_valid(raise_exception=True)
        batch = serializer.save()

        return Response(
            InventoryBatchSerializer(batch).data,
            status=status.HTTP_200_OK
        )


class StockSyncView(generics.GenericAPIView):
    """
    POST /api/inventory/sync/
    Submits physical stock-count results for all batches on one shelf.

    The entire sync is fully atomic — a failure on any single batch update
    rolls back ALL quantity changes from that sync call.
    Only Owners and above can confirm a sync.
    """
    serializer_class = StockSyncSerializer
    permission_classes = [IsOwnerOrHigher]

    def post(self, request):
        serializer = StockSyncSerializer(
            data=request.data,
            context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        result = serializer.save()
        return Response(result, status=status.HTTP_200_OK)


class StockAdjustmentListView(generics.ListAPIView):
    """
    GET /api/inventory/adjustments/
    Lists all adjustment records for this pharmacy, newest first.
    Optional filter: ?batch=<uuid> to see history for a specific batch.
    """
    serializer_class = StockAdjustmentSerializer
    permission_classes = [IsOwnerOrHigher]

    def get_queryset(self):
        qs = (
            StockAdjustment.objects
            .select_related('inventory_batch__medicine', 'adjusted_by', 'shelf__block')
            .order_by('-adjusted_at')
        )
        batch_id = self.request.query_params.get('batch')
        if batch_id:
            qs = qs.filter(inventory_batch_id=batch_id)
        return qs