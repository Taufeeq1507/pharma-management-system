from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError as DRFValidationError
from accounts.permissions import IsClerkOrHigher, IsOwnerOrHigher
from .models import SalesBill, SalesReturn, CustomerParty, LedgerEntry, BillPaymentLine
from .serializers import (
    CheckoutSerializer,
    SalesBillReadSerializer,
    SalesReturnSerializer,
    SalesReturnReadSerializer,
    CustomerPartySerializer,
    PaymentReceiptSerializer,
    LedgerEntrySerializer,
)
from django.db.models import Q, Sum, F
from django.db.models.functions import Coalesce
from decimal import Decimal
import datetime


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
        except DRFValidationError:
            # Let DRF's own exception handler return the validation errors to the client.
            # Previously the bare `except Exception` was catching these and returning
            # a generic 500, hiding validation messages from the clerk.
            raise
        except Exception:
            # Only catch unexpected server errors — log internally, never expose traceback.
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

class CustomerPartyListView(generics.ListCreateAPIView):
    """
    GET  /api/billing/customers/               — List all customers for this pharmacy.
    GET  /api/billing/customers/?search=xyz    — Search by name or phone (autocomplete).
    GET  /api/billing/customers/?has_balance=true — B2B outstanding balances for Ledger.
    GET  /api/billing/customers/?customer_type=B2B — Filter to B2B customers only.
    POST /api/billing/customers/               — Register a new CustomerParty (B2B or B2C).
    """
    serializer_class = CustomerPartySerializer
    permission_classes = [IsClerkOrHigher]

    def get_queryset(self):
        qs = CustomerParty.objects.all()
        has_balance = self.request.query_params.get('has_balance')
        customer_type = self.request.query_params.get('customer_type')

        if has_balance == 'true':
            qs = qs.filter(outstanding_balance__gt=0)

        if customer_type:
            qs = qs.filter(customer_type=customer_type)

        q = self.request.query_params.get('search', '').strip()
        if q:
            qs = qs.filter(Q(phone__icontains=q) | Q(name__icontains=q))

        # Limit results: generous for B2B lookup, tight for autocomplete
        if has_balance == 'true':
            return qs[:50]
        return qs[:15]

class PaymentReceiptView(generics.CreateAPIView):
    """
    POST /api/billing/receipt/
    Logs a payment from a B2B customer and triggers FIFO auto-allocation.
    """
    serializer_class = PaymentReceiptSerializer
    permission_classes = [IsClerkOrHigher]


class CustomerLedgerView(generics.ListAPIView):
    """
    GET /api/billing/ledger/<customer_id>/
    Returns the full double-entry ledger for a B2B customer, oldest first.
    Each entry carries debit (what they owe) and credit (what reduced the balance),
    plus a running balance_after so the frontend can display a statement.
    Optional: ?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD to slice the period.
    """
    serializer_class   = LedgerEntrySerializer
    permission_classes = [IsOwnerOrHigher]

    def get_queryset(self):
        customer_id = self.kwargs['customer_id']
        qs = LedgerEntry.objects.filter(
            customer_id=customer_id,
        ).select_related('sales_bill', 'sales_return', 'payment_receipt').order_by('entry_date', 'created_at')

        from_date = self.request.query_params.get('from_date')
        to_date   = self.request.query_params.get('to_date')
        if from_date:
            qs = qs.filter(entry_date__gte=from_date)
        if to_date:
            qs = qs.filter(entry_date__lte=to_date)
        return qs


