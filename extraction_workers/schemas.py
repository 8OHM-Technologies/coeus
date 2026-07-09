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
    document_date: date = Field(
        ..., description="The date associated with this record."
    )
    record_type: str = Field(..., description="The category of data being extracted.")


class GenericDocumentExtraction(BaseModel):
    """A completely flexible schema for any document type."""

    metadata: BaseExtractedRecord
    extracted_data: Dict[str, Any] = Field(
        ..., description="Key-value pairs of the core data points."
    )
    data_quality_flags: DataQualityFlags

class SafliiCaseExtraction(BaseModel):
    applicant_plaintiff: str = Field(
        ..., 
        description="The name of the applicant or plaintiff."
    )
    respondent_defendant: List[str] = Field(
        ..., 
        description="List of respondents or defendants."
    )
    judgment_date: date = Field(
        ..., 
        description="The date the judgment was delivered (YYYY-MM-DD)."
    )
    case_number: str = Field(
        ..., 
        description="The official case reference number."
    )
    reportable: bool = Field(
        ..., 
        description="Indicates whether the case is reportable."
    )
    subjects: List[str] = Field(
        ..., 
        description="List of legal subject areas or classifications."
    )
    court: str = Field(
        ..., 
        description="The court where the case was heard."
    )
    judges: List[str] = Field(
        ..., 
        description="List of presiding judges."
    )
    summary: str = Field(
        ..., 
        description="Headnotes or formal summary of the case."
    )
    court_location: str = Field(
        ..., 
        description="The city or location of the court."
    )
    result: str = Field(
        ..., 
        description="The final order or ruling delivered by the court."
    )
    ai_summary: str = Field(
        ..., 
        description="AI-generated narrative summary of the case."
    )
    ai_keywords: List[str] = Field(
        ..., 
        description="List of AI-generated keywords relevant to the case."
    )
