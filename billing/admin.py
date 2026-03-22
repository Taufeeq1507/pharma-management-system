from django.contrib import admin
from .models import SalesBill, SalesItem, SalesReturn

admin.site.register(SalesBill)
admin.site.register(SalesItem)
admin.site.register(SalesReturn)