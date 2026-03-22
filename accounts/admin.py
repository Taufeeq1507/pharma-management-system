from django.contrib import admin
from .models import CustomUser, Pharmacy

# Customizing the Pharmacy view in Admin
class PharmacyAdmin(admin.ModelAdmin):
    list_display = ('name', 'gstin', 'subscription_plan', 'created_at')
    search_fields = ('name', 'gstin',)

admin.site.register(CustomUser)
admin.site.register(Pharmacy, PharmacyAdmin)



