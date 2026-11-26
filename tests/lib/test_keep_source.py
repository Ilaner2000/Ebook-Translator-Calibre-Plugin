import copy
import json
import unittest
from unittest.mock import Mock,patch
from lxml import etree

from ...engines.base import Base
from ...lib.cache import Paragraph
from ...lib.config import Configuration
from ...lib.element import ElementHandler,PageElement
from ...lib.exception import TranslationFailed
from ...lib.only_mode import InlineAlignmentError
from . import test_inline_fallback as fixtures
from . import test_conversion as conversion_fixtures


class TestKeepSource(unittest.TestCase):
    def fixture(self,duplicate=False,position='only'):
        body='<p id="bad">N<img src="vowel.png"/>g <em>word</em><a href="n.xhtml#n">note</a></p>'
        good=body.replace('id="bad"','id="good"') if duplicate else '<p id="good">Hello</p>'
        root=etree.fromstring(('<div>'+body+good+'</div>').encode())
        handler=self.handler(root,position)
        paragraphs=[Paragraph(*row) for row in handler.originals]
        for p in paragraphs:p.row=p.id
        return root,handler,paragraphs

    def handler(self,root,position):
        handler=ElementHandler(Base.placeholder,Base.separator,position)
        handler.set_only_policy({'preserve_inline':True} if position=='only' else None)
        handler.inline_cache_to_plain=position=='below'
        handler.load_remove_rules();handler.load_reserve_rules()
        handler.prepare_original([PageElement(node) for node in root])
        return handler

    def mark_retained(self,p):
        p.keep_source=True;p.failure_reason='placeholder_unrecoverable'
        p.error='terminal protocol failure';p.translation=None

    def test_default_off_blocks_even_if_paragraph_claims_keep_source(self):
        self.assertFalse(Configuration().get('v2_unrecoverable_keep_source'))
        root,handler,paragraphs=self.fixture()
        self.mark_retained(paragraphs[0]);paragraphs[1].translation='成功'
        before=etree.tostring(root)
        with self.assertRaises(InlineAlignmentError):handler.add_translations(paragraphs)
        self.assertEqual(before,etree.tostring(root))

    def test_enabled_keeps_exact_source_image_and_translates_other_dom(self):
        root,handler,paragraphs=self.fixture();source=root[0];before=etree.tostring(source)
        handler.keep_unrecoverable_source=True
        self.mark_retained(paragraphs[0]);paragraphs[1].translation='成功'
        handler.add_translations(paragraphs)
        self.assertIs(source,root[0]);self.assertEqual(before,etree.tostring(source))
        self.assertEqual('成功',root[1].text)
        self.assertEqual(['vowel.png'],root.xpath('.//img/@src'))
        self.assertIsNone(paragraphs[0].translation)

    def test_bilingual_kept_paragraph_has_no_duplicate(self):
        root,only,paragraphs=self.fixture()
        paired=copy.deepcopy(root);handler=self.handler(paired,'below')
        handler.keep_unrecoverable_source=True
        self.mark_retained(paragraphs[0]);paragraphs[1].translation='成功'
        before=etree.tostring(paired[0])
        handler.add_translations(paragraphs)
        self.assertEqual(3,len(paired))
        self.assertEqual(before,etree.tostring(paired[0]))
        self.assertEqual(1,len(paired.xpath('.//img')))
        self.assertEqual('Hello',paired[1].text);self.assertEqual('成功',paired[2].text)

    def test_duplicate_text_row_does_not_inherit_another_rows_keep_decision(self):
        for kept in [0,1]:
            with self.subTest(kept=kept):
                root,handler,paragraphs=self.fixture(duplicate=True)
                self.assertEqual(paragraphs[0].original,paragraphs[1].original)
                handler.keep_unrecoverable_source=True
                self.mark_retained(paragraphs[kept]);before=etree.tostring(root[kept])
                other=paragraphs[1-kept]
                other.translation=other.original.replace('word','詞')
                handler.add_translations(paragraphs)
                self.assertEqual(before,etree.tostring(root[kept]))
                self.assertIn('詞',''.join(root[1-kept].itertext()))
                self.assertEqual(2,len(root.xpath('.//img')))

    def test_duplicate_degraded_row_keeps_own_validation_and_render_status(self):
        for kept in [0,1]:
            with self.subTest(kept=kept):
                root,handler,paragraphs=self.fixture(duplicate=True)
                handler.keep_unrecoverable_source=True
                self.mark_retained(paragraphs[kept])
                source=root[kept];before=etree.tostring(source)
                other=paragraphs[1-kept]
                other.translation='target{{id_00000}}'
                other.degraded=True
                target_element=handler.elements[1-kept]
                handler.add_translations(paragraphs)
                self.assertEqual(before,etree.tostring(source))
                self.assertTrue(target_element.degraded)
                self.assertIn('target',''.join(root[1-kept].itertext()))
                self.assertEqual(2,len(root.xpath('.//img')))

    def test_retained_source_skips_original_color_for_ignored_descendant(self):
        for position in ['only','below']:
            with self.subTest(position=position):
                root,handler,paragraphs=self.fixture(position=position)
                handler.keep_unrecoverable_source=True
                self.mark_retained(paragraphs[0]);paragraphs[1].translation='成功'
                descendant=PageElement(root[0].find('em'),ignored=True)
                descendant.set_position(position)
                descendant.set_original_color('red')
                handler.elements[2]=descendant
                before=etree.tostring(root[0])
                handler.add_translations(paragraphs)
                self.assertEqual(before,etree.tostring(root[0]))

    def test_stale_raw_source_cannot_authorize_output(self):
        root,handler,paragraphs=self.fixture();handler.keep_unrecoverable_source=True
        self.mark_retained(paragraphs[0]);paragraphs[0].raw='<p>wrong</p>'
        paragraphs[1].translation='成功'
        before=etree.tostring(root)
        with self.assertRaises(InlineAlignmentError):handler.add_translations(paragraphs)
        self.assertEqual(before,etree.tostring(root))

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_job_summary_and_second_pass_keep_decision_do_not_store_english(self,_sleep):
        from . import test_placeholder_retry as retry_fixtures
        worker,engine=retry_fixtures.TestBoundedPlaceholderRetry().worker(['丟失圖片']*3)
        worker.keep_unrecoverable_source=True
        root,handler,paragraphs=self.fixture()
        paragraphs[1].translation='成功'
        originals=[(p.md5,p.original,p.raw) for p in paragraphs]
        saved=[];logs=[]
        worker.set_callback(lambda p:saved.append((p.id,p.translation)))
        worker.set_logging(lambda message,error=False:logs.append(message))
        from ...lib.cache import Paragraph
        filler = [Paragraph(i+10, str(i), '<p>Good</p>', 'Good', translation='成功') for i in range(8)]
        worker.handle(paragraphs + filler)
        self.assertEqual(3,engine.translate.call_count)
        self.assertIsNone(paragraphs[0].translation)
        self.assertIn((0,None),saved)
        expected=[{'id':0,'page':None,'source_preview':paragraphs[0].original[:60],
                   'reason':'placeholder_unrecoverable'}]
        self.assertEqual(expected,worker.retained_source_manifest())
        summary=json.loads(next(s.split(': ',1)[1] for s in logs if s.startswith('source retained summary:')))
        self.assertEqual(expected,summary['paragraphs'])
        restored=Paragraph(0,paragraphs[0].md5,paragraphs[0].raw,paragraphs[0].original)
        worker.translate_paragraph(restored)
        self.assertTrue(restored.keep_source);self.assertIsNone(restored.translation)
        self.assertEqual(3,engine.translate.call_count)
        # The paired pass reports this as a successful source-only render, so
        # error is None even though translation remains None.
        restored.error=None
        worker.process_translation(restored)
        self.assertTrue(any(s.startswith('Source retained (untranslated):') for s in logs))
        self.assertEqual(originals,[(p.md5,p.original,p.raw) for p in paragraphs])
        restored.md5='changed'
        self.assertFalse(worker.retained_source(restored))

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_opt_in_never_uses_corrupt_quality_target_but_retains_source(self,_sleep):
        from . import test_placeholder_retry as retry_fixtures
        worker,engine=retry_fixtures.TestBoundedPlaceholderRetry().worker(['<0x80>']*3)
        worker.keep_unrecoverable_source=True
        root,handler,paragraphs=self.fixture()
        with self.assertRaises(TranslationFailed):worker.translate_paragraph(paragraphs[0])
        self.assertTrue(paragraphs[0].keep_source)
        self.assertIsNone(paragraphs[0].translation)
        self.assertEqual('quality_exhausted',paragraphs[0].failure_reason)
        self.assertEqual(0,worker.abort_count)

    @patch('calibre_plugins.ebook_translator.lib.conversion.file_sha256',return_value='hash')
    def test_manifest_lists_retained_rows_and_job_result_reaches_store(self,_sha):
        fixture=conversion_fixtures.TestConversionWorker();fixture.setUp();fixture.configure_variant_ebook()
        worker=fixture.worker;worker.config['translate_engine']='Offline'
        rows=[{'id':42,'page':'chapter.xhtml','source_preview':'N{{id_00000}}g',
               'reason':'placeholder_unrecoverable'}]
        payload=dict(fixture.variant_payload('bundle_data'),
                     v2_unrecoverable_keep_source=True,kept_source_paragraphs=rows)
        manifest=worker._variant_manifest(fixture.ebook,payload,'uuid',{'translation':7,'bilingual':None})
        self.assertTrue(manifest['v2_unrecoverable_keep_source'])
        self.assertEqual(rows,manifest['kept_source_paragraphs'])
        job=Mock();job.failed=False;job.description='fixture';job.result={
            'v2_unrecoverable_keep_source':True,'kept_source_paragraphs':rows}
        worker.working_jobs={job:(fixture.ebook,fixture.variant_payload('bundle_data'))}
        worker.config['show_notification']=False
        with patch.object(worker,'_store_variants',return_value=('/output.epub','title',0)) as store:
            worker.translate_done(job)
        self.assertEqual(rows,store.call_args.args[1]['kept_source_paragraphs'])
        self.assertTrue(store.call_args.args[1]['v2_unrecoverable_keep_source'])


