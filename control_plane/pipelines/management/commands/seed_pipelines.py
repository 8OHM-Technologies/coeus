from django.core.management.base import BaseCommand
from pipelines.models import PipelineConfiguration, ScraperType, DocumentType, PaginationStrategy, LLMEngine


class Command(BaseCommand):
    help = "Seed initial pipeline configurations for Mantech and Livestainable."

    def handle(self, *args, **options):
        # Seed Mantech
        mantech_config, created = PipelineConfiguration.objects.get_or_create(
            name="Mantech",
            defaults={
                "scraper_type": ScraperType.MANTECH,
                "industry": "Electronics",
                "document_type": DocumentType.HTML,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.mantech.co.za/Categories.aspx",
                "target_css_selector_documents": "a[id*='HyperLink1_']",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "pagination_strategy": PaginationStrategy.CLICK_NEXT,
                "requires_extraction": False,
                "llm_engine": LLMEngine.LOCAL,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "products",
                "extraction_params": {
                    "categories": ["341", "342"]
                }
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS("Successfully created Mantech pipeline configuration."))
        else:
            self.stdout.write(self.style.WARNING("Mantech pipeline configuration already exists."))

        # Seed Livestainable
        livestainable_config, created = PipelineConfiguration.objects.get_or_create(
            name="Livestainable",
            defaults={
                "scraper_type": ScraperType.LIVESTAINABLE,
                "industry": "Electronics",
                "document_type": DocumentType.HTML,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://livestainable.co.za/collections/vendors?sort_by=title-ascending&q=Keyestudio&filter.v.availability=1",
                "target_css_selector_documents": ".product-item__title",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "pagination_strategy": PaginationStrategy.CLICK_NEXT,
                "requires_extraction": False,
                "llm_engine": LLMEngine.LOCAL,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "products",
                "extraction_params": {
                    "search_keyword": "Keyestudio",
                    "max_pages": 5
                }
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS("Successfully created Livestainable pipeline configuration."))
        else:
            self.stdout.write(self.style.WARNING("Livestainable pipeline configuration already exists."))
