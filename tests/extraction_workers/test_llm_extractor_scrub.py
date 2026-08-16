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

