from .generic import (
    BaseExtractedRecord,
    DataQualityFlags,
    GenericDocumentExtraction,
)
from .saflii import (
    PrecedentCategory,
    SafliiBodyData,
    SafliiCaseExtraction,
    SafliiCourtRollExtraction,
    SafliiCourtRollRow,
    SafliiExtractedData,
    SafliiHeaderData,
    SafliiJournalGazetteExtraction,
    SafliiPrecedentsData,
)

__all__ = [
    # Generic schemas
    "DataQualityFlags",
    "BaseExtractedRecord",
    "GenericDocumentExtraction",
    # SAFLII schemas
    "PrecedentCategory",
    "SafliiHeaderData",
    "SafliiPrecedentsData",
    "SafliiBodyData",
    "SafliiExtractedData",
    "SafliiCaseExtraction",
    "SafliiJournalGazetteExtraction",
    "SafliiCourtRollRow",
    "SafliiCourtRollExtraction",
]
