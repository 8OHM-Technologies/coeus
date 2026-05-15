import json
import logging
import os

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from django.conf import settings
from django.core.management.base import BaseCommand
from django_apscheduler import util
from django_apscheduler.jobstores import DjangoJobStore
from django_apscheduler.models import DjangoJobExecution
from pipelines.models import PipelineConfiguration, ScraperType

logger = logging.getLogger(__name__)


def combine_scraper_data():
    """
    Task to combine JSON data from Mantech and Livestainable scrapers.
    Joins on 'part_number'.
    """
    logger.info("Starting data combination job...")

    mantech_pipelines = PipelineConfiguration.objects.filter(
        scraper_type=ScraperType.MANTECH
    )
    livestainable_pipelines = PipelineConfiguration.objects.filter(
        scraper_type=ScraperType.LIVESTAINABLE
    )

    combined_data = {}

    def process_pipeline(pipeline, source_name):
        # Mirroring the logic in scrapers and Airflow factory
        pipeline_id = pipeline.name.lower().replace(" ", "_").replace("-", "_")
        doc_type = pipeline.document_type.lower() if pipeline.document_type else ""

        # Scrapers save to /app/data/{pipeline_id}/{doc_type}/{pipeline_id}.json
        # If doc_type is empty, it's /app/data/{pipeline_id}/{pipeline_id}.json
        if doc_type:
            file_path = os.path.join(
                "/app/data", pipeline_id, doc_type, f"{pipeline_id}.json"
            )
        else:
            file_path = os.path.join("/app/data", pipeline_id, f"{pipeline_id}.json")

        if not os.path.exists(file_path):
            # Fallback to local data path if not in container
            if doc_type:
                file_path = os.path.join(
                    settings.BASE_DIR.parent,
                    "data",
                    pipeline_id,
                    doc_type,
                    f"{pipeline_id}.json",
                )
            else:
                file_path = os.path.join(
                    settings.BASE_DIR.parent,
                    "data",
                    pipeline_id,
                    f"{pipeline_id}.json",
                )

        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        part_number = item.get("part_number")
                        if part_number:
                            if part_number not in combined_data:
                                combined_data[part_number] = {
                                    "part_number": part_number,
                                    "sources": {},
                                }
                            combined_data[part_number]["sources"][source_name] = item
                            # Also keep some top-level fields for convenience
                            if (
                                "description" not in combined_data[part_number]
                                or not combined_data[part_number]["description"]
                            ):
                                combined_data[part_number]["description"] = item.get(
                                    "description"
                                )
                            if (
                                "manufacturer" not in combined_data[part_number]
                                or not combined_data[part_number]["manufacturer"]
                            ):
                                combined_data[part_number]["manufacturer"] = item.get(
                                    "manufacturer"
                                )
            except Exception as e:
                logger.error(f"Error processing file {file_path}: {e}")
        else:
            logger.warning(f"File not found for pipeline {pipeline.name}: {file_path}")

    for p in mantech_pipelines:
        process_pipeline(p, "mantech")

    for p in livestainable_pipelines:
        process_pipeline(p, "livestainable")

    if not combined_data:
        logger.info("No data found to combine.")
        return

    output_dir = "/app/data/combined"
    if not os.path.exists(output_dir):
        output_dir = os.path.join(settings.BASE_DIR.parent, "data", "combined")

    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "combined_results.json")

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(list(combined_data.values()), f, indent=4, ensure_ascii=False)
        logger.info(
            f"Successfully combined {len(combined_data)} records into {output_file}"
        )
    except Exception as e:
        logger.error(f"Failed to save combined data: {e}")


@util.close_old_connections
def delete_old_job_executions(max_age=604_800):
    """
    This job deletes APScheduler job execution entries from the database.
    It helps to prevent the database from filling up with old historical records.
    :param max_age: The maximum length of time to retain historical job execution records.
                    Defaults to 7 days.
    """
    DjangoJobExecution.objects.delete_old_job_executions(max_age)


class Command(BaseCommand):
    help = "Runs APScheduler."

    def handle(self, *args, **options):
        scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
        scheduler.add_jobstore(DjangoJobStore(), "default")

        scheduler.add_job(
            combine_scraper_data,
            trigger=CronTrigger(hour="0", minute="0"),  # Run daily at midnight
            id="combine_scraper_data",
            max_instances=1,
            replace_existing=True,
        )
        logger.info("Added job 'combine_scraper_data'.")

        scheduler.add_job(
            delete_old_job_executions,
            trigger=CronTrigger(
                day_of_week="mon", hour="00", minute="00"
            ),  # Midnight on Monday, before start of the next work week.
            id="delete_old_job_executions",
            max_instances=1,
            replace_existing=True,
        )
        logger.info("Added weekly job: 'delete_old_job_executions'.")

        try:
            logger.info("Starting scheduler...")
            scheduler.start()
        except KeyboardInterrupt:
            logger.info("Stopping scheduler...")
            scheduler.shutdown()
            logger.info("Scheduler shut down successfully!")
