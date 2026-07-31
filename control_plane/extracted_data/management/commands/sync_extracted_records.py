import logging
from django.core.management.base import BaseCommand
from django.db import connections, OperationalError
from extracted_data.models import Entity, ExtractedRecord, Target, Statusses

logger = logging.getLogger(__name__)


def resolve_dest_target(src_target: Target, dest_db: str) -> Target:
    """
    Given a Target object from the source database, resolve or create the corresponding
    Entity and Target objects in the destination database by natural key matching.
    """
    src_entity = src_target.entity
    dest_entity, _ = Entity.objects.using(dest_db).get_or_create(
        name=src_entity.name,
        defaults={"identifier": src_entity.identifier},
    )

    dest_target, _ = Target.objects.using(dest_db).get_or_create(
        entity=dest_entity,
        target_name=src_target.target_name,
        defaults={"location": src_target.location},
    )

    return dest_target


class Command(BaseCommand):
    help = (
        "Synchronize extracted_records between the secondary OC database ('oracle') "
        "and the master VPS database ('default')."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Number of records to process per batch (default: 500).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simulate the sync process without making changes to either database.",
        )
        parser.add_argument(
            "--direction",
            type=str,
            choices=["both", "oc_to_master", "master_to_oc"],
            default="both",
            help="Sync direction: 'both' (default), 'oc_to_master', or 'master_to_oc'.",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        dry_run = options["dry_run"]
        direction = options["direction"]

        if dry_run:
            self.stdout.write(self.style.WARNING("--- DRY RUN MODE ACTIVE ---"))

        # Verify that both database aliases are defined and accessible
        if "oracle" not in connections:
            self.stderr.write(
                self.style.ERROR("Database alias 'oracle' is not configured in settings.py.")
            )
            return

        try:
            connections["oracle"].ensure_connection()
        except OperationalError as e:
            self.stderr.write(
                self.style.WARNING(f"Could not connect to 'oracle' database: {e}")
            )

        try:
            connections["default"].ensure_connection()
        except OperationalError as e:
            self.stderr.write(
                self.style.ERROR(f"Could not connect to 'default' database: {e}")
            )
            return

        counts = {
            "oc_to_master_inserted": 0,
            "oc_to_master_updated": 0,
            "master_to_oc_updated": 0,
            "master_to_oc_stubs_inserted": 0,
        }

        # 1. Sync OC (oracle) -> Master (default)
        if direction in ("both", "oc_to_master"):
            self.stdout.write("Syncing records from OC ('oracle') to Master ('default')...")
            self.sync_oc_to_master(batch_size=batch_size, dry_run=dry_run, counts=counts)

        # 2. Sync Master (default) -> OC (oracle)
        if direction in ("both", "master_to_oc"):
            self.stdout.write("Syncing state from Master ('default') back to OC ('oracle')...")
            self.sync_master_to_oc(batch_size=batch_size, dry_run=dry_run, counts=counts)

        self.stdout.write(self.style.SUCCESS("Synchronization process finished."))
        self.stdout.write(
            f"Summary:\n"
            f"  - OC -> Master Inserted: {counts['oc_to_master_inserted']}\n"
            f"  - OC -> Master Updated: {counts['oc_to_master_updated']}\n"
            f"  - Master -> OC Status Updated: {counts['master_to_oc_updated']}\n"
            f"  - Master -> OC Stubs Created: {counts['master_to_oc_stubs_inserted']}"
        )

    def sync_oc_to_master(self, batch_size: int, dry_run: bool, counts: dict):
        """
        Move/Sync records from OC ('oracle') database into Master ('default') database.
        Natural key matching is performed on 'source_url'.
        """
        oc_qs = (
            ExtractedRecord.objects.using("oracle")
            .select_related("target__entity")
            .exclude(source_url__isnull=True)
            .exclude(source_url="")
        )

        for oc_rec in oc_qs.iterator(chunk_size=batch_size):
            source_url = oc_rec.source_url
            master_rec = (
                ExtractedRecord.objects.using("default")
                .filter(source_url=source_url)
                .first()
            )

            if not master_rec:
                # Record does not exist in master DB -> Insert record into master
                if not dry_run:
                    dest_target = resolve_dest_target(oc_rec.target, dest_db="default")
                    ExtractedRecord.objects.using("default").create(
                        target=dest_target,
                        document_date=oc_rec.document_date,
                        record_type=oc_rec.record_type,
                        data=oc_rec.data,
                        requires_human_review=oc_rec.requires_human_review,
                        review_reason=oc_rec.review_reason,
                        source_url=oc_rec.source_url,
                        scraped_at=oc_rec.scraped_at,
                        cleaned_at=oc_rec.cleaned_at,
                        detailed_at=oc_rec.detailed_at,
                        status=oc_rec.status,
                    )
                counts["oc_to_master_inserted"] += 1
                logger.info(f"Inserted record into master: {source_url}")

            else:
                # Record exists in master DB -> Check if master should be updated
                # E.g., master is indexed and OC is detailed, or OC has detailed_at set
                should_update = False

                if master_rec.status != Statusses.DETAILED and oc_rec.status == Statusses.DETAILED:
                    should_update = True
                elif master_rec.detailed_at is None and oc_rec.detailed_at is not None:
                    should_update = True

                if should_update:
                    if not dry_run:
                        master_rec.data = oc_rec.data
                        master_rec.status = oc_rec.status
                        master_rec.detailed_at = oc_rec.detailed_at or master_rec.detailed_at
                        master_rec.cleaned_at = oc_rec.cleaned_at or master_rec.cleaned_at
                        master_rec.requires_human_review = (
                            oc_rec.requires_human_review
                            if oc_rec.requires_human_review is not None
                            else master_rec.requires_human_review
                        )
                        if oc_rec.review_reason:
                            master_rec.review_reason = oc_rec.review_reason

                        master_rec.save(
                            using="default",
                            update_fields=[
                                "data",
                                "status",
                                "detailed_at",
                                "cleaned_at",
                                "requires_human_review",
                                "review_reason",
                            ],
                        )
                    counts["oc_to_master_updated"] += 1
                    logger.info(f"Updated record in master ({oc_rec.status}): {source_url}")

    def sync_master_to_oc(self, batch_size: int, dry_run: bool, counts: dict):
        """
        Sync state from Master ('default') database back to OC ('oracle') database.
        Does NOT transfer the heavy 'data' payload to keep transfers efficient.
        """
        master_qs = (
            ExtractedRecord.objects.using("default")
            .select_related("target__entity")
            .exclude(source_url__isnull=True)
            .exclude(source_url="")
        )

        for master_rec in master_qs.iterator(chunk_size=batch_size):
            source_url = master_rec.source_url
            oc_rec = (
                ExtractedRecord.objects.using("oracle")
                .filter(source_url=source_url)
                .first()
            )

            if oc_rec:
                # Record exists in OC -> Update status/dates on OC without replacing OC data
                needs_update = False
                if oc_rec.status != master_rec.status:
                    needs_update = True
                elif master_rec.detailed_at and not oc_rec.detailed_at:
                    needs_update = True
                elif master_rec.cleaned_at and not oc_rec.cleaned_at:
                    needs_update = True

                if needs_update:
                    if not dry_run:
                        oc_rec.status = master_rec.status
                        oc_rec.detailed_at = master_rec.detailed_at or oc_rec.detailed_at
                        oc_rec.cleaned_at = master_rec.cleaned_at or oc_rec.cleaned_at
                        oc_rec.requires_human_review = (
                            master_rec.requires_human_review
                            if master_rec.requires_human_review is not None
                            else oc_rec.requires_human_review
                        )
                        if master_rec.review_reason:
                            oc_rec.review_reason = master_rec.review_reason

                        oc_rec.save(
                            using="oracle",
                            update_fields=[
                                "status",
                                "detailed_at",
                                "cleaned_at",
                                "requires_human_review",
                                "review_reason",
                            ],
                        )
                    counts["master_to_oc_updated"] += 1
                    logger.info(f"Updated status in OC database: {source_url}")

            else:
                # Record does not exist in OC -> Insert lightweight stub record in OC
                if not dry_run:
                    dest_target = resolve_dest_target(master_rec.target, dest_db="oracle")
                    ExtractedRecord.objects.using("oracle").create(
                        target=dest_target,
                        document_date=master_rec.document_date,
                        record_type=master_rec.record_type,
                        data={},  # Lightweight stub payload
                        requires_human_review=master_rec.requires_human_review,
                        review_reason=master_rec.review_reason,
                        source_url=master_rec.source_url,
                        scraped_at=master_rec.scraped_at,
                        cleaned_at=master_rec.cleaned_at,
                        detailed_at=master_rec.detailed_at,
                        status=master_rec.status,
                    )
                counts["master_to_oc_stubs_inserted"] += 1
                logger.info(f"Created stub record in OC database: {source_url}")
