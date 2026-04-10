from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_gst_foundation_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='pharmacy',
            name='cn_counter',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='pharmacy',
            name='current_fy',
            field=models.CharField(blank=True, default='', max_length=10),
        ),
    ]
