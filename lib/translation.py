import re
import time
import json
import hashlib
from collections import Counter
from types import GeneratorType

from calibre.utils.localization import _  # type: ignore

from ..engines import builtin_engines
from ..engines import GoogleFreeTranslateNew
from ..engines.base import Base
from ..engines.custom import CustomTranslate

from .utils import log, sep, trim, dummy, traceback_error
from .config import get_config
from .exception import (
    TranslationFailed, TranslationCanceled, ParagraphProtocolFailed)
from .handler import Handler
from .only_mode import InlineAlignmentError, TOKEN, validate_inline_translation
from .translation_quality import normalize_translation, TranslationQualityError


load_translations()  # type: ignore


class Glossary:
    def __init__(self, placeholder):
        self.placeholder = placeholder
        self.glossary = []

    def load_from_file(self, path):
        content = None
        try:
            with open(path, 'r', newline=None) as f:
                content = f.read().strip()
        except Exception:
            pass
        if not content:
            return
        groups = re.split(r'\n{2,}', content.strip(u'\ufeff'))
        for group in filter(trim, groups):
            group = group.split('\n')
            self.glossary.append(
                (group[0], group[0] if len(group) < 2 else group[1]))

    def replace(self, content):
        for wid, words in enumerate(self.glossary):
            replacement = self.placeholder[0].format(format(wid, '06'))
            content = content.replace(words[0], replacement)
        return content

    def restore(self, content):
        for wid, words in enumerate(self.glossary):
            pattern = self.placeholder[1].format(format(wid, '06'))
            # Eliminate the impact of backslashes on substitution.
            content = re.sub(pattern, lambda _: words[1], content)
        return content


class ProgressBar:
    total = 0
    length = 0.0
    step = 0

    _count = 0

    def load(self, total):
        self.total = total
        self.step = 1.0 / total

    @property
    def count(self):
        self._count += 1
        self.length += self.step
        return self._count


class InlineResult(str):
    def __new__(cls, value, degraded=False, reason=None):
        instance = str.__new__(cls, value)
        instance.degraded = degraded
        instance.degrade_reason = reason
        return instance


