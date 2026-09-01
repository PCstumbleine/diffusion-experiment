"""
FileBackedExtractionClient (no network, no cost) -- used for a dry run
where the extraction JSON was produced by hand ahead of time.
AnthropicExtractionClient itself is deliberately not exercised here (real
API, real cost) -- same convention as every other real-network client in
this codebase (edgar_ingest_worker.py's EdgarClient).
"""
import json

import pytest

from llm_client import FileBackedExtractionClient, load_prompt_texts, PROMPT_VERSION


def test_load_prompt_texts_extracts_system_task_and_schema():
    system_prompt, task_instructions, schema = load_prompt_texts()
    assert "information-extraction system" in system_prompt
    assert "Read the document below" in task_instructions
    assert schema["required"] == ["document_id", "extraction_prompt_version", "events"]
    assert schema["properties"]["extraction_prompt_version"]["const"] == PROMPT_VERSION


def test_file_backed_client_replays_saved_json(tmp_path):
    saved = {"document_id": "doc-1", "extraction_prompt_version": PROMPT_VERSION, "events": []}
    (tmp_path / "doc-1.json").write_text(json.dumps(saved), encoding="utf-8")

    client = FileBackedExtractionClient(tmp_path)
    result = client.extract("doc-1", "some raw content", PROMPT_VERSION)
    assert result == saved


def test_file_backed_client_raises_clearly_when_no_saved_file_exists(tmp_path):
    client = FileBackedExtractionClient(tmp_path)
    with pytest.raises(FileNotFoundError):
        client.extract("missing-doc", "content", PROMPT_VERSION)
