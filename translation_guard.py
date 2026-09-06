"""有限重试和失败闭锁，避免上游吞掉 API 异常后发布未译完整的页面。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import openai
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class TranslationServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def service_error(exc: Exception) -> TranslationServiceError:
    if isinstance(exc, openai.RateLimitError):
        return TranslationServiceError(
            "rate_limited",
            "模型服务繁忙，重试后仍无法继续。已完成的页面已保留，请稍后重试或换一个模型。",
        )
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return TranslationServiceError(
            "authentication", "模型访问失败，请检查 API 密钥和使用权限。"
        )
    if isinstance(
        exc,
        (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError),
    ):
        return TranslationServiceError(
            "service_unavailable",
            "模型服务暂时无法连接。已完成的页面已保留，请稍后重试。",
        )
    return TranslationServiceError(
        "translation_failed",
        "模型未能完成翻译。已完成的页面已保留，请检查模型配置或更换模型。",
    )


class TranslationGuard:
    def __init__(
        self,
        on_wait: Callable[[str | None], None],
        *,
        attempts: int = 4,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.on_wait = on_wait
        self.attempts = attempts
        self.sleep = sleep
        self.cancel: Callable[[], None] = lambda: None
        self.failure: TranslationServiceError | None = None
        self._retrying: dict[int, str] = {}
        self._last_waiting: str | None = None
        self._lock = threading.Lock()

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure

    def _waiting(self, reason: str | None) -> None:
        with self._lock:
            thread = threading.get_ident()
            if reason:
                self._retrying[thread] = reason
            else:
                self._retrying.pop(thread, None)
            waiting = next(iter(self._retrying.values()), None)
            if waiting != self._last_waiting:
                self._last_waiting = waiting
                self.on_wait(waiting)

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        def before_sleep(state: Any) -> None:
            exc = state.outcome.exception()
            self._waiting(
                "rate_limited"
                if isinstance(exc, openai.RateLimitError)
                else "service_unavailable"
            )

        def attempt() -> Any:
            self.check()
            return fn(*args, **kwargs)

        retry = Retrying(
            stop=stop_after_attempt(self.attempts),
            wait=wait_exponential(multiplier=2, min=2, max=15),
            retry=retry_if_exception_type(
                (
                    openai.RateLimitError,
                    openai.APIConnectionError,
                    openai.APITimeoutError,
                    openai.InternalServerError,
                )
            ),
            before_sleep=before_sleep,
            reraise=True,
            **({"sleep": self.sleep} if self.sleep else {}),
        )
        try:
            result = retry(attempt)
            self.check()
            return result
        except TranslationServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 — 上游吞异常，必须记录并阻止发布不完整译文
            with self._lock:
                if self.failure is None:
                    self.failure = service_error(exc)
            self.cancel()
            raise self.failure from None
        finally:
            self._waiting(None)

    def attach(self, translator: Any) -> None:
        translator.client = translator.client.with_options(timeout=60, max_retries=0)
        for name in ("do_translate", "do_llm_translate"):
            # 固定版上游在此重试 100 次；只替换当前实例，不全局修改上游方法。
            raw = getattr(type(translator), name).__wrapped__
            setattr(
                translator,
                name,
                lambda *a, _raw=raw, **kw: self.call(_raw, translator, *a, **kw),
            )
