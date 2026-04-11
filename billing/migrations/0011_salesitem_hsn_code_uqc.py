from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0010_ledgerentry_billpaymentline'),
    ]

    operations = [
        migrations.AddField(
            model_name='salesitem',
            name='hsn_code',
            field=models.CharField(
                blank=True, default='', max_length=20,
                help_text='HSN code frozen at time of sale for GSTR-1 Table 12'
            ),
        ),
        migrations.AddField(
            model_name='salesitem',
            name='uqc',
            field=models.CharField(
                blank=True, default='NOS', max_length=10,
                help_text='Unit Quantity Code frozen at time of sale'
            ),
        ),
    ]
