import sys
import os
import pytest

# Ensure extraction_workers is importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../extraction_workers")))

from llm_extractor import regex_scrub_text, scrub_pii_data


def test_regex_scrub_text():
    # Test RSA ID redaction (13 digits)
    input_text = "The applicant's ID number is 8203155123087."
    expected = "The applicant's ID number is [RSA ID]."
    assert regex_scrub_text(input_text) == expected

    # Test Passport redaction (1 letter + 8 digits)
    input_passport = "Passport details: A12345678 or passport M98765432."
    expected_passport = "Passport details: [PASSPORT] or passport [PASSPORT]."
    assert regex_scrub_text(input_passport) == expected_passport

    # Test Bank Account / Tax Number redaction (10-16 digits)
    input_bank = "Account number: 123456789012. Tax ref: 1234567890."
    expected_bank = "Account number: [BANK/TAX NUMBER]. Tax ref: [BANK/TAX NUMBER]."
    assert regex_scrub_text(input_bank) == expected_bank

    # Test retention of Case Numbers (e.g. 1234/2023) and Years/Dates (e.g. 2025-08-08)
    input_retained = "In Case 1234/2023 heard on 2025-08-08, Judge Smith ruled."
    assert regex_scrub_text(input_retained) == input_retained


def test_scrub_pii_data_recursive():
    # Test dictionary scrubbing
    data = {
        "applicant_plaintiff": "John Doe",
        "dataset_number": "1234/2023",
        "rsa_id": "8203155123087",
        "details": {
            "passport": "M98765432",
            "account_list": ["1234567890", "plain text"]
        }
    }
    expected = {
        "applicant_plaintiff": "John Doe",
        "dataset_number": "1234/2023",
        "rsa_id": "[RSA ID]",
        "details": {
            "passport": "[PASSPORT]",
            "account_list": ["[BANK/TAX NUMBER]", "plain text"]
        }
    }
    assert scrub_pii_data(data) == expected


@pytest.mark.asyncio
async def test_run_parser_phase(mocker):
    import uuid
    from llm_extractor import run_parser_phase

    fake_id = uuid.uuid4()
    mock_conn = mocker.AsyncMock()

    # Simulate 1 unparsed record
    mock_conn.fetchval.return_value = 1
    mock_conn.fetch.side_effect = [
        [
            {"id": fake_id, "data": {"full_text": "Sample text", "center_content": "Sample center"}}
        ],
        [], # second call returns empty list to exit loop
    ]

    mock_parser = mocker.MagicMock(return_value={"header": "Sample text", "judgment": None, "null_values": []})

    count = await run_parser_phase(
        conn=mock_conn,
        pipeline_name="test_pipeline",
        parser_func=mock_parser,
        batch_size=10,
    )

    assert count == 1
    mock_parser.assert_called_once_with("Sample text", "Sample center")
    assert mock_conn.execute.call_count == 2 # 1 insert into parsed_records, 1 update to extracted_records


def test_get_record_category():
    from llm_extractor import get_record_category

    assert get_record_category({"category": "cases"}) == "cases"
    assert get_record_category({}, "https://www.saflii.org/za/cases/ZACC/2023/1.html") == "cases"
    assert get_record_category({}, "https://www.saflii.org/za/gaz/ZAGovGaz/2016/477.html") == "gaz"
    assert get_record_category({}, "https://www.saflii.org/za/journals/PER/2020/1.html") == "journals"
    assert get_record_category({}, "https://www.saflii.org/za/other/ZAGPPHCRolls/2012/55.pdf") == "other"


@pytest.mark.asyncio
async def test_run_parser_phase_saflii_skips_non_cases(mocker):
    import uuid
    from llm_extractor import run_parser_phase

    fake_gaz_id = uuid.uuid4()
    fake_case_id = uuid.uuid4()
    mock_conn = mocker.AsyncMock()

    mock_conn.fetchval.return_value = 2
    mock_conn.fetch.side_effect = [
        [
            {"id": fake_gaz_id, "data": {"category": "gaz", "full_text": "Gazette text"}, "source_url": "https://www.saflii.org/za/gaz/1.html"},
            {"id": fake_case_id, "data": {"category": "cases", "full_text": "Judgment text", "center_content": ""}, "source_url": "https://www.saflii.org/za/cases/1.html"},
        ],
        [],
    ]

    mock_parser = mocker.MagicMock(return_value={"header": "Judgment text", "judgment": None, "null_values": []})

    count = await run_parser_phase(
        conn=mock_conn,
        pipeline_name="saflii_courts",
        parser_func=mock_parser,
        batch_size=10,
    )

    assert count == 1
    mock_parser.assert_called_once_with("Judgment text", "")
    assert mock_conn.execute.call_count == 2


