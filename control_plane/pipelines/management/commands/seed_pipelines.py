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
            "gdrive_folder_id": "0AFSZrAs-mqfiUk9PVA",
            "gdrive_credentials": {
                "type": "service_account",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "client_id": "101482680066645034490",
                "token_uri": "https://oauth2.googleapis.com/token",
                "project_id": "infinity-ohm-cloud-project",
                "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC8bGmoIWJeyo4O\nBMUrE9lNvwynHkyGBhjNAsBtUCMTcGRLcGktue8sgBggSIveOcyaVQlxYyENwFIm\n0AemvJW3lopCgO2y+u5lrZLJHny+S/fJ6Ydqz8jpBK+uwzPC7fjGFbUAyBVYtETd\n4RXVLpZCDlsFPRHweyLXSOGo9BZlBPf+waVNgiGHYDqtNob63MCATFtvSzDYzrHl\nfVb5CuxCSC4VmL5Qot6DGo+k3tXiRc4n+UvrvHAxxb4lj174mX/TD2xTSYf3eSIS\ny3KrG6IJV1YMLrUdAkhMIf0Ew/WdWRMfs5h06EJx+fpufToJAu7wWTfku5zcdTsM\n1ftUNzUTAgMBAAECggEAGpbROW9t7Rgiqq9upBEQJwH7nEZQyyWSG2AKMoLOqFVs\nZHQ6S2sZFAuxcTYmPVelBpmoaaPw9qVPPSM5voQ55mcryeMIEqvet2IikWyoHXHP\n8jduVhrN7GADwKLS1kJJoFOmMAmMGvE5ZKF442qdu+LrDOwHcOBCKY94f085LSD5\nMnPVqk//8MwoEU/kVx+ep3jaBci4sv8dIXmDvccyuBUFRiIH/3wMjqxWlmmaBZbO\n2YdwXEVK8nNgqobBMUaJuDhBfge3L9/XNgAVHJc39dIDgFVhuNNRTehSFGhip7se\nTHeQ0HiPMpw4kJ6V1TJTU8OavEWagpRvmsJDd+e6dQKBgQDhTMgfEZBYngOU1Tk7\isf1umG23NrwErPE67UcPiMEGe4zDFVVD1ts40MvX+L1XFZAlFhmAgauNrN49tnM\nb3TxHKp1AAhnZ0kgZArRJYLrAW7jlt+MIfBHoecwMMTL0uKWZYCpKyj6z6kObtNv\nTAXP64IY27Y8GBcN6Shn+xQlpwKBgQDWGUHBTTs9LsPeuFsIYAqE8LKZDrmKE6Zg\nD/NjOWH+pTp5ZX0O47AReOpOr1t8J73Q+JGwu0st4mheiCY4Lc/It0DRYCqhV72B\ndWqp+56wJXxtIkOs7ABe52NEBYA3N7Z7CoF6EjjmzPc+cAayI+0OY6ehLVHw3r/g\ YiKPIaF6tQKBgAbw2Z1zahIA1DVqmDfIX76nPklm5mvM97LSXCMBmwyOS/NQpvRW\n48cn/TLhblmGvbWBnHOQDmqhjsfkOvN8X4rqCipOlPOyj+Mqkda9pBnfUm46gKqN\nhRx/1WJ7riRlW8usVtlfVgTcDuY97c+Y9Pjh1YE0i5mwWE16aF9DsewzAoGBAJzo\nz886qgLSJk0xsc32jV8XBN218/clJZdbuVXsNUyqjatw3PGvn1d+1cIrNJJOkgf4\nVNZAvf135GP7xn7/3DvPSlro7vVmV4XspurDdW7FWmalaRHvuOnVDWRJ38kYNM4C\nShhMCJXmfAGvmsiuGcuk77LpgxdUOS3a3lcmH7HNAoGAPMAjZ19psz2qQ6AN2kwY\n0MKGopgkWTO7oGg7TtVVCxHVJwJRsJNfKLRpfqNVm6kmB+S5X45AJxO9jIT+aCOS\nMJdNCknh/U5DZ/qz42vLv5eSz8OP5sj49Cm7FWDCANe1yg0zkJr8mvJ5cnQfbzBN\nuHKY1I814JRrdpcYFQc2jVM=\n-----END PRIVATE KEY-----\n",
                "client_email": "coeus-vps-proxy@infinity-ohm-cloud-project.iam.gserviceaccount.com",
                "private_key_id": "6b1a4a78642fa667ef0ed54e2e6723ae732a4f0f",
                "universe_domain": "googleapis.com",
                "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/coeus-vps-proxy%40infinity-ohm-cloud-project.iam.gserviceaccount.com",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs"
            }
        }

        # Seed SAFLII Labour Court - Appeals
        get_or_create_pipeline(
            name="Saflii Labour Court - Appeals",
            defaults={
                "scraper_type": ScraperType.NEW_SAFLII,
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
                "scraper_type": ScraperType.NEW_SAFLII,
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
                "scraper_type": ScraperType.NEW_SAFLII,
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
                "scraper_type": ScraperType.NEW_SAFLII,
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
                "scraper_type": ScraperType.NEW_SAFLII,
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

