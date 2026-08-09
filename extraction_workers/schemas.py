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
    metadata: BaseExtractedRecord

    applicant_plaintiff: str = Field(
        ..., 
        description="The name of the applicant or plaintiff."
    )
    respondent_defendant: List[str] = Field(
        ..., 
        description="List of respondents or defendants."
    )
    hearing_date: date = Field(
        ..., 
        description="The date the hearing was heared and judgment was delivered (YYYY-MM-DD)."
    )
    dataset_number: str = Field(
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
    court_location: str = Field(
        ..., 
        description="The city or location of the court."
    )
    result: str = Field(
        ..., 
        description="The final order or ruling delivered by the court."
    )
    summary: str = Field(
        ..., 
        description="The headnotes or narrative summary of the case."
    )
    keywords: List[str] = Field(
        ..., 
        description="List of keywords/slugs relevant to the case."
    )
    formatted_text: str = Field(
        ..., 
        description="The full formatted text of the judgment document. Found between the <!-- sino index --> and <!-- sino noindex --> HTML comment markers in the center_content JSON field."
    )
    
    data_quality_flags: DataQualityFlags


class SafliiJournalGazetteExtraction(BaseModel):
    metadata: BaseExtractedRecord
    formatted_text: str = Field(
        ...,
        description="The clean, well-formatted, and highly readable plain text content of the journal or gazette document."
    )
    data_quality_flags: DataQualityFlags


class SafliiCourtRollRow(BaseModel):
    column_1: str = Field(
        ...,
        description="Value of the first column (Typically Date, Time, or Case/Roll Number)."
    )
    column_2: str = Field(
        ...,
        description="Value of the second column (Typically Parties, Case Name, or Matter details)."
    )
    column_3: Optional[str] = Field(
        None,
        description="Value of the third column if present (Typically Presiding Judge, Courtroom, or Status)."
    )


class SafliiCourtRollExtraction(BaseModel):
    metadata: BaseExtractedRecord
    rows: List[SafliiCourtRollRow] = Field(
        ...,
        description="Tabular data rows representing each entry in the court roll."
    )
    data_quality_flags: DataQualityFlags
