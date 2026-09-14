from types import SimpleNamespace

import openpyxl
import pytest

from src.processing.result_parsing import clean_json_text, extract_message_text
from src.processing.xlsx_schema import (
    extract_sheet_headers,
    extract_sheet_names,
    resolve_target_columns,
    validate_headers,
)


def test_xlsx_schema_validates_and_resolves_columns(tmp_path):
    path = tmp_path / "schema.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.title = "Data"
    workbook.active.append([" Title ", "Description"])
    workbook.create_sheet("Meta").append(["Value"])
    workbook.save(path)
    workbook.close()

    assert extract_sheet_names(path) == ["Data", "Meta"]
    assert extract_sheet_headers(path, "Data") == ["Title", "Description"]
    assert resolve_target_columns(["Title", "Description"], ["description", "title"]) == ["Title", "Description"]


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        (["", ""], "表头为空"),
        (["Title", ""], "空表头"),
        (["Title", "title"], "重复表头"),
    ],
)
def test_xlsx_schema_rejects_invalid_headers(headers, message):
    with pytest.raises(ValueError, match=message):
        validate_headers(headers)


def test_result_parsing_handles_fenced_json_and_message_parts():
    assert clean_json_text("```json\n[{\"ok\": true}]\n```") == '[{"ok": true}]'
    response = SimpleNamespace(content=["prefix", {"text": "-body"}, 3])
    assert extract_message_text(response) == "prefix-body3"
