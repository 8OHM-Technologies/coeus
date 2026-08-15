from django.core.exceptions import ValidationError
from django.db import models


class DocumentType(models.Model):
    PDF = "pdf"
    JSON = "json"
    HTML = "html"

    name = models.CharField(
        max_length=50,
        unique=True,
        help_text="The programmatic identifier (e.g., 'pdf', 'json').",
    )
    label = models.CharField(
        max_length=100,
        help_text="User-friendly name (e.g., 'PDF Document').",
    )

    def __str__(self):
        return f"{self.label} ({self.name})"


class LLMEngine(models.Model):
    LOCAL = "ollama/phi4-mini"

    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="The programmatic identifier (e.g., 'ollama/phi4-mini').",
    )
    label = models.CharField(
        max_length=150,
        help_text="User-friendly name (e.g., 'Local Phi-4 Mini (Ollama)').",
    )

    def __str__(self):
        return f"{self.label} ({self.name})"


class ScraperType(models.Model):
    LOTTO = "lotto"
    SEDARPLUS = "sedarplus"
    MANTECH = "mantech"
    LIVESTAINABLE = "livestainable"
    SABINET = "sabinet"
    SAFLII = "new_saflii"

    name = models.CharField(
        max_length=100,
        unique=True,
        help_text="The programmatic identifier (e.g., 'lotto', 'new_saflii').",
    )
    label = models.CharField(
        max_length=150,
        help_text="User-friendly name (e.g., 'National Lottery (Playwright)').",
    )

    def __str__(self):
        return f"{self.label} ({self.name})"


def get_default_scraper_type():
    try:
        obj, _ = ScraperType.objects.get_or_create(
            name="new_saflii",
            defaults={"label": "SAFLII (BeautifulSoup + AI)"}
        )
        return obj.pk
    except Exception:
        return None


def get_default_document_type():
    try:
        obj, _ = DocumentType.objects.get_or_create(
            name="json",
            defaults={"label": "JSON Document"}
        )
        return obj.pk
    except Exception:
        return None


def get_default_llm_engine():
    try:
        obj, _ = LLMEngine.objects.get_or_create(
            name="ollama/phi4-mini",
            defaults={"label": "Local Phi-4 Mini (Ollama)"}
        )
        return obj.pk
    except Exception:
        return None

