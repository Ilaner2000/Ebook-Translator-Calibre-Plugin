"""Device-confirmed reading-asset choices never open a hidden desktop dialog."""
import unittest
from unittest.mock import Mock,patch
from . import test_conversion as prior
from ...lib.remote_bridge import RemoteWorker
from ...lib.ebook import Ebook

MODULE='calibre_plugins.ebook_translator.lib.conversion'
class RemoteGuardTests(unittest.TestCase):
    def setUp(self):
        helper=prior.TestConversionWorker('test_create_worker');helper.setUp()
        self.worker=helper.worker
        self.ebook=Ebook(1,'English book',{'epub':'/input.epub'},'epub','English')
        self.ebook.set_output_format('epub');self.ebook.set_target_lang('Chinese');self.ebook.set_lang_code('zh')
        values={'to_library':True,'translation_variant_storage':'bundle_data','translation_variant_main':'bilingual'}
        self.worker.config=Mock()
        self.worker.config.get.side_effect=lambda key,default=None:values.get(key,default)
        self.worker.api.annotation_count_for_book.return_value=3
        self.worker.api.get_last_read_positions.return_value=[{'epubcfi':'x'}]
    @patch(MODULE+'.temporary_path',return_value='/tmp/output.epub')
    @patch(MODULE+'.temporary_copy',return_value='/tmp/source.epub')
    @patch(MODULE+'.question_dialog')
    def test_remote_keep_original_uses_linked_pair_without_dialog(self,dialog,copy,path):
        job=self.worker.translate_variants(self.ebook,'/input.epub',reading_policy='linked_pair',interactive=False)
        self.assertIsNotNone(job);dialog.assert_not_called()
        self.assertEqual(self.worker.working_jobs[job][1]['effective_storage'],'linked_pair')
    @patch(MODULE+'.temporary_path',return_value='/tmp/output.epub')
    @patch(MODULE+'.temporary_copy',return_value='/tmp/source.epub')
    @patch(MODULE+'.question_dialog')
    def test_remote_explicit_replace_preserves_choice_without_dialog(self,dialog,copy,path):
        job=self.worker.translate_variants(self.ebook,'/input.epub',reading_policy='replace_main',interactive=False)
        self.assertEqual(self.worker.working_jobs[job][1]['effective_storage'],'bundle_data');dialog.assert_not_called()
    @patch(MODULE+'.question_dialog')
    def test_remote_missing_choice_fails_before_job(self,dialog):
        with self.assertRaises(ValueError):self.worker.translate_variants(self.ebook,'/input.epub',interactive=False)
        dialog.assert_not_called();self.worker.gui.job_manager.run_job.assert_not_called()
    def test_remote_store_failure_is_not_reported_as_success(self):
        worker=RemoteWorker.__new__(RemoteWorker);job=Mock();job.failed=False;job.result={}
        worker.working_jobs={job:(self.ebook,{'variants':True})};worker.done=Mock()
        worker._store_variants=Mock(side_effect=RuntimeError('write refused'))
        worker.translate_done(job);worker.done.assert_called_once_with(False,'write refused')
