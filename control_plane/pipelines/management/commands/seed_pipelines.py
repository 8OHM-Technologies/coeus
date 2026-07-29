from django.core.management.base import BaseCommand
from pipelines.models import PipelineConfiguration, ScraperType, DocumentType, LLMEngine


class Command(BaseCommand):
    help = "Seed initial pipeline configurations including Mantech, Livestainable, Sabinet, and SAFLII pipelines."

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

        pdf_doc = DocumentType.objects.get(name=DocumentType.PDF)
        json_doc = DocumentType.objects.get(name=DocumentType.JSON)
        html_doc = DocumentType.objects.get(name=DocumentType.HTML)

        local_llm = LLMEngine.objects.get(name=LLMEngine.LOCAL)

        # ── Electronics pipelines ────────────────────────────────────────────

        # Seed Mantech (id=1)
        get_or_create_pipeline(
            name="Mantech",
            defaults={
                "scraper_type": mantech_scraper,
                "industry": "Electronics",
                "document_type": html_doc,
                "is_active": False,
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
                },
                "pipeline_state": {},
            }
        )

        # Seed Livestainable (id=2)
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
                    "max_pages": 5,
                    "search_keyword": "Keyestudio"
                },
                "pipeline_state": {},
            }
        )

        # ── SAFLII Labour Court pipelines (document_type: html, scraper_type_id=6) ──

        # Seed SAFLII Labour Court - Appeals (id=3)
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
                "target_table": "",
                "extraction_params": {"use_proxy": "false"},
                "pipeline_state": {},
            }
        )

        # Seed SAFLII Labour Court - DB (id=4)
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
                "target_table": "",
                "extraction_params": {"use_proxy": "false"},
                "pipeline_state": {},
            }
        )

        # Seed SAFLII Labour Court - PE (id=5)
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
                "target_table": "",
                "extraction_params": {"use_proxy": "false"},
                "pipeline_state": {},
            }
        )

        # Seed SAFLII Labour Court - CCT (id=6)
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
                "target_table": "",
                "extraction_params": {"use_proxy": "false"},
                "pipeline_state": {},
            }
        )

        # Seed SAFLII Labour Court - JHB (id=7)
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
                "target_table": "",
                "extraction_params": {
                    "use_proxy": "true",
                    "concurrency": 2,
                    "cooldown_seconds": 1
                },
                "pipeline_state": {},
            }
        )

        # ── Sabinet CCMA pipelines (document_type: pdf, scraper_type_id=5) ──

        # Seed Sabinet CCMA - Oldest First (id=11)
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
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": {
                    "concurrency": 2,
                    "cooldown_seconds": 1,
                    "shared_record_type": "sabinet_ccma"
                },
                "pipeline_state": {},
            }
        )

        # Seed Sabinet CCMA - Newest First (id=12)
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
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": {
                    "concurrency": 2,
                    "cooldown_seconds": 1,
                    "reverse_direction": True,
                    "shared_record_type": "sabinet_ccma"
                },
                "pipeline_state": {},
            }
        )

        # ── SAFLII High Court pipelines (document_type: pdf, scraper_type_id=6) ──
        # Shared extraction params for High Court pipelines
        saflii_hc_extraction_params = {
            "use_proxy": "true",
            "concurrency": 2,
            "cooldown_seconds": 1
        }

        # Seed Saflii High Court - South GP (id=13)
        get_or_create_pipeline(
            name="Saflii High Court - South GP",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": pdf_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZAGPJHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - North GP (id=14)
        get_or_create_pipeline(
            name="Saflii High Court - North GP",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": pdf_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZAGPPHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - Western Cape (id=15)
        get_or_create_pipeline(
            name="Saflii High Court - Western Cape",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": pdf_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZAWCHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - NW Mafikeng (id=16)
        get_or_create_pipeline(
            name="Saflii High Court - NW Mafikeng",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": pdf_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZANWHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - NC Kimberley (id=17)
        get_or_create_pipeline(
            name="Saflii High Court - NC Kimberley",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": pdf_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZANCHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # ── SAFLII High Court pipelines (document_type: json, scraper_type_id=6) ──

        # Seed Saflii High Court - MP Middelburg (id=18)
        get_or_create_pipeline(
            name="Saflii High Court - MP Middelburg",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": json_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZAMPMHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - MP Mbombela (id=19)
        get_or_create_pipeline(
            name="Saflii High Court - MP Mbombela",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": json_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZAMPMBHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - LP Thohoy (id=20)
        get_or_create_pipeline(
            name="Saflii High Court - LP Thohoy",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": json_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZALMPTHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - LP Polokwane (id=21)
        get_or_create_pipeline(
            name="Saflii High Court - LP Polokwane",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": json_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZALMPPHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - KZN PMB (id=22)
        get_or_create_pipeline(
            name="Saflii High Court - KZN PMB",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": json_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZAKZPHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - KZN DBN (id=23)
        get_or_create_pipeline(
            name="Saflii High Court - KZN DBN",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": json_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZAKZDHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - KZN (id=24)
        get_or_create_pipeline(
            name="Saflii High Court - KZN",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": json_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZAKZHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - Gauteng (id=25)
        get_or_create_pipeline(
            name="Saflii High Court - Gauteng",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": json_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZAGPHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - EC (id=26)
        get_or_create_pipeline(
            name="Saflii High Court - EC",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": json_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZAECHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )

        # Seed Saflii High Court - FS Bloem (id=27)
        get_or_create_pipeline(
            name="Saflii High Court - FS Bloem",
            defaults={
                "scraper_type": saflii_scraper,
                "industry": "Legal",
                "document_type": json_doc,
                "is_active": True,
                "schedule_cron": "0 0 * * *",
                "start_url": "https://www.saflii.org/za/cases/ZAFSHC/",
                "allow_insecure_https": True,
                "allow_insecure_requests": True,
                "use_proxy": False,
                "requires_extraction": True,
                "llm_engine": local_llm,
                "pydantic_schema_name": "",
                "extraction_instructions": "",
                "target_table": "extracted_records",
                "extraction_params": saflii_hc_extraction_params,
                "pipeline_state": {},
            }
        )
