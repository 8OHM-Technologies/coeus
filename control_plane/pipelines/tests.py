from datetime import datetime, timezone as dt_timezone
from django.test import TestCase
from .analytics import calculate_last_hour_intervals, calculate_uptime_and_rate

class AnalyticsTestCase(TestCase):
    def test_calculate_last_hour_intervals_basic(self):
        # Reference time: 15:07:00 UTC
        now = datetime(2026, 7, 21, 15, 7, 0, tzinfo=dt_timezone.utc)

        # Scrapes at:
        # 1. 14:56:00 (inside [14:55, 15:00)) -> count=1 for interval starting 14:55
        # 2. 14:58:30 (inside [14:55, 15:00)) -> count=2 total for interval starting 14:55
        # 3. 15:02:00 (inside [15:00, 15:05)) -> count=1 for interval starting 15:00
        # 4. 15:06:00 (inside [15:05, 15:10)) -> in-progress interval, MUST BE EXCLUDED!
        timestamps = [
            datetime(2026, 7, 21, 14, 56, 0, tzinfo=dt_timezone.utc),
            datetime(2026, 7, 21, 14, 58, 30, tzinfo=dt_timezone.utc),
            datetime(2026, 7, 21, 15, 2, 0, tzinfo=dt_timezone.utc),
            datetime(2026, 7, 21, 15, 6, 0, tzinfo=dt_timezone.utc),
        ]

        res = calculate_last_hour_intervals(timestamps, now=now, interval_minutes=5)

        self.assertEqual(res["interval_minutes"], 5)
        self.assertEqual(res["total_intervals"], 12)
        intervals = res["intervals"]

        # Newest completed interval should be [15:00, 15:05)
        last_interval = intervals[-1]
        self.assertEqual(last_interval["start_label"], "15:00")
        self.assertEqual(last_interval["end_label"], "15:05")
        self.assertEqual(last_interval["count"], 1)
        self.assertEqual(last_interval["scrape_rate_per_hour"], 12.0)  # 1 * 12

        # Second newest completed interval should be [14:55, 15:00)
        second_last = intervals[-2]
        self.assertEqual(second_last["start_label"], "14:55")
        self.assertEqual(second_last["end_label"], "15:00")
        self.assertEqual(second_last["count"], 2)
        self.assertEqual(second_last["scrape_rate_per_hour"], 24.0)  # 2 * 12

        # Oldest interval should be [14:05, 14:10)
        first_interval = intervals[0]
        self.assertEqual(first_interval["start_label"], "14:05")
        self.assertEqual(first_interval["end_label"], "14:10")
        self.assertEqual(first_interval["count"], 0)
        self.assertEqual(first_interval["scrape_rate_per_hour"], 0.0)

    def test_calculate_last_hour_intervals_empty(self):
        now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=dt_timezone.utc)
        res = calculate_last_hour_intervals([], now=now, interval_minutes=5)
        self.assertEqual(len(res["intervals"]), 12)
        for item in res["intervals"]:
            self.assertEqual(item["count"], 0)
            self.assertEqual(item["scrape_rate_per_hour"], 0.0)
