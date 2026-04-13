# control_plane/pipelines/admin.py
from django.contrib import admin

from .models import PipelineConfiguration


@admin.register(PipelineConfiguration)
class PipelineConfigurationAdmin(admin.ModelAdmin):
    # ---------------------------------------------------------
    # List View Configuration
    # ---------------------------------------------------------
    list_display = (
        "name",
        "is_active",
        "llm_engine",
        "target_table",
        "updated_at",
    )

    list_filter = ("is_active", "llm_engine", "pagination_strategy")
    search_fields = ("name", "start_url")

    readonly_fields = (
        "created_at",
        "updated_at",
        "discovery_script",
    )

    fieldsets = (
        ("Metadata & Scheduling", {"fields": ("name", "is_active", "schedule_cron")}),
        (
            "Phase 1: Ingestion Config (Playwright)",
            {"fields": ("start_url", "target_css_selector", "pagination_strategy")},
        ),
        (
            "Phase 2: Extraction Config (LLM)",
            {"fields": ("llm_engine", "pydantic_schema_name")},
        ),
        ("Phase 3: Loading Config (Postgres)", {"fields": ("target_table",)}),
        (
            "System Tracking",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )
