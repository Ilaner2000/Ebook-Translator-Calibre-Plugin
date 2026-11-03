import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile
from ...lib.chapter_trial import render_cached_blocks, build_epub, parse, link_report
from ...lib.only_mode import OnlyPolicy, InlineAlignmentError

HTML=b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Test</title></head><body><p id="ji_93" class="tx">old</p></body></html>'
NOTE=b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Notes</title></head><body><p id="ji_965">notes</p></body></html>'


class TestChapterTrial(unittest.TestCase):
    def row(self):
        return {'id':1,'raw':'<p id="ji_93" class="tx">a <em>word</em></p>',
                'original':'a word','translation':'一個詞','ignored':False}

    def test_cache_reuse_renders_only_and_does_not_modify_input(self):
        row=self.row();root,ids=render_cached_blocks(HTML,[row],OnlyPolicy())
        self.assertEqual(['ji_93'],ids)
        self.assertEqual('一個詞',root.xpath('//*[@id="ji_93"]')[0].text)
        self.assertIsNone(root.xpath('//*[@id="ji_93"]')[0].get('lang'))
        self.assertEqual('old',parse(HTML).xpath('//*[@id="ji_93"]')[0].text)
        row['original']='wrong source'
        with self.assertRaises(ValueError):render_cached_blocks(HTML,[row],OnlyPolicy())

    def test_bad_marked_target_and_missing_identity_fail_before_output(self):
        with self.assertRaises(InlineAlignmentError):
            render_cached_blocks(HTML,[self.row()],OnlyPolicy(preserve_inline=True),{1:'missing markers'})
        with self.assertRaises(ValueError):
            render_cached_blocks(HTML.replace(b'ji_93',b'other'),[self.row()],OnlyPolicy())

    def test_link_validator_reports_broken_fragment(self):
        good=HTML.replace(b'old',b'<a href="notes.xhtml#ji_965">1</a>')
        bad=good.replace(b'#ji_965',b'#missing')
        self.assertEqual([],link_report({'chapter.xhtml':good,'notes.xhtml':NOTE})['invalid'])
        self.assertEqual(1,len(link_report({'chapter.xhtml':bad,'notes.xhtml':NOTE})['invalid']))

    def test_package_is_new_single_chapter_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'input.epub';target=Path(directory)/'trial.epub'
            with ZipFile(source,'x') as z:
                z.writestr('META-INF/container.xml','<container><rootfile full-path="old.opf"/></container>')
                z.writestr('old.opf','<package><manifest><item href="style.css" media-type="text/css"/></manifest></package>')
                z.writestr('style.css','p{line-height:1.2}')
                z.writestr('other.xhtml','unselected chapter')
            before=source.read_bytes()
            result=build_epub(source,target,'chapter.xhtml',parse(HTML),'notes.xhtml',parse(NOTE),'Trial')
            self.assertEqual([],result['links']['invalid'])
            with ZipFile(target) as z:
                self.assertNotIn('other.xhtml',z.namelist())
                self.assertEqual('mimetype',z.namelist()[0])
                self.assertEqual(1,len(parse(z.read('content.opf')).xpath('//*[local-name()="itemref" and not(@linear="no")]')))
            trial=target.read_bytes()
            with self.assertRaises(FileExistsError):build_epub(source,target,'chapter.xhtml',parse(HTML),'notes.xhtml',parse(NOTE),'Trial')
            with self.assertRaises(ValueError):build_epub(source,source,'chapter.xhtml',parse(HTML),'notes.xhtml',parse(NOTE),'Trial')
            self.assertEqual(trial,target.read_bytes());self.assertEqual(before,source.read_bytes())
