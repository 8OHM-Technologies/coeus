from datetime import date
from typing import Any, Dict, Optional

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
    document_date: date = Field(
        ..., description="The date associated with this record."
    )
    record_type: str = Field(..., description="The category of data being extracted.")
    case_number: Optional[str] = Field(
        None, description="The case number associated with this record, if applicable."
    )


class GenericDocumentExtraction(BaseModel):
    """A completely flexible schema for any document type."""

    metadata: BaseExtractedRecord
    title: str = Field(
        ...,
        description="The title of the case."
    )
    
    extracted_data: Dict[str, Any] = Field(
        ..., description="Key-value pairs of the core data points."
    )
    data_quality_flags: DataQualityFlags
