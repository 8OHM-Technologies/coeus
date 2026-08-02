from datetime import datetime, timedelta, timezone as dt_timezone
from django.db.models import Count, Q
from extracted_data.models import ExtractedRecord

from .models import PipelineConfiguration, ScrapingPipelineMetrics, ScraperType


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

def make_aware(ts):
    """Ensure a timestamp is timezone-aware (UTC).

    Naive timestamps from the scrapers (via datetime.now().isoformat()) are in
    local time, NOT UTC. Python's astimezone() on a naive datetime correctly
    interprets it as the system local time and converts to UTC — unlike
    replace(tzinfo=utc) which would incorrectly assume the naive value is UTC.
    """
    if ts.tzinfo is None:
        return ts.astimezone(dt_timezone.utc)
    return ts.astimezone(dt_timezone.utc)

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
            "scrape_rate": 0.0,
            "scrape_rate_per_hour": 0.0
        }

    # Ensure all timestamps are timezone-aware (assumes UTC if naive) and sort
    aware_timestamps = [make_aware(ts) for ts in timestamps]
    aware_timestamps.sort()

    uptime_seconds = 0.0
    active_scraped_count = 0

    for i in range(1, len(aware_timestamps)):
        diff = (aware_timestamps[i] - aware_timestamps[i-1]).total_seconds()
        if diff <= 120.0:  # 2 minutes threshold
            uptime_seconds += diff
            active_scraped_count += 1

    rate = 0.0
    if uptime_seconds > 0:
        rate = active_scraped_count / uptime_seconds

    return {
        "uptime_seconds": uptime_seconds,
        "total_scraped": len(aware_timestamps),
        "active_scraped_count": active_scraped_count,
        "scrape_rate": rate,
        "scrape_rate_per_hour": rate * 3600
    }

def calculate_last_hour_intervals(timestamps, now=None, interval_minutes=5, indexed_timestamps=None, detailed_timestamps=None):
    """
    Calculates scrape rate projections for intervals over the previous hour.
    Default interval length is 5 minutes (producing 12 intervals for 1 hour).

    Only includes fully elapsed intervals. For example, if current time is 15:07,
    the in-progress interval [15:05, 15:10) is excluded. Completed intervals
    going back 1 hour are returned.

    For each 5-minute interval:
    - count: pages scraped in that 5-minute window
    - scrape_rate_per_hour: count * (60 / interval_minutes) projected scrape rate
    """
    if now is None:
        now = datetime.now(dt_timezone.utc)
    else:
        now = make_aware(now)

    aware_all = [make_aware(ts) for ts in timestamps] if timestamps else []
    aware_indexed = [make_aware(ts) for ts in indexed_timestamps] if indexed_timestamps else []
    aware_detailed = [make_aware(ts) for ts in detailed_timestamps] if detailed_timestamps else []

    # Floor 'now' to the start of the current incomplete interval
    minute_offset = now.minute % interval_minutes
    current_interval_start = now.replace(minute=now.minute - minute_offset, second=0, microsecond=0)

    num_intervals = 60 // interval_minutes
    multiplier = 60 // interval_minutes
    intervals = []

    # Build intervals chronologically (oldest to newest)
    for i in range(num_intervals, 0, -1):
        end_ts = current_interval_start - timedelta(minutes=(i - 1) * interval_minutes)
        start_ts = end_ts - timedelta(minutes=interval_minutes)

        count_all = sum(1 for ts in aware_all if start_ts <= ts < end_ts)
        count_indexed = sum(1 for ts in aware_indexed if start_ts <= ts < end_ts)
        count_detailed = sum(1 for ts in aware_detailed if start_ts <= ts < end_ts)

        intervals.append({
            "interval_start": start_ts.isoformat(),
            "interval_end": end_ts.isoformat(),
            "start_label": start_ts.strftime("%H:%M"),
            "end_label": end_ts.strftime("%H:%M"),
            "label": start_ts.strftime("%H:%M"),
            "count": count_all,
            "count_indexed": count_indexed,
            "count_detailed": count_detailed,
            "scrape_rate_per_hour": float(count_all * multiplier),
            "indexing_scrape_rate_per_hour": float(count_indexed * multiplier),
            "detailing_scrape_rate_per_hour": float(count_detailed * multiplier),
        })

    return {
        "interval_minutes": interval_minutes,
        "total_intervals": len(intervals),
        "intervals": intervals
    }

