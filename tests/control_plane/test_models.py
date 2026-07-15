import pytest
from django.core.exceptions import ValidationError
from pipelines.models import PipelineConfiguration, ScraperType, DocumentType, LLMEngine

@pytest.mark.django_db
def test_pipeline_configuration_creation_and_defaults():
    config = PipelineConfiguration.objects.create(
        name="Test Pipeline",
        start_url="https://example.com",
    )
    
    assert config.name == "Test Pipeline"
    assert config.scraper_type == ScraperType.SAFLII
    assert config.document_type == DocumentType.JSON
    assert config.llm_engine == LLMEngine.LOCAL
    assert config.is_active is True
    assert config.schedule_cron == "0 0 * * *"
    assert config.extraction_params == {}
    assert str(config) == "🟢 Active | Test Pipeline (0 0 * * *)"


@pytest.mark.django_db
def test_pipeline_configuration_clean_validation():
    # Valid cron expressions
    config_valid = PipelineConfiguration(
        name="Valid Pipeline",
        start_url="https://example.com",
        schedule_cron="*/5 * * * *",
    )
    config_valid.clean()  # Should not raise ValidationError

    # Invalid cron expression (no wildcards, not digit-based)
    config_invalid = PipelineConfiguration(
        name="Invalid Pipeline",
        start_url="https://example.com",
        schedule_cron="invalid_cron_expression",
    )
    
    with pytest.raises(ValidationError) as excinfo:
        config_invalid.clean()
    
    assert "schedule_cron" in excinfo.value.message_dict
    assert "Please enter a valid cron expression." in excinfo.value.message_dict["schedule_cron"]


@pytest.mark.django_db
def test_to_blueprint_serialization():
    config = PipelineConfiguration.objects.create(
        name="Test-Pipeline Ingestion",
        scraper_type=ScraperType.SAFLII,
        industry="Legal",
        document_type=DocumentType.PDF,
        is_active=False,
        schedule_cron="0 12 * * *",
        start_url="https://saflii.org/content/recent",
        allow_insecure_https=False,
        allow_insecure_requests=False,
        use_proxy=True,
        extraction_params={"cooldown_seconds": 2.5},
        requires_extraction=True,
        llm_engine=LLMEngine.LOCAL,
        pydantic_schema_name="GenericDocumentExtraction",
        extraction_instructions="Extract court details.",
        target_table="saflii_records",
    )

    blueprint = config.to_blueprint()

    assert blueprint["pipeline_id"] == "test_pipeline_ingestion"
    assert blueprint["name"] == "Test-Pipeline Ingestion"
    assert blueprint["scraper_type"] == ScraperType.SAFLII
    assert blueprint["is_active"] is False
    assert blueprint["schedule"] == "0 12 * * *"
    
    assert blueprint["metadata"]["industry"] == "Legal"
    assert blueprint["metadata"]["document_type"] == DocumentType.PDF

    phase1 = blueprint["phase_1_ingestion"]
    assert phase1["start_url"] == "https://saflii.org/content/recent"
    assert phase1["allow_insecure_https"] is False
    assert phase1["allow_insecure_requests"] is False
    assert phase1["use_proxy"] is True

    phase2 = blueprint["phase_2_extraction"]
    assert phase2["requires_extraction"] is True
    assert phase2["engine"] == LLMEngine.LOCAL
    assert phase2["expected_schema"] == "GenericDocumentExtraction"
    assert phase2["extraction_instructions"] == "Extract court details."
    assert phase2["extraction_params"] == {"cooldown_seconds": 2.5}

    phase3 = blueprint["phase_3_loading"]
    assert phase3["table_name"] == "saflii_records"
