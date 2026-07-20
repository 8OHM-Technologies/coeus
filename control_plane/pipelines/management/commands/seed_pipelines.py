from django.core.management.base import BaseCommand
from pipelines.models import PipelineConfiguration, ScraperType, DocumentType, LLMEngine


class Command(BaseCommand):
    help = "Seed initial pipeline configurations including Mantech, Livestainable, and the 5 SAFLII pipelines."

    def handle(self, *args, **options):
        # Helper to report creation status
        def get_or_create_pipeline(name, defaults):
            config, created = PipelineConfiguration.objects.get_or_create(
                name=name,
                defaults=defaults
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f"Successfully created {name} pipeline configuration."))
            else:
                self.stdout.write(self.style.WARNING(f"{name} pipeline configuration already exists."))
            return config

        # Seed Mantech
        get_or_create_pipeline(
            name="Mantech",
            defaults={
                "scraper_type": ScraperType.MANTECH,
                "industry": "Electronics",
                "document_type": DocumentType.HTML,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.mantech.co.za/Categories.aspx",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
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

        # Seed Livestainable
        get_or_create_pipeline(
            name="Livestainable",
            defaults={
                "scraper_type": ScraperType.LIVESTAINABLE,
                "industry": "Electronics",
                "document_type": DocumentType.HTML,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://livestainable.co.za/collections/vendors?sort_by=title-ascending&q=Keyestudio&filter.v.availability=1",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
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

        # Shared credentials/params for SAFLII
        saflii_extraction_params = {
            "use_proxy": "false",
            "concurrency": 8,
            "cooldown_seconds": 1.5,
        }

        # Seed SAFLII Labour Court - Appeals
        get_or_create_pipeline(
            name="Saflii Labour Court - Appeals",
            defaults={
                "scraper_type": ScraperType.SAFLII,
                "industry": "Legal",
                "document_type": DocumentType.HTML,
                "is_active": True,
                "schedule_cron": "0 0 28 2 *",
                "start_url": "https://www.saflii.org/za/cases/ZALAC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": LLMEngine.LOCAL,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "",
                "extraction_params": saflii_extraction_params
            }
        )

        # Seed SAFLII Labour Court - DB
        get_or_create_pipeline(
            name="Saflii Labour Court - DB",
            defaults={
                "scraper_type": ScraperType.SAFLII,
                "industry": "Legal",
                "document_type": DocumentType.HTML,
                "is_active": True,
                "schedule_cron": "0 0 28 2 *",
                "start_url": "https://www.saflii.org/za/cases/ZALCD/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": LLMEngine.LOCAL,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "",
                "extraction_params": saflii_extraction_params
            }
        )

        # Seed SAFLII Labour Court - PE
        get_or_create_pipeline(
            name="Saflii Labour Court - PE",
            defaults={
                "scraper_type": ScraperType.SAFLII,
                "industry": "Legal",
                "document_type": DocumentType.HTML,
                "is_active": True,
                "schedule_cron": "0 0 28 2 *",
                "start_url": "https://www.saflii.org/za/cases/ZALCPE/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": LLMEngine.LOCAL,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "",
                "extraction_params": saflii_extraction_params
            }
        )

        # Seed SAFLII Labour Court - CCT
        get_or_create_pipeline(
            name="Saflii Labour Court - CCT",
            defaults={
                "scraper_type": ScraperType.SAFLII,
                "industry": "Legal",
                "document_type": DocumentType.HTML,
                "is_active": True,
                "schedule_cron": "0 0 28 2 *",
                "start_url": "https://www.saflii.org/za/cases/ZALCCT/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": False,
                "llm_engine": LLMEngine.LOCAL,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "",
                "extraction_params": saflii_extraction_params
            }
        )

        # Seed SAFLII Labour Court - JHB
        get_or_create_pipeline(
            name="Saflii Labour Court - JHB",
            defaults={
                "scraper_type": ScraperType.SAFLII,
                "industry": "Legal",
                "document_type": DocumentType.HTML,
                "is_active": True,
                "schedule_cron": "0 0 28 2 *",
                "start_url": "https://www.saflii.org/za/cases/ZALCJHB/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": False,
                "llm_engine": LLMEngine.LOCAL,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "",
                "extraction_params": saflii_extraction_params
            }
        )

        # Seed Sabinet CCMA - Oldest First
        get_or_create_pipeline(
            name="Sabinet CCMA - Oldest First",
            defaults={
                "scraper_type": ScraperType.SABINET,
                "industry": "Legal",
                "document_type": DocumentType.PDF,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards&resultsortOption=%22Date+Oldest+first%22",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": LLMEngine.LOCAL,
                "pydantic_schema_name": "GenericDocumentExtraction",
                "extraction_instructions": "Extract court details.",
                "extraction_params": {
                    "shared_record_type": "sabinet_ccma",
                    "concurrency": 8,
                    "cooldown_seconds": 2.0
                }
            }
        )

        # Seed Sabinet CCMA - Newest First
        get_or_create_pipeline(
            name="Sabinet CCMA - Newest First",
            defaults={
                "scraper_type": ScraperType.SABINET,
                "industry": "Legal",
                "document_type": DocumentType.PDF,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards&resultsortOption=%22Date+Newest+first%22",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": LLMEngine.LOCAL,
                "pydantic_schema_name": "GenericDocumentExtraction",
                "extraction_instructions": "Extract court details.",
                "target_table": "extracted_records",
                "extraction_params": {
                    "shared_record_type": "sabinet_ccma",
                    "reverse_direction": True,
                    "concurrency": 8,
                    "cooldown_seconds": 2.0
                }
            }
        )


