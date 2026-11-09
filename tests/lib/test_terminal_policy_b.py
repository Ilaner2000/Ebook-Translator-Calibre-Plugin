"""AY B: bounded row fallback, fatal exclusions, and visible 10% book cap."""
import json,unittest
from unittest.mock import patch
from lxml import etree
from ...lib.cache import Paragraph,TranslationCache
from ...lib.exception import (ParagraphTerminalFailed,ParagraphProtocolFailed,TranslationFailed,TranslationCanceled,UnexpectedResult,UnsupportedModel)
from . import test_placeholder_retry as retry_mod
from . import test_inline_fallback as native_mod
from . import test_keep_source as dom_mod

Retry = lambda: retry_mod.TestBoundedPlaceholderRetry()
Native = lambda: native_mod.TestDegradedNativeCache()
DOM = lambda: dom_mod.TestKeepSource()

class TestTerminalPolicyB(unittest.TestCase):
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_bounded_quality_and_response_failure_are_opt_in_only(self,_sleep):
  for enabled in [False,True]:
   for value,reason in [('<0x80>','quality_exhausted'),(UnexpectedResult('response parse; transport timeout'),'provider_response_exhausted')]:
    with self.subTest(enabled=enabled,reason=reason):
     worker,engine=Retry().worker([value]*3);engine.request_attempt=999;worker.keep_unrecoverable_source=enabled
     p=Retry().paragraph('CHAPTER XV')
     with self.assertRaises(TranslationFailed) as caught:worker.translate_paragraph(p)
     self.assertEqual(3,engine.translate.call_count);self.assertIsNone(p.translation)
     self.assertEqual(enabled,p.keep_source)
     if enabled:self.assertIsInstance(caught.exception,ParagraphTerminalFailed);self.assertEqual(reason,p.failure_reason)
     else:self.assertNotIsInstance(caught.exception,ParagraphTerminalFailed)
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_auth_fatal_and_unknown_exceptions_never_authorize_retention(self,_sleep):
  for error in [UnexpectedResult('401 Unauthorized'),UnexpectedResult('403 Forbidden'),UnexpectedResult('404 model not found'),UnsupportedModel('unsupported model'),MemoryError('memory exhausted'),RuntimeError('unknown internal bug')]:
   with self.subTest(error=type(error).__name__+str(error)):
    worker,engine=Retry().worker([error]*3);worker.keep_unrecoverable_source=True
    p=Retry().paragraph('CHAPTER XV')
    with self.assertRaises(TranslationFailed):worker.translate_paragraph(p)
    self.assertFalse(p.keep_source);self.assertEqual([],worker.retained_source_manifest())
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_wrapped_fatal_chain_and_cancel_never_authorize_retention(self,_sleep):
  wrapped=UnexpectedResult('outer response parser');wrapped.__cause__=UnsupportedModel('missing model')
  worker,engine=Retry().worker([wrapped]*3);worker.keep_unrecoverable_source=True;p=Retry().paragraph('plain')
  with self.assertRaises(TranslationFailed):worker.translate_paragraph(p)
  self.assertFalse(p.keep_source)
  worker,engine=Retry().worker([TranslationCanceled('user cancel')]);worker.keep_unrecoverable_source=True;p=Retry().paragraph('plain')
  with self.assertRaises(TranslationCanceled):worker.translate_paragraph(p)
  self.assertFalse(p.keep_source);self.assertEqual(1,engine.translate.call_count)
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_earlier_protocol_does_not_hide_final_fatal(self,_sleep):
  worker,engine=Retry().worker(['bad','bad',UnsupportedModel('unsupported model')]);worker.keep_unrecoverable_source=True
  p=Retry().paragraph('⟦ET0O⟧word⟦ET0C⟧')
  with self.assertRaises(TranslationFailed) as caught:worker.translate_paragraph(p)
  self.assertNotIsInstance(caught.exception,ParagraphTerminalFailed);self.assertFalse(p.keep_source)
 def test_both_new_reasons_native_roundtrip_and_new_job_have_no_provider_calls(self):
  for reason in ['quality_exhausted','provider_response_exhausted']:
   f=Native();f.setUp()
   try:
    p=f.cache.paragraph(7);p.keep_source=True;p.failure_reason=reason;f.cache.update_paragraph(p)
    f.cache.close();f.cache=TranslationCache('fallback');p=f.cache.paragraph(7)
    worker,engine=Retry().worker([]);worker.keep_unrecoverable_source=True;worker.translate_paragraph(p)
    self.assertTrue(p.keep_source);self.assertEqual(reason,p.failure_reason);self.assertIsNone(p.translation);engine.translate.assert_not_called()
   finally:f.tearDown()
 def test_both_renderers_preserve_original_node_for_both_new_reasons(self):
  for reason in ['quality_exhausted','provider_response_exhausted']:
   for position in ['only','below']:
    root,h,ps=DOM().fixture(position=position);original=etree.tostring(root[0])
    ps[0].keep_source=True;ps[0].failure_reason=reason;ps[0].translation=None;ps[1].translation='成功';h.keep_unrecoverable_source=True
    h.add_translations(ps);self.assertEqual(original,etree.tostring(root[0]));self.assertEqual(1,len(root.xpath('.//img')))
 def book(self,bad,total=10):
  worker,engine=Retry().worker(['<0x80>']*(bad*3));worker.keep_unrecoverable_source=True;ps=[]
  for i in range(total):
   p=Retry().paragraph('Plain row '+str(i));p.id=i+1
   if i>=bad:p.translation='成功'
   ps.append(p)
  logs=[];progress=[];saved=[];worker.set_logging(lambda message,error=False:logs.append(message));worker.set_progress(lambda *args:progress.append(args));worker.set_callback(lambda p:saved.append((p.id,p.translation)))
  return worker,engine,ps,logs,progress,saved
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_ten_percent_boundary_completes_with_count_ids_in_log_and_message(self,_sleep):
  worker,engine,ps,logs,progress,saved=self.book(1)
  worker.handle(ps);summary=worker.retention_summary(ps)
  self.assertEqual({'count':1,'ids':[1],'required_rows':10,'max_ratio':.1,'ratio':.1,'blocked':False},summary)
  self.assertIn('本書 1 列保留原文',progress[-1][1]);self.assertIn('IDs: [1]',progress[-1][1]);self.assertIn((1,None),saved)
  self.assertEqual(1,json.loads(next(x.split(': ',1)[1] for x in logs if x.startswith('source retained count/IDs:')))['count'])
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_above_ten_percent_fails_loud_after_saving_null_targets(self,_sleep):
  worker,engine,ps,logs,progress,saved=self.book(2)
  with self.assertRaisesRegex(TranslationFailed,'limit exceeded: 2/10'):worker.handle(ps)
  self.assertEqual(6,engine.translate.call_count);self.assertIn((1,None),saved);self.assertIn((2,None),saved)
  self.assertEqual([1,2],worker.retention_summary(ps)['ids']);self.assertIn('limit exceeded',progress[-1][1])
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_ignored_rows_do_not_dilute_retention_ratio(self,_sleep):
  worker,engine,ps,logs,progress,saved=self.book(2)
  for i in range(100):ps.append(Paragraph(i+100,'ignored','<p>Ignore</p>','Ignore',ignored=True,translation='成功'))
  with self.assertRaisesRegex(TranslationFailed,'2/10'):worker.handle(ps)
  self.assertEqual(10,worker.retention_summary(ps)['required_rows'])

 def test_real_variant_completion_status_and_popup_include_count_ids(self):
  from . import test_conversion as fixtures
  f=fixtures.TestConversionWorker();f.setUp();f.configure_variant_ebook();worker=f.worker
  worker.config['show_notification']=True
  from unittest.mock import Mock
  job=Mock();job.failed=False;job.description='fixture';job.result={'v2_unrecoverable_keep_source':True,'kept_source_paragraphs':[{'id':42,'page':'x','reason':'quality_exhausted'}]}
  worker.working_jobs={job:(f.ebook,f.variant_payload('bundle_data'))}
  with patch.object(worker,'_store_variants',return_value=('/output.epub','Title',0)):
   worker.translate_done(job)
  self.assertIn('本書 1 列保留原文',worker.gui.status_bar.show_message.call_args.args[0])
  popup=worker.gui.proceed_question.call_args.args[5]
  self.assertIn('本書 1 列保留原文',popup);self.assertIn('IDs: [42]',popup)
