import os
import os.path
import json
import shutil
import hashlib
import traceback
from contextlib import contextmanager
from types import MethodType
from typing import Callable
from tempfile import gettempdir
from datetime import datetime, timezone
from uuid import uuid4

from calibre import sanitize_file_name  # type: ignore
from calibre.gui2 import (  # type: ignore
    Dispatcher, error_dialog, question_dialog)
from calibre.constants import DEBUG, __version__  # type: ignore
from calibre.db.constants import DATA_DIR_NAME  # type: ignore
from calibre.utils.localization import _  # type: ignore
from calibre.utils.logging import Stream  # type: ignore
from calibre.ebooks.conversion.plumber import (  # type: ignore
    Plumber, CompositeProgressReporter)
from calibre.ptempfile import PersistentTemporaryFile  # type: ignore
from calibre.ebooks.metadata.meta import (  # type: ignore
    get_metadata, set_metadata)

from .. import EbookTranslator

from .config import get_config
from .utils import log, sep, uid, open_path, open_file
from .cache import get_cache
from .element import (
    get_element_handler, get_srt_elements, get_toc_elements, get_page_elements,
    get_metadata_elements, get_pgn_elements)
from .translation import get_translator, get_translation
from .exception import ConversionAbort


load_translations()  # type: ignore


variant_storage_values = (
    'bundle_data', 'linked_pair', 'three_records')


_missing_instance_method = object()


@contextmanager
def temporary_method_binding(instance, name, method):
    """Temporarily override an instance method without leaking its state."""
    instance_attributes = vars(instance)
    previous = instance_attributes.get(name, _missing_instance_method)
    setattr(instance, name, MethodType(method, instance))
    try:
        yield
    finally:
        if previous is _missing_instance_method:
            instance_attributes.pop(name, None)
        else:
            instance_attributes[name] = previous


def temporary_path(suffix):
    return PersistentTemporaryFile(suffix=suffix).name


def temporary_copy(path, suffix):
    temp = PersistentTemporaryFile(suffix=suffix)
    with open(path, 'rb') as source:
        shutil.copyfileobj(source, temp)
    temp.close()
    return temp.name


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class PrepareStream:
    mode = 'r'

    def __init__(self, callback):
        self.callback = callback
        self.temp = ''

    def write(self, text):
        self.temp += text
        if text == '\n':
            self.callback(self.temp.strip('\n'))
            self.temp = ''

    def flush(self):
        pass


def convert_book(
        input_path, output_path, translation, element_handler, cache,
        debug_info, encoding, notification) -> None:
    """Process ebooks that Calibre supported."""
    policy = getattr(element_handler, 'only_policy', None)
    translation.strict_inline = bool(element_handler.position == 'only' and policy and policy.preserve_inline)
    translation.quality_gate = bool(translation.strict_inline or getattr(element_handler, 'inline_cache_to_plain', False))
    translation.translator.inline_protocol = translation.strict_inline
    plumber = Plumber(
        input_path, output_path, log=log, report_progress=notification)
    _convert = plumber.output_plugin.convert
    elements = []

    def convert(self, oeb, output_path, input_plugin, opts, log):
        backup_progress = self.report_progress.global_min
        self.report_progress = CompositeProgressReporter(0, 1, notification)
        log.info('Translating ebook content... (this will take a while)')
        log.info(debug_info)
        translation.set_progress(self.report_progress)

        elements.extend(get_metadata_elements(oeb.metadata))
        # The number of elements may vary with format conversion.
        elements.extend(get_toc_elements(oeb.toc.nodes, []))
        elements.extend(get_page_elements(oeb.manifest.items))
        original_group = element_handler.prepare_original(elements)
        cache.save(original_group)

        paragraphs = cache.all_paragraphs()
        translation.handle(paragraphs)
        element_handler.add_translations(paragraphs)

        log.info(sep())
        log.info(_('Start to convert ebook format...'))
        log.info(sep())

        self.report_progress = CompositeProgressReporter(
            backup_progress, 1, notification)
        self.report_progress(0., _('Outputting ebook file...'))
        _convert(oeb, output_path, input_plugin, opts, log)

    with temporary_method_binding(
            plumber.output_plugin, 'convert', convert):
        plumber.run()


