from datetime import datetime
from django.db.models import Q
from extracted_data.models import ExtractedRecord
from .models import PipelineConfiguration, ScrapingPipelineMetrics

def parse_iso_datetime(val):
    """Parses ISO format datetime strings, handling the 'Z' offset suffix."""
    if not isinstance(val, str):
        return None
    if val.endswith('Z'):
        val = val[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None

def calculate_uptime_and_rate(records_list):
    """
    Calculates the active uptime duration, number of active intervals, and average scrape rate.
    An interval is active if the difference between consecutive scrapes is <= 2 minutes (120s).
    """
    if not records_list:
        return {
            "uptime_seconds": 0.0,
            "total_scraped": 0,
            "active_scraped_count": 0,
            "average_scrape_rate": 0.0
        }

    # Extract timestamps
    timestamps = []
    for r in records_list:
        ts = None
        data = r.get('data') or {}
        # Try different possible scraped timestamp keys
        for key in ('details_scraped_at', 'scraped_at', 'index_scraped_at'):
            val = data.get(key)
            if val:
                ts = parse_iso_datetime(val)
                if ts:
                    break
        if not ts:
            ts = r.get('extracted_at')
        if ts:
            timestamps.append(ts)

    if not timestamps:
        return {
            "uptime_seconds": 0.0,
            "total_scraped": len(records_list),
            "active_scraped_count": 0,
            "average_scrape_rate": 0.0
        }

    # Sort timestamps in ascending order
    timestamps.sort()

    uptime_seconds = 0.0
    active_scraped_count = 0

    for i in range(1, len(timestamps)):
        diff = (timestamps[i] - timestamps[i-1]).total_seconds()
        if diff <= 120.0:  # 2 minutes threshold
            uptime_seconds += diff
            active_scraped_count += 1

    rate = 0.0
    if uptime_seconds > 0:
        rate = active_scraped_count / uptime_seconds

    return {
        "uptime_seconds": uptime_seconds,
        "total_scraped": len(records_list),
        "active_scraped_count": active_scraped_count,
        "average_scrape_rate": rate
    }

def update_pipeline_analytics():
    """
    Background worker function that aggregates records from the extracted_records table,
    calculates metrics per pipeline and per worker, and saves the results to the db.
    """
    # 1. Fetch all configurations
    pipelines = PipelineConfiguration.objects.all()
    pipeline_configs = {p.name: p for p in pipelines}

    # 2. Fetch all extracted records to avoid large number of N+1 queries
    records = ExtractedRecord.objects.values(
        'id', 'record_type', 'extracted_at', 'data', 'target__entity__name'
    )

    # 3. Group records by pipeline
    grouped_records = {}
    for r in records:
        pipeline_name = r['target__entity__name'] or r['record_type'] or 'Unknown'
        if pipeline_name not in grouped_records:
            grouped_records[pipeline_name] = []
        grouped_records[pipeline_name].append(r)

    # 4. Calculate metrics for each pipeline
    results = {}
    for name, recs in grouped_records.items():
        # Pipeline level
        pipeline_metrics = calculate_uptime_and_rate(recs)
        
        # Concurrency/Worker level breakdown
        worker_groups = {}
        for r in recs:
            data = r.get('data') or {}
            worker_id = data.get('worker_id')
            worker_key = str(worker_id) if worker_id is not None else 'unknown'
            if worker_key not in worker_groups:
                worker_groups[worker_key] = []
            worker_groups[worker_key].append(r)

        workers_breakdown = {}
        for w_key, w_recs in worker_groups.items():
            workers_breakdown[w_key] = calculate_uptime_and_rate(w_recs)

        # Get configured concurrency
        config = pipeline_configs.get(name)
        configured_concurrency = None
        if config:
            configured_concurrency = (config.extraction_params or {}).get('concurrency')

        metrics_payload = {
            "pipeline_name": name,
            "configured_concurrency": configured_concurrency,
            "overall": pipeline_metrics,
            "workers": workers_breakdown
        }
        results[name] = metrics_payload

        # Save to database
        ScrapingPipelineMetrics.objects.update_or_create(
            pipeline_name=name,
            defaults={"metrics": metrics_payload}
        )

    return results
