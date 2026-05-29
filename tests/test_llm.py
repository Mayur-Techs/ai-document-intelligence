"""
Legacy tests for processor/llm.py prompt and response parsing.

The active production pipeline is extractor/pipeline.py with Cerebras and Groq.
These tests remain because processor/llm.py still contains useful prompt-building
and response-parsing behavior from an older Gemini-based implementation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from processor.llm import ExtractionOutput, build_prompt, extract_fields, parse_llm_response


class TestBuildPrompt:
    def test_prompt_contains_document_text(self, sample_extracted_text):
        prompt = build_prompt(sample_extracted_text, document_type="invoice")
        assert "Sharma Freight" in prompt or sample_extracted_text[:50] in prompt

    def test_prompt_contains_document_type(self, sample_extracted_text):
        prompt = build_prompt(sample_extracted_text, document_type="contract")
        assert "contract" in prompt.lower()

    def test_prompt_requests_json_only(self, sample_extracted_text):
        prompt = build_prompt(sample_extracted_text, document_type="invoice")
        assert "json" in prompt.lower()
        assert (
            "only" in prompt.lower()
            or "no other" in prompt.lower()
            or "no preamble" in prompt.lower()
        )

    def test_prompt_asks_for_confidence_score(self, sample_extracted_text):
        prompt = build_prompt(sample_extracted_text, document_type="invoice")
        assert "confidence" in prompt.lower()

    def test_prompt_truncates_long_text(self):
        """build_prompt must cap text length so unbounded prompts do not blow token limits."""
        long_text = "Invoice data: " + "A" * 100_000
        prompt = build_prompt(long_text, document_type="invoice")
        assert len(prompt) < 16_000


class TestParseLlmResponse:
    def test_valid_json_parsed_correctly(self, sample_llm_response):
        raw_text = sample_llm_response["content"][0]["text"]
        output = parse_llm_response(raw_text)
        assert output.success is True
        assert output.data["vendor_name"] == "Sharma Freight Solutions Pvt Ltd"
        assert output.data["invoice_number"] == "INV-2026-04892"
        assert output.data["total_amount"] == 197355.00
        assert output.confidence == pytest.approx(0.94, abs=0.01)

    def test_json_with_markdown_fences_parsed(self):
        raw = '```json\n{"vendor_name": "Test Corp", "total_amount": 1000, "confidence_score": 80}\n```'
        output = parse_llm_response(raw)
        assert output.success is True
        assert output.data["vendor_name"] == "Test Corp"

    def test_missing_confidence_defaults_to_none(self):
        raw = '{"vendor_name": "Test", "total_amount": 500}'
        output = parse_llm_response(raw)
        assert output.success is True
        assert output.confidence is None or output.confidence == 0.0

    def test_malformed_json_returns_error(self):
        raw = "Sorry, I cannot extract that."
        output = parse_llm_response(raw)
        assert output.success is False
        assert output.error is not None

    def test_partial_json_still_parsed(self):
        raw = '{"vendor_name": "Partial Corp", "total_amount": 750'
        output = parse_llm_response(raw)
        assert isinstance(output, ExtractionOutput)


class TestExtractFields:
    @pytest.mark.skip(reason="Legacy provider integration replaced with Cerebras+Groq")
    async def test_successful_extraction(self, sample_extracted_text, sample_llm_response):
        """Mock legacy AI API returns valid JSON and maps it into ExtractionOutput."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_llm_response
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("processor.llm.httpx.AsyncClient", return_value=mock_client), patch(
            "processor.llm.GEMINI_API_KEY", "test-key"
        ):
            output = await extract_fields(sample_extracted_text, document_type="invoice")

        assert output.success is True
        assert output.data["vendor_name"] == "Sharma Freight Solutions Pvt Ltd"
        assert output.data["total_amount"] == 197355.00
        assert output.confidence == pytest.approx(0.94, abs=0.01)

    @pytest.mark.skip(reason="Legacy provider integration replaced with Cerebras+Groq")
    async def test_missing_api_key_returns_error(self, sample_extracted_text):
        with patch("processor.llm.GEMINI_API_KEY", ""):
            output = await extract_fields(sample_extracted_text, document_type="invoice")

        assert output.success is False
        assert "api_key" in output.error.lower() or "not set" in output.error.lower()

    @pytest.mark.skip(reason="Legacy provider integration replaced with Cerebras+Groq")
    async def test_http_error_returns_error(self, sample_extracted_text):
        import httpx

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "rate limited",
                request=MagicMock(),
                response=MagicMock(status_code=429),
            )
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("processor.llm.httpx.AsyncClient", return_value=mock_client), patch(
            "processor.llm.GEMINI_API_KEY", "test-key"
        ):
            output = await extract_fields(sample_extracted_text, document_type="invoice")

        assert output.success is False
        assert output.error is not None

    @pytest.mark.skip(reason="Legacy provider integration replaced with Cerebras+Groq")
    async def test_malformed_response_returns_error(self, sample_extracted_text):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "content": [{"type": "text", "text": "I cannot process this document."}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("processor.llm.httpx.AsyncClient", return_value=mock_client), patch(
            "processor.llm.GEMINI_API_KEY", "test-key"
        ):
            output = await extract_fields(sample_extracted_text, document_type="invoice")

        assert isinstance(output, ExtractionOutput)