class CashBookView(APIView):
    """
    GET /api/billing/cash-book/?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD

    Aggregates BillPaymentLine rows by payment mode for the given date range.
    Returns total collections per mode (CASH, UPI, CREDIT, CARD, etc.)
    plus a grand total — matching the pharmacist's physical cash-book tally.
    Defaults to the current calendar month if no dates provided.
    """
    permission_classes = [IsOwnerOrHigher]

    def get(self, request):
        try:
            from_date = datetime.date.fromisoformat(request.query_params.get('from_date', ''))
            to_date   = datetime.date.fromisoformat(request.query_params.get('to_date',   ''))
        except ValueError:
            today     = datetime.date.today()
            from_date = today.replace(day=1)
            to_date   = today

        rows = (
            BillPaymentLine.objects
            .filter(bill__bill_date__date__gte=from_date, bill__bill_date__date__lte=to_date)
            .values('mode')
            .annotate(total=Sum('amount'))
            .order_by('mode')
        )

        mode_totals = [{'mode': r['mode'], 'total': str(r['total'])} for r in rows]
        grand_total = sum(r['total'] for r in rows) if rows else Decimal('0.00')

        return Response({
            'period': {
                'from':  from_date.isoformat(),
                'to':    to_date.isoformat(),
                'label': f"{from_date.strftime('%d %b')} – {to_date.strftime('%d %b %Y')}",
            },
            'by_mode':     mode_totals,
            'grand_total': str(grand_total),
        })


