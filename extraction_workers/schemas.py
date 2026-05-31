from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# =========================================================
# Generic Base Schemas
# =========================================================


class DataQualityFlags(BaseModel):
    """Agentic self-verification flags to determine if human review is needed."""

    requires_human_review: bool = Field(
        ...,
        description="Set to True IF the data is ambiguous, key values are missing, or the document contains conflicting information.",
    )
    review_reason: Optional[str] = Field(
        None,
        description="If requires_human_review is True, briefly explain why based on the text.",
    )


class BaseExtractedRecord(BaseModel):
    """Generic base class for a single record extracted from a document."""

    entity_name: str = Field(
        ..., description="The name of the organization or company."
    )
    target_name: str = Field(
        ..., description="The specific project, asset, or location name."
    )
    document_date: date = Field(..., description="The date associated with this record.")
    record_type: str = Field(..., description="The category of data being extracted.")


class GenericDocumentExtraction(BaseModel):
    """A completely flexible schema for any document type."""

    metadata: BaseExtractedRecord
    extracted_data: Dict[str, Any] = Field(
        ..., description="Key-value pairs of the core data points."
    )
    data_quality_flags: DataQualityFlags


# =========================================================
# Specialized Industry Examples (e.g. Mining)
# =========================================================


class GradeEstimates(BaseModel):
    """Commodity grades extracted from the resource table."""

    Zn_percent: Optional[float] = None
    Pb_percent: Optional[float] = None
    Cu_percent: Optional[float] = None
    Ag_gt: Optional[float] = None
    Au_gt: Optional[float] = None


class ResourceEstimateItem(BaseModel):
    """An individual row of a resource estimate table."""

    target_name: str = Field(..., description="The name of the mine or deposit.")
    classification: str = Field(..., description="Measured, Indicated, or Inferred.")
    tonnage_mt: float = Field(..., description="Tonnage in millions of tonnes.")
    grades: GradeEstimates


class MiningResourceExtraction(BaseModel):
    """The root schema for extracting mining technical reports."""

    entity_name: str = Field(..., description="The company name.")
    effective_date: date = Field(..., description="The date of the estimate.")
    estimates: List[ResourceEstimateItem]
    data_quality_flags: DataQualityFlags
