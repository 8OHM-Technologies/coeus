from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field

from .generic import BaseExtractedRecord, DataQualityFlags

# =========================================================
# SAFLII Specific Schemas
# =========================================================


class PrecedentCategory(BaseModel):
    case_name_citation: str = Field(
        ...,
        description="The full name and citation of the past case referenced."
    )
    treatment: str = Field(
        ...,
        description="How the court treated this case. Must be either 'Applied/Followed' or 'Distinguished/Overruled'."
    )
    reasoning: str = Field(
        ...,
        description="A brief explanation of why the court applied or distinguished this specific precedent."
    )
    url: str = Field(
        ...,    
        description="URL pointing to the full text or HTML of the precedent."
    )


class SafliiHeaderData(BaseModel):
    applicant_plaintiff: Optional[str] = Field(
        None, 
        description="The full name of the applicant or plaintiff in this case."
    )
    respondent_defendant: List[str] = Field(
        default_factory=list, 
        description="List of respondents or defendants in this case."
    )
    hearing_date: Optional[date] = Field(
        None, 
        description="The date the hearing/trial was heard (YYYY-MM-DD)."
    )
    judgment_date: Optional[date] = Field(
        None, 
        description="The date the judgment was handed down (YYYY-MM-DD)."
    )
    reportable: Optional[bool] = Field(
        None, 
        description="Indicates whether the case is reportable."
    )
    court: Optional[str] = Field(
        None, 
        description="The standardized court where the case was heard (e.g., 'Constitutional Court of South Africa', 'Supreme Court of Appeal of South Africa', 'Gauteng High Court, Johannesburg', 'Western Cape High Court, Cape Town', 'Competition Appeal Court of South Africa', 'Labour Court of South Africa')."
    )
    judges: List[str] = Field(
        default_factory=list, 
        description="Full list of all presiding judges and justices in Title Case with uppercase judicial title abbreviations (e.g., 'Davis JP', 'Cameron J', 'Chaskalson P', 'Langa DP', 'Moseneke DCJ', 'Rogers AJA', 'Froneman J', 'Madlanga J', 'Nuku AJ', 'Mlambo DCJ', 'Dambuza J', 'Van der Westhuizen J'). Do NOT use all caps (e.g. 'MADLANGA J' is invalid). Do NOT include parties, applicants, respondents, advocates, or attorneys."
    )
    court_location: Optional[str] = Field(
        None, 
        description="The city or location of the court."
    )


class SafliiPrecedentsData(BaseModel):
    precedents_cited: List[PrecedentCategory] = Field(
        default_factory=list,
        description="A list of key past cases referenced by the court, categorized by how the court treated or distinguished them."
    )


class SafliiBodyData(BaseModel):
    ratio_decidendi: str = Field(
        ...,
        description="A detailed textual summary of the core binding legal rule, principle, statutory interpretation, or legal test established or relied upon by the majority to resolve the issue."
    )
    obiter_dicta: str = Field(
        ...,
        description="A textual summary of non-binding side remarks, hypotheticals, policy commentary, or observations made by the judges. If no obiter dicta exists, explicitly write 'No notable obiter dicta identified in this judgment.'"
    )
    order: str = Field(
        ..., 
        description="A concise and precise summary of the court's final order or ruling."
    )
    summary: str = Field(
        ..., 
        description="A comprehensive narrative summary of the case, covering the key legal issues, arguments presented, and the court's reasoning."
    )
    keywords: List[str] = Field(
        default_factory=list, 
        description="List of 5 to 10 key legal concepts, doctrines, statutory provisions, and topics discussed in the case (e.g., 'Constitutional Law', 'Section 27 Access to Healthcare', 'Abuse of Dominance', 'Margin Squeeze', 'Administrative Justice', 'Interdict')."
    )