def update_pipeline_analytics():
    """
    Aggregates records from the extracted_records table, calculates metrics
    per scraper type and per worker, differentiating between indexing and detailing stages.
    Saves the results to the db.
    """
    # 1. Fetch all scraper types and pipeline configurations to map any name/record_type -> scraper type name
    known_scraper_types = set(ScraperType.objects.values_list('name', flat=True))
    
    mapping = {}
    for st in ScraperType.objects.all():
        mapping[st.name] = st.name
        mapping[st.label] = st.name
    
    # Common aliases
    mapping["saflii"] = "new_saflii"

    pipelines = PipelineConfiguration.objects.select_related('scraper_type').all()
    
    for p in pipelines:
        if not p.scraper_type:
            continue
        st_name = p.scraper_type.name
        if p.name:
            mapping[p.name] = st_name
        if p.subset:
            mapping[p.subset] = st_name
        shared_rt = (p.extraction_params or {}).get('shared_record_type')
        if shared_rt:
            mapping[shared_rt] = st_name

    def resolve_scraper_type(entity_name, record_type):
        if record_type and record_type in mapping:
            return mapping[record_type]
        if entity_name and entity_name in mapping:
            return mapping[entity_name]
        
        if record_type:
            rt_lower = record_type.lower()
            for key, st_name in mapping.items():
                if key and key.lower() == rt_lower:
                    return st_name
        if entity_name:
            ent_lower = entity_name.lower()
            for key, st_name in mapping.items():
                if key and key.lower() == ent_lower:
                    return st_name
        
        search_target = f"{entity_name or ''} {record_type or ''}".lower()
        for st_name in known_scraper_types:
            if st_name.lower() in search_target:
                return st_name
        if "saflii" in search_target:
            return "new_saflii"

        return entity_name or record_type or 'Unknown'

    # Sum/combine configured concurrency per scraper type
    scraper_type_concurrencies = {}
    for p in pipelines:
        if p.scraper_type:
            st_name = p.scraper_type.name
            concurrency = (p.extraction_params or {}).get('concurrency')
            if concurrency is not None:
                try:
                    c_int = int(concurrency)
                    scraper_type_concurrencies[st_name] = scraper_type_concurrencies.get(st_name, 0) + c_int
                except (ValueError, TypeError):
                    pass

    # 7-day wall-clock window
    now = datetime.now(dt_timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    SEVEN_DAYS_SECONDS = 7 * 24 * 3600  # 604800

    # 2. Query all-time counts grouped by target entity, record type, and status.
    all_time_counts = ExtractedRecord.objects.values(
        'target__entity__name', 'record_type', 'status'
    ).annotate(count=Count('id'))

    overall_totals = {}
    for item in all_time_counts:
        entity_name = item['target__entity__name']
        record_type = item['record_type']
        status = item['status'] or 'indexed'
        count = item['count']

        scraper_type_name = resolve_scraper_type(entity_name, record_type)

        if scraper_type_name not in overall_totals:
            overall_totals[scraper_type_name] = {
                "total_indexed": 0,
                "total_detailed": 0,
                "total_records": 0
            }

        if status == 'indexed':
            overall_totals[scraper_type_name]['total_indexed'] += count
        elif status == 'detailed':
            overall_totals[scraper_type_name]['total_detailed'] += count
        
        overall_totals[scraper_type_name]['total_records'] += count

    # 3. Query only recent records (last 7 days) to calculate detailed uptime and rate.
    recent_records = ExtractedRecord.objects.filter(
        Q(scraped_at__gte=seven_days_ago) | Q(detailed_at__gte=seven_days_ago)
    ).values(
        'id',
        'record_type',
        'status',
        'scraped_at',
        'detailed_at',
        'target__entity__name',
    )

    data_by_scraper = {}
    for r in recent_records:
        entity_name = r['target__entity__name']
        record_type = r['record_type']
        scraper_type_name = resolve_scraper_type(entity_name, record_type)
        
        ts_scraped = r.get('scraped_at')
        ts_detailed = r.get('detailed_at') or ts_scraped
        status = r.get('status') or 'indexed'
        worker_key = 'unknown'

        if scraper_type_name not in data_by_scraper:
            data_by_scraper[scraper_type_name] = {
                'indexed': {'overall': [], 'workers': {}},
                'detailed': {'overall': [], 'workers': {}},
                'all': {'overall': [], 'workers': {}}
            }

        scraper_data = data_by_scraper[scraper_type_name]

        def append_to_stage(stage, ts_val):
            if not ts_val:
                return
            stage['overall'].append(ts_val)
            if worker_key not in stage['workers']:
                stage['workers'][worker_key] = []
            stage['workers'][worker_key].append(ts_val)

        if ts_scraped:
            append_to_stage(scraper_data['indexed'], ts_scraped)

        if status == 'detailed' and ts_detailed:
            append_to_stage(scraper_data['detailed'], ts_detailed)
            append_to_stage(scraper_data['all'], ts_detailed)
        elif ts_scraped:
            append_to_stage(scraper_data['all'], ts_scraped)

    # Determine all unique scraper names to populate (strictly ScraperType names)
    all_scraper_names = set(known_scraper_types) | set(overall_totals.keys()) | set(data_by_scraper.keys())

    # 4. Compute metrics and update databases
    results = {}
    for name in all_scraper_names:
        pipe_data = data_by_scraper.get(name)

        # If no recent records found for this scraper type, perform a fallback query to load its latest 200 records.
        # This allows us to display worker lists and historical uptime/rates for inactive pipelines without loading all history.
        if not pipe_data:
            query_names = [cfg_name for cfg_name, st_name in mapping.items() if st_name == name]
            query_names = list(set(query_names + [name]))
            
            fallback_qs = ExtractedRecord.objects.filter(
                Q(target__entity__name__in=query_names) |
                Q(record_type__in=query_names) |
                Q(target__entity__name__icontains=name) |
                Q(record_type__icontains=name)
            ).order_by('-scraped_at')[:200].values(
                'id',
                'record_type',
                'status',
                'scraped_at',
                'detailed_at',
                'target__entity__name',
            )

            pipe_data = {
                'indexed': {'overall': [], 'workers': {}},
                'detailed': {'overall': [], 'workers': {}},
                'all': {'overall': [], 'workers': {}}
            }

            for r in fallback_qs:
                ts_scraped = r.get('scraped_at')
                ts_detailed = r.get('detailed_at') or ts_scraped
                status = r.get('status') or 'indexed'
                worker_key = 'unknown'

                def append_to_stage(stage, ts_val):
                    if not ts_val:
                        return
                    stage['overall'].append(ts_val)
                    if worker_key not in stage['workers']:
                        stage['workers'][worker_key] = []
                    stage['workers'][worker_key].append(ts_val)

                if ts_scraped:
                    append_to_stage(pipe_data['indexed'], ts_scraped)

                if status == 'detailed' and ts_detailed:
                    append_to_stage(pipe_data['detailed'], ts_detailed)
                    append_to_stage(pipe_data['all'], ts_detailed)
                elif ts_scraped:
                    append_to_stage(pipe_data['all'], ts_scraped)

        indexed_metrics = calculate_uptime_and_rate(pipe_data['indexed']['overall'])
        indexed_workers = {w: calculate_uptime_and_rate(ts_list) for w, ts_list in pipe_data['indexed']['workers'].items()}

        detailed_metrics = calculate_uptime_and_rate(pipe_data['detailed']['overall'])
        detailed_workers = {w: calculate_uptime_and_rate(ts_list) for w, ts_list in pipe_data['detailed']['workers'].items()}

        overall_metrics = calculate_uptime_and_rate(pipe_data['all']['overall'])
        overall_workers = {w: calculate_uptime_and_rate(ts_list) for w, ts_list in pipe_data['all']['workers'].items()}

        # Override total_scraped inside metric calculations with the true all-time counts from database aggregation
        totals = overall_totals.get(name, {"total_indexed": 0, "total_detailed": 0, "total_records": 0})
        overall_metrics["total_scraped"] = totals["total_records"]
        indexed_metrics["total_scraped"] = totals["total_indexed"]
        detailed_metrics["total_scraped"] = totals["total_detailed"]

        # 7-day wall-clock rate (includes downtime)
        recent_all = [ts for ts in pipe_data['all']['overall'] if make_aware(ts) >= seven_days_ago]
        recent_indexed = [ts for ts in pipe_data['indexed']['overall'] if make_aware(ts) >= seven_days_ago]
        recent_detailed = [ts for ts in pipe_data['detailed']['overall'] if make_aware(ts) >= seven_days_ago]

        seven_day_rate = {
            "period_seconds": SEVEN_DAYS_SECONDS,
            "total_records": len(recent_all),
            "total_indexed": len(recent_indexed),
            "total_detailed": len(recent_detailed),
            "scrape_rate": len(recent_all) / SEVEN_DAYS_SECONDS,
            "scrape_rate_per_hour": len(recent_all) / SEVEN_DAYS_SECONDS * 3600,
        }

        # Last hour 5-minute intervals
        last_hour_intervals = calculate_last_hour_intervals(
            timestamps=pipe_data['all']['overall'],
            now=now,
            interval_minutes=5,
            indexed_timestamps=pipe_data['indexed']['overall'],
            detailed_timestamps=pipe_data['detailed']['overall']
        )

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

        configured_concurrency = scraper_type_concurrencies.get(name)

        metrics_payload = {
            "pipeline_name": name,
            "configured_concurrency": configured_concurrency,
            "overall_totals": overall_totals.get(name, {
                "total_indexed": 0,
                "total_detailed": 0,
                "total_records": 0
            }),
            "overall": overall_metrics,
            "indexing": indexed_metrics,
            "detailing": detailed_metrics,
            "last_7_days": seven_day_rate,
            "last_hour_5min_intervals": last_hour_intervals,
            "workers": workers_breakdown
        }
        results[name] = metrics_payload

    # Delete obsolete metrics from database
    active_names = list(results.keys())
    ScrapingPipelineMetrics.objects.exclude(pipeline_name__in=active_names).delete()

    # Save to database
    for name, metrics_payload in results.items():
        ScrapingPipelineMetrics.objects.update_or_create(
            pipeline_name=name,
            defaults={"metrics": metrics_payload}
        )

    return results
