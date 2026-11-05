class UnexpectedResult(Exception):
    pass


class ConversionFailed(Exception):
    pass


class ConversionAbort(Exception):
    pass


class TranslationFailed(Exception):
    pass


class ParagraphProtocolFailed(TranslationFailed):
    """Terminal row validation failure; other rows may still be translated."""
    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


class TranslationCanceled(Exception):
    pass


class BadApiKeyFormat(TranslationCanceled):
    pass


class NoAvailableApiKey(TranslationCanceled):
    pass


class UnsupportedModel(Exception):
    pass
