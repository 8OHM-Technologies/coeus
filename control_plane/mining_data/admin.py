from django.contrib import admin

from .models import Asset, Company, ResourceEstimate


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "ticker", "created_at")
    search_fields = ("name", "ticker")


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("asset_name", "company", "location")
    list_filter = ("company",)
    search_fields = ("asset_name", "company__name")


@admin.register(ResourceEstimate)
class ResourceEstimateAdmin(admin.ModelAdmin):
    list_display = (
        "asset",
        "classification",
        "commodity",
        "tonnage_mt",
        "grade",
        "effective_date",
        "requires_human_review",
    )

    list_filter = ("requires_human_review", "classification", "commodity")

    search_fields = ("asset__asset_name", "asset__company__name")
    readonly_fields = ("extracted_at",)

    list_editable = ("requires_human_review",)

    def get_queryset(self, request):
        """
        Uses a SQL JOIN to fetch the Asset and Company in a single query,
        rather than hitting the database 100 separate times for a list of 100 rows.
        """
        return super().get_queryset(request).select_related("asset", "asset__company")
