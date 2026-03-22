# inventory/tests.py
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from accounts.models import Pharmacy, CustomUser
from .models import Supplier, MedicineMaster, PurchaseBill, PurchaseItem, InventoryBatch, PurchaseReturn


# ---------------------------------------------------------------------------
# Base test helper — creates a pharmacy + owner + clerk and pre-auths clients
# ---------------------------------------------------------------------------

class BaseInventoryTest(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Create a pharmacy
        self.pharmacy = Pharmacy.objects.create(
            name="Test Pharmacy",
            gstin="22AAAAA0000A1Z5"
        )

        # Owner (level 2)
        self.owner = CustomUser.objects.create_user(
            phone_number="9000000001",
            password="ownerpass",
            pharmacy=self.pharmacy,
            privilege_level=2,
        )

        # Clerk (level 1)
        self.clerk = CustomUser.objects.create_user(
            phone_number="9000000002",
            password="clerkpass",
            pharmacy=self.pharmacy,
            privilege_level=1,
        )

        # A second pharmacy — used to verify tenant isolation
        self.other_pharmacy = Pharmacy.objects.create(name="Other Pharmacy")
        self.other_owner = CustomUser.objects.create_user(
            phone_number="9000000099",
            password="otherpass",
            pharmacy=self.other_pharmacy,
            privilege_level=2,
        )

        # Force-auth the owner by default
        self.client.force_authenticate(user=self.owner)

        # Create common test fixtures
        self.supplier = Supplier.objects.create(
            name="PharmaCo Distributors",
            pharmacy=self.pharmacy,
        )
        self.medicine = MedicineMaster.objects.create(
            name="Dolo 650",
            company="Micro Labs",
            category="Tablet",
            pack_qty=10,
            pharmacy=self.pharmacy,
        )

    # Helper: Build a valid purchase bill payload
    def _purchase_payload(self, invoice_number="INV-001", quantity=20, free_qty=0, mrp="15.00"):
        return {
            "supplier": str(self.supplier.id),
            "invoice_number": invoice_number,
            "bill_date": "2026-03-16",
            "discount": "0.00",
            "payment_status": "PENDING",
            "items": [
                {
                    "medicine": str(self.medicine.id),
                    "batch_number": "BATCH-A1",
                    "expiry_date": "2027-12-31",
                    "quantity": quantity,
                    "free_quantity": free_qty,
                    "purchase_rate_base": "10.00",
                    "gst_percentage": "12.00",
                    "mrp": mrp,
                }
            ]
        }


# ---------------------------------------------------------------------------
# Test 1: Basic purchase bill creation
# ---------------------------------------------------------------------------

class PurchaseBillCreateTest(BaseInventoryTest):
    def test_creates_bill_with_items_and_batch(self):
        url = reverse('purchase-list')
        payload = self._purchase_payload(quantity=20)
        response = self.client.post(url, payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Bill header created
        bill = PurchaseBill.objects.get(invoice_number="INV-001")
        self.assertEqual(bill.pharmacy, self.pharmacy)

        # Line item created
        self.assertEqual(PurchaseItem.objects.filter(purchase_bill=bill).count(), 1)

        # Inventory batch created with correct TABLET quantity (strips * pack_qty)
        # 20 strips × pack_qty=10 = 200 individual tablets on the shelf
        batch = InventoryBatch.objects.get(medicine=self.medicine, batch_number="BATCH-A1")
        self.assertEqual(batch.available_quantity, 200)  # 20 strips * 10 tabs/strip
        self.assertEqual(batch.pharmacy, self.pharmacy)

    def test_financials_auto_calculated(self):
        """subtotal, total_tax, grand_total must be calculated server-side."""
        url = reverse('purchase-list')
        payload = self._purchase_payload(quantity=10)
        # Even if the client sends wrong totals, they should be ignored
        payload['subtotal'] = '9999.00'
        response = self.client.post(url, payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        bill = PurchaseBill.objects.get(invoice_number="INV-001")
        # Expected: 10 * 10.00 = 100.00 subtotal, 100 * 12% = 12.00 tax
        self.assertEqual(bill.subtotal, Decimal('100.00'))
        self.assertEqual(bill.total_tax, Decimal('12.00'))
        self.assertEqual(bill.grand_total, Decimal('112.00'))


# ---------------------------------------------------------------------------
# Test 2: Upsert — same batch on a second invoice increments quantity
# ---------------------------------------------------------------------------

class PurchaseBillUpsertTest(BaseInventoryTest):
    def test_same_batch_mrp_increments_quantity(self):
        url = reverse('purchase-list')
        # First bill: 20 units
        self.client.post(url, self._purchase_payload("INV-001", quantity=20), format='json')
        # Second bill (different invoice number): 15 units of same batch/MRP
        self.client.post(url, self._purchase_payload("INV-002", quantity=15), format='json')

        # Should NOT create a duplicate batch row
        self.assertEqual(
            InventoryBatch.objects.filter(medicine=self.medicine, batch_number="BATCH-A1").count(), 1
        )
        batch = InventoryBatch.objects.get(medicine=self.medicine, batch_number="BATCH-A1")
        # 20 strips + 15 strips = 35 strips total × pack_qty=10 = 350 tablets
        self.assertEqual(batch.available_quantity, 350)

    def test_different_mrp_creates_separate_batch(self):
        url = reverse('purchase-list')
        self.client.post(url, self._purchase_payload("INV-001", mrp="15.00"), format='json')
        self.client.post(url, self._purchase_payload("INV-002", mrp="16.00"), format='json')

        # Different MRP = different batch row (uniqueness is on pharmacy+medicine+batch+mrp)
        self.assertEqual(
            InventoryBatch.objects.filter(medicine=self.medicine, batch_number="BATCH-A1").count(), 2
        )


# ---------------------------------------------------------------------------
# Test 3: Duplicate invoice number rejected
# ---------------------------------------------------------------------------

class DuplicateInvoiceTest(BaseInventoryTest):
    def test_duplicate_invoice_returns_400(self):
        url = reverse('purchase-list')
        self.client.post(url, self._purchase_payload("INV-DUP"), format='json')
        response = self.client.post(url, self._purchase_payload("INV-DUP"), format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Test 4 & 5: Purchase returns
# ---------------------------------------------------------------------------

class PurchaseReturnTest(BaseInventoryTest):
    def setUp(self):
        super().setUp()
        # Pre-stock the shelf
        url = reverse('purchase-list')
        self.client.post(url, self._purchase_payload("INV-001", quantity=50), format='json')
        self.batch = InventoryBatch.objects.get(medicine=self.medicine, batch_number="BATCH-A1")

    def test_return_deducts_from_batch(self):
        url = reverse('purchase_return')
        payload = {
            "supplier": str(self.supplier.id),
            "medicine": str(self.medicine.id),
            "batch_number": "BATCH-A1",
            "return_quantity": 10,
            "refund_amount": "100.00",
            "return_date": "2026-03-16",
            "reason": "Expired",
        }
        response = self.client.post(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.batch.refresh_from_db()
        # Shelf had 500 tablets (50 strips × 10), returned 10 individual tablets → 490 remain
        self.assertEqual(self.batch.available_quantity, 490)

        self.assertEqual(PurchaseReturn.objects.count(), 1)

    def test_return_exceeding_stock_returns_400(self):
        url = reverse('purchase_return')
        payload = {
            "supplier": str(self.supplier.id),
            "medicine": str(self.medicine.id),
            "batch_number": "BATCH-A1",
            "return_quantity": 999,  # Way more than 50 on shelf
            "refund_amount": "999.00",
            "return_date": "2026-03-16",
            "reason": "Damaged",
        }
        response = self.client.post(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Batch should be untouched — still 500 tablets (50 strips × 10)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.available_quantity, 500)


# ---------------------------------------------------------------------------
# Test 6: Stock list only shows batches with stock
# ---------------------------------------------------------------------------

class StockListTest(BaseInventoryTest):
    def setUp(self):
        super().setUp()
        # Create one batch with stock and one empty batch (simulates sold-out)
        InventoryBatch.objects.create(
            medicine=self.medicine, batch_number="FULL-BATCH",
            expiry_date="2027-12-31", available_quantity=25,
            mrp="15.00", pharmacy=self.pharmacy
        )
        InventoryBatch.objects.create(
            medicine=self.medicine, batch_number="EMPTY-BATCH",
            expiry_date="2027-12-31", available_quantity=0,
            mrp="15.00", pharmacy=self.pharmacy
        )

    def test_stock_list_excludes_empty_batches(self):
        url = reverse('stock_list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        batch_numbers = [item['batch_number'] for item in response.data['results']]
        self.assertIn("FULL-BATCH", batch_numbers)
        self.assertNotIn("EMPTY-BATCH", batch_numbers)


# ---------------------------------------------------------------------------
# Test 7: Medicine search
# ---------------------------------------------------------------------------

class MedicineSearchTest(BaseInventoryTest):
    def test_search_returns_matching_results(self):
        url = reverse('medicine_search') + '?q=dolo'
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item['name'] for item in response.data['results']]
        self.assertIn("Dolo 650", names)

    def test_empty_query_returns_empty_list(self):
        url = reverse('medicine_search')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 0)


# ---------------------------------------------------------------------------
# Test 8: Permission — Clerk blocked from posting a purchase bill
# ---------------------------------------------------------------------------

class PermissionTest(BaseInventoryTest):
    def test_clerk_cannot_post_purchase_bill(self):
        self.client.force_authenticate(user=self.clerk)
        url = reverse('purchase-list')
        response = self.client.post(url, self._purchase_payload(), format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_clerk_can_read_stock(self):
        self.client.force_authenticate(user=self.clerk)
        url = reverse('stock_list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_tenant_isolation_other_pharmacy_cannot_see_bills(self):
        """Owner from a different pharmacy should see 0 purchase bills."""
        self.client.force_authenticate(user=self.other_owner)
        url = reverse('purchase-list')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 0)