def test_clean_saflii_text_lawcite_and_noise():
    from utils.saflii_document_parser import clean_saflii_text

    raw_input = (
        "Home | Databases | WorldLII | DecSearch | LawCite\n"
        "Download original files\n"
        "PDF format\n"
        "RTF format\n\n\n"
        "POTCHEFSTROOM ELECTRONIC LAW JOURNAL\n"
        "2020 VOLUME 23\n\n\n"
        "Author: Prof John Doe\n\n"
        "Links to summary\n"
        "Heads of argument\n\n"
        "1. Introduction\n"
        "This article discusses constitutional law principles."
    )

    cleaned = clean_saflii_text(raw_input)

    assert "LawCite" not in cleaned
    assert "Home | Databases" not in cleaned
    assert "Download original files" not in cleaned
    assert "PDF format" not in cleaned
    assert "RTF format" not in cleaned
    assert "Links to summary" not in cleaned
    assert "Heads of argument" not in cleaned
    assert "POTCHEFSTROOM ELECTRONIC LAW JOURNAL" in cleaned
    assert "Author: Prof John Doe" in cleaned
    assert "1. Introduction" in cleaned
    # Ensure blank lines are normalized (no 3+ runs)
    assert "\n\n\n" not in cleaned


def test_clean_saflii_text_header_collapse():
    from utils.saflii_document_parser import clean_saflii_text

    raw_header = (
        "LawCite\n\n"
        "HIGH COURT OF SOUTH AFRICA\n\n\n"
        "CASE NO: 1234/2023\n"
    )

    cleaned = clean_saflii_text(raw_header, collapse_to_single_newline=True)
    assert cleaned == "HIGH COURT OF SOUTH AFRICA\nCASE NO: 1234/2023"


@pytest.mark.asyncio
async def test_fix_saflii_journal_records(mocker):
    import uuid
    from scripts.fix_saflii_journal_records import fix_journal_records

    fake_scrubbed_id = uuid.uuid4()
    fake_rec_id = uuid.uuid4()
    mock_conn = mocker.AsyncMock()

    raw_text = "LawCite\nDownload original files\n\nArticle Title\n\nContent here."
    mock_conn.fetch.return_value = [
        {
            "scrubbed_id": fake_scrubbed_id,
            "extracted_record_id": fake_rec_id,
            "scrubbed_data": {"formatted_text": raw_text, "title": "Article Title"},
            "source_url": "https://www.saflii.org/za/journals/PER/2020/1.html",
            "extracted_data": {},
            "target_name": "PER",
        }
    ]

    mock_conn.transaction = mocker.MagicMock()
    mock_conn.transaction.return_value.__aenter__ = mocker.AsyncMock()
    mock_conn.transaction.return_value.__aexit__ = mocker.AsyncMock()
    mocker.patch("scripts.fix_saflii_journal_records.get_db_connection", return_value=mock_conn)

    # Test dry run (no execute)
    await fix_journal_records(dry_run=True)
    assert mock_conn.execute.call_count == 0

    # Test force execute (performs update)
    await fix_journal_records(force=True)
    assert mock_conn.execute.call_count == 1
    call_args = mock_conn.execute.call_args[0]
    assert "UPDATE scrubbed_records" in call_args[0]
    updated_data_json = call_args[1]
    assert "LawCite" not in updated_data_json
    assert "Download original files" not in updated_data_json
    assert "Article Title" in updated_data_json


