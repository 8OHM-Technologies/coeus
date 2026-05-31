import uuid

from django.db import models


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
        managed = False
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
    location = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
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
        max_length=100, help_text="e.g., 'Resource Estimate' or 'Financial Summary'"
    )

    # The flexible payload
    data = models.JSONField(help_text="Industry-specific data extracted by the LLM.")

    requires_human_review = models.BooleanField(default=False)
    review_reason = models.TextField(blank=True, null=True)
    source_url = models.TextField(blank=True, null=True)
    extracted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "extracted_records"
        unique_together = (("target", "document_date", "record_type"),)

    def __str__(self) -> str:
        return f"{self.target} | {self.record_type} ({self.document_date})"
