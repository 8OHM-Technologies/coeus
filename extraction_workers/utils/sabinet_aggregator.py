import json
import re
from datetime import datetime
import pandas as pd

def extract_case_year(case_number: str) -> int:
    """
    Extracts the initiation year from South African case numbers.
    Handles standard formats (e.g., 2026/061774) and specialized court formats (e.g., J1044/25).
    """
    # Matches /2026, /26, or leading 2026 digits
    match = re.search(r'(20\d{2})|/(\d{2})$|/(\d{4})', case_number)
    if not match:
        return None
    
    raw_year = match.group(0).replace('/', '')
    if len(raw_year) == 2:
        return int("20" + raw_year)
    return int(raw_year)

def process_legal_metrics(json_data: list) -> dict:
    """
    Processes raw judgment arrays into structured dashboard metrics.
    """
    records = []
    all_subjects = []
    all_keywords = []

    for case in json_data:
        # 1. Temporal Analytics: Calculate case lifecycle duration
        case_year = extract_case_year(case["case_number"])
        judgment_year = datetime.strptime(case["judgment_date"], "%Y-%m-%d").year
        duration_years = max(0, judgment_year - case_year) if case_year else None

        # Flatten structure for easy Pandas processing
        records.append({
            "case_number": case["case_number"],
            "court": case["court"],
            "division": case["division_location"],
            "judge": case["judge"],
            "reportable": case["reportable"],
            "outcome": case["result"]["outcome_type"],
            "costs_order": case["result"]["costs_order"],
            "duration_years": duration_years,
            "applicant": case["parties"]["applicant_plaintiff"],
            "respondent": case["parties"]["respondent_defendant"]
        })
        
        # Collect nested items
        all_subjects.extend(case.get("subjects", []))
        all_keywords.extend(case.get("ai_metadata", {}).get("keywords", []))

    df = pd.DataFrame(records)

    # 2. Compute Dashboard Aggregations
    dashboard_metrics = {
        "kpis": {
            "total_cases_analyzed": len(df),
            "average_case_lifecycle_years": round(df["duration_years"].dropna().mean(), 1),
            "reportable_ratio": df["reportable"].value_counts(normalize=True).to_dict()
        },
        "court_workload": {
            "by_court_type": df["court"].value_counts().to_dict(),
            "by_division": df["division"].value_counts().to_dict()
        },
        "litigation_trends": {
            "top_outcomes": df["outcome"].value_counts().to_dict(),
            "top_subject_matters": pd.Series(all_subjects).value_counts().head(10).to_dict(),
            "trending_keywords": pd.Series(all_keywords).value_counts().head(10).to_dict()
        },
        "judge_analytics": {
            "case_volumes_per_judge": df["judge"].value_counts().to_dict(),
            # Cross-tabulates exactly how each judge rules on applications
            "judge_outcome_matrix": df.groupby(["judge", "outcome"]).size().unstack(fill_value=0).to_dict(orient="index")
        }
    }
    
    return dashboard_metrics

# --- Example Dashboard Execution ---
if __name__ == "__main__":
    # Load your JSON dataset 
    with open("sa_legal_data.json", "w") as f:
        # Mocking a collection array based on your inputs
        mock_data = [
            {
                "case_number": "2026/061774", "court": "High Court", "division_location": "Gauteng [Johannesburg]",
                "judge": "Maduray [J]", "judgment_date": "2026-06-11", "reportable": False,
                "parties": {"applicant_plaintiff": "GIB Insurance", "respondent_defendant": "Maduray"},
                "subjects": ["Labour > Restraint of trade"], "result": {"outcome_type": "Dismissed", "costs_order": "Applicant to pay"}
            },
            {
                "case_number": "2024/014290", "court": "High Court", "division_location": "Gauteng [Pretoria]",
                "judge": "Mnguni [AJ]", "judgment_date": "2026-06-18", "reportable": True,
                "parties": {"applicant_plaintiff": "Nedbank Ltd", "respondent_defendant": "Khumalo"},
                "subjects": ["Commercial Law > Banking"], "result": {"outcome_type": "Granted", "costs_order": "Respondent to pay"}
            }
        ]
        json.dump(mock_data, f)

    # Execute processing
    with open("sa_legal_data.json", "r") as f:
        raw_cases = json.load(f)
        
    compiled_metrics = process_legal_metrics(raw_cases)
    print(json.dumps(compiled_metrics, indent=4))