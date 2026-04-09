from django.urls import path
from .views import (
    CheckoutView,
    SalesBillListView,
    SalesBillDetailView,
    CustomerHistoryView,
    SalesReturnView,
    CustomerPartyListView,
    PaymentReceiptView,
)

urlpatterns = [
    path('checkout/',                  CheckoutView.as_view(),        name='checkout'),
    path('history/',                   SalesBillListView.as_view(),   name='sales_history'),
    path('history/<uuid:pk>/',         SalesBillDetailView.as_view(), name='sales_detail'),
    path('customer/<str:phone>/',      CustomerHistoryView.as_view(), name='customer_history'),
    path('customers/',                 CustomerPartyListView.as_view(), name='customer_list'),
    path('receipt/',                   PaymentReceiptView.as_view(),  name='payment_receipt'),
    path('return/',                    SalesReturnView.as_view(),     name='sales_return'),
]