class TestPairedRetainedNavigation(unittest.TestCase):
    """Real S601 row 333 plus the preceding Unicode-spaced NCX title."""
    def fixture(self, position='below', navigation=True):
        from calibre.ebooks.oeb.base import TOC
        from ...lib.element import TocElement
        from ...engines.base import Base
        markup='<div><h1 class="sec1"><b class="calibre6">Meditation and Formal<br class="calibre7"/> Practice</b></h1><p>Hello</p></div>'
        root=etree.fromstring(markup.encode())
        h=ElementHandler(Base.placeholder,Base.separator,position)
        h.set_only_policy({'preserve_inline':True} if position=='only' else None)
        h.inline_cache_to_plain=position=='below';h.keep_unrecoverable_source=True
        h.load_remove_rules();h.load_reserve_rules()
        toc=TOC();toc.add("IV\u2003Meditation and Formal Practice",'Ja_12_ch11.html','pt4')
        elements=([TocElement(toc.nodes[0],'toc.ncx')] if navigation else [])+[PageElement(n,'Ja_12_ch11') for n in root]
        h.prepare_original(elements)
        return root,toc,h

    def paragraphs(self):
        root,toc,only=self.fixture('only')
        rows=[Paragraph(*row) for row in only.originals]
        rows[0].translation='IV\u2003冥想與正式練習'
        rows[1].keep_source=True;rows[1].failure_reason='placeholder_unrecoverable'
        rows[2].translation='成功'
        self.assertEqual('⟦ET0O⟧Meditation and Formal{{id_00000}} Practice⟦ET0C⟧',rows[1].original)
        return rows

    def test_real_heading_and_unicode_toc_render_in_both_variants_for_all_retention_reasons(self):
        from ...lib.cache import SOURCE_RETENTION_OUTPUT_REASONS
        for position in ['only','below']:
            for reason in SOURCE_RETENTION_OUTPUT_REASONS:
                with self.subTest(position=position,reason=reason):
                    root,toc,h=self.fixture(position);rows=self.paragraphs()
                    rows[1].failure_reason=reason;before=etree.tostring(root[0])
                    h.add_translations(rows)
                    self.assertEqual(before,etree.tostring(root[0]))
                    self.assertEqual(1,len(root.xpath('.//br')))
                    self.assertEqual(3 if position=='below' else 2,len(root))
                    self.assertIn('成功',''.join(root.itertext()))
                    self.assertEqual("IV\u2003Meditation and Formal Practice IV\u2003冥想與正式練習" if position=='below' else 'IV\u2003冥想與正式練習',toc.nodes[0].title)
                    self.assertIsNone(rows[1].translation)

    def test_unicode_toc_is_rendered_when_retention_option_is_off(self):
        root,toc,h=self.fixture();rows=self.paragraphs();h.keep_unrecoverable_source=False
        rows[1].keep_source=False;rows[1].translation='⟦ET0O⟧冥想{{id_00000}}練習⟦ET0C⟧'
        h.add_translations(rows)
        self.assertIn('冥想與正式練習',toc.nodes[0].title)

    def test_missing_nonretained_target_still_blocks_before_dom_mutation(self):
        root,toc,h=self.fixture();rows=self.paragraphs();rows[2].translation=None
        before=etree.tostring(root);title=toc.nodes[0].title
        with self.assertRaises(InlineAlignmentError):h.add_translations(rows)
        self.assertEqual(before,etree.tostring(root));self.assertEqual(title,toc.nodes[0].title)

    def test_wrong_raw_retention_cannot_authorize_real_heading(self):
        root,toc,h=self.fixture();rows=self.paragraphs();rows[1].raw='<h1>stale</h1>'
        before=etree.tostring(root)
        with self.assertRaises(InlineAlignmentError):h.add_translations(rows)
        self.assertEqual(before,etree.tostring(root))

    def test_metadata_and_toc_keep_exact_whitespace_identity(self):
        from ...lib.element import MetadataElement,TocElement
        from calibre.ebooks.oeb.base import TOC,Metadata
        from ...engines.base import Base
        from types import SimpleNamespace
        metadata=SimpleNamespace(title=[SimpleNamespace(content='A\u2003Title')])
        toc=TOC();toc.add('A\u2003Title','c.xhtml','c');toc.add('A Title','d.xhtml','d')
        h=ElementHandler(Base.placeholder,Base.separator,'below');h.inline_cache_to_plain=True
        h.keep_unrecoverable_source=True;h.load_remove_rules();h.load_reserve_rules()
        h.prepare_original([MetadataElement(metadata.title[0],'content.opf'),TocElement(toc.nodes[0],'toc.ncx'),TocElement(toc.nodes[1],'toc.ncx')])
        rows=[Paragraph(*r) for r in h.originals]
        for row,target in zip(rows,['甲','乙','丙']):row.translation=target
        h.add_translations(rows)
        self.assertEqual('A\u2003Title 甲',metadata.title[0].content)
        self.assertEqual(['A\u2003Title 乙','A Title 丙'],[n.title for n in toc.nodes])
