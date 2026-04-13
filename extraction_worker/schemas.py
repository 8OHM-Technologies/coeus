from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field


class GradeEstimates(BaseModel):
    """Commodity grades extracted from the resource table."""

    Zn_percent: Optional[float] = Field(
        None, description="Zinc grade as a percentage. Leave null if not present."
    )
    Pb_percent: Optional[float] = Field(
        None, description="Lead grade as a percentage. Leave null if not present."
    )
    Cu_percent: Optional[float] = Field(
        None, description="Copper grade as a percentage. Leave null if not present."
    )
    Ag_gt: Optional[float] = Field(
        None, description="Silver grade in grams per tonne (g/t)."
    )
    Au_gt: Optional[float] = Field(
        None, description="Gold grade in grams per tonne (g/t)."
    )


class ResourceEstimate(BaseModel):
    """An individual row of a resource estimate table."""

    asset_name: str = Field(
        ..., description="The specific name of the mine or deposit (e.g., 'Errington')."
    )
    classification: str = Field(
        ...,
        description="The resource category: usually 'Measured', 'Indicated', or 'Inferred'.",
    )
    tonnage_mt: float = Field(
        ...,
        description="The tonnage in millions of tonnes (Mt). Convert to millions if listed otherwise.",
    )
    grades: GradeEstimates = Field(
        ..., description="The specific mineral grades associated with this tonnage."
    )


class TechnicalParameters(BaseModel):
    """Underlying financial and geological parameters used for the estimate."""

    cut_off_grade: Optional[str] = Field(
        None,
        description="The cut-off grade used, usually found in the footnotes (e.g., '1.0% Zn').",
    )
    nsr_cutoff_usd: Optional[float] = Field(
        None,
        description="The Net Smelter Return (NSR) cut-off value in USD, if stated.",
    )


class DataQualityFlags(BaseModel):
    """Agentic self-verification flags to determine if human review is needed."""

    is_historical_estimate: bool = Field(
        ...,
        description="True if the text explicitly calls this a 'historical' estimate.",
    )
    requires_human_review: bool = Field(
        ...,
        description="Set to True IF the footnotes indicate that parameters are unverified, if key data is missing, or if the estimate does not comply with standard NI 43-101/CIM definitions.",
    )
    review_reason: Optional[str] = Field(
        None,
        description="If requires_human_review is True, briefly explain why based on the text.",
    )


class NI43101ReportExtraction(BaseModel):
    """The root schema for extracting data from an NI 43-101 technical report excerpt."""

    company_name: str = Field(
        ..., description="The company that owns the project or commissioned the report."
    )
    effective_date: date = Field(
        ..., description="The effective date of the resource estimate (YYYY-MM-DD)."
    )
    estimates: List[ResourceEstimate] = Field(
        ..., description="List of all resource and reserve estimates found in the text."
    )
    technical_parameters: TechnicalParameters
    data_quality_flags: DataQualityFlags
