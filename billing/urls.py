from django.urls import path
from .views import (
    CheckoutView,
    SalesBillListView,
    SalesBillDetailView,
    CustomerHistoryView,
    SalesReturnView,
)

urlpatterns = [
    path('checkout/',                  CheckoutView.as_view(),        name='checkout'),
    path('history/',                   SalesBillListView.as_view(),   name='sales_history'),
    path('history/<uuid:pk>/',         SalesBillDetailView.as_view(), name='sales_detail'),
    path('customer/<str:phone>/',      CustomerHistoryView.as_view(), name='customer_history'),
    path('return/',                    SalesReturnView.as_view(),     name='sales_return'),
]