"""R3: actual request boundary and visible terminal row fate."""
import io,json,unittest
from urllib.error import HTTPError
from unittest.mock import patch,Mock
from ...engines.base import Base
from ...lib import utils
from ...lib.translation import row_error_kind,fatal_row_error
from ...lib.exception import TranslationFailed,TranslationCanceled,ParagraphTerminalFailed
from . import test_placeholder_retry as retry_mod

class RequestEngine(Base):
 name='R3 request boundary';need_api_key=False
 def get_endpoint(self):return 'http://offline.invalid'
 def get_body(self,text):return {}
 def get_headers(self):return {}
 def get_result(self,response):return response

class TestRowFateR3(unittest.TestCase):
 def worker(self,responses):
  worker,engine=retry_mod.TestBoundedPlaceholderRetry().worker(responses)
  worker.keep_unrecoverable_source=True
  return worker,engine
 def paragraph(self,source='plain'):
  p=retry_mod.TestBoundedPlaceholderRetry().paragraph(source)
  p.id=729;p.page='footnotes';p.md5='source-md5'
  return p
 def test_actual_request_preserves_HTTP_500_in_V2_but_legacy_wrapper_stays(self):
  for inline in (False,True):
   engine=RequestEngine();engine.inline_protocol=inline
   error=HTTPError('http://offline.invalid',500,'Internal Server Error',{},io.BytesIO(b'page 401'))
   with patch.object(utils,'Browser') as browser:
    browser.return_value.open.side_effect=error
    with self.assertRaises(Exception) as caught:engine.translate('plain')
   self.assertEqual('transport' if inline else None,row_error_kind(caught.exception))
   self.assertFalse(fatal_row_error(caught.exception))
 def test_actual_request_401_and_404_are_fatal_with_benign_body(self):
  for code in (401,404):
   engine=RequestEngine();engine.inline_protocol=True
   error=HTTPError('http://offline.invalid',code,'HTTP failure',{},io.BytesIO(b'benign'))
   with patch.object(utils,'Browser') as browser:
    browser.return_value.open.side_effect=error
    with self.assertRaises(Exception) as caught:engine.translate('plain')
   self.assertTrue(fatal_row_error(caught.exception));self.assertIsNone(row_error_kind(caught.exception))
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_unknown_row_prints_identity_chain_and_cannot_complete_successfully(self,_sleep):
  worker,engine=self.worker([RuntimeError('unclassified final request')]);p=self.paragraph()
  logs=[];worker.set_logging(lambda msg,error=False:logs.append(msg))
  with self.assertRaisesRegex(TranslationFailed,'failed paragraph rows:.*729'):worker.handle([p])
  entry=json.loads(next(s.split(': ',1)[1] for s in logs if s.startswith('V2 blocked failure row: ')))
  self.assertEqual(729,entry['id']);self.assertEqual('footnotes',entry['page'])
  self.assertIn('RuntimeError',entry['error_chain']);self.assertFalse(p.keep_source)
  self.assertFalse(any('Translation completed' in s for s in logs))
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_cancelled_row_prints_identity_and_does_not_report_success(self,_sleep):
  worker,engine=self.worker([TranslationCanceled('cancelled')]);p=self.paragraph();logs=[]
  worker.set_logging(lambda msg,error=False:logs.append(msg))
  with self.assertRaisesRegex(TranslationFailed,'failed paragraph rows:.*729'):worker.handle([p])
  self.assertTrue(any('V2 blocked failure row:' in s and 'TranslationCanceled' in s for s in logs))
  self.assertFalse(any('Translation completed' in s for s in logs));self.assertFalse(p.keep_source)
 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_post_translate_glossary_failure_is_logged_and_not_silently_retained(self,_sleep):
  worker,engine=self.worker(['valid']);p=self.paragraph();logs=[]
  worker.glossary.restore=Mock(side_effect=RuntimeError('glossary restore failure'))
  worker.set_logging(lambda msg,error=False:logs.append(msg))
  with self.assertRaisesRegex(TranslationFailed,'failed paragraph rows:.*729'):worker.handle([p])
  self.assertTrue(any('V2 blocked failure row:' in s and 'glossary restore failure' in s for s in logs))
  self.assertFalse(p.keep_source);self.assertIsNone(p.translation)
 def test_unprocessed_NULL_without_exception_is_blocked(self):
  worker,engine=self.worker([]);p=self.paragraph();logs=[];p.translation=None
  worker.set_logging(lambda msg,error=False:logs.append(msg))
  with patch('calibre_plugins.ebook_translator.lib.translation.Handler'):
   with self.assertRaisesRegex(TranslationFailed,'failed paragraph rows:.*729'):worker.handle([p])
  engine.translate.assert_not_called();self.assertFalse(any('Translation completed' in s for s in logs))

 @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
 def test_previous_variant_retention_cannot_mask_cancellation(self,_sleep):
  worker,engine=self.worker(['bad']*3);p=self.paragraph('word{{id_00000}}')
  with self.assertRaises(ParagraphTerminalFailed):worker.translate_paragraph(p)
  self.assertTrue(p.keep_source)
  worker.set_cancel_request(lambda:True);logs=[]
  worker.set_logging(lambda msg,error=False:logs.append(msg))
  with self.assertRaisesRegex(TranslationFailed,'failed paragraph rows:.*729'):worker.handle([p])
  self.assertFalse(p.keep_source);self.assertFalse(any('Translation completed' in s for s in logs))
