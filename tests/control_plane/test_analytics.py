import pytest
from datetime import datetime, timezone, timedelta, date
from extracted_data.models import Entity, Target, ExtractedRecord
from pipelines.models import PipelineConfiguration, ScrapingPipelineMetrics
from pipelines.analytics import calculate_uptime_and_rate, update_pipeline_analytics

def test_calculate_uptime_and_rate_mixed_tz():
    # Mix of naive and aware datetimes
    naive_dt1 = datetime(2026, 7, 26, 8, 0, 0)
    aware_dt2 = datetime(2026, 7, 26, 8, 1, 0, tzinfo=timezone.utc)
    naive_dt3 = datetime(2026, 7, 26, 8, 2, 0)
    
    timestamps = [naive_dt1, aware_dt2, naive_dt3]
    
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
    # Record 1: Naive scraped_at (similar to new_saflii_scraper and sabinet_scraper)
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
    
    # Record 2: Aware details_scraped_at (similar to details scraper updates)
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
    
    # Record 3: No scraped_at timestamps, should fall back to extracted_at (timezone-aware)
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

