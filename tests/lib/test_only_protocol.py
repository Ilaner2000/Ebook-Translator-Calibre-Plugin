import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from lxml import etree

from ...engines.base import Base
from ...engines.custom import CustomTranslate
from ...lib.config import Configuration
from ...lib.element import ElementHandler, PageElement, get_element_handler
from ...lib.only_mode import (OnlyPolicy, InlineAlignmentError, INLINE_INSTRUCTION,
                              InlinePlan, apply_typography, prepare_inline_body, validate_inline_translation)
from ...lib.translation import Glossary, Translation
from ...lib.conversion import translation_cache_identity


class TestOnlyProtocol(unittest.TestCase):
    def test_shared_cache_normalizes_whitespace_across_semantic_boundaries(self):
        root = etree.fromstring(b'<div><p>Some <em> text </em> next</p></div>')
        handler = ElementHandler(Base.placeholder, Base.separator, 'below')
        handler.set_only_policy(None); handler.inline_cache_to_plain = True
        handler.load_remove_rules(); handler.load_reserve_rules()
        handler.prepare_original([PageElement(root[0])])
        paragraph = SimpleNamespace(original='Some ⟦ET0O⟧ text ⟦ET0C⟧ next', translation='ONE ⟦ET0O⟧ TWO ⟦ET0C⟧ THREE')
        handler.add_translations([paragraph])
        self.assertEqual(2, len(root))
        self.assertIn('ONE', ''.join(root[1].itertext()))
        self.assertIn('TWO', ''.join(root[1].itertext()))

    def test_paired_partial_cache_fails_before_even_successful_element_changes(self):
        root = etree.fromstring(b'<div><p>First</p><p>Missing</p></div>')
        handler = ElementHandler(Base.placeholder, Base.separator, 'below')
        handler.set_only_policy(None); handler.inline_cache_to_plain = True
        handler.load_remove_rules(); handler.load_reserve_rules()
        handler.prepare_original([PageElement(x) for x in root]); before = etree.tostring(root)
        paragraphs = [SimpleNamespace(original='First', translation='第一')]
        with self.assertRaisesRegex(InlineAlignmentError, 'missing bilingual target at element 1'):
            handler.add_translations(paragraphs)
        self.assertEqual(before, etree.tostring(root))

    @patch('calibre_plugins.ebook_translator.engines.base.request')
    def test_real_custom_translate_sends_protocol_without_mutating_request(self, network):
        class Engine(CustomTranslate):
            lang_codes = {'source': {'English': 'en'}, 'target': {'Chinese': 'zh'}}
            request = {'url': 'https://unused.invalid', 'headers': {'Content-Type': 'application/json'},
                       'data': {'model': 'test', 'stream': False, 'options': {'num_ctx': 131072},
                                'messages': [{'role': 'system', 'content': 'translate'}, {'role': 'user', 'content': '<text>'}]}}
            response = "response['message']['content']"
        engine = Engine(); engine.inline_protocol = True
        engine.set_source_lang('English'); engine.set_target_lang('Chinese')
        source = '⟦ET0O⟧text⟦ET0C⟧'; result = '⟦ET0O⟧譯文⟦ET0C⟧'
        before = copy.deepcopy(Engine.request)
        network.return_value = json.dumps({'message': {'content': result}, 'prompt_eval_count': 9, 'eval_count': 4})
        self.assertEqual(result, engine.translate(source))
        sent = json.loads(network.call_args.kwargs['data'])
        self.assertIn(INLINE_INSTRUCTION, sent['messages'][0]['content'])
        self.assertEqual(source, sent['messages'][1]['content'])
        self.assertEqual(before, Engine.request)
        self.assertEqual(4, engine.get_session_usage()['output_tokens'])

    def test_paired_bilingual_reuses_v2_cache_without_visible_markers(self):
        root = etree.fromstring(b'<div><p>Some <em>text</em></p></div>')
        handler = ElementHandler(Base.placeholder, Base.separator, 'below')
        handler.set_only_policy(None); handler.load_remove_rules(); handler.load_reserve_rules()
        handler.inline_cache_to_plain = True
        handler.prepare_original([PageElement(root[0])])
        paragraph = SimpleNamespace(original='Some ⟦ET0O⟧text⟦ET0C⟧', translation='一些⟦ET0O⟧文字⟦ET0C⟧')
        handler.add_translations([paragraph])
        self.assertEqual('Some text', ''.join(root[0].itertext()))
        self.assertEqual('一些文字', ''.join(root[1].itertext()))
        self.assertNotIn('⟦ET', etree.tostring(root).decode())

    def test_v2_native_cache_namespace_is_separate_from_legacy_and_stable(self):
        handler = ElementHandler(Base.placeholder, Base.separator, 'only')
        handler.set_only_policy({'preserve_inline': True})
        args = ('book.epub', 'engine', 'Chinese', '0', '')
        v2 = translation_cache_identity(*args, handler)
        self.assertEqual(v2, translation_cache_identity(*args, handler))
        handler.set_only_policy(None)
        legacy = translation_cache_identity(*args, handler)
        self.assertNotEqual(legacy, v2)
        handler.position = 'below'
        self.assertEqual(legacy, translation_cache_identity(*args, handler))

    def test_valid_but_stale_cache_cannot_silently_leave_source_english(self):
        root = etree.fromstring(b'<div><p>New <em>text</em></p></div>')
        handler = ElementHandler(Base.placeholder, Base.separator, 'only')
        handler.set_only_policy({'preserve_inline': True}); handler.load_remove_rules(); handler.load_reserve_rules()
        handler.prepare_original([PageElement(root[0])]); before = etree.tostring(root)
        stale = SimpleNamespace(row=0, original='New text', translation='old translation', error=None)
        with self.assertRaisesRegex(InlineAlignmentError, 'extraction/cache mismatch'):
            handler.add_translations([stale])
        self.assertEqual(before, etree.tostring(root))

    def test_repeated_real_extraction_does_not_duplicate_footnote_reserves(self):
        root = etree.fromstring(b'<div><p class="TX">Text <em>word</em><sup><a href="notes.xhtml#n">1</a></sup></p></div>')
        handler = ElementHandler(Base.placeholder, Base.separator, 'only')
        handler.set_only_policy({'preserve_inline': True}); handler.load_remove_rules(); handler.load_reserve_rules()
        element = PageElement(root[0]); rows = handler.prepare_original([element])
        first = element.get_content(); self.assertEqual(rows[0][3], first)
        self.assertEqual(1, len(element.reserve_elements))
        paragraph = SimpleNamespace(row=0, original=first, translation=first.replace('Text', '文字').replace('word', '詞'), error=None)
        handler.add_translations([paragraph])
        self.assertEqual(1, len(root[0].findall('.//sup')))
        self.assertEqual('notes.xhtml#n', root[0].find('.//a').get('href'))

    def test_native_list_extraction_retains_list_structure_and_item_ids(self):
        node = etree.fromstring(b'<ul><li id="i1">One <em>item</em></li><li id="i2">Two</li></ul>')
        plan = InlinePlan(node, OnlyPolicy(preserve_inline=True))
        translated = plan.marked_source.replace('One', '一').replace('item', '項').replace('Two', '二')
        validate_inline_translation(plan.marked_source, translated, Base.placeholder)
        restored = etree.fromstring(('<ul>' + plan.restore_xml(translated) + '</ul>').encode())
        self.assertEqual(['li', 'li'], [x.tag for x in restored])
        self.assertEqual(['i1', 'i2'], [x.get('id') for x in restored])
        self.assertEqual('項', restored[0].find('em').text)

    def test_wire_protocol_preserves_model_options_and_config(self):
        data = {'model': 'test-model', 'think': False, 'stream': False,
                'options': {'num_ctx': 131072, 'num_predict': 4096},
                'messages': [{'role': 'system', 'content': 'original instruction'},
                             {'role': 'user', 'content': 'Translate ⟦ET0O⟧text⟦ET0C⟧'}]}
        before = copy.deepcopy(data)
        out = json.loads(prepare_inline_body(json.dumps(data).encode(), '⟦ET0O⟧text⟦ET0C⟧'))
        self.assertEqual(before, data)
        self.assertEqual(before['messages'][1:], out['messages'][1:])
        self.assertEqual('original instruction\n\n' + INLINE_INSTRUCTION, out['messages'][0]['content'])
        for key in ('model', 'think', 'stream', 'options'):
            self.assertEqual(before[key], out[key])
        plain = json.dumps(before).encode()
        self.assertIs(plain, prepare_inline_body(plain, 'ordinary text'))
        with self.assertRaises(InlineAlignmentError):
            prepare_inline_body({'text': '⟦ET0O⟧text⟦ET0C⟧'}, '⟦ET0O⟧text⟦ET0C⟧')

    def test_cache_validator_rejects_lost_parent_zero_width_and_reserves(self):
        source = '⟦ET0O⟧a⟦ET1O⟧b⟦ET1C⟧⟦ET0C⟧⟦ET2O⟧⟦ET2C⟧{{id_00000}}'
        validate_inline_translation(source, source.replace('a', '甲').replace('b', '乙'), Base.placeholder)
        for bad in (source.replace('⟦ET1C⟧', ''), source.replace('⟦ET2C⟧', 'x⟦ET2C⟧'),
                    source.replace('{{id_00000}}', ''), source + '{{id_99999}}',
                    source.replace('⟦ET1O⟧b⟦ET1C⟧', '') + '⟦ET1O⟧b⟦ET1C⟧',
                    source + '⟦ETbroken⟧'):
            with self.subTest(bad=bad), self.assertRaises(InlineAlignmentError):
                validate_inline_translation(source, bad, Base.placeholder)

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_bad_provider_response_retries_before_native_cache_callback(self, sleep):
        engine = Mock(placeholder=Base.placeholder, request_attempt=1, max_error_count=0, merge_enabled=False)
        engine.match_error.return_value = False
        engine.translate.side_effect = ['bad', '⟦ET0O⟧譯文⟦ET0C⟧{{id_00000}}']
        worker = Translation(engine, Glossary(Base.placeholder))
        worker.strict_inline = True
        paragraph = SimpleNamespace(row=4, original='⟦ET0O⟧text⟦ET0C⟧{{id_00000}}', translation=None, error=None)
        worker.translate_paragraph(paragraph)
        self.assertEqual(2, engine.translate.call_count)
        self.assertEqual('⟦ET0O⟧譯文⟦ET0C⟧{{id_00000}}', paragraph.translation)
        self.assertFalse(paragraph.is_cache)

    def test_invalid_cached_translation_uses_official_engine(self):
        engine = Mock(placeholder=Base.placeholder, merge_enabled=False,
                      request_attempt=3)
        engine.translate.return_value = '⟦ET0O⟧譯文⟦ET0C⟧'
        worker = Translation(engine, Glossary(Base.placeholder)); worker.strict_inline = True
        paragraph = SimpleNamespace(row=1, original='⟦ET0O⟧text⟦ET0C⟧', translation='lost tokens', error=None)
        worker.translate_paragraph(paragraph)
        engine.translate.assert_called_once_with(paragraph.original)
        self.assertFalse(paragraph.is_cache)
        engine.reset_mock(); worker.translate_paragraph(paragraph)
        engine.translate.assert_not_called(); self.assertTrue(paragraph.is_cache)

    @patch('calibre_plugins.ebook_translator.lib.element.get_config')
    def test_default_and_legacy_none_enable_v2_only_false_opts_out(self, config):
        for extra in ({}, {'only_typography': None}, {'only_typography': False}):
            for position in ('only', 'below', 'above', 'left', 'right'):
                config.return_value = Configuration({'translation_position': position, **extra})
                handler = get_element_handler(Base.placeholder, Base.separator, 'auto')
                if position == 'only' and extra.get('only_typography') is not False:
                    self.assertTrue(handler.only_policy.preserve_inline)
                    self.assertTrue(handler.only_policy.auto_roles)
                else:
                    self.assertIsNone(handler.only_policy)

    def test_any_missing_paragraph_blocks_output_before_source_mutation(self):
        root = etree.fromstring(b'<div><p class="TX">One</p><p class="TX">Two</p></div>')
        handler = ElementHandler(Base.placeholder, Base.separator, 'only')
        handler.set_only_policy({'preserve_inline': True}); handler.load_remove_rules(); handler.load_reserve_rules()
        handler.prepare_original([PageElement(x) for x in root])
        before = etree.tostring(root)
        paragraphs = [SimpleNamespace(row=0, original='One', translation='一', error=None),
                      SimpleNamespace(row=1, original='Two', translation=None, error='provider failed')]
        with self.assertRaisesRegex(InlineAlignmentError, 'rows: 1'):
            handler.add_translations(paragraphs)
        self.assertEqual(before, etree.tostring(root))

    def test_bodytext_role_respects_note_quote_and_caption_context(self):
        policy = OnlyPolicy(preserve_inline=True, auto_roles=True)
        for context, expect_gap, expect_height in (
                ('div', True, True), ('blockquote', False, True),
                ('aside', False, False), ('section role="doc-endnote"', False, False)):
            tag = context.split()[0]
            root = etree.fromstring(('<%s><p class="EB03BodyTextIndented" style="text-indent:1em">text</p></%s>' % (context, tag)).encode())
            node = root[0]; apply_typography(node, policy)
            self.assertEqual(expect_gap, 'margin-top:0.75em' in node.get('style'))
            self.assertEqual(expect_height, 'line-height:1.7' in node.get('style'))
            self.assertIn('text-indent:1em', node.get('style'))
        for cls in ('EB28InlineCaption', 'note', 'EB15ContentsText'):
            node = etree.fromstring(('<p class="%s">text</p>' % cls).encode()); apply_typography(node, policy)
            self.assertIsNone(node.get('style'))

    def test_provider_usage_has_only_reported_numeric_counters(self):
        class Engine(CustomTranslate):
            request = {'url': 'unused'}
            response = "response['message']['content']"
        engine = Engine()
        for response in ({'message': {'content': '一'}, 'prompt_eval_count': 12, 'eval_count': 3, 'secret': 'do-not-retain'},
                         {'message': {'content': '二'}}):
            engine.get_result(json.dumps(response))
        self.assertEqual({'responses': 2, 'reported_responses': 1, 'input_tokens': 12, 'output_tokens': 3}, engine.get_session_usage())