class GSTReportView(APIView):
    """
    GET /api/billing/gst-report/?from=2025-03-01&to=2025-03-31

    Returns a structured GST report covering:
    - GSTR-3B: outward taxable (intra + inter), ITC from purchases, ITC reversals
    - GSTR-1: B2B invoice list, B2C consolidated, HSN summary, credit notes
    - Adjustments: StockAdjustment write-offs, PurchaseReturns, SalesReturns
    """
    permission_classes = [IsOwnerOrHigher]

    def get(self, request):
        from inventory.models import PurchaseBill, PurchaseItem, PurchaseReturn, StockAdjustment

        # ── Parse date range ─────────────────────────────────────────────────
        try:
            date_from = datetime.date.fromisoformat(request.query_params.get('from', ''))
            date_to   = datetime.date.fromisoformat(request.query_params.get('to',   ''))
        except ValueError:
            # Default: current calendar month
            today     = datetime.date.today()
            date_from = today.replace(day=1)
            date_to   = today

        D = lambda x: x or Decimal('0.00')

        # ── Sales bills in period ────────────────────────────────────────────
        bills = SalesBill.objects.filter(
            bill_date__date__gte=date_from,
            bill_date__date__lte=date_to,
        ).select_related('customer')

        # ── GSTR-3B: Outward supplies ────────────────────────────────────────
        # Exclude fully exempt bills (total_tax=0) from taxable supplies.
        # A bill with 0% GST medicine has total_igst=0 AND total_tax=0 — it
        # should not appear in the "intra-state taxable" row of GSTR-3B.
        taxable_bills = bills.exclude(total_tax=0)
        intra = taxable_bills.filter(total_igst=0)
        inter = taxable_bills.filter(total_igst__gt=0)

        intra_agg = intra.aggregate(
            taxable=Coalesce(Sum('subtotal'),   Decimal('0')),
            cgst   =Coalesce(Sum('total_cgst'), Decimal('0')),
            sgst   =Coalesce(Sum('total_sgst'), Decimal('0')),
        )
        inter_agg = inter.aggregate(
            taxable=Coalesce(Sum('subtotal'),   Decimal('0')),
            igst   =Coalesce(Sum('total_igst'), Decimal('0')),
        )

        # ── B2B invoice list (for GSTR-1 Table 4) ───────────────────────────
        b2b_bills = bills.filter(customer__customer_type='B2B', customer__gstin__isnull=False)
        b2b_list = []
        for b in b2b_bills:
            b2b_list.append({
                'invoice_number':  b.invoice_number,
                'invoice_date':    b.bill_date.date().isoformat(),
                'buyer_gstin':     b.customer.gstin if b.customer else None,
                'buyer_name':      b.customer.name  if b.customer else b.customer_name,
                'place_of_supply': b.place_of_supply,
                'taxable_value':   str(b.subtotal),
                'cgst':            str(b.total_cgst),
                'sgst':            str(b.total_sgst),
                'igst':            str(b.total_igst),
                'total':           str(b.grand_total),
            })

        # ── B2C consolidated by GST rate (for GSTR-1 Table 7) ───────────────
        from inventory.models import SalesItem as _SI  # avoid clash
        from .models import SalesItem
        b2c_items = SalesItem.objects.filter(
            sales_bill__bill_date__date__gte=date_from,
            sales_bill__bill_date__date__lte=date_to,
        ).exclude(sales_bill__customer__customer_type='B2B')

        rate_map = {}
        for item in b2c_items.values('gst_percentage').annotate(
            taxable=Sum('taxable_value'),
            cgst=Sum('cgst_amount'), sgst=Sum('sgst_amount'), igst=Sum('igst_amount')
        ):
            rate_map[str(item['gst_percentage'])] = {
                'gst_rate':     str(item['gst_percentage']),
                'taxable_value':str(item['taxable'] or 0),
                'cgst':         str(item['cgst']    or 0),
                'sgst':         str(item['sgst']    or 0),
                'igst':         str(item['igst']    or 0),
            }
        b2c_summary = list(rate_map.values())

        # ── HSN Summary (for GSTR-1 Table 12) ───────────────────────────────
        all_items = SalesItem.objects.filter(
            sales_bill__bill_date__date__gte=date_from,
            sales_bill__bill_date__date__lte=date_to,
        ).select_related('medicine')

        hsn_map = {}
        for item in all_items:
            hsn  = item.medicine.hsn_code or 'UNKNOWN'
            uqc  = item.medicine.uqc
            name = item.medicine.name
            key  = f"{hsn}-{uqc}"
            if key not in hsn_map:
                hsn_map[key] = {
                    'hsn_code': hsn, 'description': name, 'uqc': uqc,
                    'total_qty': 0,
                    'taxable_value': Decimal('0'), 'gst_rate': str(item.gst_percentage),
                    'cgst': Decimal('0'), 'sgst': Decimal('0'), 'igst': Decimal('0'),
                }
            e = hsn_map[key]
            e['total_qty']      += item.quantity
            e['taxable_value']  += item.taxable_value
            e['cgst']           += item.cgst_amount
            e['sgst']           += item.sgst_amount
            e['igst']           += item.igst_amount
        hsn_summary = [
            {**v, 'taxable_value': str(v['taxable_value']), 'cgst': str(v['cgst']),
             'sgst': str(v['sgst']), 'igst': str(v['igst'])}
            for v in hsn_map.values()
        ]

        # ── ITC from Purchases (GSTR-3B Table 4A) ───────────────────────────
        purchase_items_agg = PurchaseItem.objects.filter(
            purchase_bill__bill_date__gte=date_from,
            purchase_bill__bill_date__lte=date_to,
        ).aggregate(
            cgst=Coalesce(Sum('cgst_amount'), Decimal('0')),
            sgst=Coalesce(Sum('sgst_amount'), Decimal('0')),
            igst=Coalesce(Sum('igst_amount'), Decimal('0')),
        )

        # ── ITC Reversals: Purchase Returns with credit note (GSTR-3B 4B1) ──
        pr_reversal = PurchaseReturn.objects.filter(
            return_date__gte=date_from, return_date__lte=date_to, has_credit_note=True,
        ).aggregate(
            cgst=Coalesce(Sum('cgst_amount'), Decimal('0')),
            sgst=Coalesce(Sum('sgst_amount'), Decimal('0')),
            igst=Coalesce(Sum('igst_amount'), Decimal('0')),
        )

        # ── ITC Reversals: Stock write-offs Section 17(5)(h) (GSTR-3B 4B2) ─
        adj_reversal = StockAdjustment.objects.filter(
            adjusted_at__date__gte=date_from,
            adjusted_at__date__lte=date_to,
            delta__lt=0,
        ).aggregate(
            cgst=Coalesce(Sum('cgst_reversal'), Decimal('0')),
            sgst=Coalesce(Sum('sgst_reversal'), Decimal('0')),
            igst=Coalesce(Sum('igst_reversal'), Decimal('0')),
            total=Coalesce(Sum('tax_reversal_amount'), Decimal('0')),
        )

        # ── Sales Returns (Credit Notes) ─────────────────────────────────────
        returns = SalesReturn.objects.filter(
            return_date__gte=date_from, return_date__lte=date_to,
        ).select_related('sales_bill__customer')

        # B2B credit notes → GSTR-1 Table 9B (Registered)
        b2b_credit_notes = []
        b2c_return_cgst = Decimal('0')
        b2c_return_sgst = Decimal('0')
        b2c_return_igst = Decimal('0')
        b2c_return_refund = Decimal('0')

        for ret in returns:
            is_b2b_return = (
                ret.sales_bill and
                ret.sales_bill.customer and
                ret.sales_bill.customer.customer_type == 'B2B'
            )
            if is_b2b_return:
                b2b_credit_notes.append({
                    'credit_note_number':   ret.credit_note_number,
                    'credit_note_date':     ret.return_date.isoformat(),
                    'original_invoice_no':  ret.sales_bill.invoice_number if ret.sales_bill else None,
                    'buyer_gstin':          ret.sales_bill.customer.gstin if ret.sales_bill and ret.sales_bill.customer else None,
                    'refund_amount':        str(ret.refund_amount),
                    'cgst':                 str(ret.cgst_amount),
                    'sgst':                 str(ret.sgst_amount),
                    'igst':                 str(ret.igst_amount),
                    'reason':               ret.reason,
                })
            else:
                b2c_return_cgst   += ret.cgst_amount
                b2c_return_sgst   += ret.sgst_amount
                b2c_return_igst   += ret.igst_amount
                b2c_return_refund += ret.refund_amount

        # ── Purchase Returns without credit note (fresh outward supply) ──────
        pr_fresh_supply = PurchaseReturn.objects.filter(
            return_date__gte=date_from, return_date__lte=date_to, has_credit_note=False,
        ).aggregate(
            cgst=Coalesce(Sum('cgst_amount'), Decimal('0')),
            sgst=Coalesce(Sum('sgst_amount'), Decimal('0')),
            igst=Coalesce(Sum('igst_amount'), Decimal('0')),
            total=Coalesce(Sum('refund_amount'), Decimal('0')),
        )

        # ── Net tax liability (GSTR-3B) ──────────────────────────────────────
        net_cgst = (
            intra_agg['cgst']
            - purchase_items_agg['cgst']
            + pr_reversal['cgst']
            + adj_reversal['cgst']
        )
        net_sgst = (
            intra_agg['sgst']
            - purchase_items_agg['sgst']
            + pr_reversal['sgst']
            + adj_reversal['sgst']
        )
        net_igst = (
            inter_agg['igst']
            - purchase_items_agg['igst']
            + pr_reversal['igst']
            + adj_reversal['igst']
        )

        return Response({
            'period': {
                'from':  date_from.isoformat(),
                'to':    date_to.isoformat(),
                'label': f"{date_from.strftime('%d %b')} – {date_to.strftime('%d %b %Y')}",
            },
            'gstr3b': {
                'outward_intra':     {**intra_agg, **{k: str(v) for k,v in intra_agg.items()}},
                'outward_inter':     {**inter_agg, **{k: str(v) for k,v in inter_agg.items()}},
                'itc_eligible':      {k: str(v) for k, v in purchase_items_agg.items()},
                'itc_reversal_purchase_returns': {k: str(v) for k, v in pr_reversal.items()},
                'itc_reversal_write_offs':       {k: str(v) for k, v in adj_reversal.items()},
                'net_liability':     {
                    'cgst': str(max(net_cgst, Decimal('0'))),
                    'sgst': str(max(net_sgst, Decimal('0'))),
                    'igst': str(max(net_igst, Decimal('0'))),
                },
            },
            'gstr1': {
                'b2b_invoices':    b2b_list,
                'b2c_summary':     b2c_summary,
                'hsn_summary':     hsn_summary,
                'credit_notes_b2b':    b2b_credit_notes,
                'credit_notes_b2c_net': {
                    'refund_amount': str(b2c_return_refund),
                    'cgst': str(b2c_return_cgst),
                    'sgst': str(b2c_return_sgst),
                    'igst': str(b2c_return_igst),
                },
                'purchase_returns_fresh_supply': {k: str(v) for k, v in pr_fresh_supply.items()},
            },
            'summary': {
                'total_bills':         bills.count(),
                'total_sales_value':   str(bills.aggregate(t=Coalesce(Sum('grand_total'), Decimal('0')))['t']),
                'total_returns_value': str(b2c_return_refund + sum(Decimal(c['refund_amount']) for c in b2b_credit_notes)),
                'net_tax_payable':     str(max(net_cgst, Decimal('0')) + max(net_sgst, Decimal('0')) + max(net_igst, Decimal('0'))),
            },
        })