def convert_srt(
        input_path, output_path, translation, element_handler, cache,
        debug_info, encoding, notification) -> None:
    log.info('Translating subtitles content... (this will take a while)')
    log.info(debug_info)

    elements = get_srt_elements(input_path, encoding)
    original_group = element_handler.prepare_original(elements)
    cache.save(original_group)

    paragraphs = cache.all_paragraphs()
    translation.set_progress(notification)
    translation.handle(paragraphs)
    element_handler.add_translations(paragraphs)

    log.info(sep())
    log.info(_('Starting to output subtitles file...'))
    log.info(sep())

    with open(output_path, 'w') as file:
        file.write('\n\n'.join([e.get_translation() for e in elements]))

    log.info(_('The translation of the subtitles file was completed.'))


def convert_pgn(
        input_path, output_path, translation, element_handler, cache,
        debug_info, encoding, notification) -> None:
    log.info('Translating PGN content... (this may be take a while)')
    log.info(debug_info)

    elements = get_pgn_elements(input_path, encoding)
    original_group = element_handler.prepare_original(elements)
    cache.save(original_group)

    paragraphs = cache.all_paragraphs()
    translation.set_progress(notification)
    translation.handle(paragraphs)
    element_handler.add_translations(paragraphs)

    log.info(sep())
    log.info(_('Starting to output PGN file...'))
    log.info(sep())

    pgn_content = open_file(input_path, encoding)
    for element in elements:
        pgn_content = pgn_content.replace(
            element.get_raw(), element.get_translation(), 1)
    with open(output_path, 'w', encoding='utf-8') as file:
        file.write(pgn_content)

    log.info(_('The translation of the PGN file was completed.'))


extra_formats: dict[str, dict[str, Callable]] = {
    'srt': {
        'extractor': get_srt_elements,
        'convertor': convert_srt,
    },
    'pgn': {
        'extractor': get_pgn_elements,
        'convertor': convert_pgn,
    }
}


def extract_item(input_path, input_format, encoding, callback=None):
    if callback is not None:
        log.outputs = [Stream(PrepareStream(callback))]
    handler = extra_formats.get(input_format)
    extractor = extract_book if handler is None else handler['extractor']
    return extractor(input_path, encoding)


def extract_book(input_path, encoding):
    elements = []
    output_path = os.path.join(gettempdir(), 'temp.epub')
    plumber = Plumber(input_path, output_path, log=log)

    def convert(self, oeb, output_path, input_plugin, opts, log):
        # for item in oeb.manifest.items:
        #     if item.media_type == 'text/css':
        #         for rule in item.data.cssRules:
        #             print('='*20)
        #             # CSSStyleRule or CSSPageRule
        #             print(type(rule))
        #             # CSSStyleDeclaration
        #             print(rule.style.keys())
        elements.extend(get_metadata_elements(oeb.metadata))
        elements.extend(get_toc_elements(oeb.toc.nodes, []))
        elements.extend(get_page_elements(oeb.manifest.items))
        raise ConversionAbort()
    try:
        with temporary_method_binding(
                plumber.output_plugin, 'convert', convert):
            plumber.run()
    except ConversionAbort:
        return elements


