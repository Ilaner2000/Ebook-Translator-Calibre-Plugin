"""V2 output quality checks. This restores bytes, not translation semantics."""
import re

from .only_mode import InlineAlignmentError, TOKEN


class TranslationQualityError(InlineAlignmentError):
    pass


BYTE_RUN = re.compile(r'(?:<0x[0-9a-fA-F]{2}>)+')
BYTE_LIKE = re.compile(r'<0x', re.IGNORECASE)
RESERVE = re.compile(r'\{\{id_\d+\}\}')
REPEATED = re.compile(r'(.{1,32}?)\1{15,}', re.DOTALL)


def _readable(text):
    return RESERVE.sub('', TOKEN.sub('', text))


def _longest_repeat(text):
    return max((len(m.group()) for m in REPEATED.finditer(text)), default=0)


def normalize_translation(source, target):
    """Strictly decode maximal byte runs and reject dominant runaway repeats.

    UTF8's strict decoder handles consecutive 2/3/4-byte codepoints in one run;
    it rejects partial bytes, surrogates, overlong encodings and invalid scalars.
    Normal text, semantic markers, reserve identifiers and source are untouched.
    """
    if not isinstance(target, str) or not target.strip():
        raise TranslationQualityError('empty/non-text translation')

    def decode(match):
        data = bytes(int(value, 16) for value in re.findall(r'<0x([0-9a-fA-F]{2})>', match.group()))
        try:
            return data.decode('utf-8', 'strict')
        except UnicodeDecodeError as error:
            raise TranslationQualityError('invalid or incomplete UTF8 byte-token sequence') from error

    normalized = BYTE_RUN.sub(decode, target)
    if BYTE_LIKE.search(normalized):
        raise TranslationQualityError('unrestored/malformed byte-token sequence')
    if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', normalized):
        raise TranslationQualityError('XML 1.0 forbidden character in translation')
    readable = _readable(normalized)
    repeated = _longest_repeat(readable)
    # Require >=16 adjacent repetitions, >=64 chars and >=half the target.
    # Preserve literal repetition already present in the source (math/tables).
    if (repeated >= 64 and repeated >= len(readable) * .5
            and repeated > _longest_repeat(_readable(source)) * 1.25):
        raise TranslationQualityError('runaway repeated translation')
    return normalized
