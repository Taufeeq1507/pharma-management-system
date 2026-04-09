from rest_framework import generics, status
from rest_framework.response import Response
from accounts.permissions import IsClerkOrHigher, IsOwnerOrHigher
from .models import SalesBill, SalesReturn, CustomerParty
from .serializers import (
    CheckoutSerializer,
    SalesBillReadSerializer,
    SalesReturnSerializer,
    SalesReturnReadSerializer,
    CustomerPartySerializer,
    PaymentReceiptSerializer,
)
from django.db.models import Q


import traceback
import logging

logger = logging.getLogger(__name__)

class CheckoutView(generics.CreateAPIView):
    permission_classes = [IsClerkOrHigher]

    def get_serializer_class(self):
        return CheckoutSerializer

    def create(self, request, *args, **kwargs):
        try:
            serializer = CheckoutSerializer(
                data=request.data,
                context={'request': request}
            )
            serializer.is_valid(raise_exception=True)
            bill = serializer.save()
            return Response(
                SalesBillReadSerializer(bill).data,
                status=status.HTTP_201_CREATED
            )
        except Exception as e:
            # Bug 8 fix: log internally, never expose traceback to clients
            logger.error(f"Checkout error: {traceback.format_exc()}")
            return Response(
                {"error": "A server error occurred during checkout. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class SalesBillListView(generics.ListAPIView):
    """
    GET /api/billing/history/
    Lists all bills newest first.
    Optional filter: ?customer_phone=9876543210
    """
    serializer_class   = SalesBillReadSerializer
    permission_classes = [IsClerkOrHigher]

    def get_queryset(self):
        qs = (
            SalesBill.objects
            .prefetch_related('items__medicine', 'items__inventory_batch')
            .select_related('billed_by')
            .order_by('-bill_date')
        )
        phone = self.request.query_params.get('customer_phone')
        if phone:
            qs = qs.filter(customer_phone=phone)
        return qs


class SalesBillDetailView(generics.RetrieveAPIView):
    """
    GET /api/billing/history/<uuid>/
    Full bill detail with all line items — for reprint and audit.
    """
    serializer_class   = SalesBillReadSerializer
    permission_classes = [IsClerkOrHigher]

    def get_queryset(self):
        return (
            SalesBill.objects
            .prefetch_related('items__medicine', 'items__inventory_batch')
            .select_related('billed_by')
        )


class CustomerHistoryView(generics.ListAPIView):
    """
    GET /api/billing/customer/<phone>/
    All bills for a specific customer phone number, newest first.
    """
    serializer_class   = SalesBillReadSerializer
    permission_classes = [IsClerkOrHigher]

    def get_queryset(self):
        return (
            SalesBill.objects
            .prefetch_related('items__medicine', 'items__inventory_batch')
            .select_related('billed_by')
            .filter(customer_phone=self.kwargs['phone'])
            .order_by('-bill_date')
        )


class SalesReturnView(generics.CreateAPIView):
    """
    POST /api/billing/return/
    Processes a customer return.
    Restores stock to the exact batch it was sold from.
    Supports partial returns across multiple requests.
    Only owners and above can process returns.
    """
    permission_classes = [IsOwnerOrHigher]

    def get_serializer_class(self):
        return SalesReturnSerializer

    def create(self, request, *args, **kwargs):
        serializer = SalesReturnSerializer(
            data=request.data,
            context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        sales_return = serializer.save()
        return Response(
            SalesReturnReadSerializer(sales_return).data,
            status=status.HTTP_201_CREATED
        )

class CustomerPartyListView(generics.ListAPIView):
    """
    GET /api/billing/customers/?q=987
    Filters customers by phone or name to power the POS Autocomplete.
    """
    serializer_class = CustomerPartySerializer
    permission_classes = [IsClerkOrHigher]

    def get_queryset(self):
        qs = CustomerParty.objects.all()
        has_balance = self.request.query_params.get('has_balance')
        if has_balance == 'true':
            qs = qs.filter(outstanding_balance__gt=0)
            
        q = self.request.query_params.get('search', '').strip()
        if q:
            qs = qs.filter(Q(phone__icontains=q) | Q(name__icontains=q))
        return qs[:50] if has_balance == 'true' else qs[:10]

class PaymentReceiptView(generics.CreateAPIView):
    """
    POST /api/billing/receipt/
    Logs a payment from a B2B customer and triggers FIFO auto-allocation.
    """
    serializer_class = PaymentReceiptSerializer
    permission_classes = [IsClerkOrHigher]