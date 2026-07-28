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

        # Seed choices models
        document_types_data = {
            "pdf": "PDF Document",
            "json": "JSON Document",
            "html": "HTML",
        }
        for name, label in document_types_data.items():
            DocumentType.objects.get_or_create(name=name, defaults={"label": label})

        llm_engines_data = {
            "ollama/phi4-mini": "Local Phi-4 Mini (Ollama)",
        }
        for name, label in llm_engines_data.items():
            LLMEngine.objects.get_or_create(name=name, defaults={"label": label})

        scraper_types_data = {
            "lotto": "National Lottery (Playwright)",
            "sedarplus": "SEDAR+ (Playwright + Misstcha)",
            "mantech": "Mantech (Playwright)",
            "livestainable": "Livestainable (Playwright)",
            "sabinet": "Sabinet (Playwright + Misstcha)",
            "new_saflii": "SAFLII (BeautifulSoup + AI)",
        }
        for name, label in scraper_types_data.items():
            ScraperType.objects.get_or_create(name=name, defaults={"label": label})

        # Fetch model instances
        mantech_scraper = ScraperType.objects.get(name=ScraperType.MANTECH)
        livestainable_scraper = ScraperType.objects.get(name=ScraperType.LIVESTAINABLE)
        saflii_scraper = ScraperType.objects.get(name=ScraperType.SAFLII)
        sabinet_scraper = ScraperType.objects.get(name=ScraperType.SABINET)

        html_doc = DocumentType.objects.get(name=DocumentType.HTML)
        pdf_doc = DocumentType.objects.get(name=DocumentType.PDF)

        local_llm = LLMEngine.objects.get(name=LLMEngine.LOCAL)

        # Seed Mantech
        get_or_create_pipeline(
            name="Mantech",
            defaults={
                "scraper_type": mantech_scraper,
                "industry": "Electronics",
                "document_type": html_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.mantech.co.za/Categories.aspx",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": False,
                "llm_engine": local_llm,
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
                "scraper_type": livestainable_scraper,
                "industry": "Electronics",
                "document_type": html_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://livestainable.co.za/collections/vendors?sort_by=title-ascending&q=Keyestudio&filter.v.availability=1",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": False,
                "llm_engine": local_llm,
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
            "concurrency": 2,
            "cooldown_seconds": 1,
        }

        # Seed SAFLII Labour Court - Appeals
        get_or_create_pipeline(
            name="Saflii Labour Court - Appeals",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": html_doc,
                "is_active": True,
                "schedule_cron": "0 0 28 2 *",
                "start_url": "https://www.saflii.org/za/cases/ZALAC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_extraction_params
            }
        )

        # Seed SAFLII Labour Court - DB
        get_or_create_pipeline(
            name="Saflii Labour Court - DB",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": html_doc,
                "is_active": True,
                "schedule_cron": "0 0 28 2 *",
                "start_url": "https://www.saflii.org/za/cases/ZALCD/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_extraction_params
            }
        )

        # Seed SAFLII Labour Court - PE
        get_or_create_pipeline(
            name="Saflii Labour Court - PE",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": html_doc,
                "is_active": True,
                "schedule_cron": "0 0 28 2 *",
                "start_url": "https://www.saflii.org/za/cases/ZALCPE/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_extraction_params
            }
        )

        # Seed SAFLII Labour Court - CCT
        get_or_create_pipeline(
            name="Saflii Labour Court - CCT",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": html_doc,
                "is_active": True,
                "schedule_cron": "0 0 28 2 *",
                "start_url": "https://www.saflii.org/za/cases/ZALCCT/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": False,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_extraction_params
            }
        )

        # Seed SAFLII Labour Court - JHB
        get_or_create_pipeline(
            name="Saflii Labour Court - JHB",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": html_doc,
                "is_active": True,
                "schedule_cron": "0 0 28 2 *",
                "start_url": "https://www.saflii.org/za/cases/ZALCJHB/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": False,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_extraction_params
            }
        )

        # Seed Sabinet CCMA - Oldest First
        get_or_create_pipeline(
            name="Sabinet CCMA - Oldest First",
            defaults={
                "scraper_type": sabinet_scraper,
                "industry": "Legal",
                "document_type": pdf_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards&resultsortOption=%22Date+Oldest+first%22",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "GenericDocumentExtraction",
                "extraction_instructions": "Extract court details.",
                "extraction_params": {
                    "shared_record_type": "sabinet_ccma",
                    "concurrency": 2,
                    "cooldown_seconds": 1
                }
            }
        )

        # Seed Sabinet CCMA - Newest First
        get_or_create_pipeline(
            name="Sabinet CCMA - Newest First",
            defaults={
                "scraper_type": sabinet_scraper,
                "industry": "Legal",
                "document_type": pdf_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards&resultsortOption=%22Date+Newest+first%22",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "GenericDocumentExtraction",
                "extraction_instructions": "Extract court details.",
                "target_table": "extracted_records",
                "extraction_params": {
                    "shared_record_type": "sabinet_ccma",
                    "reverse_direction": True,
                    "concurrency": 2,
                    "cooldown_seconds": 1
                }
            }
        )


