import unittest
from unittest.mock import patch
from lxml import etree
from ...lib.only_mode import OnlyPolicy, InlinePlan, InlineAlignmentError, apply_typography
from ...lib.element import PageElement, ElementHandler, ElementHandlerMerge, get_element_handler
from ...lib.config import Configuration


class TestOnlyMode(unittest.TestCase):
    def root(self,markup):return etree.fromstring(markup.encode())

    def test_split_word_around_page_anchor_is_one_semantic_run(self):
        src=self.root('<p>Use <span class="ital">Drosoph</span><a id="page45"/><span class="ital">ila</span> here.</p>')
        plan=InlinePlan(src,OnlyPolicy(preserve_inline=True))
        self.assertIn('Drosophila',plan.marked_source)
        self.assertEqual(2,len(plan.templates))
        out=self.root('<p>'+plan.restore_xml('用⟦ET0O⟧⟦ET0C⟧⟦ET1O⟧果蠅⟦ET1C⟧。')+'</p>')
        self.assertEqual('page45',out.find('a').get('id'))
        self.assertEqual('果蠅',out.find('span').text)
        self.assertEqual('Drosoph',src.find('span').text)

    def test_a2_requires_each_existing_reserve_placeholder_once(self):
        from ...engines.base import Base
        for bad in ('⟦ET0O⟧⟦ET0C⟧',
                    '⟦ET0O⟧{{id_00000}}{{id_00000}}⟦ET0C⟧',
                    '⟦ET0O⟧{{id_00000}}⟦ET0C⟧{{id_99999}}'):
            holder=self.root('<div><p><span class="superscript"><sup><a href="notes.xhtml#n1">1</a></sup></span></p></div>')
            element=PageElement(holder[0]);element.set_position('only');element.set_only_policy(OnlyPolicy(preserve_inline=True));element.set_placeholder(Base.placeholder)
            handler=ElementHandler(Base.placeholder,Base.separator,'only');handler.load_reserve_rules();element.set_reserve_pattern(handler.reserve_pattern)
            element.get_content();element.set_original_color('red');before=etree.tostring(holder)
            with self.subTest(bad=bad),self.assertRaises(InlineAlignmentError):element.add_translation(bad)
            self.assertEqual(before,etree.tostring(holder))

    def test_typography_is_opt_in_and_preserves_roles(self):
        p=self.root('<p class="TX" style="text-indent:20pt;color:red">text</p>')
        before=etree.tostring(p);apply_typography(p,None);self.assertEqual(before,etree.tostring(p))
        apply_typography(p,OnlyPolicy());self.assertIn('text-indent:20pt',p.get('style'));self.assertIn('line-height:1.7',p.get('style'));self.assertIn('margin-top:0.75em',p.get('style'))
        for cls in ('CN','CT','H1','CPT','FN'):
            p=self.root('<p class="%s" style="margin-top:2em">text</p>'%cls);before=etree.tostring(p);apply_typography(p,OnlyPolicy());self.assertEqual(before,etree.tostring(p))
        p=self.root('<p class="COTX" style="margin-top:2em">first</p>');apply_typography(p,OnlyPolicy());self.assertIn('margin-top:2em',p.get('style'));self.assertNotIn('margin-top:0.75em',p.get('style'))

    def test_policy_validates_parameters(self):
        for kw in ({'line_height':float('nan')},{'line_height':True},{'line_height':3},{'paragraph_gap':-1},{'text_align':'right'},{'body_classes':['bad class']}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):OnlyPolicy(**kw)

    def test_pair_markers_restore_structure_and_source_attributes(self):
        src=self.root('<p>Read <em class="ITAL">carefully</em> and <a href="notes.xhtml#n1" id="r1">note</a>.</p>');plan=InlinePlan(src,OnlyPolicy(preserve_inline=True))
        self.assertIn('⟦ET0O⟧carefully⟦ET0C⟧',plan.marked_source)
        out=self.root('<p>'+plan.restore_xml('請⟦ET0O⟧仔細⟦ET0C⟧閱讀⟦ET1O⟧註解⟦ET1C⟧。')+'</p>')
        self.assertEqual('ITAL',out.find('em').get('class'));self.assertEqual('notes.xhtml#n1',out.find('a').get('href'));self.assertEqual('r1',out.find('a').get('id'))

    def test_missing_duplicate_crossed_and_unknown_markers_fail(self):
        p=InlinePlan(self.root('<p><em>one <strong>two</strong></em></p>'),OnlyPolicy(preserve_inline=True))
        for value in ('plain','⟦ET0O⟧x⟦ET0O⟧','⟦ET0O⟧x⟦ET1O⟧y⟦ET0C⟧⟦ET1C⟧','⟦ET9O⟧x⟦ET9C⟧','⟦ET0O⟧x⟦ET1O⟧y⟦ET1C⟧'):
            with self.subTest(value=value),self.assertRaises(InlineAlignmentError):p.restore_xml(value)

    def test_reordered_siblings_are_allowed_but_nesting_not(self):
        p=InlinePlan(self.root('<p><em>first</em><strong>second</strong></p>'),OnlyPolicy(preserve_inline=True))
        out=self.root('<p>'+p.restore_xml('⟦ET1O⟧乙⟦ET1C⟧、⟦ET0O⟧甲⟦ET0C⟧')+'</p>');self.assertEqual(['strong','em'],[x.tag for x in out])
        with self.assertRaises(InlineAlignmentError):p.restore_xml('⟦ET0O⟧⟦ET1O⟧乙⟦ET1C⟧甲⟦ET0C⟧')

    def test_opening_run_is_lexical_then_target_grapheme(self):
        p=InlinePlan(self.root('<p><span class="_idGenDropcap-1">I</span><span class="first-line">n many airports</span>, text.</p>'),OnlyPolicy(preserve_inline=True))
        self.assertIn('In many airports',p.marked_source);out=self.root('<p>'+p.restore_xml('⟦ET0O⟧在許多機場⟦ET0C⟧，文字。')+'</p>');run=out.find('span');self.assertEqual('first-line',run.get('class'));self.assertEqual('在',run.find('span').text);self.assertEqual('_idGenDropcap-1',run.find('span').get('class'));self.assertEqual('在許多機場，文字。',''.join(out.itertext()))

    def test_leading_heading_anchor_retained(self):
        p=InlinePlan(self.root('<p><a id="chapter1"/><a href="toc.xhtml#ch1">ONE</a></p>'),OnlyPolicy(preserve_inline=True));out=self.root('<p>'+p.restore_xml('⟦ET0O⟧第一章⟦ET0C⟧')+'</p>');self.assertEqual('chapter1',out[0].get('id'));self.assertEqual('toc.xhtml#ch1',out[1].get('href'))

    def test_escaped_translation_cannot_add_active_html(self):
        p=InlinePlan(self.root('<p><em>word</em></p>'),OnlyPolicy(preserve_inline=True));out=self.root('<p>'+p.restore_xml('⟦ET0O⟧&lt;script&gt;bad&lt;/script&gt;⟦ET0C⟧')+'</p>');self.assertIsNone(out.find('.//script'));self.assertIn('<script>',out.find('em').text)

    def test_nonleading_page_anchor_is_explicit_zero_width_marker(self):
        p=InlinePlan(self.root('<p><em>first</em><a id="page2"/> second</p>'),OnlyPolicy(preserve_inline=True))
        out=self.root('<p>'+p.restore_xml('⟦ET0O⟧首段⟦ET0C⟧⟦ET1O⟧⟦ET1C⟧次段')+'</p>');self.assertEqual(['em','a'],[x.tag for x in out]);self.assertEqual('page2',out[1].get('id'));self.assertEqual('次段',out[1].tail)
        with self.assertRaises(InlineAlignmentError):p.restore_xml('⟦ET0O⟧首段⟦ET0C⟧⟦ET1O⟧不應填入⟦ET1C⟧次段')

    def test_source_marker_and_unsafe_link_rejected(self):
        for markup in ('<p>⟦ET0O⟧</p>','<p><a href="javascript:alert(1)">x</a></p>','<p><a id="page" href="javascript:alert(1)"/></p>','<p><foo>x</foo></p>'):
            with self.subTest(markup=markup),self.assertRaises(InlineAlignmentError):InlinePlan(self.root(markup),OnlyPolicy(preserve_inline=True))

    def test_real_only_branch_failure_keeps_original(self):
        root=self.root('<div><p class="TX">An <em>important</em> fact.</p></div>');el=PageElement(root[0]);el.set_position('only');el.set_only_policy({'preserve_inline':True});el.get_content();before=etree.tostring(root)
        with self.assertRaises(InlineAlignmentError):el.add_translation('缺少標記')
        self.assertEqual(before,etree.tostring(root));el.add_translation('一項⟦ET0O⟧重要⟦ET0C⟧事實。');self.assertEqual('重要',root[0].find('em').text);self.assertIn('line-height:1.7',root[0].get('style'))

    def test_merge_mode_semantics_explicitly_rejected(self):
        merged=ElementHandlerMerge(('{{id_{}}}',r'{{id_{}}}'),'\n\n','only')
        with self.assertRaises(InlineAlignmentError):merged.set_only_policy({'preserve_inline':True})

    @patch('calibre_plugins.ebook_translator.lib.element.get_config')
    def test_factory_applies_policy_only_to_only_mode(self,config):
        for position in ('below','above','only'):
            config.return_value=Configuration({'translation_position':position,'only_typography':{'preserve_inline':True}})
            handler=get_element_handler(('{{id_{}}}',r'{{id_{}}}'),'\n\n','auto')
            if position=='only':self.assertTrue(handler.only_policy.preserve_inline)
            else:self.assertIsNone(handler.only_policy)

    @patch('calibre_plugins.ebook_translator.lib.element.get_config')
    def test_factory_source_prepare_to_only_render(self,config):
        config.return_value=Configuration({'translation_position':'only','only_typography':{'preserve_inline':True}})
        handler=get_element_handler(('{{id_{}}}',r'{{id_{}}}'),'\n\n','auto')
        root=self.root('<div><p class="TX">An <strong>important</strong> fact.</p></div>');element=PageElement(root[0]);items=handler.prepare_original([element]);self.assertIn('⟦ET0O⟧',items[0][3]);element.add_translation('一項⟦ET0O⟧重要⟦ET0C⟧事實。');self.assertEqual('重要',root[0].find('strong').text)
