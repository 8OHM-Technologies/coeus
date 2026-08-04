"""
Django management command to consolidate old per-pipeline SAFLII records
into the unified ``saflii_courts`` record_type.

Usage (from inside the control-plane container):
    python manage.py migrate_saflii_records
    python manage.py migrate_saflii_records --dry-run
"""

from django.core.management.base import BaseCommand
from django.db import connection


OLD_SAFLII_RECORD_TYPES = [
    # Slugified variants (as actually stored in DB)
    "saflii_labour_court___appeals",
    "saflii_labour_court___db",
    "saflii_labour_court___pe",
    "saflii_labour_court___cct",
    "saflii_labour_court___jhb",
    "saflii_high_court___south_gp",
    "saflii_high_court___north_gp",
    "saflii_high_court___western_cape",
    "saflii_high_court___nw_mafikeng",
    "saflii_high_court___nc_kimberley",
    "saflii_high_court___mp_middelburg",
    "saflii_high_court___mp_mbombela",
    "saflii_high_court___lp_thohoy",
    "saflii_high_court___lp_polokwane",
    "saflii_high_court___kzn_pmb",
    "saflii_high_court___kzn_dbn",
    "saflii_high_court___kzn",
    "saflii_high_court___gauteng",
    "saflii_high_court___ec",
    "saflii_high_court___fs_bloem",
    # Display-name variants (legacy runs)
    "Saflii Labour Court - Appeals",
    "Saflii Labour Court - DB",
    "Saflii Labour Court - PE",
    "Saflii Labour Court - CCT",
    "Saflii Labour Court - JHB",
    "Saflii High Court - South GP",
    "Saflii High Court - North GP",
    "Saflii High Court - Western Cape",
    "Saflii High Court - NW Mafikeng",
    "Saflii High Court - NC Kimberley",
    "Saflii High Court - MP Middelburg",
    "Saflii High Court - MP Mbombela",
    "Saflii High Court - LP Thohoy",
    "Saflii High Court - LP Polokwane",
    "Saflii High Court - KZN PMB",
    "Saflii High Court - KZN DBN",
    "Saflii High Court - KZN",
    "Saflii High Court - Gauteng",
    "Saflii High Court - EC",
    "Saflii High Court - FS Bloem",
    # Test records
    "saflii_test",
]

NEW_RECORD_TYPE = "saflii_courts"


class Command(BaseCommand):
    help = "Consolidate old per-pipeline SAFLII extracted_records into the unified 'saflii_courts' record_type."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be migrated without making changes.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        total_migrated = 0

        with connection.cursor() as cursor:
            for old_type in OLD_SAFLII_RECORD_TYPES:
                # Count first
                cursor.execute(
                    "SELECT COUNT(*) FROM extracted_records WHERE record_type = %s",
                    [old_type],
                )
                count = cursor.fetchone()[0]

                if count > 0:
                    if dry_run:
                        self.stdout.write(
                            self.style.WARNING(
                                f"  [DRY RUN] Would migrate {count:>6} records: "
                                f"'{old_type}' → '{NEW_RECORD_TYPE}'"
                            )
                        )
                    else:
                        cursor.execute(
                            "UPDATE extracted_records SET record_type = %s WHERE record_type = %s",
                            [NEW_RECORD_TYPE, old_type],
                        )
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  Migrated {count:>6} records: '{old_type}' → '{NEW_RECORD_TYPE}'"
                            )
                        )
                    total_migrated += count

            if total_migrated > 0:
                action = "would be migrated" if dry_run else "consolidated"
                self.stdout.write(
                    self.style.SUCCESS(
                        f"\n✅ {total_migrated} records {action} to '{NEW_RECORD_TYPE}'."
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"\nNo records found to migrate. All records may already use '{NEW_RECORD_TYPE}'."
                    )
                )

            # Report final state
            cursor.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(CASE WHEN status = 'indexed' THEN 1 END) AS indexed,
                       COUNT(CASE WHEN status = 'detailed' THEN 1 END) AS detailed
                FROM extracted_records
                WHERE record_type = %s
                """,
                [NEW_RECORD_TYPE],
            )
            row = cursor.fetchone()
            if row:
                self.stdout.write(
                    f"\nCurrent state for '{NEW_RECORD_TYPE}': "
                    f"{row[0]} total, {row[1]} indexed, {row[2]} detailed"
                )