def convert_item(
        ebook_title, input_path, output_path, source_lang, target_lang,
        cache_only, is_batch, format, encoding, direction, notification):
    """The following parameters need attention:
    :cache_only: Only use the translation which exists in the cache.
    :notification: It is automatically added by arbitrary_n.
    """
    translator = get_translator()
    translator.set_source_lang(source_lang)
    translator.set_target_lang(target_lang)

    element_handler = get_element_handler(
        translator.placeholder, translator.separator, direction)
    element_handler.set_translation_lang(
        translator.get_iso639_target_code(target_lang))

    merge_length = str(element_handler.get_merge_length())
    _encoding = ''
    if encoding.lower() != 'utf-8':
        _encoding = encoding.lower()
    cache_id = translation_cache_identity(
        input_path, translator.name, target_lang, merge_length, _encoding, element_handler)
    cache = get_cache(cache_id)
    cache.set_cache_only(cache_only)
    cache.set_info('title', ebook_title)
    cache.set_info('engine_name', translator.name)
    cache.set_info('target_lang', target_lang)
    cache.set_info('merge_length', merge_length)
    cache.set_info('plugin_version', EbookTranslator.__version__)
    cache.set_info('calibre_version', __version__)

    translation = get_translation(
        translator, lambda text, error=False: log.info(text))
    translation.set_batch(is_batch)
    translation.set_callback(cache.update_paragraph)

    debug_info = '{0}\n| Diagnosis Information\n{0}'.format(sep())
    debug_info += '\n| Calibre Version: %s\n' % __version__
    debug_info += '| Plugin Version: %s\n' % EbookTranslator.__version__
    debug_info += '| Translation Engine: %s\n' % translator.name
    debug_info += '| Source Language: %s\n' % source_lang
    debug_info += '| Target Language: %s\n' % target_lang
    debug_info += '| Encoding: %s\n' % encoding
    debug_info += '| Cache Enabled: %s\n' % cache.is_persistence()
    debug_info += '| Merging Length: %s\n' % element_handler.merge_length
    debug_info += '| Concurrent requests: %s\n' % translator.concurrency_limit
    debug_info += '| Request Interval: %s\n' % translator.request_interval
    debug_info += '| Request Attempt: %s\n' % translator.request_attempt
    debug_info += '| Request Timeout: %s\n' % translator.request_timeout
    debug_info += '| Input Path: %s\n' % input_path
    debug_info += '| Output Path: %s' % output_path

    handler: dict[str, Callable] | None = extra_formats.get(format)
    convertor = convert_book if handler is None else handler['convertor']
    convertor(
        input_path, output_path, translation, element_handler, cache,
        debug_info, encoding, notification)
    cache.done()


def translation_cache_identity(input_path, engine_name, target_lang, merge_length, encoding, handler):
    # Marker-bearing originals cannot share the legacy plain-text cache table.
    # Keep the native cache implementation and all its persistence/retry logic.
    policy = getattr(handler, 'only_policy', None)
    suffix = '|only-inline-v2' if handler.position == 'only' and policy and policy.preserve_inline else ''
    return uid(input_path + engine_name + target_lang + merge_length + encoding + suffix)


def convert_variants_item(
        ebook_title, input_path, translation_output_path,
        bilingual_output_path, source_lang, target_lang, cache_only,
        is_batch, format, encoding, direction, bilingual_gap, notification):
    """Render translation-only and bilingual EPUBs from one cache context.

    The first render may call the translation provider. The second render is
    forced to cache-only and reuses the same TranslationCache instance.
    """
    translator = get_translator()
    translator.set_source_lang(source_lang)
    translator.set_target_lang(target_lang)

    def create_handler(position, vertical_gap=0.0):
        handler = get_element_handler(
            translator.placeholder, translator.separator, direction)
        handler.position = position
        handler.set_only_policy(get_config().get(
            'only_typography', {'preserve_inline': True, 'auto_roles': True}) if position == 'only' else None)
        handler.set_vertical_gap(vertical_gap)
        handler.set_translation_lang(
            translator.get_iso639_target_code(target_lang))
        return handler

    translation_handler = create_handler('only')
    merge_length = str(translation_handler.get_merge_length())
    _encoding = ''
    if encoding.lower() != 'utf-8':
        _encoding = encoding.lower()
    cache_id = translation_cache_identity(
        input_path, translator.name, target_lang, merge_length, _encoding, translation_handler)
    cache = get_cache(cache_id)
    cache.set_cache_only(cache_only)
    cache.set_info('title', ebook_title)
    cache.set_info('engine_name', translator.name)
    cache.set_info('target_lang', target_lang)
    cache.set_info('merge_length', merge_length)
    cache.set_info('plugin_version', EbookTranslator.__version__)
    cache.set_info('calibre_version', __version__)

    translation = get_translation(
        translator, lambda text, error=False: log.info(text))
    translation.set_batch(is_batch)
    translation.set_callback(cache.update_paragraph)

    def get_debug_info(output_path):
        debug_info = '{0}\n| Diagnosis Information\n{0}'.format(sep())
        debug_info += '\n| Calibre Version: %s\n' % __version__
        debug_info += '| Plugin Version: %s\n' % EbookTranslator.__version__
        debug_info += '| Translation Engine: %s\n' % translator.name
        debug_info += '| Source Language: %s\n' % source_lang
        debug_info += '| Target Language: %s\n' % target_lang
        debug_info += '| Encoding: %s\n' % encoding
        debug_info += '| Cache Enabled: %s\n' % cache.is_persistence()
        debug_info += '| Merging Length: %s\n' % merge_length
        debug_info += '| Input Path: %s\n' % input_path
        debug_info += '| Output Path: %s' % output_path
        return debug_info

    handler: dict[str, Callable] | None = extra_formats.get(format)
    convertor = convert_book if handler is None else handler['convertor']
    convertor(
        input_path, translation_output_path, translation,
        translation_handler, cache, get_debug_info(translation_output_path),
        encoding, notification)

    cache.set_cache_only(True)
    # Translation.handle() accumulates ``total``. Reusing the same object is
    # important for one provider/cache context, but each render needs an
    # independent progress total.
    translation.total = 0
    bilingual_handler = create_handler('below', bilingual_gap)
    bilingual_handler.inline_cache_to_plain = bool(
        translation_handler.only_policy and translation_handler.only_policy.preserve_inline)
    convertor(
        input_path, bilingual_output_path, translation, bilingual_handler,
        cache, get_debug_info(bilingual_output_path), encoding, notification)
    cache.done()


