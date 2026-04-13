# control_plane/mining_data/models.py
import uuid

from django.db import models


class Company(models.Model):
    """Maps to the 'companies' table created by init.sql"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, unique=True)
    ticker = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "companies"
        verbose_name_plural = "Companies"

    def __str__(self) -> str:
        return str(self.name)


class Asset(models.Model):
    """Maps to the 'assets' table created by init.sql"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    asset_name = models.CharField(max_length=255)
    location = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "assets"
        unique_together = (("company", "asset_name"),)

    def __str__(self) -> str:
        return f"{self.company.name} - {self.asset_name}"


class ResourceEstimate(models.Model):
    """Maps to the 'resource_estimates' table created by init.sql"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE)
    effective_date = models.DateField()
    classification = models.CharField(max_length=50)
    commodity = models.CharField(max_length=50)

    tonnage_mt = models.DecimalField(
        max_digits=10, decimal_places=2, blank=True, null=True
    )
    grade = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    grade_unit = models.CharField(max_length=20, blank=True, null=True)

    requires_human_review = models.BooleanField(default=False)
    review_reason = models.TextField(blank=True, null=True)
    source_url = models.TextField(blank=True, null=True)
    extracted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "resource_estimates"
        unique_together = (("asset", "effective_date", "classification", "commodity"),)

    def __str__(self) -> str:
        return f"{self.asset} | {self.classification} {self.commodity} ({self.effective_date})"
