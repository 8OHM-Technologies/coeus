import pytest
from datetime import datetime, timezone, timedelta, date
from extracted_data.models import Entity, Target, ExtractedRecord
from pipelines.models import PipelineConfiguration, ScrapingPipelineMetrics
from pipelines.analytics import calculate_uptime_and_rate, update_pipeline_analytics

def test_calculate_uptime_and_rate_mixed_tz():
    # Timestamps with UTC timezone
    aware_dt1 = datetime(2026, 7, 26, 8, 0, 0, tzinfo=timezone.utc)
    aware_dt2 = datetime(2026, 7, 26, 8, 1, 0, tzinfo=timezone.utc)
    aware_dt3 = datetime(2026, 7, 26, 8, 2, 0, tzinfo=timezone.utc)
    
    timestamps = [aware_dt1, aware_dt2, aware_dt3]
    
    res = calculate_uptime_and_rate(timestamps)
    
    # 2 intervals of 1 minute each (diff <= 120s)
    # Total active uptime: 120 seconds
    assert res["total_scraped"] == 3
    assert res["active_scraped_count"] == 2
    assert res["uptime_seconds"] == 120.0
    assert res["scrape_rate"] == 2 / 120.0
    assert res["scrape_rate_per_hour"] == (2 / 120.0) * 3600

def test_calculate_uptime_and_rate_empty():
    res = calculate_uptime_and_rate([])
    assert res["uptime_seconds"] == 0.0
    assert res["total_scraped"] == 0
    assert res["active_scraped_count"] == 0
    assert res["scrape_rate"] == 0.0
    assert res["scrape_rate_per_hour"] == 0.0

@pytest.mark.django_db
def test_update_pipeline_analytics_e2e():
    # 1. Create pipeline configuration
    pipeline_name = "test_analytics_pipeline"
    config = PipelineConfiguration.objects.create(
        name=pipeline_name,
        start_url="https://test.com",
    )
    
    # 2. Create target & entity
    entity = Entity.objects.create(name=pipeline_name)
    target = Target.objects.create(entity=entity, target_name="subset", location="https://test.com")
    
    # 3. Create records with different scraper timestamp formats:
    ExtractedRecord.objects.create(
        target=target,
        document_date=date(2026, 1, 1),
        record_type=pipeline_name,
        status="detailed",
        data={
            "scraped_at": "2026-07-26T08:00:00",
            "worker_id": 1
        }
    )
    
    ExtractedRecord.objects.create(
        target=target,
        document_date=date(2026, 1, 1),
        record_type=pipeline_name,
        status="detailed",
        data={
            "details_scraped_at": "2026-07-26T08:01:00Z",
            "worker_id": 2
        }
    )
    
    ExtractedRecord.objects.create(
        target=target,
        document_date=date(2026, 1, 1),
        record_type=pipeline_name,
        status="detailed",
        data={
            "worker_id": 3
        }
    )
    
    # 4. Run update_pipeline_analytics
    results = update_pipeline_analytics()
    
    # Verify results
    assert "new_saflii" in results
    metrics = results["new_saflii"]
    assert metrics["overall_totals"]["total_detailed"] == 3
    
    # Confirm metrics were saved in DB
    saved_metrics = ScrapingPipelineMetrics.objects.get(pipeline_name="new_saflii")
    assert saved_metrics.metrics == metrics


@pytest.mark.django_db
def test_update_pipeline_analytics_groups_by_scraper_type_sabinet():
    from pipelines.models import ScraperType
    sabinet_st, _ = ScraperType.objects.get_or_create(
        name="sabinet",
        defaults={"label": "Sabinet (Playwright + Misstcha)"}
    )

    # Config 1: Oldest
    cfg_oldest = PipelineConfiguration.objects.create(
        name="Sabinet SA Judgments - Oldest",
        scraper_type=sabinet_st,
        start_url="https://test.com/oldest",
        extraction_params={"concurrency": 2}
    )
    entity_oldest = Entity.objects.create(name=cfg_oldest.name)
    target_oldest = Target.objects.create(entity=entity_oldest, target_name="oldest", location="https://test.com/oldest")

    # Config 2: Newest
    cfg_newest = PipelineConfiguration.objects.create(
        name="Sabinet SA Judgment - Newest",
        scraper_type=sabinet_st,
        start_url="https://test.com/newest",
        extraction_params={"concurrency": 3}
    )
    entity_newest = Entity.objects.create(name=cfg_newest.name)
    target_newest = Target.objects.create(entity=entity_newest, target_name="newest", location="https://test.com/newest")

    # Create 2 records under oldest
    for i in range(2):
        ExtractedRecord.objects.create(
            target=target_oldest,
            document_date=date(2026, 1, 1),
            record_type="sabinet_ccma",
            status="detailed",
            data={}
        )

    # Create 3 records under newest
    for i in range(3):
        ExtractedRecord.objects.create(
            target=target_newest,
            document_date=date(2026, 1, 1),
            record_type="sabinet_ccma",
            status="indexed",
            data={}
        )

    results = update_pipeline_analytics()

    # Must be grouped under 'sabinet', NOT under individual pipeline config names
    assert "sabinet" in results
    assert "Sabinet SA Judgments - Oldest" not in results
    assert "Sabinet SA Judgment - Newest" not in results

    metrics = results["sabinet"]
    assert metrics["pipeline_name"] == "sabinet"
    assert metrics["configured_concurrency"] == 5  # 2 + 3
    assert metrics["overall_totals"]["total_records"] == 5
    assert metrics["overall_totals"]["total_detailed"] == 2
    assert metrics["overall_totals"]["total_indexed"] == 3

    # Check database persistence
    saved_metrics = ScrapingPipelineMetrics.objects.get(pipeline_name="sabinet")
    assert saved_metrics.metrics["overall_totals"]["total_records"] == 5
    assert not ScrapingPipelineMetrics.objects.filter(pipeline_name="Sabinet SA Judgments - Oldest").exists()