class Translation:
    # Total V2 provider budget, including the one available legacy fallback.
    # An engine's larger request_attempt must never expand this bound.
    inline_request_limit = 3
    def __init__(self, translator, glossary):
        self.translator = translator
        self.glossary = glossary

        self.fresh = False
        self.batch = False
        self.progress = dummy
        self.log = dummy
        self.streaming = dummy
        self.callback = dummy
        self.cancel_request = dummy

        self.total = 0
        self.progress_bar = ProgressBar()
        self.abort_count = 0
        self.strict_inline = False
        self.quality_gate = False
        self.keep_unrecoverable_source = False
        # Per-job only: never turn source text into a successful cached target.
        self.retained_sources = {}

    def quality_enabled(self):
        return self.strict_inline or self.quality_gate

    def retained_source(self, paragraph):
        if self.keep_unrecoverable_source is not True:
            return False
        record = self.retained_sources.get(paragraph.id)
        if record is None and not self.fresh:
            from .cache import source_retention_matches
            cached = getattr(paragraph, 'source_retention', None)
            if source_retention_matches(paragraph, cached):
                record = {
                    'id': paragraph.id, 'page': paragraph.page,
                    'source_md5': paragraph.md5, 'original': paragraph.original,
                    'source_preview': paragraph.original[:60], 'reason': cached['reason'],
                    'raw_sha256': cached['raw_sha256'],
                }
                self.retained_sources[paragraph.id] = record
                paragraph.retained_from_cache = True
        return (record is not None and record['source_md5'] == paragraph.md5
                and record['original'] == paragraph.original
                and record['page'] == paragraph.page
                and (record.get('raw_sha256') is None or record['raw_sha256'] ==
                     hashlib.sha256(paragraph.raw.encode('utf-8')).hexdigest())
                and paragraph.translation is None)

    def retained_source_manifest(self):
        return [{k:row[k] for k in (
            'id', 'page', 'source_preview', 'reason')}
            for _key,row in sorted(self.retained_sources.items())]

    def set_fresh(self, fresh):
        self.fresh = fresh

    def set_batch(self, batch):
        self.batch = batch

    def set_progress(self, progress):
        self.progress = progress

    def set_logging(self, log):
        self.log = log

    def set_streaming(self, streaming):
        self.streaming = streaming

    def set_callback(self, callback):
        self.callback = callback

    def set_cancel_request(self, cancel_request):
        self.cancel_request = cancel_request

    def need_stop(self):
        # Cancel the request if there are more than max continuous errors.
        return self.translator.max_error_count > 0 and \
            self.abort_count >= self.translator.max_error_count

    def translate_text(self, row, text, retry=0, interval=0):
        """Translation engine service error code documentation:
        * https://cloud.google.com/apis/design/errors
        * https://www.deepl.com/docs-api/api-access/error-handling/
        * https://platform.openai.com/docs/guides/error-codes/api-errors
        * https://ai.youdao.com/DOCSIRMA/html/trans/api/wbfy/index.html
        * https://api.fanyi.baidu.com/doc/21
        """
        if self.quality_enabled():
            return self._translate_inline_text(row, text)
        if self.cancel_request():
            raise TranslationCanceled(_('Translation canceled.'))
        try:
            translation = self.translator.translate(text)
            if self.strict_inline:
                if isinstance(translation, GeneratorType):
                    translation = ''.join(translation)
                validate_inline_translation(text, translation, self.translator.placeholder)
            self.abort_count = 0
            return translation
        except Exception as e:
            if self.cancel_request() or self.need_stop():
                raise TranslationCanceled(_('Translation canceled.'))
            self.abort_count += 1
            message = _('Failed to retrieve data from translate engine API.')
            if retry >= self.translator.request_attempt:
                raise TranslationFailed('{}\n{}'.format(message, str(e)))
            retry += 1
            interval += 5
            # Logging any errors that occur during translation.
            logged_text = text[:200] + '...' if len(text) > 200 else text
            error_messages = [
                sep(), _('Original: {}').format(logged_text), sep('┈'),
                _('Status: Failed {} times / Sleeping for {} seconds')
                .format(retry, interval), sep('┈'), _('Error: {}')
                .format(traceback_error())]
            if row >= 0:
                error_messages.insert(1, _('Row: {}').format(row))
            self.log('\n'.join(error_messages), True)
            if self.translator.match_error(str(e)):
                raise TranslationCanceled(_('Translation canceled.'))
            time.sleep(interval)
            return self.translate_text(row, text, retry, interval)

    def _translate_inline_text(self, row, text):
        """At most three provider attempts, including one legacy fallback.

        The engine/config/cache identity is unchanged. No shared translator
        flags are toggled during concurrent requests.
        """
        last_error = None
        reserve = re.compile(self.translator.placeholder[1].format(r'\d+')) \
            if self.translator.placeholder else None
        has_reserves = bool(reserve and reserve.search(text))
        can_degrade = bool(TOKEN.search(text) or has_reserves)
        strict_limit = min(
            self.inline_request_limit - int(can_degrade),
            max(1, int(self.translator.request_attempt) + 1))
        placeholder_failed = False
        protocol_seen = False
        quality_seen = False
        requests = 0
        for attempt in range(strict_limit):
            if self.cancel_request() or (self.abort_count > 0 and self.need_stop()):
                raise TranslationCanceled(_('Translation canceled.'))
            try:
                requests += 1
                target = self.translator.translate(text)
                if isinstance(target, GeneratorType):
                    target = ''.join(target)
                target = normalize_translation(text, target)
                if has_reserves:
                    placeholder_failed = placeholder_failed or (
                        Counter(m.group() for m in reserve.finditer(text)) !=
                        Counter(m.group() for m in reserve.finditer(target)))
                validate_inline_translation(text, target, self.translator.placeholder)
                self.abort_count = 0
                return InlineResult(target)
            except TranslationCanceled:
                raise
            except Exception as err:
                last_error = err
                quality_seen = quality_seen or isinstance(err, TranslationQualityError)
                protocol_seen = protocol_seen or (
                    isinstance(err, InlineAlignmentError)
                    and not isinstance(err, TranslationQualityError))
                if self.cancel_request() or self.translator.match_error(str(err)):
                    raise TranslationCanceled(_('Translation canceled.'))
                if attempt + 1 >= strict_limit:
                    break
                delay = (attempt + 1) * 5
                self.log('Row: %s\nOriginal: %s\nStatus: Failed %s times / Sleeping for %s seconds\nError: %s' %
                         (row, text[:200], attempt + 1, delay, str(err)), True)
                time.sleep(delay)
        reason = ('placeholder_exhausted' if has_reserves
                  else 'inline_protocol_exhausted')
        if can_degrade:
            if self.cancel_request() or (self.abort_count > 0 and self.need_stop()):
                raise TranslationCanceled(_('Translation canceled.'))
            try:
                plain = trim(TOKEN.sub('', text))
                requests += 1
                target = self.translator.translate(plain)
                if isinstance(target, GeneratorType):
                    target = ''.join(target)
                target = normalize_translation(text, target)
                validate_inline_translation(text, target, self.translator.placeholder, degraded=True)
                self.abort_count = 0
                # Without ET formatting the third attempt is an ordinary retry:
                # stripping ET changed no input, so do not inflate degradation.
                degraded = bool(TOKEN.search(text))
                return InlineResult(
                    target, degraded=degraded,
                    reason=reason if degraded else None)
            except TranslationCanceled:
                raise
            except Exception as err:
                last_error = err
                quality_seen = quality_seen or isinstance(err, TranslationQualityError)
                protocol_seen = protocol_seen or (
                    isinstance(err, InlineAlignmentError)
                    and not isinstance(err, TranslationQualityError))
                if self.cancel_request() or self.translator.match_error(str(err)):
                    raise TranslationCanceled(_('Translation canceled.'))
        # Validation failure is terminal for this ROW, not a provider outage.
        # Never let ten malformed rows cancel the remaining book queue.
        last_is_protocol = (isinstance(last_error, InlineAlignmentError)
                            and not isinstance(last_error, TranslationQualityError))
        # A final timeout must not erase the earlier proven inline failure.
        # Keep-source opt-in alone never permits pure outages or quality errors.
        mixed_protocol_failure = (self.keep_unrecoverable_source is True
                                  and protocol_seen and not quality_seen
                                  and not isinstance(last_error, InlineAlignmentError))
        protocol_failed = last_is_protocol or mixed_protocol_failure
        if protocol_failed:
            reason = ('placeholder_unrecoverable' if has_reserves
                      else 'inline_protocol_exhausted')
        else:
            self.abort_count += 1
        self.log('V2 terminal failure: ' + json.dumps({
            'row': row, 'reason': reason, 'requests': requests,
            'request_limit': self.inline_request_limit,
            'reserve_loss_seen': placeholder_failed,
            'protocol_seen': protocol_seen, 'quality_seen': quality_seen,
            'last_error_type': type(last_error).__name__}), True)
        message = (
            'V2 paragraph exhausted (%s; %s/%s requests); '
            'available legacy fallback failed: %s' % (
                reason, requests, self.inline_request_limit, last_error))
        error = (ParagraphProtocolFailed(message, reason) if protocol_failed
                 else TranslationFailed(message))
        error.failure_context = {
            'reason': reason, 'requests': requests, 'request_limit': self.inline_request_limit,
            'protocol_seen': protocol_seen, 'quality_seen': quality_seen,
            'last_error_type': type(last_error).__name__,
        }
        raise error

    def translate_paragraph(self, paragraph):
        if self.cancel_request():
            raise TranslationCanceled(_('Translation canceled.'))
        paragraph.failure_reason = None
        paragraph.keep_source = False
        paragraph.retained_from_cache = False
        if self.retained_source(paragraph):
            paragraph.keep_source = True
            paragraph.failure_reason = self.retained_sources[paragraph.id]['reason']
            paragraph.is_cache = True
            return
        if self.keep_unrecoverable_source is True:
            self.retained_sources.pop(paragraph.id, None)
        if paragraph.translation and not self.fresh:
            try:
                if self.quality_enabled():
                    normalized = normalize_translation(paragraph.original, paragraph.translation)
                    validate_inline_translation(paragraph.original, normalized, self.translator.placeholder,
                                                degraded=getattr(paragraph, 'degraded', False) is True)
                    old_view = getattr(paragraph, '_normalized_cache_view', None)
                    same_view = old_view == (getattr(paragraph, 'md5', None), paragraph.original, paragraph.translation)
                    paragraph.memory_normalized = normalized != paragraph.translation or same_view
                    paragraph.translation = normalized
                    paragraph._normalized_cache_view = (getattr(paragraph, 'md5', None), paragraph.original, normalized) if paragraph.memory_normalized else None
                paragraph.is_cache = True
                if self.quality_enabled():
                    self.abort_count = 0
                return
            except InlineAlignmentError:
                # Repair through the official provider/cache path, never use a
                # corrupt cached response just because its MD5 matched.
                paragraph.translation = None
        paragraph.memory_normalized = False
        paragraph._normalized_cache_view = None
        self.streaming('')
        self.streaming(_('Translating...'))
        text = self.glossary.replace(paragraph.original)
        try:
            translation = self.translate_text(paragraph.row, text)
        except ParagraphProtocolFailed as error:
            paragraph.failure_context = getattr(error, 'failure_context', {})
            paragraph.failure_reason = error.reason
            paragraph.translation = None
            if self.keep_unrecoverable_source is True:
                paragraph.keep_source = True
                self.retained_sources[paragraph.id] = {
                    'id': paragraph.id, 'page': paragraph.page,
                    'source_md5': paragraph.md5,
                    'original': paragraph.original,
                    'source_preview': paragraph.original[:60],
                    'reason': error.reason,
                }
            self.log('V2 retained failure row: ' + json.dumps({
                'id': paragraph.id, 'page': paragraph.page, 'source_md5': paragraph.md5,
                'reason': error.reason, 'retained': paragraph.keep_source,
                'failure': paragraph.failure_context}, ensure_ascii=False), True)
            raise
        degraded = getattr(translation, 'degraded', False) is True
        degrade_reason = getattr(translation, 'degrade_reason', None)
        # Process streaming text
        if isinstance(translation, GeneratorType):
            if self.total == 1:
                # Only for a single translation.
                temp = ''
                clear = True
                for char in translation:
                    if clear:
                        self.streaming('')
                        clear = False
                    self.streaming(char)
                    time.sleep(0.05)
                    temp += char
            else:
                temp = ''.join([char for char in translation])
            translation = temp
        translation = self.glossary.restore(translation)
        if self.quality_enabled():
            translation = normalize_translation(paragraph.original, translation)
            validate_inline_translation(paragraph.original, translation, self.translator.placeholder, degraded=degraded)
            paragraph.degraded = degraded
            paragraph.degrade_reason = (
                degrade_reason or 'inline_protocol_exhausted') if degraded else None
        paragraph.translation = translation.strip()
        # Apply aligment checking and processing.
        if self.translator.merge_enabled:
            paragraph.do_aligment(self.translator.separator)
        paragraph.engine_name = self.translator.name
        paragraph.target_lang = self.translator.get_target_lang()
        paragraph.is_cache = False

    def process_translation(self, paragraph):
        self.progress(
            self.progress_bar.length, _('Translating: {}/{}').format(
                self.progress_bar.count, self.progress_bar.total))

        self.streaming(paragraph)
        # A decoded warm result is a render-only view. Its native target and
        # hash-bound degraded metadata must remain byte/value-identical.
        normalized_view = getattr(paragraph, '_normalized_cache_view', None)
        retained_cache_view = (getattr(paragraph, 'retained_from_cache', False) is True
                               and paragraph.is_cache and self.retained_source(paragraph))
        if not retained_cache_view and not (self.quality_enabled() and paragraph.is_cache
                and getattr(paragraph, 'memory_normalized', False)
                and normalized_view == (getattr(paragraph, 'md5', None), paragraph.original, paragraph.translation)):
            self.callback(paragraph)

        row = paragraph.row
        original = paragraph.original.strip()
        if getattr(paragraph, 'keep_source', False) is True:
            self.log('Source retained (untranslated): ' + json.dumps({
                'id': paragraph.id, 'page': paragraph.page,
                'source_preview': paragraph.original[:60]}, ensure_ascii=False))
            return
        if paragraph.error is None:
            if getattr(paragraph, 'degraded', False) is True:
                self.log('degraded: ' + json.dumps({
                    'row': paragraph.row, 'id': paragraph.id, 'page': paragraph.page,
                    'source_md5': paragraph.md5, 'cached': paragraph.is_cache,
                    'reason': paragraph.degrade_reason}, ensure_ascii=False))
            self.log(sep())
            if row >= 0:
                self.log(_('Row: {}').format(row))
            self.log(_('Original: {}').format(original))
            self.log(sep('┈'))
            message = _('Translation: {}')
            if paragraph.is_cache:
                message = _('Translation (Cached): {}')
            self.log(message.format(paragraph.translation.strip()))

    def handle(self, paragraphs=[]):
        start_time = time.time()
        char_count = 0
        for paragraph in paragraphs:
            self.total += 1
            char_count += len(paragraph.original)

        self.log(sep())
        self.log(_('Start to translate ebook content'))
        self.log(sep('┈'))
        self.log(_('Item count: {}').format(self.total))
        self.log(_('Character count: {}').format(char_count))

        if self.total < 1:
            raise Exception(_('There is no content need to translate.'))
        self.progress_bar.load(self.total)

        handler = Handler(
            paragraphs, self.translator.concurrency_limit,
            self.translate_paragraph, self.process_translation,
            self.translator.request_interval)
        handler.handle()

        self.log(sep())
        if self.batch and self.need_stop():
            raise Exception(_('Translation failed.'))
        consuming = round((time.time() - start_time) / 60, 2)
        self.log(_('Time consuming: {} minutes').format(consuming))
        if self.strict_inline:
            degraded_rows = [{'id': p.id, 'page': p.page, 'source_md5': p.md5}
                             for p in paragraphs if getattr(p, 'degraded', False) is True and p.error is None]
            self.log('degraded summary: ' + json.dumps({'count': len(degraded_rows), 'paragraphs': degraded_rows}, ensure_ascii=False))
        unrecoverable = [{
            'id': p.id, 'page': p.page,
            'reason': getattr(p, 'failure_reason', None),
            'source_preview': p.original[:60],
        } for p in paragraphs if getattr(p, 'failure_reason', None)]
        if unrecoverable:
            self.log('unrecoverable summary: ' + json.dumps({
                'count': len(unrecoverable), 'paragraphs': unrecoverable},
                ensure_ascii=False), True)
        retained = self.retained_source_manifest()
        if retained:
            self.log('source retained summary: ' + json.dumps({
                'count': len(retained), 'paragraphs': retained},
                ensure_ascii=False))
        if self.strict_inline and hasattr(self.translator, 'get_session_usage'):
            self.log('Provider token usage: ' + json.dumps(self.translator.get_session_usage(), sort_keys=True))
        blocked = any(getattr(p, 'failure_reason', None)
                      and getattr(p, 'keep_source', False) is not True
                      for p in paragraphs)
        message = (_('Translation finished with unrecoverable paragraphs; '
                     'output remains blocked.') if blocked else
                   (_('Translation completed with source paragraphs retained.')
                    if retained else _('Translation completed.')))
        self.log(message)
        self.progress(1, message)