class SafliiExtractedData(BaseModel):
    applicant_plaintiff: str = Field(
        ..., 
        description="The full name of the applicant or plaintiff in this case."
    )
    respondent_defendant: List[str] = Field(
        ..., 
        description="List of respondents or defendants in this case."
    )
    hearing_date: date = Field(
        ..., 
        description="The date the hearing/trial was heard (YYYY-MM-DD)."
    )
    judgment_date: date = Field(
        ..., 
        description="The date the judgment was handed down (YYYY-MM-DD)."
    )
    reportable: bool = Field(
        ..., 
        description="Indicates whether the case is reportable."
    )
    court: str = Field(
        ..., 
        description="The standardized court where the case was heard (e.g., 'Constitutional Court of South Africa', 'Supreme Court of Appeal of South Africa', 'Gauteng High Court, Johannesburg', 'Western Cape High Court, Cape Town', 'Competition Appeal Court of South Africa', 'Labour Court of South Africa')."
    )
    judges: List[str] = Field(
        ..., 
        description="Full list of all presiding judges and justices in Title Case with uppercase judicial title abbreviations (e.g., 'Davis JP', 'Cameron J', 'Chaskalson P', 'Langa DP', 'Moseneke DCJ', 'Rogers AJA', 'Froneman J', 'Madlanga J', 'Nuku AJ', 'Mlambo DCJ', 'Dambuza J', 'Van der Westhuizen J'). Do NOT use all caps (e.g. 'MADLANGA J' is invalid). Do NOT include parties, applicants, respondents, advocates, or attorneys."
    )
    court_location: str = Field(
        ..., 
        description="The city or location of the court."
    )
    ratio_decidendi: str = Field(
        ...,
        description="A detailed textual summary of the core binding legal rule, principle, statutory interpretation, or legal test established or relied upon by the majority to resolve the issue."
    )
    precedents_cited: List[PrecedentCategory] = Field(
        ...,
        description="A list of key past cases referenced by the court, categorized by how the court treated or distinguished them."
    )
    obiter_dicta: str = Field(
        ...,
        description="A textual summary of non-binding side remarks, hypotheticals, policy commentary, or observations made by the judges. If no obiter dicta exists, explicitly write 'No notable obiter dicta identified in this judgment.'"
    )
    order: str = Field(
        ..., 
        description="A concise and precise summary of the court's final order or ruling."
    )
    summary: str = Field(
        ..., 
        description="A comprehensive narrative summary of the case, covering the key legal issues, arguments presented, and the court's reasoning."
    )
    keywords: List[str] = Field(
        ..., 
        description="List of 5 to 10 key legal concepts, doctrines, statutory provisions, and topics discussed in the case (e.g., 'Constitutional Law', 'Section 27 Access to Healthcare', 'Abuse of Dominance', 'Margin Squeeze', 'Administrative Justice', 'Interdict')."
    )


class SafliiCaseExtraction(BaseModel):
    metadata: Optional[BaseExtractedRecord] = Field(
        None, description="System metadata record (populated by system)."
    )
    title: str = Field(
        ...,
        description="The title of the case (as per the source document)."
    )
    extracted_data: SafliiExtractedData = Field(
        ...,
        description="Structured core data points specific to a SAFLII case."
    )
    data_quality_flags: DataQualityFlags


class SafliiJournalGazetteExtraction(BaseModel):
    title: str = Field(
        ...,
        description="The title of the article/journal or gazette (as per the source document)."
    )
    formatted_text: str = Field(
        ...,
        description="The clean, well-formatted, and highly readable plain text content of the journal or gazette document."
    )
    data_quality_flags: Optional[DataQualityFlags] = None


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
    title: Optional[str] = Field(
        None,
        description="The title of the court roll."
    )
    roll_type: Optional[str] = Field(
        "Court Roll",
        description="Type of roll."
    )
    rows: List[SafliiCourtRollRow] = Field(
        ...,
        description="Tabular data rows representing each entry in the court roll."
    )
    data_quality_flags: Optional[DataQualityFlags] = None