def test_format_judge_name():
    from llm_extractor import format_judge_name

    # Basic casing and acronym preservation
    assert format_judge_name("MAHLANGA AJ") == "Mahlanga AJ"
    assert format_judge_name("Mahlanga AJ") == "Mahlanga AJ"
    assert format_judge_name("NUKU AJ") == "Nuku AJ"
    assert format_judge_name("MLAMBO DCJ") == "Mlambo DCJ"
    assert format_judge_name("DAMBUZA J") == "Dambuza J"
    assert format_judge_name("KOLLAPEN J") == "Kollapen J"
    assert format_judge_name("DAVIS JP") == "Davis JP"
    assert format_judge_name("ROGERS AJA") == "Rogers AJA"
    assert format_judge_name("MOGOENG CJ") == "Mogoeng CJ"
    assert format_judge_name("CHASKALSON P") == "Chaskalson P"
    assert format_judge_name("LANGA DP") == "Langa DP"
    assert format_judge_name("NAVSA JA") == "Navsa JA"
    assert format_judge_name("MAYA DCJ") == "Maya DCJ"

    # Multi-part names and particles (e.g. Van der Westhuizen, De Villiers)
    assert format_judge_name("VAN DER WESTHUIZEN J") == "Van der Westhuizen J"
    assert format_judge_name("DE VILLIERS AJ") == "De Villiers AJ"
    assert format_judge_name("DU PLESSIS J") == "Du Plessis J"

    # Strip prefixes and parentheticals
    assert format_judge_name("Justice Cameron J") == "Cameron J"
    assert format_judge_name("Adv. Smith") == "Smith"
    assert format_judge_name("Judge President Davis JP") == "Davis JP"
    assert format_judge_name("NUKU AJ (unanimous)") == "Nuku AJ"
    assert format_judge_name("MLAMBO DCJ (concurring)") == "Mlambo DCJ"

    # Initials
    assert format_judge_name("CK MATSHITSE") == "CK Matshitse"
    assert format_judge_name("C.K. MATSHITSE AJ") == "C.K. Matshitse AJ"


def test_normalize_court_name():
    from llm_extractor import normalize_court_name

    # Direct target code matches
    assert normalize_court_name("ZACC") == "Constitutional Court of South Africa"
    assert normalize_court_name("ZASCA") == "Supreme Court of Appeal of South Africa"
    assert normalize_court_name("ZAGPJHC") == "Gauteng High Court, Johannesburg"
    assert normalize_court_name("ZAGPPHC") == "Gauteng High Court, Pretoria"
    assert normalize_court_name("ZAWCHC") == "Western Cape High Court, Cape Town"
    assert normalize_court_name("ZACAC") == "Competition Appeal Court of South Africa"

    # Full and partial name normalizations
    assert normalize_court_name("Constitutional Court") == "Constitutional Court of South Africa"
    assert normalize_court_name("Constitutional Court of South Africa") == "Constitutional Court of South Africa"
    assert normalize_court_name("Supreme Court of Appeal") == "Supreme Court of Appeal of South Africa"
    assert normalize_court_name("HIGH COURT OF SOUTH AFRICA, GAUTENG LOCAL DIVISION, JOHANNESBURG") == "Gauteng High Court, Johannesburg"
    assert normalize_court_name("High Court of South Africa, Gauteng Division, Pretoria") == "Gauteng High Court, Pretoria"
    assert normalize_court_name("Western Cape High Court") == "Western Cape High Court, Cape Town"
    assert normalize_court_name("Free State High Court, Bloemfontein") == "Free State High Court, Bloemfontein"
    assert normalize_court_name("KwaZulu-Natal High Court, Durban") == "KwaZulu-Natal High Court, Durban"
    assert normalize_court_name("Eastern Cape High Court, Grahamstown") == "Eastern Cape High Court, Grahamstown"
    assert normalize_court_name("Eastern Cape High Court, Makhanda") == "Eastern Cape High Court, Grahamstown"
    assert normalize_court_name("Eastern Cape High Court, Port Elizabeth") == "Eastern Cape High Court, Port Elizabeth"
    assert normalize_court_name("Eastern Cape High Court, Gqeberha") == "Eastern Cape High Court, Port Elizabeth"
    assert normalize_court_name("Labour Court, Johannesburg") == "Labour Court, Johannesburg"
    assert normalize_court_name("Labour Appeal Court") == "Labour Appeal Court of South Africa"

    # Fallback to target_name
    assert normalize_court_name("High Court", target_name="ZAGPJHC") == "Gauteng High Court, Johannesburg"
    assert normalize_court_name(None, target_name="ZACC") == "Constitutional Court of South Africa"





