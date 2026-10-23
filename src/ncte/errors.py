"""Error taxonomy (spec §12): program failure / unverifiable model output /
content uncertainty / unknown billing state are kept distinct."""

from typing import Any


class NCTEError(Exception):
    """Base error for user-facing failures."""


class EpubSecurityError(NCTEError):
    """The EPUB archive failed a safety check (zip slip, zip bomb, DRM …)."""


class UnsupportedEpubError(NCTEError):
    """The EPUB uses a feature NCTE refuses to process (fixed layout, encryption …)."""


class EpubStructureError(NCTEError):
    """The EPUB package is malformed (missing container, OPF, spine items …)."""


class ConfigError(NCTEError):
    pass


class LLMError(NCTEError):
    """Base class for LLM adapter errors."""


class FatalAPIError(LLMError):
    """Authorisation, balance or model-availability problem — the run must stop (401/402/403/404)."""


class RetryableAPIError(LLMError):
    """429, 5xx, timeouts — may be retried with backoff."""


class RequestRejectedError(LLMError):
    """400/413/422 — request was rejected; retrying the same payload will not help."""


class ContentBlockedError(RequestRejectedError):
    """The provider's content filter refused this request (e.g. Gemini PROHIBITED_CONTENT).

    Specific to the request content, not to the account: the run continues, the batch is split to
    isolate the refused paragraphs, which keep the original text and are reported."""


class ModelOutputError(LLMError):
    """The model replied but the reply failed JSON/Schema/alignment validation."""


class EmptyAnswerError(ModelOutputError):
    """A CLI source's turn ended with no answer at all (e.g. the agent tried a tool it may not use and gave up).

    Nothing in the answer to correct: the same request is simply asked again, a few times (``context_pipeline``).
    The turn still ran: ``usage`` (tokens, the plan's windows, as the CLI reported them) and ``latency_ms`` are what it
    took, kept on the failed request's row so the plan and cost reports count it (ADR-102)."""

    def __init__(self, message: str = "empty answer", *, usage: dict[str, Any] | None = None,
                 latency_ms: int | None = None) -> None:
        super().__init__(message)
        self.usage = usage
        self.latency_ms = latency_ms


class BudgetExceededError(NCTEError):
    """Spending the next request would exceed the configured budget."""
