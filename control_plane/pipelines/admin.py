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
        "industry",
        "is_active",
        "llm_engine",
        "updated_at",
    )

    list_filter = ("is_active", "industry", "llm_engine", "pagination_strategy")
    search_fields = ("name", "start_url")

    readonly_fields = (
        "created_at",
        "updated_at",
        "discovery_script",
    )

    fieldsets = (
        (
            "Metadata & Scheduling",
            {
                "fields": (
                    "name",
                    "industry",
                    "document_type",
                    "is_active",
                    "schedule_cron",
                )
            },
        ),
        (
            "Phase 1: Ingestion Config (Playwright)",
            {"fields": ("start_url", "target_css_selector", "pagination_strategy")},
        ),
        (
            "Phase 2: Extraction Config (LLM)",
            {
                "fields": (
                    "requires_extraction",
                    "llm_engine",
                    "pydantic_schema_name",
                    "extraction_instructions",
                )
            },
        ),
        ("Phase 3: Loading Config (Postgres)", {"fields": ("target_table",)}),
        (
            "System Tracking",
            {
                "fields": ("created_at", "updated_at", "discovery_script"),
                "classes": ("collapse",),
            },
        ),
    )
