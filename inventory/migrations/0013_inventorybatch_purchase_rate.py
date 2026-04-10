from django.db import migrations, models
from decimal import Decimal


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0012_gst_foundation_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='inventorybatch',
            name='purchase_rate',
            field=models.DecimalField(
                decimal_places=4,
                default=Decimal('0.0000'),
                help_text='Latest purchase rate per tablet pre-GST — for COGS and ITC calculation',
                max_digits=10,
            ),
        ),
    ]
