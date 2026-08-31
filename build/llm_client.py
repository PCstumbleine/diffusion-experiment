"""
LLM client for the extraction runner. Loads the exact system + task prompt
text from extraction_prompt_v1.md (never hand-copied/paraphrased into this
file, so a future prompt edit can't silently drift out of sync with what's
actually sent) and calls the configured model with JSON output.

Deliberately NOT exercised by any test with a real network/API call --
tests inject their own stub/mock client (same pattern as
edgar_ingest_worker.py's EdgarClient / tests' make_client). Standing up a
real LLM API budget is explicitly out of scope for this task (see
docs/EXTRACTION_RUNNER_DESIGN_V2.md's "still open" infrastructure fork) --
this class exists so the wiring is real and reviewable, not so it gets run
unattended today. Set ANTHROPIC_API_KEY before using AnthropicExtractionClient
for real.
"""

from __future__ import annotations

import json
from pathlib import Path

PROMPT_PATH = Path(__file__).parent / "extraction_prompt_v1.md"
PROMPT_VERSION = "1.1.0"  # must match extraction_prompt_v1.md's own "const" schema value


def load_prompt_texts(path: Path = PROMPT_PATH) -> tuple[str, str, dict]:
    """Extracts the system prompt, task instructions, and JSON schema
    exactly as fenced in extraction_prompt_v1.md -- never re-typed by hand,
    so this can't silently drift from the file a human reviews and edits."""
    text = path.read_text(encoding="utf-8")

    def _fenced_block_after(heading: str) -> str:
        idx = text.index(heading)
        fence_start = text.index("```", idx)
        # Skip the language tag on the opening fence line, if any (e.g. ```json)
        first_newline = text.index("\n", fence_start)
        fence_end = text.index("```", first_newline)
        return text[first_newline + 1:fence_end].strip()

    system_prompt = _fenced_block_after("## System prompt")
    task_instructions = _fenced_block_after("## Task instructions")
    schema_text = _fenced_block_after("## Output schema")
    schema = json.loads(schema_text)
    return system_prompt, task_instructions, schema


EXTRACTION_TOOL_NAME = "record_extraction"


class AnthropicExtractionClient:
    """Real client -- not used by any test. Requires ANTHROPIC_API_KEY.

    Fix (code review, pre-live-dry-run): the response used to be
    constrained only by a text instruction ("Return ONLY valid JSON
    matching the schema") -- prose, nothing the API actually enforced.
    Now forces a tool call whose input_schema IS the loaded JSON schema,
    using Claude's tool-use mechanism as the real structured-output
    constraint; extraction_runner.py separately re-validates the result
    against the same schema with a real JSON Schema validator before
    treating it as parseable (belt-and-suspenders: tool-use constrains
    generation, jsonschema.validate confirms it server-side)."""

    def __init__(self, model: str, api_key: str | None = None):
        import anthropic  # deferred import: only needed for real (non-test) use

        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key)
        self.system_prompt, self.task_instructions, self.schema = load_prompt_texts()

    def extract(self, document_id: str, raw_content: str, prompt_version: str) -> dict:
        if prompt_version != PROMPT_VERSION:
            raise ValueError(
                f"Requested extraction_prompt_version {prompt_version!r} does not match "
                f"the prompt text actually loaded ({PROMPT_VERSION!r}) -- refusing to call "
                "the LLM under a version label that doesn't match what's being sent."
            )
        user_message = (
            f"{self.task_instructions}\n\n"
            f"document_id: {document_id}\n"
            f"extraction_prompt_version: {prompt_version}\n\n"
            f"--- DOCUMENT TEXT ---\n{raw_content}\n--- END DOCUMENT TEXT ---"
        )
        tool = {
            "name": EXTRACTION_TOOL_NAME,
            "description": "Records the structured extraction result. Call this exactly once with the complete result.",
            "input_schema": self.schema,
        }
        response = self.client.messages.create(
            model=self.model,
            max_tokens=8192,
            system=self.system_prompt,
            tools=[tool],
            tool_choice={"type": "tool", "name": EXTRACTION_TOOL_NAME},
            messages=[{"role": "user", "content": user_message}],
        )
        tool_use_block = next(block for block in response.content if block.type == "tool_use")
        return tool_use_block.input
