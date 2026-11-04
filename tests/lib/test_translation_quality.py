import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from lxml import etree

from ...engines.base import Base
from ...lib.cache import Paragraph, TranslationCache
from ...lib.element import ElementHandler, PageElement
from ...lib.exception import TranslationFailed
from ...lib.translation import Glossary, Translation
from ...lib.translation_quality import normalize_translation, TranslationQualityError
from ...lib.utils import uid


class TestTranslationQuality(unittest.TestCase):
    def test_adjacent_utf8_codepoints_and_protected_tokens(self):
        source = '⟦ET0O⟧word⟦ET0C⟧{{id_00002}}'
        target = '⟦ET0O⟧<0xE9><0x8C><0xAE><0xF0><0x9D><0x9D><0x81>⟦ET0C⟧{{id_00002}}'
        self.assertEqual('⟦ET0O⟧錮𝝁⟦ET0C⟧{{id_00002}}', normalize_translation(source, target))
        self.assertEqual('⟦ET0O⟧word⟦ET0C⟧{{id_00002}}', source)

    def test_invalid_partial_overlong_surrogate_and_malformed_rejected(self):
        for target in ['1<0x80>00', '<0xF0>', '<0xC0><0xAF>', '<0xED><0xA0><0x80>',
                       '<0xF4><0x90><0x80><0x80>', '<0xGG>', '<0x00>', '<0xE9>']:
            with self.subTest(target=target), self.assertRaises(TranslationQualityError):
                normalize_translation('source', target)

    def test_decoded_and_ordinary_runaway_both_rejected(self):
        for target in [('<0xF0><0x9F><0x9F><0xA8> ' * 819), '🟨 ' * 819,
                       '重複句子。' * 100, 'x' * 64]:
            with self.subTest(target=target[:40]), self.assertRaises(TranslationQualityError):
                normalize_translation('Muon Yukawa coupling', target)

    def test_source_literal_math_repetition_and_normal_prose_preserved(self):
        for source, target in [('0' * 100, '0' * 100), ('ha ' * 100, '哈 ' * 100),
                               ('ordinary source', '人人都知道，天天閱讀是好習慣。'),
                               ('math {{id_00000}}', '數學{{id_00000}}'),
                               ('binary: ' + '0' * 100 + ' end', '二進位：' + '0' * 100)]:
            self.assertEqual(target, normalize_translation(source, target))

    def worker(self, responses, strict=True):
        engine = Mock(placeholder=Base.placeholder, request_attempt=3, max_error_count=10,
                      merge_enabled=False, name='fake', concurrency_limit=1, request_interval=0)
        engine.translate.side_effect = responses; engine.match_error.return_value = False
        engine.name = 'fake'
        engine.get_target_lang.return_value = 'zh'
        worker = Translation(engine, Glossary(Base.placeholder))
        worker.strict_inline = strict; worker.quality_gate = True
        return worker, engine

    def paragraph(self, source='Muon Yukawa coupling'):
        return Paragraph(1, uid(source), '<p/>', source, page='chapter.xhtml')

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_markerless_bad_response_retries_then_decodes_success(self, sleep):
        worker, engine = self.worker(['🟨 ' * 819, '<0x80>', '緲<0xE9><0x8C><0xAE>'])
        p = self.paragraph(); worker.translate_paragraph(p)
        self.assertEqual(3, engine.translate.call_count); self.assertEqual('緲錮', p.translation)
        self.assertEqual(0, worker.abort_count)

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_markerless_exhaustion_counts_one_terminal_failure(self, sleep):
        worker, engine = self.worker(['🟨 ' * 819] * 4)
        with self.assertRaises(TranslationFailed): worker.translate_paragraph(self.paragraph())
        self.assertEqual(4, engine.translate.call_count); self.assertEqual(1, worker.abort_count)

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_fallback_uses_same_quality_gate_and_persists_decoded_hash(self, sleep):
        worker, engine = self.worker(['bad'] * 4 + ['<0xE9><0x8C><0xAE>'])
        p = self.paragraph('⟦ET0O⟧word⟦ET0C⟧'); worker.translate_paragraph(p)
        self.assertEqual('錮', p.translation); self.assertTrue(p.degraded)
        with tempfile.TemporaryDirectory() as td, patch.object(TranslationCache, 'dir_path', td), \
                patch.object(TranslationCache, 'cache_path', str(Path(td)/'cache')):
            cache = TranslationCache('quality'); cache.add(p.id,p.md5,p.raw,p.original); cache.connection.commit()
            try:
                cache.update_paragraph(p)
                flag = json.loads(cache.get_info('only_v2_degraded:1'))
                self.assertEqual(hashlib.sha256('錮'.encode()).hexdigest(), flag['translation_sha256'])
                self.assertEqual('錮', cache.paragraph(1).translation)
            finally:
                cache.close()

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_fallback_invalid_bytes_cannot_publish(self, sleep):
        worker, engine = self.worker(['bad'] * 4 + ['<0x80>'])
        with self.assertRaises(TranslationFailed):
            worker.translate_paragraph(self.paragraph('⟦ET0O⟧word⟦ET0C⟧'))
        self.assertEqual(5, engine.translate.call_count); self.assertEqual(1, worker.abort_count)

    def test_warm_decoded_only_and_bilingual_views_do_not_write_callback(self):
        for strict in [True, False]:
            worker, engine = self.worker([], strict)
            p = self.paragraph('⟦ET0O⟧word⟦ET0C⟧'); p.translation = '<0xE9><0x8C><0xAE>'; p.degraded = True
            worker.abort_count = 9; callback = Mock(); worker.set_callback(callback); worker.progress_bar.load(1)
            worker.translate_paragraph(p); worker.process_translation(p)
            self.assertEqual('錮', p.translation); self.assertTrue(p.is_cache)
            self.assertEqual(0, worker.abort_count); engine.translate.assert_not_called(); callback.assert_not_called()

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_bad_warm_cache_repairs_through_provider_path(self, sleep):
        worker, engine = self.worker(['<0x80>', '正確譯文'])
        p = self.paragraph(); p.translation = '🟨 ' * 819
        worker.translate_paragraph(p)
        self.assertEqual('正確譯文', p.translation); self.assertFalse(p.is_cache)
        self.assertEqual(2, engine.translate.call_count)

    def test_both_renderers_reject_before_dom_mutation(self):
        for position in ['only', 'below']:
            root = etree.fromstring(b'<div><p id="x">Muon Yukawa coupling</p></div>')
            handler = ElementHandler(Base.placeholder, Base.separator, position)
            handler.set_only_policy({'preserve_inline': True} if position == 'only' else None)
            handler.inline_cache_to_plain = position == 'below'
            handler.load_remove_rules(); handler.load_reserve_rules()
            p = Paragraph(*handler.prepare_original([PageElement(root[0])])[0]); p.translation = '🟨 ' * 819
            before = etree.tostring(root)
            with self.assertRaises(TranslationQualityError): handler.add_translations([p])
            self.assertEqual(before, etree.tostring(root))
