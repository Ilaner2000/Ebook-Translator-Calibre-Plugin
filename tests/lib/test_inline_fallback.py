import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from lxml import etree

from ...engines.base import Base
from ...lib.cache import Paragraph, TranslationCache
from ...lib.element import ElementHandler, PageElement
from ...lib.exception import TranslationFailed, TranslationCanceled
from ...lib.only_mode import TOKEN, InlineAlignmentError, validate_inline_translation
from ...lib.translation import Translation, Glossary
from ...lib.utils import trim, uid


class TestInlineFallback(unittest.TestCase):
    def worker(self, responses):
        engine = Mock(placeholder=Base.placeholder, request_attempt=3, max_error_count=10,
                      merge_enabled=False, name='fake engine', concurrency_limit=1, request_interval=0)
        engine.translate.side_effect = responses
        engine.match_error.return_value = False
        engine.get_target_lang.return_value = 'zh'
        worker = Translation(engine, Glossary(Base.placeholder)); worker.strict_inline = True
        return worker, engine

    def paragraph(self, source):
        p = Paragraph(1, uid(source), '<p/>', source, page='chapter.xhtml'); p.row = 1
        return p

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_four_production_shapes_exhaust_then_fallback_once(self, sleep):
        shapes = [
            '⟦ET0O⟧P⟦ET1O⟧ART⟦ET1C⟧ T⟦ET2O⟧HREE⟦ET2C⟧ : S⟦ET3O⟧TEPPING⟦ET3C⟧ B⟦ET4O⟧ACK⟦ET4C⟧ ⟦ET0C⟧',
            'As soon as sailing caught on, ⟦ET0O⟧term⟦ET0C⟧ with ⟦ET1O⟧⟦ET2O⟧emphasis⟦ET2C⟧⟦ET1C⟧.',
            'In 1608, ⟦ET0O⟧a telescope⟦ET0C⟧.',
            'So, ⟦ET0O⟧⟦ET0C⟧ as that child asked: ⟦ET1O⟧why⟦ET1C⟧? {{id_00000}}',
        ]
        for source in shapes:
            with self.subTest(source=source):
                target = '繁中降級譯文' + ('{{id_00000}}' if '{{id_' in source else '')
                worker, engine = self.worker(['bad'] * 4 + [target])
                p = self.paragraph(source); worker.translate_paragraph(p)
                self.assertEqual(5, engine.translate.call_count)
                self.assertEqual(trim(TOKEN.sub('', source)), engine.translate.call_args.args[0])
                self.assertTrue(p.degraded); self.assertEqual(target, p.translation)
                self.assertEqual(0, worker.abort_count); self.assertFalse(p.is_cache)
                self.assertEqual(source, p.original)

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_only_terminal_paragraph_failure_counts_and_cache_success_resets(self, sleep):
        source = 'A ⟦ET0O⟧word⟦ET0C⟧ {{id_00000}}'
        worker, engine = self.worker(['bad'] * 5)
        p = self.paragraph(source)
        with self.assertRaises(TranslationFailed):worker.translate_paragraph(p)
        self.assertEqual(5, engine.translate.call_count); self.assertEqual(1, worker.abort_count)
        cached = self.paragraph(source); cached.translation = source
        worker.translate_paragraph(cached)
        self.assertEqual(5, engine.translate.call_count); self.assertEqual(0, worker.abort_count)

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_many_successful_degradations_cannot_trip_abort(self, sleep):
        worker, engine = self.worker((['bad'] * 4 + ['譯文']) * 12)
        for _ in range(12):
            p = self.paragraph('⟦ET0O⟧word⟦ET0C⟧'); worker.translate_paragraph(p)
            self.assertTrue(p.degraded); self.assertEqual(0, worker.abort_count)
        self.assertEqual(60, engine.translate.call_count)

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_ten_terminal_rows_stop_before_requesting_eleventh(self, sleep):
        worker, engine = self.worker([''] * 50)
        for count in range(1, 11):
            with self.assertRaises(TranslationFailed):
                worker.translate_paragraph(self.paragraph('⟦ET0O⟧word⟦ET0C⟧'))
            self.assertEqual(count, worker.abort_count)
        with self.assertRaises(TranslationCanceled):
            worker.translate_paragraph(self.paragraph('⟦ET0O⟧next⟦ET0C⟧'))
        self.assertEqual(50, engine.translate.call_count)

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_fallback_cannot_drop_reserve_or_invent_markers(self, sleep):
        source = '⟦ET0O⟧word⟦ET0C⟧ {{id_00000}}'
        for bad in ('譯文', '譯文{{id_99999}}', '⟦ET0O⟧譯文⟦ET0C⟧{{id_00000}}'):
            worker, engine = self.worker(['bad'] * 4 + [bad])
            with self.subTest(bad=bad), self.assertRaises(TranslationFailed):
                worker.translate_paragraph(self.paragraph(source))
            self.assertEqual(1, worker.abort_count)

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_cancel_and_fatal_provider_error_do_not_degrade(self, sleep):
        worker, engine = self.worker(['bad'] * 5)
        worker.set_cancel_request(lambda: engine.translate.call_count >= 4)
        with self.assertRaises(TranslationCanceled):worker.translate_paragraph(self.paragraph('⟦ET0O⟧word⟦ET0C⟧'))
        self.assertEqual(4, engine.translate.call_count)
        worker, engine = self.worker([RuntimeError('fatal')]); engine.match_error.return_value = True
        with self.assertRaises(TranslationCanceled):worker.translate_paragraph(self.paragraph('⟦ET0O⟧word⟦ET0C⟧'))
        self.assertEqual(1, engine.translate.call_count)

    def test_undeclared_markerless_cache_stays_strict(self):
        source = '⟦ET0O⟧word⟦ET0C⟧'
        worker, engine = self.worker([source.replace('word', '譯文')])
        p = self.paragraph(source); p.translation = 'not explicitly degraded'
        worker.translate_paragraph(p)
        self.assertFalse(p.degraded); self.assertEqual(1, engine.translate.call_count)
        self.assertIn('⟦ET', p.translation)

    def test_declared_degraded_cache_avoids_provider_and_resets_abort(self):
        worker, engine = self.worker([]); worker.abort_count = 9
        p = self.paragraph('⟦ET0O⟧word⟦ET0C⟧'); p.translation = '快取譯文'; p.degraded = True
        worker.translate_paragraph(p)
        engine.translate.assert_not_called(); self.assertTrue(p.is_cache)
        self.assertEqual(0, worker.abort_count)

    def handler(self, root, position='only'):
        handler = ElementHandler(Base.placeholder, Base.separator, position)
        handler.set_only_policy({'preserve_inline': True} if position == 'only' else None)
        handler.load_remove_rules(); handler.load_reserve_rules()
        rows = handler.prepare_original([PageElement(root[0])])
        p = Paragraph(*rows[0]); p.row = 0
        return handler, p

    def test_degraded_render_retains_root_notes_empty_anchor_and_typography(self):
        root = etree.fromstring(b'<div><p id="p1" class="TX">So, <a id="loc"/> <em>word</em><sup><a href="notes.xhtml#n">1</a></sup></p></div>')
        handler, p = self.handler(root); source = p.original
        p.translation = '降級文字{{id_00000}}'; p.degraded = True
        handler.add_translations([p])
        self.assertEqual(source, p.original); self.assertEqual('p1', root[0].get('id'))
        self.assertEqual('loc', root[0][0].get('id')); self.assertIsNone(root[0].find('em'))
        self.assertEqual('notes.xhtml#n', root[0].find('.//sup/a').get('href'))
        self.assertIn('line-height:1.7', root[0].get('style'))
        self.assertNotIn('⟦ET', etree.tostring(root).decode())

    def test_degraded_render_still_fails_closed_for_reserve_loss(self):
        root = etree.fromstring(b'<div><p><em>word</em><sup>1</sup></p></div>')
        handler, p = self.handler(root); before = etree.tostring(root)
        p.translation = 'lost note'; p.degraded = True
        with self.assertRaises(InlineAlignmentError):handler.add_translations([p])
        self.assertEqual(before, etree.tostring(root))

    def test_degraded_note_backlink_and_figure_reference_survive_at_paragraph_end(self):
        root = etree.fromstring(b'<div><p id="paragraph">See <a href="figure.xhtml#f">Figure 1</a> and <a id="ref1" href="notes.xhtml#n1"><em>1</em></a><span id="location">text</span></p></div>')
        handler, p = self.handler(root); element = next(iter(handler.elements.values()))
        p.translation = '降級正文'; p.degraded = True; handler.add_translations([p])
        links = root[0].findall('a')
        self.assertEqual('location', links[0].get('id'))
        self.assertEqual(['Figure 1', '1'], [x.text for x in links if x.get('href')])
        self.assertEqual('notes.xhtml#n1', root[0].find('a[@id="ref1"]').get('href'))
        note = etree.fromstring(b'<div><p id="n1"><a href="chapter.xhtml#ref1">back</a></p></div>')
        target = note.find('.//a').get('href').split('#')[1]
        self.assertIsNotNone(root.find('.//a[@id="%s"]' % target))
        self.assertEqual({'moved_anchor_count': 1, 'moved_link_count': 2},
                         {k:element.degraded_effects[k] for k in ('moved_anchor_count','moved_link_count')})

    def test_link_with_reserved_image_keeps_only_one_image(self):
        root = etree.fromstring(b'<div><p>A <a id="imgref" href="figure.xhtml"><img src="picture.png"/></a><em>word</em></p></div>')
        handler, p = self.handler(root); p.translation = '圖{{id_00000}}'; p.degraded = True
        handler.add_translations([p])
        self.assertEqual(1, len(root.findall('.//img')))
        self.assertEqual('figure.xhtml', root[0].find('a[@id="imgref"]').get('href'))
        self.assertNotIn('{{id_', etree.tostring(root).decode())

    def test_paired_degraded_translation_is_used_without_markers(self):
        root = etree.fromstring(b'<div><p>Some <em> word </em> here.</p></div>')
        handler, p = self.handler(root, 'below'); handler.inline_cache_to_plain = True
        p.original = 'Some ⟦ET0O⟧ word ⟦ET0C⟧ here.'; p.translation = '降級譯文'; p.degraded = True
        handler.add_translations([p])
        self.assertEqual('降級譯文', ''.join(root[1].itertext()))
        self.assertEqual('Some  word  here.', ''.join(root[0].itertext()))


