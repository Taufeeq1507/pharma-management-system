import django.db.models.deletion
import django.utils.timezone
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0009_gst_foundation_fields'),
        ('accounts', '0007_pharmacy_cn_counter_current_fy'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── LedgerEntry ───────────────────────────────────────────────────────
        migrations.CreateModel(
            name='LedgerEntry',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('pharmacy', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='ledgerentry_set',
                    to='accounts.pharmacy',
                )),
                ('customer', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='ledger_entries',
                    to='billing.customerparty',
                )),
                ('entry_date', models.DateField(default=django.utils.timezone.now)),
                ('entry_type', models.CharField(
                    choices=[
                        ('SALE', 'Credit Sale'),
                        ('PAYMENT', 'Payment Received'),
                        ('RETURN', 'Sales Return / Credit Note'),
                        ('OPENING', 'Opening Balance'),
                        ('ADJUSTMENT', 'Manual Adjustment'),
                    ],
                    max_length=20,
                )),
                ('debit',         models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=10)),
                ('credit',        models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=10)),
                ('balance_after', models.DecimalField(decimal_places=2, max_digits=10)),
                ('reference_number', models.CharField(blank=True, max_length=50, null=True)),
                ('sales_bill', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='ledger_entries',
                    to='billing.salesbill',
                )),
                ('sales_return', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='ledger_entries',
                    to='billing.salesreturn',
                )),
                ('payment_receipt', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='ledger_entries',
                    to='billing.paymentreceipt',
                )),
                ('narration',   models.CharField(blank=True, max_length=255)),
                ('created_at',  models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['entry_date', 'created_at'],
                'abstract': False,
            },
        ),

        # ── BillPaymentLine ───────────────────────────────────────────────────
        migrations.CreateModel(
            name='BillPaymentLine',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('pharmacy', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='billpaymentline_set',
                    to='accounts.pharmacy',
                )),
                ('bill', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='payment_lines',
                    to='billing.salesbill',
                )),
                ('mode',   models.CharField(max_length=20)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10)),
            ],
            options={'abstract': False},
        ),
        migrations.AddConstraint(
            model_name='billpaymentline',
            constraint=models.UniqueConstraint(
                fields=['pharmacy', 'bill', 'mode'],
                name='unique_payment_mode_per_bill',
            ),
        ),
    ]
