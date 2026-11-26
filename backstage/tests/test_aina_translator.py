"""AINA failure metadata without HTTP, model calls or credentials."""
import asyncio
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from translation.aina_translator import AinaTranslator


def translator(response=None, error=None):
    target = AinaTranslator("http://translator.invalid", "es")
    client = AsyncMock()
    client.post = AsyncMock(return_value=response, side_effect=error)
    target._get_client = AsyncMock(return_value=client)
    return target


def response(payload, status=200):
    return httpx.Response(status, json=payload,
                          request=httpx.Request("POST", "http://translator.invalid/translate"))


def test_success_including_unchanged_wording_is_not_a_fallback():
    async def scenario():
        target = translator(response({"es": "Barcelona"}))
        result = await target.translate_with_status("Barcelona", chunk_id="same-word")
        assert (result.text, result.status, result.error) == ("Barcelona", "translated", "")
        assert await target.translate("Barcelona") == "Barcelona"
    asyncio.run(scenario())


@pytest.mark.parametrize("error,reason", [
    (httpx.ReadError(""), "request_error"),
    (httpx.ReadTimeout(""), "timeout"),
    (RuntimeError("private-exception-detail"), "request_error"),
])
def test_error_type_and_chunk_logged_even_for_empty_exceptions(error, reason, caplog):
    async def scenario():
        target = translator(error=error)
        with caplog.at_level(logging.ERROR, logger="translation.aina_translator"):
            result = await target.translate_with_status("private source sentence", chunk_id="chunk-18")
        assert (result.text, result.status, result.error) == ("private source sentence", "fallback", reason)
        assert "chunk=chunk-18 target=es" in caplog.text
        assert f"error_type={type(error).__name__}" in caplog.text
        assert "duration_ms=" in caplog.text
        assert 'File "' in caplog.text  # Stack survives even when exception text is empty.
        assert "private-exception-detail" not in caplog.text
        assert "private source sentence" not in caplog.text
    asyncio.run(scenario())


def test_http_error_records_status_without_response_body(caplog):
    async def scenario():
        target = translator(response({"detail": "private-response-body"}, status=503))
        result = await target.translate_with_status("source", chunk_id="chunk-http")
        assert result.status == "fallback" and result.error == "http_error"
        assert "error_type=HTTPStatusError http_status=503" in caplog.text
        assert "private-response-body" not in caplog.text
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [{}, {"es": None}, {"es": ""}, {"es": "  "},
                                     {"es": 12}, [], "unexpected"])
def test_missing_empty_or_malformed_translation_is_explicit_fallback(payload):
    async def scenario():
        result = await translator(response(payload)).translate_with_status("source")
        assert (result.text, result.status, result.error) == ("source", "fallback", "invalid_response")
    asyncio.run(scenario())


def test_status_belongs_to_each_call_not_translator_mutable_state():
    async def scenario():
        target = translator()
        async def post(url, *, json):
            await asyncio.sleep(0)
            if json["text"] == "failed":
                raise httpx.ReadError("")
            return response({"es": "traducido"})
        target._get_client.return_value.post.side_effect = post
        failed, success = await asyncio.gather(
            target.translate_with_status("failed"), target.translate_with_status("success"))
        assert failed.status == "fallback"
        assert success.status == "translated" and success.error == ""
    asyncio.run(scenario())
