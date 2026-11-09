class UnexpectedResult(Exception):
    pass


class ConversionFailed(Exception):
    pass


class ConversionAbort(Exception):
    pass


class TranslationFailed(Exception):
    pass


class ParagraphTerminalFailed(TranslationFailed):
    """Typed, bounded row failure eligible for explicit source-only fallback."""
    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


class ParagraphProtocolFailed(ParagraphTerminalFailed):
    """Terminal inline/reserve validation failure."""
    pass


class TranslationCanceled(Exception):
    pass


class BadApiKeyFormat(TranslationCanceled):
    pass


class NoAvailableApiKey(TranslationCanceled):
    pass


class UnsupportedModel(Exception):
    pass
