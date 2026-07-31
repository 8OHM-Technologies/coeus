import logging
from typing import Dict, Tuple
from django.core.management.base import BaseCommand
from django.db import connections, OperationalError
from extracted_data.models import Entity, ExtractedRecord, Target, Statusses

logger = logging.getLogger(__name__)


def chunked_iterable(iterable, chunk_size):
    """Yield successive chunks from an iterable."""
    chunk = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


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

        target_cache: Dict[Tuple[str, str, str], Target] = {}

        def get_cached_dest_target(src_target: Target, dest_db: str) -> Target:
            src_entity = src_target.entity
            key = (dest_db, src_entity.name, src_target.target_name)
            if key not in target_cache:
                dest_entity, _ = Entity.objects.using(dest_db).get_or_create(
                    name=src_entity.name,
                    defaults={"identifier": src_entity.identifier},
                )
                dest_target, _ = Target.objects.using(dest_db).get_or_create(
                    entity=dest_entity,
                    target_name=src_target.target_name,
                    defaults={"location": src_target.location},
                )
                target_cache[key] = dest_target
            return target_cache[key]

        counts = {
            "oc_to_master_inserted": 0,
            "oc_to_master_updated": 0,
            "master_to_oc_updated": 0,
            "master_to_oc_stubs_inserted": 0,
        }

        # 1. Sync OC (oracle) -> Master (default)
        if direction in ("both", "oc_to_master"):
            self.stdout.write("Syncing records from OC ('oracle') to Master ('default')...")
            self.sync_oc_to_master(
                batch_size=batch_size,
                dry_run=dry_run,
                counts=counts,
                get_cached_dest_target=get_cached_dest_target,
            )

        # 2. Sync Master (default) -> OC (oracle)
        if direction in ("both", "master_to_oc"):
            self.stdout.write("Syncing state from Master ('default') back to OC ('oracle')...")
            self.sync_master_to_oc(
                batch_size=batch_size,
                dry_run=dry_run,
                counts=counts,
                get_cached_dest_target=get_cached_dest_target,
            )

        self.stdout.write(self.style.SUCCESS("Synchronization process finished."))
        self.stdout.write(
            f"Summary:\n"
            f"  - OC -> Master Inserted: {counts['oc_to_master_inserted']}\n"
            f"  - OC -> Master Updated: {counts['oc_to_master_updated']}\n"
            f"  - Master -> OC Status Updated: {counts['master_to_oc_updated']}\n"
            f"  - Master -> OC Stubs Created: {counts['master_to_oc_stubs_inserted']}"
        )

    def sync_oc_to_master(self, batch_size: int, dry_run: bool, counts: dict, get_cached_dest_target):
        """
        Move/Sync records from OC ('oracle') database into Master ('default') database.
        Natural key matching is performed on 'source_url'.
        Uses bulk set queries and bulk creation/updates for high performance.
        """
        oc_qs = (
            ExtractedRecord.objects.using("oracle")
            .select_related("target__entity")
            .exclude(source_url__isnull=True)
            .exclude(source_url="")
        )

        total_count = oc_qs.count()
        self.stdout.write(f"Found {total_count} total records in OC ('oracle').")
        if total_count == 0:
            return

        processed_count = 0
        for batch in chunked_iterable(oc_qs.iterator(chunk_size=batch_size), batch_size):
            batch_urls = [rec.source_url for rec in batch if rec.source_url]
            if not batch_urls:
                continue

            # Query existing master records for this batch in 1 query
            master_map = {
                rec.source_url: rec
                for rec in ExtractedRecord.objects.using("default").filter(source_url__in=batch_urls)
            }

            to_create = []
            to_update = []

            for oc_rec in batch:
                master_rec = master_map.get(oc_rec.source_url)
                if not master_rec:
                    dest_target = get_cached_dest_target(oc_rec.target, "default")
                    to_create.append(
                        ExtractedRecord(
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
                    )
                else:
                    should_update = False
                    if master_rec.status != Statusses.DETAILED and oc_rec.status == Statusses.DETAILED:
                        should_update = True
                    elif master_rec.detailed_at is None and oc_rec.detailed_at is not None:
                        should_update = True

                    if should_update:
                        master_rec.data = oc_rec.data
                        master_rec.status = oc_rec.status
                        master_rec.detailed_at = oc_rec.detailed_at or master_rec.detailed_at
                        master_rec.cleaned_at = oc_rec.cleaned_at or master_rec.cleaned_at
                        if oc_rec.requires_human_review is not None:
                            master_rec.requires_human_review = oc_rec.requires_human_review
                        if oc_rec.review_reason:
                            master_rec.review_reason = oc_rec.review_reason
                        to_update.append(master_rec)

            if to_create:
                if not dry_run:
                    ExtractedRecord.objects.using("default").bulk_create(to_create, batch_size=batch_size)
                counts["oc_to_master_inserted"] += len(to_create)

            if to_update:
                if not dry_run:
                    ExtractedRecord.objects.using("default").bulk_update(
                        to_update,
                        fields=[
                            "data",
                            "status",
                            "detailed_at",
                            "cleaned_at",
                            "requires_human_review",
                            "review_reason",
                        ],
                        batch_size=batch_size,
                    )
                counts["oc_to_master_updated"] += len(to_update)

            processed_count += len(batch)
            self.stdout.write(
                f"  Processed {processed_count}/{total_count} OC records... "
                f"(Inserted: {counts['oc_to_master_inserted']}, Updated: {counts['oc_to_master_updated']})"
            )

    def sync_master_to_oc(self, batch_size: int, dry_run: bool, counts: dict, get_cached_dest_target):
        """
        Sync state from Master ('default') database back to OC ('oracle') database.
        Does NOT transfer the heavy 'data' payload to keep transfers efficient.
        Uses bulk set queries and bulk creation/updates for high performance.
        """
        master_qs = (
            ExtractedRecord.objects.using("default")
            .select_related("target__entity")
            .exclude(source_url__isnull=True)
            .exclude(source_url="")
        )

        total_count = master_qs.count()
        self.stdout.write(f"Found {total_count} total records in Master ('default').")
        if total_count == 0:
            return

        processed_count = 0
        for batch in chunked_iterable(master_qs.iterator(chunk_size=batch_size), batch_size):
            batch_urls = [rec.source_url for rec in batch if rec.source_url]
            if not batch_urls:
                continue

            # Query existing OC records for this batch in 1 query
            oc_map = {
                rec.source_url: rec
                for rec in ExtractedRecord.objects.using("oracle").filter(source_url__in=batch_urls)
            }

            stubs_to_create = []
            oc_to_update = []

            for master_rec in batch:
                oc_rec = oc_map.get(master_rec.source_url)
                if oc_rec:
                    needs_update = False
                    if oc_rec.status != master_rec.status:
                        needs_update = True
                    elif master_rec.detailed_at and not oc_rec.detailed_at:
                        needs_update = True
                    elif master_rec.cleaned_at and not oc_rec.cleaned_at:
                        needs_update = True

                    if needs_update:
                        oc_rec.status = master_rec.status
                        oc_rec.detailed_at = master_rec.detailed_at or oc_rec.detailed_at
                        oc_rec.cleaned_at = master_rec.cleaned_at or oc_rec.cleaned_at
                        if master_rec.requires_human_review is not None:
                            oc_rec.requires_human_review = master_rec.requires_human_review
                        if master_rec.review_reason:
                            oc_rec.review_reason = master_rec.review_reason
                        oc_to_update.append(oc_rec)
                else:
                    dest_target = get_cached_dest_target(master_rec.target, "oracle")
                    stubs_to_create.append(
                        ExtractedRecord(
                            target=dest_target,
                            document_date=master_rec.document_date,
                            record_type=master_rec.record_type,
                            data={},  # Lightweight stub payload - efficient transfer
                            requires_human_review=master_rec.requires_human_review,
                            review_reason=master_rec.review_reason,
                            source_url=master_rec.source_url,
                            scraped_at=master_rec.scraped_at,
                            cleaned_at=master_rec.cleaned_at,
                            detailed_at=master_rec.detailed_at,
                            status=master_rec.status,
                        )
                    )

            if stubs_to_create:
                if not dry_run:
                    ExtractedRecord.objects.using("oracle").bulk_create(stubs_to_create, batch_size=batch_size)
                counts["master_to_oc_stubs_inserted"] += len(stubs_to_create)

            if oc_to_update:
                if not dry_run:
                    ExtractedRecord.objects.using("oracle").bulk_update(
                        oc_to_update,
                        fields=[
                            "status",
                            "detailed_at",
                            "cleaned_at",
                            "requires_human_review",
                            "review_reason",
                        ],
                        batch_size=batch_size,
                    )
                counts["master_to_oc_updated"] += len(oc_to_update)

            processed_count += len(batch)
            self.stdout.write(
                f"  Processed {processed_count}/{total_count} Master records... "
                f"(Stubs Created: {counts['master_to_oc_stubs_inserted']}, Updated: {counts['master_to_oc_updated']})"
            )
