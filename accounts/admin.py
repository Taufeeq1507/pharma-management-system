from django.contrib import admin
from .models import CustomUser, Pharmacy, Organization


class PharmacyAdmin(admin.ModelAdmin):
    list_display = ('name', 'gstin', 'subscription_plan', 'organization', 'created_at')
    search_fields = ('name', 'gstin')


class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'subscription_plan', 'created_at')
    search_fields = ('name',)


admin.site.register(CustomUser)
admin.site.register(Pharmacy, PharmacyAdmin)
admin.site.register(Organization, OrganizationAdmin)