def get_engine_class(engine_name=None):
    config = get_config()
    engine_name = engine_name or config.get('translate_engine')
    engines: dict[str, type[Base]] = {
        engine.name: engine for engine in builtin_engines
        if engine.name is not None}
    custom_engines = config.get('custom_engines') or {}
    if engine_name in engines:
        engine_class = engines[engine_name]
    elif engine_name in custom_engines:
        engine_class = CustomTranslate
        engine_data = json.loads(custom_engines[engine_name])
        engine_class.set_engine_data(engine_data)
    else:
        engine_class = GoogleFreeTranslateNew
    engine_preferences = config.get('engine_preferences') or {}
    engine_class.set_config(engine_preferences.get(engine_class.name) or {})
    return engine_class


def get_translator(engine_class=None):
    config = get_config()
    engine_class = engine_class or get_engine_class()
    translator = engine_class()
    translator.set_search_paths(config.get('search_paths'))
    if config.get('proxy_enabled'):
        proxy_type: str | None = config.get('proxy_type')
        proxy_setting: dict[str, list] | None = config.get('proxy_setting')
        if proxy_type is not None and proxy_setting is not None:
            # Compatible with old proxy settings stored as a list.
            if isinstance(proxy_setting, list):
                proxy_setting = {'http': proxy_setting}
            host, port = proxy_setting.get(proxy_type) or ['', '']
            translator.set_proxy(proxy_type, host, port)
    translator.set_merge_enabled(config.get('merge_enabled'))
    return translator


def get_translation(translator, log=None):
    config = get_config()
    glossary = Glossary(translator.placeholder)
    if config.get('glossary_enabled'):
        glossary.load_from_file(config.get('glossary_path'))
    translation = Translation(translator, glossary)
    translation.keep_unrecoverable_source = config.get(
        'v2_unrecoverable_keep_source', False) is True
    if get_config().get('log_translation'):
        translation.set_logging(log)
    return translation
