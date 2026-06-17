import json
import logging
import os

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django_apscheduler import util
from django_apscheduler.jobstores import DjangoJobStore
from django_apscheduler.models import DjangoJobExecution
from pipelines.models import PipelineConfiguration, ScraperType

logger = logging.getLogger(__name__)


def run_medusa_sync():
    """Trigger the sync_medusa management command."""
    try:
        logger.info("Triggering Medusa synchronization...")
        call_command("sync_medusa")
    except Exception as e:
        logger.error(f"Medusa synchronization failed: {e}")


def get_pipeline_file_path(pipeline):
    """Helper to resolve the JSON data path for a pipeline."""
    pipeline_id = pipeline.name.lower().replace(" ", "_").replace("-", "_")
    doc_type = pipeline.document_type.lower() if pipeline.document_type else ""

    # Primary path (container)
    if doc_type:
        path = os.path.join("/app/data", pipeline_id, doc_type, f"{pipeline_id}.json")
    else:
        path = os.path.join("/app/data", pipeline_id, f"{pipeline_id}.json")

    if not os.path.exists(path):
        # Fallback path (local)
        if doc_type:
            path = os.path.join(
                settings.BASE_DIR.parent,
                "data",
                pipeline_id,
                doc_type,
                f"{pipeline_id}.json",
            )
        else:
            path = os.path.join(
                settings.BASE_DIR.parent,
                "data",
                pipeline_id,
                f"{pipeline_id}.json",
            )
    return path


def combine_scraper_data():
    """
    Task to combine JSON data from Mantech and Livestainable scrapers.
    Uses Mantech as the 'master' record.
    Joins Mantech 'stock_code' with Livestainable 'part_number'.
    Destructively updates original files to leave only unmatched records.
    """
    logger.info("Starting data combination job...")

    mantech_pipelines = PipelineConfiguration.objects.filter(
        scraper_type=ScraperType.MANTECH
    )
    livestainable_pipelines = PipelineConfiguration.objects.filter(
        scraper_type=ScraperType.LIVESTAINABLE
    )

    # 1. Load all Livestainable data into a lookup dictionary and keep track of files
    ls_files_data = {}  # file_path -> original items
    ls_lookup = {}  # part_number -> (item, file_path)
    for p in livestainable_pipelines:
        file_path = get_pipeline_file_path(p)
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    ls_files_data[file_path] = data
                    for item in data:
                        pn = item.get("part_number")
                        if pn:
                            ls_lookup[pn] = (item, file_path)
            except Exception as e:
                logger.error(f"Error loading Livestainable file {file_path}: {e}")

    # 2. Process Mantech data as Master
    final_results = []
    processed_stock_codes = set()
    matched_ls_records = (
        set()
    )  # To track which LS records were matched (part_number, file_path)

    for p in mantech_pipelines:
        file_path = get_pipeline_file_path(p)
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    mantech_data = json.load(f)

                unmatched_mantech_items = []
                for item in mantech_data:
                    stock_code = item.get("stock_code")
                    if not stock_code:
                        unmatched_mantech_items.append(item)
                        continue

                    # Ignore entries where stock_code ends with -MANTECH-EC (removed permanently)
                    if stock_code.endswith("-MANTECH-EC"):
                        continue

                    # Avoid duplicates if multiple pipelines cover the same items
                    if stock_code in processed_stock_codes:
                        continue

                    # Look for match in Livestainable
                    ls_match_info = ls_lookup.get(stock_code)
                    if ls_match_info:
                        ls_match, ls_file_path = ls_match_info
                        # Add Livestainable image as image_url2
                        item["image_url2"] = ls_match.get("image_url")

                        # Update pricing array
                        pricing = item.get("pricing", [])

                        ls_pricing = ls_match.get("pricing", [])
                        if ls_pricing and len(ls_pricing) > 0:
                            ls_price = ls_pricing[0].get("price")
                            if ls_price:
                                pricing.append({"qty": "markup", "price": ls_price})

                        ls_compare = ls_match.get("compare_at_price")
                        if ls_compare:
                            pricing.append({"qty": "no_discount", "price": ls_compare})

                        item["pricing"] = pricing

                        final_results.append(item)
                        processed_stock_codes.add(stock_code)
                        matched_ls_records.add((stock_code, ls_file_path))
                    else:
                        unmatched_mantech_items.append(item)
                        processed_stock_codes.add(stock_code)

                # Write unmatched records back to the Mantech file
                with open(file_path, "w", encoding="utf-8") as fw:
                    json.dump(unmatched_mantech_items, fw, indent=4, ensure_ascii=False)
                    logger.info(
                        f"Updated Mantech file {file_path} with {len(unmatched_mantech_items)} unmatched records."
                    )

            except Exception as e:
                logger.error(f"Error processing Mantech file {file_path}: {e}")

    # 3. Process Livestainable files to remove matched records
    for file_path, data in ls_files_data.items():
        unmatched_ls_items = []
        for item in data:
            pn = item.get("part_number")
            if not pn or (pn, file_path) not in matched_ls_records:
                unmatched_ls_items.append(item)

        try:
            with open(file_path, "w", encoding="utf-8") as fw:
                json.dump(unmatched_ls_items, fw, indent=4, ensure_ascii=False)
                logger.info(
                    f"Updated Livestainable file {file_path} with {len(unmatched_ls_items)} unmatched records."
                )
        except Exception as e:
            logger.error(f"Error updating Livestainable file {file_path}: {e}")

    if not final_results:
        logger.info("No data found to combine.")
        return

    output_dir = "/app/data/combined"
    if not os.path.exists(output_dir):
        output_dir = os.path.join(settings.BASE_DIR.parent, "data", "combined")

    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, "combined_results.json")

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(final_results, f, indent=4, ensure_ascii=False)
        logger.info(
            f"Successfully combined {len(final_results)} records into {output_file}"
        )
        # Trigger Saleor Sync after successful combination
        run_saleor_sync()
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

    def add_arguments(self, parser):
        parser.add_argument(
            "--now",
            action="store_true",
            help="Run the combination task immediately and exit.",
        )
        parser.add_argument(
            "--sync-medusa",
            action="store_true",
            help="Run the Medusa synchronization immediately and exit.",
        )

    def handle(self, *args, **options):
        if options["sync_medusa"]:
            run_medusa_sync()
            return

        if options["now"]:
            self.stdout.write("Triggering combination task immediately...")
            combine_scraper_data()
            self.stdout.write(self.style.SUCCESS("Task complete."))
            return

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
