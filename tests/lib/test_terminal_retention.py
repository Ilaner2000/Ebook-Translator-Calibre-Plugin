"""S797 narrow mixed-failure and native terminal-record roundtrip regressions."""
import copy
import json
import unittest
from unittest.mock import Mock, patch
from lxml import etree
from ...lib.cache import TranslationCache, source_retention_matches
from ...lib.exception import ParagraphProtocolFailed, TranslationFailed
from ...lib.only_mode import InlineAlignmentError
from . import test_keep_source as ks
from . import test_inline_fallback as fb
from . import test_placeholder_retry as retry

BAD = '⟦ET1O⟧⟦ET0O⟧錯⟦ET1C⟧⟦ET0C⟧'
SOURCE = 'An ⟦ET0O⟧important ⟦ET1O⟧note⟦ET1C⟧⟦ET0C⟧.'


class TestMixedTerminal(unittest.TestCase):
    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_proven_inline_failures_survive_last_timeout_opt_in(self, _sleep):
        worker, engine = retry.TestBoundedPlaceholderRetry().worker([BAD, BAD, TimeoutError('fallback')])
        worker.keep_unrecoverable_source = True
        p = retry.TestBoundedPlaceholderRetry().paragraph(SOURCE)
        with self.assertRaises(TranslationFailed) as caught: worker.translate_paragraph(p)
        self.assertIsInstance(caught.exception, ParagraphProtocolFailed)
        self.assertTrue(p.keep_source)
        self.assertEqual('inline_protocol_exhausted', p.failure_reason)
        self.assertEqual(3, engine.translate.call_count)
        self.assertEqual(0, worker.abort_count)
        self.assertTrue(p.failure_context['protocol_seen'])
        self.assertEqual('TimeoutError', p.failure_context['last_error_type'])
    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_reserve_protocol_then_timeout_keeps_placeholder_reason(self, _sleep):
        worker, engine = retry.TestBoundedPlaceholderRetry().worker([BAD, BAD, TimeoutError('fallback')])
        worker.keep_unrecoverable_source = True
        p = retry.TestBoundedPlaceholderRetry().paragraph(SOURCE + ' {{id_00000}}')
        with self.assertRaises(ParagraphProtocolFailed): worker.translate_paragraph(p)
        self.assertTrue(p.keep_source)
        self.assertEqual('placeholder_unrecoverable', p.failure_reason)
        self.assertIsNone(p.translation)
        self.assertEqual(3, engine.translate.call_count)
        self.assertTrue(p.failure_context['protocol_seen'])
        self.assertEqual('TimeoutError', p.failure_context['last_error_type'])
    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_default_off_keeps_existing_provider_failure_semantics(self, _sleep):
        worker, engine = retry.TestBoundedPlaceholderRetry().worker([BAD, BAD, TimeoutError('fallback')])
        p = retry.TestBoundedPlaceholderRetry().paragraph(SOURCE)
        with self.assertRaises(TranslationFailed) as caught: worker.translate_paragraph(p)
        self.assertNotIsInstance(caught.exception, ParagraphProtocolFailed)
        self.assertFalse(p.keep_source)
        self.assertEqual(1, worker.abort_count)
    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_pure_outage_and_quality_corruption_never_become_retained(self, _sleep):
        for responses in ([TimeoutError('offline')] * 3, [BAD, BAD, '<0x80>']):
            with self.subTest(responses=responses):
                worker, _engine = retry.TestBoundedPlaceholderRetry().worker(responses)
                worker.keep_unrecoverable_source = True
                p = retry.TestBoundedPlaceholderRetry().paragraph(SOURCE)
                with self.assertRaises(TranslationFailed) as caught: worker.translate_paragraph(p)
                self.assertNotIsInstance(caught.exception, ParagraphProtocolFailed)
                self.assertFalse(p.keep_source)
                self.assertEqual([], worker.retained_source_manifest())


