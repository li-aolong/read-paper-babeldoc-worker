from types import SimpleNamespace

import httpx
import openai
import pytest

from compact_ir import CompactIRCaptureError, CompactIRCollector, observe_babeldoc
from translation_guard import TranslationGuard, TranslationServiceError


def rate_limit():
    return openai.RateLimitError(
        "secret-request-details",
        response=httpx.Response(
            429, request=httpx.Request("POST", "https://example.com")
        ),
        body=None,
    )


def test_retries_are_bounded_and_failure_prevents_new_requests():
    waits = []
    calls = []
    cancellations = []
    guard = TranslationGuard(waits.append, sleep=lambda _: None)
    guard.cancel = lambda: cancellations.append(True)

    def unavailable():
        calls.append(True)
        raise rate_limit()

    with pytest.raises(TranslationServiceError, match="模型服务繁忙") as error:
        guard.call(unavailable)
    assert error.value.code == "rate_limited"
    assert "secret-request-details" not in str(error.value)
    assert len(calls) == 4
    assert cancellations == [True]
    assert waits[0] == "rate_limited" and waits[-1] is None
    with pytest.raises(TranslationServiceError):
        guard.call(unavailable)
    assert len(calls) == 4


def test_transient_limit_recovers_without_poisoning_task():
    waits = []
    calls = []
    guard = TranslationGuard(waits.append, sleep=lambda _: None)

    def recover():
        calls.append(True)
        if len(calls) == 1:
            raise rate_limit()
        return "译文"

    assert guard.call(recover) == "译文"
    assert waits == ["rate_limited", None]
    guard.check()


def test_authentication_failure_does_not_retry_or_leak_key():
    calls = []
    guard = TranslationGuard(lambda _: None, sleep=lambda _: None)

    def bad_key():
        calls.append(True)
        raise openai.AuthenticationError(
            "private-key",
            response=httpx.Response(
                401, request=httpx.Request("POST", "https://example.com")
            ),
            body=None,
        )

    with pytest.raises(TranslationServiceError) as error:
        guard.call(bad_key)
    assert error.value.code == "authentication"
    assert "private-key" not in str(error.value)
    assert len(calls) == 1


def test_swallowed_paragraph_error_cannot_be_published(monkeypatch):
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    from babeldoc.format.pdf.document_il.midend.styles_and_formulas import (
        StylesAndFormulas,
    )

    # 上游段落线程会吞掉 API 异常；observer 必须在捕获译文前再检查。
    monkeypatch.setattr(StylesAndFormulas, "process", lambda *_: None)
    monkeypatch.setattr(ILTranslator, "translate", lambda *_: None)
    document = SimpleNamespace(page=[SimpleNamespace(page_number=0, pdf_paragraph=[])])
    collector = CompactIRCollector(
        source_sha256="hash",
        source_filename="paper.pdf",
        source_language="en",
        target_language="zh",
        engine={},
    )
    guard = TranslationGuard(lambda _: None)
    guard.failure = TranslationServiceError("rate_limited", "模型服务繁忙")
    with observe_babeldoc(SimpleNamespace(), collector, check_translation=guard.check):
        StylesAndFormulas.process(None, document)
        with pytest.raises(TranslationServiceError):
            ILTranslator.translate(None, document)
    with pytest.raises(CompactIRCaptureError):
        collector.to_dict(page_numbers={1})


def test_attach_replaces_only_instance_retries():
    from babeldoc.translator.translator import OpenAITranslator

    translator = OpenAITranslator(
        "en", "zh", "test-model", api_key="test-only", base_url="https://example.com/v1"
    )
    original_method = OpenAITranslator.do_translate
    guard = TranslationGuard(lambda _: None)
    guard.attach(translator)
    assert translator.client.max_retries == 0
    assert translator.client.timeout == 60
    assert OpenAITranslator.do_translate is original_method
    translator.client.close()
