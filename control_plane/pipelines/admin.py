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
        "scraper_type",
        "industry",
        "is_active",
        "llm_engine",
        "updated_at",
    )

    list_filter = ("is_active", "scraper_type", "industry", "llm_engine", "pagination_strategy")
    search_fields = ("name", "start_url")

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (
            "Metadata & Scheduling",
            {
                "fields": (
                    "name",
                    "scraper_type",
                    "industry",
                    "document_type",
                    "is_active",
                    "schedule_cron",
                )
            },
        ),
        (
            "Phase 1: Ingestion Config (Playwright)",
            {
                "fields": (
                    "start_url",
                    "target_css_selector",
                    "target_css_selector_categories",
                    "target_css_selector_documents",
                    "pagination_strategy",
                    "allow_insecure_https",
                    "allow_insecure_requests",
                )
            },
        ),
        (
            "Phase 2: Extraction Config (LLM)",
            {
                "fields": (
                    "requires_extraction",
                    "llm_engine",
                    "pydantic_schema_name",
                    "extraction_instructions",
                    "extraction_params",
                )
            },
        ),
        ("Phase 3: Loading Config (Postgres)", {"fields": ("target_table",)}),
        (
            "System Tracking",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )
