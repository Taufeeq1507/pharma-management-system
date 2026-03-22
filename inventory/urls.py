# inventory/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    SupplierViewSet,
    MedicineMasterViewSet,
    PurchaseBillViewSet,
    StockListView,
    MedicineSearchView,
    PurchaseReturnView,
    WarehouseBlockViewSet,
    BlockShelvesView,
    ShelfDetailView,
    UnassignedBatchListView,
    ShelfAssignView,
    StockSyncView,
    StockAdjustmentListView,
)

router = DefaultRouter()
router.register(r'suppliers', SupplierViewSet,      basename='supplier')
router.register(r'medicines', MedicineMasterViewSet, basename='medicine')
router.register(r'purchase',  PurchaseBillViewSet,   basename='purchase')
router.register(r'blocks',    WarehouseBlockViewSet,  basename='block')

urlpatterns = [
    # Router-generated ViewSet routes
    path('', include(router.urls)),

    # Module 2: Inward flow endpoints
    path('stock/',   StockListView.as_view(),     name='stock_list'),
    path('search/',  MedicineSearchView.as_view(), name='medicine_search'),
    path('return/',  PurchaseReturnView.as_view(), name='purchase_return'),

    # Warehouse block + shelf management endpoints
    path('blocks/<str:block_letter>/shelves/',     BlockShelvesView.as_view(),        name='block_shelves'),
    path('shelves/<uuid:pk>/',                     ShelfDetailView.as_view(),         name='shelf_detail'),
    path('batches/unassigned/',                    UnassignedBatchListView.as_view(), name='unassigned_batches'),
    path('batches/<uuid:batch_id>/assign/',        ShelfAssignView.as_view(),         name='shelf_assign'),
    path('sync/',                                  StockSyncView.as_view(),           name='stock_sync'),
    path('adjustments/',                           StockAdjustmentListView.as_view(), name='adjustment_list'),
]