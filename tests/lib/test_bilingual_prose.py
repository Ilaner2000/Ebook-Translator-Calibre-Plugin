import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree
from calibre.ebooks.conversion.plumber import Plumber
from calibre.utils.logging import Log

from ...engines.base import Base
from ...lib.cache import Paragraph
from ...lib.conversion import temporary_method_binding
from ...lib.element import ElementHandler, get_page_elements, apply_bilingual_prose_spacing
from ...lib.exception import ConversionAbort


class TestBilingualProse(unittest.TestCase):
    def render(self, position='below', gap=1, merge=0):
        css = ('p {margin:0;padding:0} .margin {margin-bottom:1em} '
               '.nextmargin {margin-top:.5em} .padding {padding-bottom:.5em} '
               '.nextpadding {padding-top:.5em} #positional > p:nth-child(2) {margin-bottom:1em}')
        cases = [('zero', '', '', ''), ('bottom', 'margin', '', ''),
                 ('top', '', 'nextmargin', ''), ('padding', 'padding', '', ''),
                 ('nextpadding', '', 'nextpadding', ''),
                 ('inline', 'margin', '', 'style="margin-bottom:0"'),
                 ('break', '', '', ''), ('positional', '', '', '')]
        body = ''.join('<div id="wrap-%s"><p id="%s" class="%s" %s>English paragraph%s</p>'
                       '<p class="%s">Following English paragraph</p></div>' %
                       (key, key, source, inline, '<br/>two lines' if key == 'break' else '', following)
                       for key, source, following, inline in cases)
        body = body.replace('id="wrap-positional"', 'id="positional"').replace('id="positional" class=', 'id="position-source" class=')
        result = {}
        with tempfile.TemporaryDirectory(prefix='bilingual-prose-') as folder:
            path = Path(folder)
            with zipfile.ZipFile(path/'input.epub', 'w') as z:
                z.writestr('mimetype', 'application/epub+zip')
                z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
                z.writestr('content.opf', '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">spacing-test</dc:identifier><dc:title>Spacing</dc:title><dc:language>en</dc:language></metadata><manifest><item id="p" href="chapter.xhtml" media-type="application/xhtml+xml"/><item id="s" href="style.css" media-type="text/css"/></manifest><spine><itemref idref="p"/></spine></package>')
                z.writestr('style.css', css)
                z.writestr('chapter.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Spacing</title><link rel="stylesheet" href="style.css" type="text/css"/></head><body>'+body+'</body></html>')
            logger = Log(); logger.outputs = []
            plumber = Plumber(str(path/'input.epub'), str(path/'output.epub'), log=logger)
            def convert(self, oeb, output_path, input_plugin, opts, log):
                for item in oeb.manifest.items:
                    if isinstance(item.data, etree._Element):
                        head = item.data.find('{http://www.w3.org/1999/xhtml}head')
                        if head is not None:
                            style = etree.SubElement(head, '{http://www.w3.org/1999/xhtml}style', type='text/css')
                            style.text = '#position-source + p[dir] {margin-bottom:1em}'
                elements = list(get_page_elements(oeb.manifest.items))
                before = {item.id: etree.tostring(item.data) if isinstance(item.data, etree._Element) else item.data.cssText for item in oeb.manifest.items if isinstance(item.data, etree._Element) or item.media_type == 'text/css'}
                handler = ElementHandler(Base.placeholder, Base.separator, position)
                handler.set_vertical_gap(gap); handler.set_merge_length(merge)
                handler.load_remove_rules(); handler.load_reserve_rules()
                originals = {e.element: etree.tostring(e.element) for e in elements}
                paragraphs = [Paragraph(*row) for row in handler.prepare_original(elements)]
                for paragraph in paragraphs: paragraph.translation = '中文譯文'
                handler.add_translations(paragraphs)
                apply_bilingual_prose_spacing(elements, oeb, opts, handler)
                result['source_unchanged'] = all(etree.tostring(node) == raw for node, raw in originals.items())
                result['css_unchanged'] = all(item.data.cssText == before[item.id] for item in oeb.manifest.items if item.media_type == 'text/css')
                result['flags'] = {e.element.get('id'): e.bilingual_prose_gap for e in elements if e.element.get('id')}
                result['target_styles'] = {e.element.get('id'): e.element.getnext().get('style', '') for e in elements if e.element.get('id') and e.element.getnext() is not None}
                raise ConversionAbort()
            try:
                with temporary_method_binding(plumber.output_plugin, 'convert', convert): plumber.run()
            except ConversionAbort: pass
        return result

    def test_real_epub_css_cascade_only_fills_zero_boundaries(self):
        result = self.render()
        self.assertTrue(result['source_unchanged'])
        self.assertTrue(result['css_unchanged'])
        self.assertEqual({'zero':True, 'bottom':False, 'top':False, 'padding':False,
                          'nextpadding':False, 'inline':True, 'break':False, 'position-source':False}, result['flags'])
        for key in ('zero', 'inline'):
            self.assertIn('padding-top:1em;padding-bottom:1em', result['target_styles'][key])
        for key in ('bottom', 'top', 'padding', 'nextpadding', 'break', 'position-source'):
            self.assertNotIn('padding-bottom:1em', result['target_styles'][key])

    def test_zero_gap_and_merged_paragraphs_keep_legacy_spacing(self):
        for options in ({'gap':0}, {'merge':500}):
            with self.subTest(options=options):
                result = self.render(**options)
                self.assertFalse(any(result['flags'].values()))

    def test_other_layouts_do_not_acquire_prose_flags(self):
        for position in ('only', 'above', 'left', 'right'):
            with self.subTest(position=position):
                self.assertFalse(any(self.render(position=position)['flags'].values()))
