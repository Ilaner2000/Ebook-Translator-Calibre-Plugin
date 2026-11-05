import json
import unittest
from unittest.mock import patch

from ...lib.exception import ParagraphProtocolFailed, TranslationCanceled
from . import test_inline_fallback as fixtures


class TestBoundedPlaceholderRetry(unittest.TestCase):
    def worker(self, responses):
        worker,engine=fixtures.TestInlineFallback().worker(responses)
        engine.name='fake engine'
        engine.get_session_usage.return_value={'responses':0}
        return worker,engine

    def paragraph(self, source):
        return fixtures.TestInlineFallback().paragraph(source)

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_reserve_only_missing_token_terminates_in_three(self, _sleep):
        worker, engine = self.worker(['缺少圖片'] * 3)
        engine.request_attempt = 1000000
        p = self.paragraph('N{{id_00000}}g{{id_00001}}rjuna')
        with self.assertRaisesRegex(ParagraphProtocolFailed,
                                    'placeholder_unrecoverable; 3/3'):
            worker.translate_paragraph(p)
        self.assertEqual(3, engine.translate.call_count)
        self.assertIsNone(p.translation)
        self.assertEqual('placeholder_unrecoverable', p.failure_reason)
        self.assertEqual(0, worker.abort_count)

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_recoverable_reserve_failure_uses_last_slot_and_reason(self, _sleep):
        worker, engine = self.worker(['缺圖片', '缺圖片', '譯文{{id_00000}}'])
        p = self.paragraph('⟦ET0O⟧Tath{{id_00000}}gata⟦ET0C⟧')
        worker.translate_paragraph(p)
        self.assertEqual(3, engine.translate.call_count)
        self.assertTrue(p.degraded)
        self.assertEqual('placeholder_exhausted', p.degrade_reason)
        self.assertEqual('譯文{{id_00000}}', p.translation)

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_reserve_only_degraded_flag_survives_native_cache(self, _sleep):
        from .test_inline_fallback import TestDegradedNativeCache
        fixture = TestDegradedNativeCache(); fixture.setUp()
        try:
            source = 'N{{id_00000}}g'
            fixture.cache.update(7, original=source)
            p = fixture.cache.paragraph(7)
            worker, engine = self.worker(['缺圖片', '缺圖片', '保留{{id_00000}}'])
            worker.translate_paragraph(p);fixture.cache.update_paragraph(p)
            restored = fixture.cache.paragraph(7)
            self.assertTrue(restored.degraded)
            self.assertEqual('placeholder_exhausted', restored.degrade_reason)
            worker.translate_paragraph(restored)
            self.assertTrue(restored.is_cache)
            self.assertEqual(3, engine.translate.call_count)
        finally:fixture.tearDown()

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_all_bad_rows_finish_queue_and_later_valid_row_persists(self, _sleep):
        # A validation failure must not trip the provider-outage abort guard.
        worker, engine = self.worker(['缺圖片'] * 36 + ['成功'])
        paragraphs = [self.paragraph('word{{id_00000}}') for _ in range(12)]
        for index,p in enumerate(paragraphs):p.id=index+1
        paragraphs.append(self.paragraph('plain prose'))
        saved=[];logs=[]
        worker.set_callback(lambda p:saved.append((p.id,p.translation)))
        worker.set_logging(lambda message,error=False:logs.append(message))
        worker.handle(paragraphs)
        self.assertEqual(37, engine.translate.call_count)
        self.assertEqual(13,len(saved))
        self.assertEqual('成功',paragraphs[-1].translation)
        self.assertEqual(0,worker.abort_count)
        summary=next(s for s in logs if s.startswith('unrecoverable summary:'))
        result=json.loads(summary.split(': ',1)[1])
        self.assertEqual(12,result['count'])
        self.assertTrue(all(p['reason']=='placeholder_unrecoverable'
                            and len(p['source_preview'])<=60
                            for p in result['paragraphs']))
        self.assertTrue(all(p.translation is None for p in paragraphs[:-1]))

    @patch('calibre_plugins.ebook_translator.lib.translation.time.sleep')
    def test_fatal_provider_and_cancel_never_degrade(self, _sleep):
        worker,engine=self.worker([RuntimeError('fatal')])
        engine.match_error.return_value=True
        with self.assertRaises(TranslationCanceled):
            worker.translate_paragraph(self.paragraph('text{{id_00000}}'))
        self.assertEqual(1,engine.translate.call_count)
        worker,engine=self.worker(['bad'])
        worker.set_cancel_request(lambda:True)
        with self.assertRaises(TranslationCanceled):
            worker.translate_paragraph(self.paragraph('text{{id_00000}}'))
        engine.translate.assert_not_called()

    def test_repaired_warm_row_clears_prior_terminal_reason(self):
        worker,engine=self.worker([])
        p=self.paragraph('word{{id_00000}}')
        p.failure_reason='placeholder_unrecoverable'
        p.translation='譯文{{id_00000}}'
        worker.translate_paragraph(p)
        self.assertIsNone(p.failure_reason)
        self.assertTrue(p.is_cache)
        engine.translate.assert_not_called()