class PipelineConfiguration(models.Model):
    """
    Stores the dynamic blueprint for a Coeus extraction pipeline.
    Airflow/Dagster will query this table to generate DAGs at runtime.
    """

    # ---------------------------------------------------------
    # Metadata & Scheduling
    # ---------------------------------------------------------
    name = models.CharField(
        max_length=255, unique=True, help_text="e.g., Saflii"
    )
    subset = models.CharField(
        max_length=255,
        blank=True,
        help_text="Subset label used as the Target name for scraped records (e.g., 'CCMA Awards', 'ZACC').",
    )
    scraper_type = models.ForeignKey(
        ScraperType,
        on_delete=models.PROTECT,
        default=get_default_scraper_type,
        help_text="The specific worker script to execute.",
    )
    industry = models.CharField(
        max_length=100, blank=True, help_text="e.g., Finance, Mining, Healthcare"
    )
    document_type = models.ForeignKey(
        DocumentType,
        on_delete=models.PROTECT,
        default=get_default_document_type,
        help_text="The primary document type that will be scraped. e.g. PDF, JSON or sometimes plain HTML",
    )
    is_active = models.BooleanField(
        default=True, help_text="Uncheck to pause this pipeline in Dagster."
    )
    schedule_cron = models.CharField(
        max_length=50, default="0 0 * * *", help_text="Midnight - Standard Cron syntax."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ---------------------------------------------------------
    # Phase 1: Ingestion Config (Playwright)
    # ---------------------------------------------------------
    start_url = models.URLField(help_text="The root URL for the crawler to begin.")
    allow_insecure_https = models.BooleanField(
        default=True, help_text="Ignore SSL errors in Playwright."
    )
    allow_insecure_requests = models.BooleanField(
        default=True, help_text="Ignore SSL errors in requests (urllib3)."
    )
    use_proxy = models.BooleanField(
        default=False,
        help_text="Route all scraper traffic through the rotating Webshare proxy.",
    )
    extraction_params = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary key-value pairs for specific scraper logic (e.g., filter_keyword).",
    )
    pipeline_state = models.JSONField(
        default=dict,
        blank=True,
        help_text="Scraper progress state (e.g., last_year/last_month for rolling-window scrapers). Managed automatically.",
    )

    # -----------------------------------------
    # Phase 2: Extraction Config (LLM)
    # -----------------------------------------
    requires_extraction = models.BooleanField(
        default=True,
        help_text="Uncheck to skip LLM extraction (e.g., for raw document hoarding - NOTE: stores PDF/JSON on local filesystem).",
    )
    llm_engine = models.ForeignKey(
        LLMEngine,
        on_delete=models.PROTECT,
        default=get_default_llm_engine,
        help_text="The AI model used for extraction (Ollama model string).",
    )
    pydantic_schema_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="The exact name of the Python class in extraction_workers/schemas to enforce.",
    )
    extraction_instructions = models.TextField(
        blank=True,
        null=True,
        help_text="Custom instructions for the LLM to guide extraction.",
    )

    # ---------------------------------------------------------
    # Phase 3: Loading Config (Postgres)
    # ---------------------------------------------------------
    target_table = models.CharField(
        max_length=100,
        blank=True,
        help_text="The database table where the structured data will be UPSERTed.",
    )

    def clean(self):
        """Custom validation to ensure data integrity before saving via the Django Admin."""
        if (
            not self.schedule_cron.replace(" ", "")
            .replace("*", "")
            .replace("-", "")
            .replace("/", "")
            .isdigit()
            and "*" not in self.schedule_cron
        ):
            # Basic pseudo-validation just to show you know how to hook into model cleaning
            raise ValidationError(
                {"schedule_cron": "Please enter a valid cron expression."}
            )

    def __str__(self):
        status = "🟢 Active" if self.is_active else "🔴 Paused"
        return f"{status} | {self.name} ({self.schedule_cron})"

    def to_blueprint(self) -> dict:
        """
        Serializes the model into the exact JSON dictionary format
        expected by the Dagster dynamic DAG generator.
        """
        return {
            "pipeline_id": self.name.lower().replace(" ", "_").replace("-", "_"),
            "name": self.name,
            "subset": self.subset,
            "scraper_type": self.scraper_type.name if self.scraper_type else "",
            "is_active": self.is_active,
            "schedule": self.schedule_cron,
            "metadata": {
                "industry": self.industry,
                "document_type": self.document_type.name if self.document_type else "",
            },
            "phase_1_ingestion": {
                "start_url": self.start_url,
                "allow_insecure_https": self.allow_insecure_https,
                "allow_insecure_requests": self.allow_insecure_requests,
                "use_proxy": self.use_proxy,
            },
            "phase_2_extraction": {
                "requires_extraction": self.requires_extraction,
                "engine": self.llm_engine.name if self.llm_engine else "",
                "expected_schema": self.pydantic_schema_name,
                "extraction_instructions": self.extraction_instructions or "",
                "extraction_params": self.extraction_params,
            },
            "phase_3_loading": {"table_name": self.target_table},
        }


class ScrapingPipelineMetrics(models.Model):
    """
    Stores aggregated scraping analytics metrics per pipeline.
    Calculated and updated periodically by a background task.
    """
    pipeline_name = models.CharField(
        max_length=255, unique=True, help_text="The pipeline name or identifier."
    )
    metrics = models.JSONField(
        default=dict, help_text="Calculated metrics including average scrape rate, uptime, and worker breakdown."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Scraping Pipeline Metrics"
        verbose_name_plural = "Scraping Pipeline Metrics"

    def __str__(self):
        return f"{self.pipeline_name} (updated {self.updated_at})"
