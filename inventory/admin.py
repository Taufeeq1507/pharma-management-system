from django.contrib import admin
from .models import Supplier, MedicineMaster, WarehouseBlock, ShelfLocation, StockAdjustment

admin.site.register(Supplier)
admin.site.register(MedicineMaster)
admin.site.register(WarehouseBlock)
admin.site.register(ShelfLocation)
admin.site.register(StockAdjustment)