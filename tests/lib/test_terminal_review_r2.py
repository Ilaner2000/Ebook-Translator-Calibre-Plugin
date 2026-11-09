"""R1 findings: real Base parser provenance, mixed unsafe errors, transient retry."""
import errno,unittest
from urllib.error import HTTPError,URLError
from unittest.mock import patch,Mock
from lxml import etree
from ...engines.base import Base
from ...lib.cache import TranslationCache
from ...lib.translation import fatal_row_error,row_error_kind
from ...lib.exception import (ParagraphTerminalFailed,TranslationFailed,TranslationCanceled,UnexpectedResult)
from . import test_placeholder_retry as retry_mod
from . import test_inline_fallback as native_mod
Retry=lambda:retry_mod.TestBoundedPlaceholderRetry()
Native=lambda:native_mod.TestDegradedNativeCache()

class OfflinePageParser(Base):
 name='offline page parser';config={'api_keys':['dummy_a','dummy_b']};need_api_key=True
 def get_body(self,text):return {}
 def get_endpoint(self):return 'http://offline.invalid'
 def get_headers(self):return {}
 def get_result(self,response):raise self.parser_failure

class TestReviewFindings(unittest.TestCase):
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_real_base_payload_pages_and_XML_column_do_not_become_fatal(self,_sleep):
  for parser_error in [ValueError('response pages 401, 404–410'),etree.XMLSyntaxError('bad provider response',0,1,401)]:
   with self.subTest(kind=type(parser_error).__name__):
    engine=OfflinePageParser();engine.inline_protocol=True;engine.request_attempt=99;engine.parser_failure=parser_error
    worker,_=Retry().worker([]);worker.translator=engine;worker.keep_unrecoverable_source=True;p=Retry().paragraph('CHAPTER XV')
    with patch('calibre_plugins.ebook_translator.engines.base.request',return_value='See pages 401 and 404–410') as request,patch.object(engine,'swap_api_key',wraps=engine.swap_api_key) as swap,patch.object(engine,'match_error',wraps=engine.match_error) as matcher:
     try:worker.translate_paragraph(p)
     except (TranslationFailed,TranslationCanceled):pass
     else:self.fail('expected terminal source-only fallback')
    self.assertTrue(p.keep_source);self.assertEqual('provider_response_exhausted',p.failure_reason)
    self.assertEqual(3,request.call_count);swap.assert_not_called();matcher.assert_not_called()
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_typed_HTTP_401_is_fatal_even_if_response_text_looks_benign(self,_sleep):
  error=UnexpectedResult('benign response');error.response_stage='request';error.__cause__=HTTPError('http://offline.invalid',401,'Unauthorized',{},None)
  self.assertTrue(fatal_row_error(error));self.assertIsNone(row_error_kind(error))
  worker,engine=Retry().worker([error]*3);worker.keep_unrecoverable_source=True;p=Retry().paragraph('plain')
  with self.assertRaises((TranslationFailed,TranslationCanceled)):worker.translate_paragraph(p)
  self.assertFalse(p.keep_source);self.assertEqual([],worker.retained_source_manifest())
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_quality_or_protocol_never_washes_unknown_or_fatal_last_error(self,_sleep):
  for first in ['<0x80>','missing inline tokens']:
   for last in [RuntimeError('unknown internal logic failure'),OSError(errno.ENOSPC,'No space left on device'),RecursionError('recursive failure')]:
    with self.subTest(first=first,last=type(last).__name__):
     worker,engine=Retry().worker([first,first,last]);worker.keep_unrecoverable_source=True;p=Retry().paragraph('⟦ET0O⟧word⟦ET0C⟧')
     with self.assertRaises((TranslationFailed,TranslationCanceled)) as caught:worker.translate_paragraph(p)
     self.assertNotIsInstance(caught.exception,ParagraphTerminalFailed);self.assertFalse(p.keep_source);self.assertEqual([],worker.retained_source_manifest())
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_wrapped_ENOSPC_and_unknown_parser_context_remain_ineligible(self,_sleep):
  for cause in [OSError(errno.ENOSPC,'No space left on device'),RuntimeError('unexpected parser logic')]:
   error=UnexpectedResult('response body');error.response_stage='parse';error.__cause__=cause
   worker,engine=Retry().worker(['<0x80>','<0x80>',error]);worker.keep_unrecoverable_source=True;p=Retry().paragraph('plain')
   with self.assertRaises((TranslationFailed,TranslationCanceled)):worker.translate_paragraph(p)
   self.assertFalse(p.keep_source);self.assertIsNone(row_error_kind(error))
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_known_parse_does_not_hide_unknown_sibling_context(self,_sleep):
  error=UnexpectedResult('received provider response');error.response_stage='parse';error.__cause__=ValueError('parse failure');error.__context__=RuntimeError('unknown context')
  self.assertIsNone(row_error_kind(error))
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_transport_retention_is_job_only_and_next_job_retries_only_NULL(self,_sleep):
  f=Native();f.setUp()
  try:
   p=f.cache.paragraph(7)
   f.cache.add(8,'warm-md5','<p>Good</p>','Good');f.cache.update(8,translation='成功');f.cache.connection.commit()
   warm_before=f.cache.first(id=8)
   worker,engine=Retry().worker([URLError(TimeoutError('timed out'))]*3);worker.keep_unrecoverable_source=True
   with self.assertRaises(ParagraphTerminalFailed):worker.translate_paragraph(p)
   self.assertEqual('provider_transport_exhausted',p.failure_reason);self.assertTrue(p.keep_source)
   f.cache.update_paragraph(p);self.assertIsNone(f.cache.get_info('only_v2_source_retained:7'))
   loaded=f.cache.paragraph(7);worker.translate_paragraph(loaded)
   self.assertTrue(loaded.keep_source);self.assertEqual(3,engine.translate.call_count)
   f.cache.close();f.cache=TranslationCache('fallback');loaded=f.cache.paragraph(7)
   self.assertIsNone(loaded.source_retention)
   next_job,next_engine=Retry().worker([loaded.original]);next_job.keep_unrecoverable_source=True;next_job.set_fresh(False)
   for item in f.cache.all_paragraphs():
    next_job.translate_paragraph(item);f.cache.update_paragraph(item)
   self.assertEqual(1,next_engine.translate.call_count);self.assertIsNotNone(f.cache.paragraph(7).translation)
   self.assertEqual(warm_before,f.cache.first(id=8));self.assertEqual([],next_job.retained_source_manifest())
  finally:f.tearDown()
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_transient_completion_notice_explains_next_ordinary_job_retry(self,_sleep):
  from ...lib.conversion import retained_completion_notice
  message=retained_completion_notice({'kept_source_paragraphs':[{'id':7,'reason':'provider_transport_exhausted'}]})
  self.assertIn('本書 1 列保留原文',message);self.assertIn('IDs: [7]',message);self.assertIn('下次一般翻譯工作自動重試',message)
 def test_unknown_prepare_wrapper_is_not_a_response_parse_failure(self):
  error=UnexpectedResult('body preparation failed');error.response_stage='prepare';error.__cause__=KeyError('missing config')
  self.assertIsNone(row_error_kind(error))