class TestDegradedNativeCache(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.patches = [patch.object(TranslationCache, name, str(root/path))
                        for name, path in (('dir_path', '.'), ('cache_path', 'cache'), ('temp_path', 'temp'))]
        for item in self.patches:item.start()
        self.cache = TranslationCache('fallback')
        self.source = 'Text ⟦ET0O⟧word⟦ET0C⟧ {{id_00000}}'
        self.cache.add(7, uid(self.source), '<p/>', self.source, page='chapter.xhtml')
        self.cache.connection.commit()

    def tearDown(self):
        self.cache.close()
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    def degraded(self):
        p = self.cache.paragraph(7); p.translation = '譯文{{id_00000}}'
        p.degraded = True; p.degrade_reason = 'inline_protocol_exhausted'
        return p

    def test_flag_round_trip_keeps_original_md5_and_schema_unchanged(self):
        p = self.degraded(); original_md5 = p.md5
        self.cache.update_paragraph(p); self.cache.close(); self.cache = TranslationCache('fallback')
        for p in (self.cache.paragraph(7), self.cache.get_paragraphs([7])[0], self.cache.all_paragraphs()[0]):
            self.assertTrue(p.degraded); self.assertEqual(self.source, p.original)
            self.assertEqual(original_md5, p.md5); self.assertEqual('譯文{{id_00000}}', p.translation)
        self.assertEqual(10, len(self.cache.cursor.execute('PRAGMA table_info(cache)').fetchall()))

    def test_changed_target_or_invalid_flag_never_authorizes_degradation(self):
        self.cache.update_paragraph(self.degraded())
        self.cache.update(7, translation='changed')
        self.assertFalse(self.cache.paragraph(7).degraded)
        self.cache.set_info('only_v2_degraded:7', 'not JSON')
        self.assertFalse(self.cache.paragraph(7).degraded)

    def test_normal_retranslation_clears_degraded_metadata(self):
        self.cache.update_paragraph(self.degraded())
        p = self.cache.paragraph(7); p.translation = self.source; p.degraded = False
        self.cache.update_paragraph(p)
        self.assertIsNone(self.cache.get_info('only_v2_degraded:7'))
        self.assertFalse(self.cache.paragraph(7).degraded)

    def test_metadata_and_target_write_roll_back_together(self):
        p = self.degraded()
        with patch.object(self.cache, 'update', side_effect=RuntimeError('write failed')):
            with self.assertRaises(RuntimeError):self.cache.update_paragraph(p)
        self.assertIsNone(self.cache.get_info('only_v2_degraded:7'))
        self.assertIsNone(self.cache.paragraph(7).translation)
