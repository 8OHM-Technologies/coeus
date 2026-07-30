from django.contrib import admin

from .models import Entity, ExtractedRecord, Target, ScrubbedRecord


@admin.register(Entity)
class EntityAdmin(admin.ModelAdmin):
    list_display = ("name", "identifier", "created_at")
    search_fields = ("name", "identifier")


@admin.register(Target)
class TargetAdmin(admin.ModelAdmin):
    list_display = ("target_name", "entity", "location")
    list_filter = ("entity",)
    search_fields = ("target_name", "entity__name")


@admin.register(ExtractedRecord)
class ExtractedRecordAdmin(admin.ModelAdmin):
    list_display = (
        "target",
        "record_type",
        "document_date",
        "requires_human_review",
        "scraped_at",
    )

    list_filter = ("requires_human_review", "record_type")
    search_fields = ("target__target_name", "target__entity__name", "record_type")
    readonly_fields = ("scraped_at",)
    list_editable = ("requires_human_review",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("target", "target__entity")


@admin.register(ScrubbedRecord)
class ScrubbedRecordAdmin(admin.ModelAdmin):
    list_display = ("extracted_record", "created_at")
    readonly_fields = ("created_at",)