class ConversionWorker:
    def __init__(self, gui, icon):
        self.gui = gui
        self.icon = icon
        self.config = get_config()
        self.db = gui.current_db
        self.api = self.db.new_api
        self.working_jobs = self.gui.bookfere_ebook_translator.jobs

    def translate_ebook(self, ebook, cache_only=False, is_batch=False):
        input_path = ebook.get_input_path()
        if self.config.get('translation_variants_enabled', False) and \
                self.config.get('to_library'):
            return self.translate_variants(
                ebook, input_path, cache_only, is_batch)
        if not self.config.get('to_library'):
            filename = sanitize_file_name(ebook.title[:200])
            output_path = self.config.get('output_path')
            if output_path is None or not os.path.isdir(output_path):
                raise Exception(
                    _('Please set a valid output path.'))
            output_path = os.path.join(
                output_path, f'{filename}.{ebook.output_format}')
        else:
            output_path = PersistentTemporaryFile(
                suffix='.' + ebook.output_format).name
        job = self.gui.job_manager.run_job(
            Dispatcher(self.translate_done),
            'arbitrary_n',
            args=(
                'calibre_plugins.ebook_translator.lib.conversion',
                'convert_item',
                (ebook.title, input_path, output_path, ebook.source_lang,
                 ebook.target_lang, cache_only, is_batch, ebook.input_format,
                 ebook.encoding, ebook.target_direction)),
            description=(_('[{} > {}] Translating "{}"').format(
                ebook.source_lang, ebook.target_lang, ebook.title)))
        self.working_jobs[job] = (ebook, output_path)

    def translate_variants(
            self, ebook, input_path, cache_only=False, is_batch=False):
        scope_is_valid = (
            self.config.get('to_library') and ebook.id is not None and
            ebook.input_format.lower() == 'epub' and
            ebook.output_format.lower() == 'epub')
        if not scope_is_valid:
            error_dialog(
                self.gui, _('Translation Variants'), _(
                    'Translation variants currently require a Calibre '
                    'library book with EPUB input, EPUB output, and Library '
                    'as the output path.'), show=True)
            return None

        requested_storage = self.config.get(
            'translation_variant_storage', 'bundle_data')
        if requested_storage not in variant_storage_values:
            requested_storage = 'bundle_data'
        effective_storage = requested_storage
        if requested_storage == 'bundle_data':
            try:
                has_annotations = bool(
                    self.api.annotation_count_for_book(ebook.id))
                has_last_read = bool(self.api.get_last_read_positions(
                    ebook.id, fmt='EPUB', limit=1))
            except Exception:
                # Fail safe: inability to prove the record has no reading
                # assets must require an explicit user decision before
                # replacing its main EPUB.
                has_annotations = has_last_read = True
            if has_annotations or has_last_read:
                keep_preset_a = question_dialog(
                    self.gui, _('Translation Variants'), _(
                        'The source book has highlights or reading progress, '
                        'or their status could not be verified. Preset A '
                        'replaces the main EPUB and may make those reading '
                        'assets unusable. Do you want to keep preset A for '
                        'this run?'),
                    default_yes=False,
                    yes_text=_('Keep preset A'),
                    no_text=_('Use preset B (safe)'))
                if not keep_preset_a:
                    effective_storage = 'linked_pair'

        run_id = '%s-%s' % (
            datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
            uuid4().hex[:8])
        try:
            source_snapshot = temporary_copy(input_path, '.source.epub')
            translation_path = temporary_path('.translation.epub')
            bilingual_path = temporary_path('.bilingual.epub')
        except Exception:
            error_dialog(
                self.gui, _('Translation Variants'), _(
                    'Could not stage the source EPUB for variant output.'),
                det_msg=traceback.format_exc(), show=True)
            return None

        job = self.gui.job_manager.run_job(
            Dispatcher(self.translate_done),
            'arbitrary_n',
            args=(
                'calibre_plugins.ebook_translator.lib.conversion',
                'convert_variants_item',
                (ebook.title, input_path, translation_path, bilingual_path,
                 ebook.source_lang, ebook.target_lang, cache_only, is_batch,
                 ebook.input_format, ebook.encoding,
                 ebook.target_direction,
                 self.config.get('translation_variant_gap', 1.0))),
            description=(_('[{} > {}] Translating variants for "{}"').format(
                ebook.source_lang, ebook.target_lang, ebook.title)))
        self.working_jobs[job] = (ebook, {
            'variants': True,
            'source_path': source_snapshot,
            'translation_path': translation_path,
            'bilingual_path': bilingual_path,
            'requested_storage': requested_storage,
            'effective_storage': effective_storage,
            'main_variant': self._variant_main(),
            'run_id': run_id,
        })
        return job

    def _prepare_variant_metadata(
            self, path, ebook, kind, source_uuid, role_tags=None):
        with open(path, 'r+b') as file:
            metadata = get_metadata(file, 'epub')
            base_title = ebook.custom_title or metadata.title or ebook.title
            if kind == 'translation':
                title = '%s [%s]' % (base_title, ebook.target_lang)
            else:
                title = '%s [%s + %s]' % (
                    base_title, _('Original'), ebook.target_lang)
            metadata.title = title
            ebook_metadata_config = self.config.get('ebook_metadata') or {}
            if ebook_metadata_config.get('lang_code') and ebook.lang_code:
                metadata.language = ebook.lang_code
            subjects = ebook_metadata_config.get('subjects') or []
            tags = list(metadata.tags or [])
            for tag in subjects + list(role_tags or []) + [
                    'Translated by Ebook Translator: '
                    'https://translator.bookfere.com']:
                if tag not in tags:
                    tags.append(tag)
            metadata.tags = tags
            metadata.set_identifier(
                'ebook-translator-source', str(source_uuid))
            set_metadata(file, metadata, 'epub')
        return metadata, title

    @staticmethod
    def _is_chinese_target(ebook):
        lang_code = str(ebook.lang_code or '').lower().replace('_', '-')
        target_lang = str(ebook.target_lang or '').lower()
        return (lang_code == 'zho' or lang_code.startswith('zh') or
                'chinese' in target_lang or '中文' in target_lang)

    def _variant_main(self):
        value = self.config.get('translation_variant_main', 'translation')
        return value if value in ('translation', 'bilingual') else 'translation'

    def _variant_role_tags(
            self, ebook, storage, kind, main_variant='translation'):
        if not self._is_chinese_target(ebook):
            return []
        if kind == 'bilingual':
            return ['雙語_', '附中譯_'] if (
                storage == 'bundle_data' and main_variant == 'bilingual'
            ) else ['雙語_']
        tags = ['中譯_']
        if storage == 'linked_pair' or (
                storage == 'bundle_data' and main_variant == 'translation'):
            tags.append('附雙語_')
        return tags

    def _create_variant_book(self, metadata, path):
        book_id = self.db.create_book_entry(metadata)
        try:
            added = self.api.add_format(
                book_id, 'EPUB', path, run_hooks=False)
            if added is False:
                raise RuntimeError(
                    'Could not add EPUB format to derivative book')
        except Exception:
            self._remove_created_books([book_id])
            raise
        return book_id

    def _write_manifest(self, manifest):
        temp = PersistentTemporaryFile(suffix='.manifest.json')
        temp.write(json.dumps(
            manifest, ensure_ascii=False, indent=2,
            sort_keys=True).encode('utf-8'))
        temp.close()
        return temp.name

    def _add_verified_extra_files(self, book_id, files):
        self.api.add_extra_files(
            book_id, files, replace=False, auto_rename=False)
        listed = {
            item.relpath: item.file_path for item in
            self.api.list_extra_files(book_id, use_cache=False)}
        for relpath, source_path in files.items():
            stored_path = listed.get(relpath)
            if stored_path is None:
                raise RuntimeError(
                    'Missing stored variant data file: %s' % relpath)
            if file_sha256(source_path) != file_sha256(stored_path):
                raise RuntimeError(
                    'Stored variant data hash mismatch: %s' % relpath)

    def _remove_extra_files(self, book_id, relpaths):
        try:
            self.api.remove_extra_files(
                book_id, list(relpaths), permanent=False)
        except Exception as error:
            log.warn('Could not clean incomplete variant data:', error)

    def _remove_created_books(self, book_ids):
        if not book_ids:
            return
        try:
            self.api.remove_books(list(book_ids), permanent=False)
        except Exception as error:
            log.warn('Could not trash incomplete variant books:', error)

    def _variant_manifest(
            self, ebook, payload, source_uuid, derivative_book_ids):
        # The configured value is empty when the built-in default is used.
        # Record the engine that will actually be selected, not null.
        engine_name = self.config.get('translate_engine') or \
            get_translator().name
        engine_preferences = self.config.get('engine_preferences') or {}
        model = (engine_preferences.get(engine_name) or {}).get('model')
        plugin_version = str(EbookTranslator.__version__)
        return {
            'schema_version': 1,
            'storage_layout': 'calibre-data-v1',
            'run_id': payload['run_id'],
            'created_at': datetime.now(timezone.utc).isoformat(),
            'source_book_id': ebook.id,
            'source_uuid': str(source_uuid),
            'derivative_book_ids': derivative_book_ids,
            'requested_storage': payload['requested_storage'],
            'effective_storage': payload['effective_storage'],
            'main_variant': (
                payload.get('main_variant', 'translation')
                if payload['effective_storage'] == 'bundle_data'
                else 'translation'),
            'source_language': ebook.source_lang,
            'target_language': ebook.target_lang,
            'target_language_code': ebook.lang_code,
            'translation_engine': engine_name,
            'model': model,
            'merge_enabled': self.config.get('merge_enabled', False),
            'merge_length': self.config.get('merge_length'),
            'bilingual_position': 'below',
            'vertical_gap_em': self.config.get('vertical_gap', 1.0),
            'plugin_version': plugin_version,
            'calibre_version': __version__,
            'sha256': {
                'source': file_sha256(payload['source_path']),
                'translation': file_sha256(payload['translation_path']),
                'bilingual': file_sha256(payload['bilingual_path']),
            },
        }

    def _store_variants(self, ebook, payload):
        source_id = ebook.id
        source_uuid = self.api.field_for('uuid', source_id) or source_id
        storage = payload['effective_storage']
        main_variant = payload.get('main_variant', self._variant_main())
        if main_variant not in ('translation', 'bilingual'):
            main_variant = 'translation'
        # Freeze the effective role in the manifest as well as the main file.
        payload = dict(payload, main_variant=main_variant)
        translation_role_tags = self._variant_role_tags(
            ebook, storage, 'translation', main_variant)
        bilingual_role_tags = self._variant_role_tags(
            ebook, storage, 'bilingual', main_variant)
        translation_metadata, translation_title = \
            self._prepare_variant_metadata(
                payload['translation_path'], ebook, 'translation',
                source_uuid, translation_role_tags)
        bilingual_metadata, bilingual_title = self._prepare_variant_metadata(
            payload['bilingual_path'], ebook, 'bilingual', source_uuid,
            bilingual_role_tags)
        namespace = '%s/translation-variants/%s' % (
            DATA_DIR_NAME, payload['run_id'])
        source_relpath = '%s/source.epub' % namespace
        bilingual_relpath = '%s/bilingual.%s.epub' % (
            namespace, ebook.lang_code or 'target')
        manifest_relpath = '%s/manifest.json' % namespace

        if storage == 'bundle_data':
            other_variant = (
                'translation' if main_variant == 'bilingual' else 'bilingual')
            main_path = payload[main_variant + '_path']
            other_relpath = '%s/%s.%s.epub' % (
                namespace, other_variant, ebook.lang_code or 'target')
            main_tags = (bilingual_role_tags if main_variant == 'bilingual'
                         else translation_role_tags)
            original_identifiers = dict(
                self.api.field_for('identifiers', source_id) or {})
            original_tags = list(
                self.api.field_for('tags', source_id) or [])
            marked_identifiers = dict(original_identifiers)
            marked_identifiers['ebook-translator-variant'] = \
                '%s:%s' % (main_variant, payload['run_id'])
            marked_tags = list(original_tags)
            for tag in main_tags:
                if tag not in marked_tags:
                    marked_tags.append(tag)
            derivative_ids = {
                'translation': (
                    source_id if main_variant == 'translation' else None),
                'bilingual': source_id if main_variant == 'bilingual' else None,
            }
            manifest_path = self._write_manifest(self._variant_manifest(
                ebook, payload, source_uuid, derivative_ids))
            files = {
                source_relpath: payload['source_path'],
                other_relpath: payload[other_variant + '_path'],
                manifest_relpath: manifest_path,
            }
            try:
                self._add_verified_extra_files(source_id, files)
            except Exception:
                self._remove_extra_files(source_id, files.keys())
                raise
            try:
                self.api.set_field('tags', {source_id: marked_tags})
            except Exception:
                self._remove_extra_files(source_id, files.keys())
                raise
            try:
                added = self.api.add_format(
                    source_id, 'EPUB', main_path,
                    replace=True, run_hooks=False)
                if added is False:
                    raise RuntimeError(
                        'Could not replace source EPUB with %s' % main_variant)
                output_path = self.api.format_abspath(source_id, 'EPUB')
                if file_sha256(main_path) != \
                        file_sha256(output_path):
                    raise RuntimeError(
                        'Replaced main EPUB failed SHA-256 verification')
                self.api.set_field(
                    'identifiers', {source_id: marked_identifiers})
            except Exception as replace_error:
                try:
                    # Restoring from the separately staged source snapshot is
                    # safe even when the failed replace did not mutate data.
                    restored = self.api.add_format(
                        source_id, 'EPUB', payload['source_path'],
                        replace=True, run_hooks=False)
                    if restored is False:
                        raise RuntimeError(
                            'Calibre rejected the source EPUB restoration')
                    self.api.set_field(
                        'identifiers', {source_id: original_identifiers})
                    self.api.set_field('tags', {source_id: original_tags})
                except Exception as restore_error:
                    # Keep the verified source Data file for manual recovery.
                    raise RuntimeError(
                        'Replacing and restoring the source EPUB both failed; '
                        'the verified source remains in %s' % source_relpath
                    ) from restore_error
                self._remove_extra_files(source_id, files.keys())
                raise replace_error
            model = self.gui.library_view.model()
            if hasattr(model, 'refresh_ids'):
                model.refresh_ids({source_id})
            main_title = (bilingual_title if main_variant == 'bilingual'
                          else translation_title)
            return output_path, main_title, 0

        created_ids = []
        try:
            translation_id = self._create_variant_book(
                translation_metadata, payload['translation_path'])
            created_ids.append(translation_id)
            bilingual_id = None
            if storage == 'three_records':
                bilingual_id = self._create_variant_book(
                    bilingual_metadata, payload['bilingual_path'])
                created_ids.append(bilingual_id)
            derivative_ids = {
                'translation': translation_id,
                'bilingual': bilingual_id,
            }
            manifest_path = self._write_manifest(self._variant_manifest(
                ebook, payload, source_uuid, derivative_ids))
            files = {manifest_relpath: manifest_path}
            if storage == 'linked_pair':
                files[bilingual_relpath] = payload['bilingual_path']
            self._add_verified_extra_files(translation_id, files)
        except Exception:
            self._remove_created_books(created_ids)
            raise

        self.gui.library_view.model().books_added(len(created_ids))
        output_path = self.api.format_abspath(translation_id, 'EPUB')
        return output_path, translation_title, len(created_ids)

    def translate_done(self, job):
        ebook, output_path = self.working_jobs.pop(job)

        if job.failed:
            if not DEBUG:
                self.gui.job_exception(
                    job, dialog_title=_('Translation job failed'))
            return

        if isinstance(output_path, dict) and output_path.get('variants'):
            try:
                variant_output_path, ebook_title, created_count = \
                    self._store_variants(ebook, output_path)
            except Exception:
                error_dialog(
                    self.gui, _('Translation Variants'), _(
                        'The translated EPUBs were created, but storing the '
                        'three variants failed. The source book was kept or '
                        'restored.'), det_msg=traceback.format_exc(), show=True)
                return
            self.gui.status_bar.show_message(
                job.description + ' ' + _('completed'), 5000)

            def open_variant(payload):
                payload('ebook-viewer', kwargs={
                    'args': ['ebook-viewer', variant_output_path]})

            if self.config.get('show_notification', True):
                self.gui.proceed_question(
                    open_variant,
                    self.gui.job_manager.launch_gui_app,
                    job.log_path,
                    _('Ebook Translation Log'), _('Translation Completed'),
                    _('The translation variants for "{}" were completed. '
                      'Do you want to open the Chinese book?').format(
                          ebook_title),
                    log_is_file=True, icon=self.icon, auto_hide_after=10)
            return

        # TODO: Try to use the calibre generated metadata file.
        ebook_metadata_config = self.config.get('ebook_metadata') or {}
        if not ebook.is_extra_format():
            with open(output_path, 'r+b') as file:
                metadata = get_metadata(file, ebook.output_format)
                ebook_title = metadata.title
                if ebook.custom_title is not None:
                    ebook_title = ebook.custom_title
                if ebook_metadata_config.get('lang_mark'):
                    ebook_title = '%s [%s]' % (ebook_title, ebook.target_lang)
                metadata.title = ebook_title
                if ebook_metadata_config.get('lang_code'):
                    metadata.language = ebook.lang_code
                subjects = ebook_metadata_config.get('subjects')
                metadata.tags += (subjects or []) + [
                    'Translated by Ebook Translator: '
                    'https://translator.bookfere.com']
                # metadata.authors = ['bookfere.com']
                # metadata.author_sort = 'bookfere.com'
                # metadata.book_producer = 'Ebook Translator'
                set_metadata(file, metadata, ebook.output_format)
        else:
            metadata = self.api.get_metadata(ebook.id)
            ebook_title = ebook.title
            if ebook.custom_title is not None:
                ebook_title = ebook.custom_title
            if ebook_metadata_config.get('lang_mark'):
                ebook_title = '%s [%s]' % (ebook_title, ebook.target_lang)
            metadata.title = ebook_title

        if self.config.get('to_library'):
            book_id = self.db.create_book_entry(metadata)
            self.api.add_format(
                book_id, ebook.output_format, output_path, run_hooks=False)
            self.gui.library_view.model().books_added(1)
            output_path = self.api.format_abspath(book_id, ebook.output_format)
        else:
            dirname = os.path.dirname(output_path)
            filename = sanitize_file_name(ebook_title[:200])
            new_output_path = os.path.join(
                dirname, '%s.%s' % (filename, ebook.output_format))
            os.rename(output_path, new_output_path)
            output_path = new_output_path

        self.gui.status_bar.show_message(
            job.description + ' ' + _('completed'), 5000)

        def callback(payload):
            if ebook.input_format in extra_formats.keys():
                open_path(output_path)
            else:
                kwargs = {'args': ['ebook-viewer', output_path]}
                payload('ebook-viewer', kwargs=kwargs)

        if self.config.get('show_notification', True):
            self.gui.proceed_question(
                callback,
                self.gui.job_manager.launch_gui_app,
                job.log_path,
                _('Ebook Translation Log'), _('Translation Completed'),
                _('The translation of "{}" was completed. Do you want to '
                  'open the book?').format(ebook_title),
                log_is_file=True, icon=self.icon, auto_hide_after=10)
