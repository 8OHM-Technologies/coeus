from datetime import datetime
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

def calculate_uptime_and_rate(timestamps):
    """
    Calculates the active uptime duration, number of active intervals, and average scrape rate.
    An interval is active if the difference between consecutive scrapes is <= 2 minutes (120s).
    """
    if not timestamps:
        return {
            "uptime_seconds": 0.0,
            "total_scraped": 0,
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
        "total_scraped": len(timestamps),
        "active_scraped_count": active_scraped_count,
        "average_scrape_rate": rate
    }

def update_pipeline_analytics():
    """
    Aggregates records from the extracted_records table, calculates metrics
    per pipeline and per worker, differentiating between indexing and detailing stages.
    Saves the results to the db.
    """
    # 1. Fetch all configurations
    pipelines = PipelineConfiguration.objects.all()
    pipeline_configs = {p.name: p for p in pipelines}

    # 2. Query only the light fields from ExtractedRecord (avoid loading massive HTML content)
    records = ExtractedRecord.objects.values(
        'id',
        'record_type',
        'status',
        'extracted_at',
        'target__entity__name',
        'data__worker_id',
        'data__scraped_at',
        'data__details_scraped_at',
        'data__index_scraped_at'
    )

    # 3. Group timestamps by pipeline, stage, and worker in memory
    data_by_pipeline = {}

    for r in records:
        pipeline_name = r['target__entity__name'] or r['record_type'] or 'Unknown'
        
        # Resolve timestamp
        ts = None
        for key in ('data__details_scraped_at', 'data__scraped_at', 'data__index_scraped_at'):
            val = r.get(key)
            if val:
                ts = parse_iso_datetime(val)
                if ts:
                    break
        if not ts:
            ts = r.get('extracted_at')

        if not ts:
            continue

        worker_id = r.get('data__worker_id')
        worker_key = str(worker_id) if worker_id is not None else 'unknown'
        status = r.get('status') or 'indexed'

        if pipeline_name not in data_by_pipeline:
            data_by_pipeline[pipeline_name] = {
                'indexed': {'overall': [], 'workers': {}},
                'detailed': {'overall': [], 'workers': {}},
                'all': {'overall': [], 'workers': {}}
            }

        pipe_data = data_by_pipeline[pipeline_name]

        def append_to_stage(stage):
            stage['overall'].append(ts)
            if worker_key not in stage['workers']:
                stage['workers'][worker_key] = []
            stage['workers'][worker_key].append(ts)

        if status == 'indexed':
            append_to_stage(pipe_data['indexed'])
        elif status == 'detailed':
            append_to_stage(pipe_data['detailed'])

        append_to_stage(pipe_data['all'])

    # 4. Compute metrics and update databases
    results = {}
    for name, pipe_data in data_by_pipeline.items():
        indexed_metrics = calculate_uptime_and_rate(pipe_data['indexed']['overall'])
        indexed_workers = {w: calculate_uptime_and_rate(ts_list) for w, ts_list in pipe_data['indexed']['workers'].items()}

        detailed_metrics = calculate_uptime_and_rate(pipe_data['detailed']['overall'])
        detailed_workers = {w: calculate_uptime_and_rate(ts_list) for w, ts_list in pipe_data['detailed']['workers'].items()}

        overall_metrics = calculate_uptime_and_rate(pipe_data['all']['overall'])
        overall_workers = {w: calculate_uptime_and_rate(ts_list) for w, ts_list in pipe_data['all']['workers'].items()}

        # Build worker breakdown
        all_workers_keys = set(pipe_data['all']['workers'].keys())
        workers_breakdown = {}
        for w in all_workers_keys:
            workers_breakdown[w] = {
                "overall_totals": {
                    "total_indexed": len(pipe_data['indexed']['workers'].get(w, [])),
                    "total_detailed": len(pipe_data['detailed']['workers'].get(w, [])),
                    "total_records": len(pipe_data['all']['workers'].get(w, []))
                },
                "overall": overall_workers.get(w, {}),
                "indexing": indexed_workers.get(w, {}),
                "detailing": detailed_workers.get(w, {})
            }

        config = pipeline_configs.get(name)
        configured_concurrency = None
        if config:
            configured_concurrency = (config.extraction_params or {}).get('concurrency')

        metrics_payload = {
            "pipeline_name": name,
            "configured_concurrency": configured_concurrency,
            "overall_totals": {
                "total_indexed": len(pipe_data['indexed']['overall']),
                "total_detailed": len(pipe_data['detailed']['overall']),
                "total_records": len(pipe_data['all']['overall'])
            },
            "overall": overall_metrics,
            "indexing": indexed_metrics,
            "detailing": detailed_metrics,
            "workers": workers_breakdown
        }
        results[name] = metrics_payload

        # Save to database
        ScrapingPipelineMetrics.objects.update_or_create(
            pipeline_name=name,
            defaults={"metrics": metrics_payload}
        )

    return results
