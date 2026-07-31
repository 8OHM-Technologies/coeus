import uuid

from django.db import models

class Statusses(models.TextChoices):
    INDEXED = "indexed", "Indexed"
    DETAILED = "detailed", "Detailed"

class Entity(models.Model):
    """
    Represents an organization, company, or individual being monitored.
    Maps to the 'entities' table.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    identifier = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="e.g., Stock ticker or registration number.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = "entities"
        verbose_name_plural = "Entities"

    def __str__(self) -> str:
        return str(self.name)


class Target(models.Model):
    """
    Represents a specific project, asset, or location belonging to an Entity.
    Maps to the 'targets' table.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="targets")
    target_name = models.CharField(max_length=255)
    location = models.URLField(blank=False, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = "targets"
        unique_together = (("entity", "target_name"),)

    def __str__(self) -> str:
        return f"{self.entity.name} - {self.target_name}"


class ExtractedRecord(models.Model):
    """
    Stores generic extracted data from documents.
    Maps to the 'extracted_records' table.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.ForeignKey(
        Target, on_delete=models.CASCADE, related_name="records"
    )
    document_date = models.DateField()
    record_type = models.CharField(
        max_length=100, help_text="e.g., 'Case Law' or 'Financial Summary'"
    )

    # The flexible payload
    data = models.JSONField(help_text="Data extracted by the LLM.")

    requires_human_review = models.BooleanField(default=False, null=True)
    review_reason = models.TextField(blank=True, null=True)
    source_url = models.TextField(blank=True, null=True, unique=True)
    scraped_at = models.DateTimeField(auto_now_add=True)
    cleaned_at = models.DateTimeField(auto_now_add=False, null=True)
    detailed_at = models.DateTimeField(auto_now_add=False, null=True)
    status = models.CharField(
        max_length=20,
        choices=Statusses.choices,
        default=Statusses.INDEXED,
        help_text="The status of the scraping workflow.",
    )

    class Meta:
        managed = True
        db_table = "extracted_records"

    def __str__(self) -> str:
        return f"{self.target} | {self.record_type} ({self.document_date})"


class ScrubbedRecord(models.Model):
    """
    Stores scrubbed (cleaned PII) data associated with an ExtractedRecord.
    Maps to the 'scrubbed_records' table.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    extracted_record = models.OneToOneField(
        ExtractedRecord,
        on_delete=models.CASCADE,
        related_name="scrubbed_record",
    )
    data = models.JSONField(help_text="Cleaned JSON data (PII removed).")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = "scrubbed_records"

    def __str__(self) -> str:
        return f"Scrubbed | {self.extracted_record.id}"
