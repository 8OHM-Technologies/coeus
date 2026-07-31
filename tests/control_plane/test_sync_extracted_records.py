from datetime import date, datetime, timezone
import pytest
from django.core.management import call_command
from extracted_data.models import Entity, ExtractedRecord, Target, Statusses


@pytest.mark.django_db(databases=["default", "oracle"])
def test_sync_oc_to_master_new_record():
    """Test syncing a new record from OC ('oracle') to Master ('default')."""
    # 1. Create Entity and Target on OC
    oc_entity = Entity.objects.using("oracle").create(name="Entity OC 1", identifier="EOC1")
    oc_target = Target.objects.using("oracle").create(entity=oc_entity, target_name="Target OC 1")

    # 2. Create ExtractedRecord on OC
    oc_rec = ExtractedRecord.objects.using("oracle").create(
        target=oc_target,
        document_date=date(2026, 7, 31),
        record_type="Case Law",
        data={"title": "OC Document 1", "content": "Full detailed content"},
        source_url="https://example.com/doc1",
        status=Statusses.INDEXED,
    )

    # Verify master does not have record yet
    assert ExtractedRecord.objects.using("default").filter(source_url="https://example.com/doc1").count() == 0

    # 3. Run sync command
    call_command("sync_extracted_records", direction="oc_to_master")

    # 4. Verify record now exists in master with full data payload
    master_rec = ExtractedRecord.objects.using("default").get(source_url="https://example.com/doc1")
    assert master_rec.data == {"title": "OC Document 1", "content": "Full detailed content"}
    assert master_rec.status == Statusses.INDEXED
    assert master_rec.target.entity.name == "Entity OC 1"
    assert master_rec.target.target_name == "Target OC 1"


@pytest.mark.django_db(databases=["default", "oracle"])
def test_sync_oc_to_master_upgrade_indexed_to_detailed():
    """Test that an OC record with status 'detailed' updates an existing 'indexed' master record."""
    # Create entity and target on both DBs
    master_entity = Entity.objects.using("default").create(name="Shared Entity", identifier="SE1")
    master_target = Target.objects.using("default").create(entity=master_entity, target_name="Shared Target")

    oc_entity = Entity.objects.using("oracle").create(name="Shared Entity", identifier="SE1")
    oc_target = Target.objects.using("oracle").create(entity=oc_entity, target_name="Shared Target")

    # Master has indexed record with partial data
    master_rec = ExtractedRecord.objects.using("default").create(
        target=master_target,
        document_date=date(2026, 7, 31),
        record_type="Case Law",
        data={"title": "Indexed Only"},
        source_url="https://example.com/doc_upgrade",
        status=Statusses.INDEXED,
        detailed_at=None,
    )

    # OC has detailed record with enriched data
    now_dt = datetime.now(timezone.utc)
    oc_rec = ExtractedRecord.objects.using("oracle").create(
        target=oc_target,
        document_date=date(2026, 7, 31),
        record_type="Case Law",
        data={"title": "Indexed Only", "details": "Enriched analysis content"},
        source_url="https://example.com/doc_upgrade",
        status=Statusses.DETAILED,
        detailed_at=now_dt,
    )

    # Run sync command
    call_command("sync_extracted_records", direction="oc_to_master")

    # Master record should now be updated to DETAILED with new data payload and detailed_at set
    master_rec.refresh_from_db(using="default")
    assert master_rec.status == Statusses.DETAILED
    assert master_rec.data == {"title": "Indexed Only", "details": "Enriched analysis content"}
    assert master_rec.detailed_at is not None


@pytest.mark.django_db(databases=["default", "oracle"])
def test_sync_master_to_oc_status_update_and_stub_creation():
    """Test syncing state from master to OC: updates status without data, and creates stubs with data={}."""
    # Create record in master
    master_entity = Entity.objects.using("default").create(name="Master Entity", identifier="ME1")
    master_target = Target.objects.using("default").create(entity=master_entity, target_name="Master Target")

    # Record 1: Exists on master (detailed) and on OC (indexed)
    master_rec1 = ExtractedRecord.objects.using("default").create(
        target=master_target,
        document_date=date(2026, 7, 31),
        record_type="Case Law",
        data={"large_payload": "Heavy text on master"},
        source_url="https://example.com/doc_shared",
        status=Statusses.DETAILED,
        detailed_at=datetime.now(timezone.utc),
    )

    oc_entity = Entity.objects.using("oracle").create(name="Master Entity", identifier="ME1")
    oc_target = Target.objects.using("oracle").create(entity=oc_entity, target_name="Master Target")

    oc_rec1 = ExtractedRecord.objects.using("oracle").create(
        target=oc_target,
        document_date=date(2026, 7, 31),
        record_type="Case Law",
        data={"oc_local_data": "Keep this untouched"},
        source_url="https://example.com/doc_shared",
        status=Statusses.INDEXED,
        detailed_at=None,
    )

    # Record 2: Exists on master only
    master_rec2 = ExtractedRecord.objects.using("default").create(
        target=master_target,
        document_date=date(2026, 7, 31),
        record_type="Case Law",
        data={"large_payload_2": "Heavy text master 2"},
        source_url="https://example.com/doc_master_only",
        status=Statusses.INDEXED,
    )

    # Run sync master -> OC
    call_command("sync_extracted_records", direction="master_to_oc")

    # Check Record 1: OC status updated to DETAILED, but oc_rec1.data was NOT overwritten by master's data payload!
    oc_rec1.refresh_from_db(using="oracle")
    assert oc_rec1.status == Statusses.DETAILED
    assert oc_rec1.data == {"oc_local_data": "Keep this untouched"}  # data payload NOT overwritten

    # Check Record 2: Stub created on OC with empty data payload data={}
    oc_rec2 = ExtractedRecord.objects.using("oracle").get(source_url="https://example.com/doc_master_only")
    assert oc_rec2.status == Statusses.INDEXED
    assert oc_rec2.data == {}  # Lightweight stub payload
