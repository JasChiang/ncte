"""Error taxonomy (spec §12): program failure / unverifiable model output /
content uncertainty / unknown billing state are kept distinct."""


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


class BudgetExceededError(NCTEError):
    """Spending the next request would exceed the configured budget."""
