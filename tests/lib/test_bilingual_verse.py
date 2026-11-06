import unittest
from lxml import etree
from ...engines.base import Base
from ...lib.element import ElementHandler, PageElement
from ...lib.cache import Paragraph


class TestBilingualVerse(unittest.TestCase):
    def render(self, body, position='below', gap=1):
        root=etree.fromstring(('<body>'+body+'</body>').encode())
        sources=list(root)
        before=[etree.tostring(n) for n in sources]
        handler=ElementHandler(Base.placeholder,Base.separator,position)
        handler.set_vertical_gap(gap)
        handler.load_remove_rules();handler.load_reserve_rules()
        paragraphs=[Paragraph(*row) for row in handler.prepare_original([PageElement(n) for n in sources])]
        flags=[e.bilingual_verse_line for e in handler.elements.values()]
        for p in paragraphs:p.translation='中文'+str(p.id)
        handler.add_translations(paragraphs)
        return root,sources,before,flags

    def test_short_quote_lines_keep_source_and_target_in_one_pair(self):
        body=''.join('<blockquote class="quote"><p class="verse" id="line%d">Line %d</p></blockquote>'%(i,i) for i in range(4))
        root,sources,before,flags=self.render(body)
        self.assertEqual([True]*4,flags)
        self.assertEqual(8,len(root))
        self.assertEqual([etree.tostring(n) for n in sources],before)
        self.assertEqual(['line0','line1','line2','line3'],root.xpath('.//@id'))
        for i in range(4):
            target=root[i*2+1]
            self.assertEqual('padding-bottom:1em',target.get('style'))
            self.assertEqual('quote',target.get('class'))
            self.assertEqual('p',target[0].tag)
            self.assertEqual('verse',target[0].get('class'))
            self.assertEqual('margin-top:0;margin-bottom:0',target[0].get('style'))
            self.assertEqual('中文'+str(i),target[0].text)

    def test_long_prose_quotations_keep_legacy_gap_and_structure(self):
        body=''.join('<blockquote class="quote"><p>'+('Long prose sentence. '*20)+'</p></blockquote>' for _ in range(3))
        root,_,_,flags=self.render(body)
        self.assertEqual([False]*3,flags)
        for target in list(root)[1::2]:
            self.assertEqual('padding-top:1em',target.get('style'))
            self.assertEqual(0,len(target))

    def test_ordinary_paragraphs_and_isolated_quotes_do_not_enter_verse_mode(self):
        for body in ('<p>Short one</p><p>Short two</p><p>Short three</p>',
                     '<blockquote><p>One</p></blockquote><blockquote><p>Two</p></blockquote>'):
            with self.subTest(body=body):
                root,_,_,flags=self.render(body)
                self.assertFalse(any(flags))
                self.assertTrue(all(n.get('style')=='padding-top:1em' for n in list(root)[1::2]))

    def test_only_above_and_side_outputs_do_not_enter_verse_mode(self):
        body='<blockquote><p>One</p></blockquote>'*3
        for position in ('only','above','left','right'):
            with self.subTest(position=position):
                _,_,_,flags=self.render(body,position)
                self.assertFalse(any(flags))

    def test_zero_gap_and_existing_paragraph_inline_layout_are_preserved(self):
        body='<blockquote><p class="verse" style="text-indent:-2em;margin-left:2em" lang="en" id="x%d">Line</p></blockquote>'
        root,_,_,flags=self.render(''.join(body%i for i in range(3)),gap=0)
        self.assertTrue(all(flags))
        for target in list(root)[1::2]:
            self.assertNotIn('padding-',target.get('style',''))
            self.assertEqual('text-indent:-2em;margin-left:2em;margin-top:0;margin-bottom:0',target[0].get('style'))
            self.assertIsNone(target[0].get('lang'));self.assertIsNone(target[0].get('id'))
