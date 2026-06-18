from django.contrib import admin
from .models import SiteContactForm

@admin.register(SiteContactForm)
class SiteContactFormAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "division", "created_at")
    list_filter = ("division", "created_at")
    search_fields = ("name", "email", "message")
    readonly_fields = ("created_at",)