class TestNativeTerminalRecord(unittest.TestCase):
    def setUp(self):
        self.fixture = fb.TestDegradedNativeCache()
        self.fixture.setUp()
        self.cache = self.fixture.cache
    def tearDown(self):
        self.fixture.cache = self.cache
        self.fixture.tearDown()
    def retain(self):
        p = self.cache.paragraph(7)
        p.keep_source = True
        p.failure_reason = 'placeholder_unrecoverable'
        p.translation = None
        self.cache.update_paragraph(p)
        return p
    def test_native_roundtrip_restores_only_explicit_hash_bound_null_row(self):
        self.retain()
        self.cache.close()
        self.cache = TranslationCache('fallback')
        p = self.cache.paragraph(7)
        self.assertIsNone(p.translation)
        self.assertTrue(source_retention_matches(p, p.source_retention))
        self.cache.set_cache_only(True)
        self.assertEqual([], self.cache.all_paragraphs())
        self.cache.include_retained_sources = True
        self.assertEqual([7], [p.id for p in self.cache.all_paragraphs()])
        self.assertEqual(10, len(self.cache.cursor.execute('PRAGMA table_info(cache)').fetchall()))
    def test_new_job_uses_retention_without_provider_or_warm_cache_write(self):
        self.retain()
        p = self.cache.paragraph(7)
        worker, engine = retry.TestBoundedPlaceholderRetry().worker([])
        worker.keep_unrecoverable_source = True
        callback = Mock(side_effect=AssertionError('warm cache write'))
        worker.set_callback(callback)
        worker.progress_bar.load(1)
        worker.translate_paragraph(p)
        worker.process_translation(p)
        self.assertTrue(p.keep_source)
        self.assertEqual('placeholder_unrecoverable', p.failure_reason)
        self.assertIsNone(p.translation)
        self.assertEqual(0, engine.translate.call_count)
        callback.assert_not_called()
        self.assertEqual([7], [x['id'] for x in worker.retained_source_manifest()])
    def test_tampered_source_record_and_raw_cannot_authorize_skip(self):
        self.retain()
        p = self.cache.paragraph(7)
        record = copy.deepcopy(p.source_retention)
        for key, value in [('id', 8), ('page', 'other.xhtml'), ('source_md5', 'bad'),
                           ('raw_sha256', 'bad'), ('original_sha256', 'bad'), ('reason', 'provider_outage')]:
            with self.subTest(key=key):
                bad = dict(record, **{key: value})
                self.assertFalse(source_retention_matches(p, bad))
        p.raw += 'changed'
        self.assertFalse(source_retention_matches(p, record))
        self.cache.set_info('only_v2_source_retained:7', 'not JSON')
        self.assertIsNone(self.cache.paragraph(7).source_retention)
    def test_target_change_or_successful_fresh_retry_clears_old_retention(self):
        self.retain()
        p = self.cache.paragraph(7)
        worker, engine = retry.TestBoundedPlaceholderRetry().worker([p.original])
        worker.keep_unrecoverable_source = True
        worker.set_fresh(True)
        worker.translate_paragraph(p)
        self.assertFalse(p.keep_source)
        self.assertEqual(1, engine.translate.call_count)
        self.cache.update_paragraph(p)
        self.assertIsNone(self.cache.get_info('only_v2_source_retained:7'))
        self.assertIsNone(self.cache.paragraph(7).source_retention)
    def test_terminal_metadata_and_null_target_rollback_as_one_transaction(self):
        p = self.cache.paragraph(7)
        p.keep_source = True
        p.failure_reason = 'inline_protocol_exhausted'
        with patch.object(self.cache, 'update', side_effect=RuntimeError('commit failed')):
            with self.assertRaises(RuntimeError): self.cache.update_paragraph(p)
        self.assertIsNone(self.cache.get_info('only_v2_source_retained:7'))
        self.assertIsNone(self.cache.paragraph(7).translation)
    def test_other_untranslated_row_never_inherits_retention(self):
        self.retain()
        self.cache.add(8, 'other-md5', '<p>Other</p>', 'Other', page='chapter.xhtml')
        self.cache.connection.commit()
        record = self.cache.get_info('only_v2_source_retained:7')
        self.cache.set_info('only_v2_source_retained:8', record)
        self.cache.set_cache_only(True)
        self.cache.include_retained_sources = True
        self.assertEqual([7], [p.id for p in self.cache.all_paragraphs()])
    def test_two_renderers_retain_exact_source_after_native_reload(self):
        self.cache.delete([7])
        root, handler, paragraphs = ks.TestKeepSource().fixture()
        original = etree.tostring(root)
        for p in paragraphs:
            self.cache.add(p.id, p.md5, p.raw, p.original, page=p.page)
        self.cache.connection.commit()
        paragraphs[0].keep_source = True
        paragraphs[0].failure_reason = 'placeholder_unrecoverable'
        paragraphs[1].translation = '成功'
        for p in paragraphs: self.cache.update_paragraph(p)
        self.cache.set_cache_only(True)
        self.cache.include_retained_sources = True
        loaded = self.cache.all_paragraphs()
        worker, engine = retry.TestBoundedPlaceholderRetry().worker([])
        worker.keep_unrecoverable_source = True
        for p in loaded: worker.translate_paragraph(p)
        handler.keep_unrecoverable_source = True
        bad = etree.tostring(root[0])
        handler.add_translations(loaded)
        self.assertEqual(bad, etree.tostring(root[0]))
        self.assertEqual('成功', root[1].text)
        paired_root = etree.fromstring(original)
        paired = ks.TestKeepSource().handler(paired_root, 'below')
        paired.keep_unrecoverable_source = True
        paired.add_translations(loaded)
        self.assertEqual(bad, etree.tostring(paired_root[0]))
        self.assertEqual(3, len(paired_root))
        self.assertEqual(0, engine.translate.call_count)