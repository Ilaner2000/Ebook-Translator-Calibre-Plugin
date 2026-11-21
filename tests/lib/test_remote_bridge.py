"""Device-confirmed reading-asset choices never open a hidden desktop dialog."""
import unittest
import json
import tempfile
import os
from pathlib import Path
from types import SimpleNamespace
from ...lib import remote_core as core
from ...lib.remote_bridge import RemoteBridge,gui_blocked
from qt.core import QApplication,QDialog,QTimer,Qt
from calibre.gui2 import Application
_qt_app=None
from unittest.mock import Mock,patch
from . import test_conversion as prior
from ...lib.remote_bridge import RemoteWorker
from ...lib.ebook import Ebook

MODULE='calibre_plugins.ebook_translator.lib.conversion'
class RemoteGuardTests(unittest.TestCase):
    def setUp(self):
        helper=prior.TestConversionWorker('test_create_worker');helper.setUp()
        self.worker=helper.worker
        self.worker.working_jobs={}
        self.worker.gui.bookfere_ebook_translator.jobs=self.worker.working_jobs
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
        worker=RemoteWorker.__new__(RemoteWorker);worker.gui=self.worker.gui;job=Mock();job.failed=False;job.result={}
        worker.working_jobs={job:(self.ebook,{'variants':True})};worker.done=Mock()
        worker.check_before_store=Mock()
        worker.preserve_conflict=Mock(return_value=Path('/retained'))
        worker._store_variants=Mock(side_effect=RuntimeError('write refused'))
        worker.translate_done(job);self.assertFalse(worker.done.call_args.args[0]);self.assertIn('存回未完成',worker.done.call_args.args[1])

    def test_storing_precedes_write_and_success_follows_write(self):
        worker=RemoteWorker.__new__(RemoteWorker);worker.gui=self.worker.gui;job=Mock();job.failed=False;job.result={}
        worker.working_jobs={job:(self.ebook,{'variants':True})};trace=[]
        def done(success,detail):trace.append(('done',success))
        done.storing=lambda:trace.append(('storing',))
        worker.check_before_store=Mock()
        worker.done=done;worker._store_variants=lambda *args:trace.append(('write',))
        worker.translate_done(job)
        self.assertEqual(trace,[('storing',),('write',),('done',True)])
        self.assertFalse(worker.working_jobs)

    @patch('calibre_plugins.ebook_translator.lib.remote_core.library_identity',return_value=('/library-A','library-A-uuid'))
    @patch(MODULE+'.temporary_path',return_value='/tmp/output.epub')
    @patch(MODULE+'.temporary_copy',return_value='/tmp/source.epub')
    @patch(MODULE+'.file_sha256',return_value='source-sha')
    def test_remote_snapshot_input_and_fixed_worker_job_identity(self,sha,copy,path,identity):
        self.worker.db.library_path='/library-A';self.worker.api.field_for.return_value='book-A-uuid'
        self.worker.gui.current_db=SimpleNamespace(library_path='/library-B')
        job=self.worker.translate_variants(self.ebook,'/live-library.epub',reading_policy='linked_pair',interactive=False,expected_source_sha256='source-sha')
        scheduled=self.worker.gui.job_manager.run_job.call_args.kwargs['args'][2]
        self.assertEqual(scheduled[1],'/tmp/source.epub')
        self.assertEqual(job.wasn_source_identity,{'known':True,'library_uuid':'library-A-uuid','library_path':'/library-A','book_id':1,'book_uuid':'book-A-uuid'})
        bridge=RemoteBridge(SimpleNamespace(gui=self.worker.gui))
        self.assertEqual(bridge.manual_jobs(),[job.wasn_source_identity])

    @patch(MODULE+'.temporary_copy',return_value='/tmp/source.epub')
    @patch(MODULE+'.file_sha256',return_value='changed-sha')
    def test_changed_source_during_snapshot_refuses_before_job(self,sha,copy):
        with self.assertRaises(ValueError):self.worker.translate_variants(self.ebook,'/input.epub',reading_policy='linked_pair',interactive=False,expected_source_sha256='original-sha')
        self.worker.gui.job_manager.run_job.assert_not_called()

    @patch('calibre_plugins.ebook_translator.lib.remote_core.library_identity',return_value=('/library-A','library-A-uuid'))
    @patch(MODULE+'.PersistentTemporaryFile')
    def test_manual_registration_also_binds_fixed_worker_database(self,tmp,identity):
        self.worker.db.library_path='/library-A';self.worker.api.field_for.return_value='book-A-uuid'
        self.worker.gui.current_db=SimpleNamespace(library_path='/library-B')
        tmp.return_value.name='/output.epub'
        self.worker.translate_ebook(self.ebook)
        job=next(iter(self.worker.working_jobs))
        self.assertEqual(job.wasn_source_identity['library_uuid'],'library-A-uuid')
        self.assertEqual(job.wasn_source_identity['book_uuid'],'book-A-uuid')

    def test_legacy_job_identity_is_unknown_and_not_relabeled(self):
        job=object();gui=SimpleNamespace(bookfere_ebook_translator=SimpleNamespace(jobs={job:(self.ebook,'out')}))
        rows=RemoteBridge(SimpleNamespace(gui=gui)).manual_jobs()
        self.assertEqual(rows,[{'known':False,'book_id':1}])

    def test_store_conflict_preserves_three_outputs_before_failure(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("WASN_TEST_TMP_ROOT")) as td:
            root=Path(td);paths={}
            for role in ('source','translation','bilingual'):
                paths[role]=root/(role+'.epub');paths[role].write_bytes((role+' actual output').encode())
            original={'id':1,'uuid':'book-A','library_uuid':'lib-A','library_path':'/library-A','title':'Original','authors':['Author'],'languages':['eng'],'facts':{'sha256':core.file_hash(paths['source']),'languages':['en']}}
            worker=RemoteWorker.__new__(RemoteWorker);worker.remote_request={'id':'a'*32,'book_id':1,'book_uuid':'book-A','source_evidence':core.book_evidence(original),'source_sha256':original['facts']['sha256']}
            worker.gui=self.worker.gui;worker.api=SimpleNamespace(field_for=lambda *args:'book-A')
            worker.db=SimpleNamespace(library_path='/library-A');worker.bridge_root=root/'spool';worker.backup_root=root/'backups'
            changed=dict(original,title='Changed book')
            job=Mock();job.failed=False;job.result={}
            payload=dict(variants=True,**{role+'_path':str(path) for role,path in paths.items()})
            worker.working_jobs={job:(self.ebook,payload)};worker.done=Mock();worker._store_variants=Mock()
            with patch('calibre_plugins.ebook_translator.lib.remote_core.snapshot',return_value=changed):worker.translate_done(job)
            worker._store_variants.assert_not_called();self.assertFalse(worker.done.call_args.args[0])
            manifest=json.loads((worker.backup_root/('a'*32)/'manifest.json').read_text(encoding='utf-8'))
            self.assertFalse(manifest['library_written'])
            for role,entry in manifest['files'].items():
                self.assertEqual(Path(entry['path']).read_bytes(),paths[role].read_bytes());self.assertEqual(core.file_hash(entry['path']),entry['sha256'])
            self.assertIn('未存回',worker.done.call_args.args[1])

    def test_missing_context_cannot_store(self):
        worker=RemoteWorker.__new__(RemoteWorker);worker.remote_request=None
        with self.assertRaises(core.BridgeError):worker.check_before_store({})

    def test_real_nested_qdialog_blocks_consumer_then_stale_editor_cannot_overwrite(self):
        global _qt_app
        _qt_app=QApplication.instance() or Application([])
        _qt_app.setQuitOnLastWindowClosed(False)
        with tempfile.TemporaryDirectory(dir=os.environ.get('WASN_TEST_TMP_ROOT')) as td:
            root=Path(td);model={'comments':'original','writes':0};before=model['comments']
            class Consumer:
                def heartbeat(self):pass
                def tick(self):model.update(comments=model['comments']+' tool-version-line',writes=model['writes']+1)
            gui=SimpleNamespace(current_db=SimpleNamespace(library_path=str(root/'library'),new_api=None),
                bookfere_ebook_translator=SimpleNamespace(windows={},jobs={}))
            bridge=RemoteBridge(SimpleNamespace(gui=gui));bridge.consumer=Consumer();bridge.binding=(str(root.resolve()),str((root/'library').resolve()))
            dialog=QDialog();dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
            dialog.accepted.connect(lambda:model.update(comments=before))
            observed=[]
            def inside():
                observed.append(QApplication.activeModalWidget() is dialog)
                bridge.tick();observed.append(model['writes']);dialog.accept()
            QTimer.singleShot(0,inside)
            with patch('calibre_plugins.ebook_translator.lib.remote_bridge.get_config',return_value={'remote_bridge_root':str(root),'remote_conflict_root':str(root/'backups')}):
                dialog.exec();self.assertEqual(observed,[True,0]);self.assertEqual(model['comments'],'original')
                bridge.tick()
            self.assertEqual(model['writes'],1);self.assertEqual(model['comments'],'original tool-version-line')
            gui.bookfere_ebook_translator.windows={'advanced_1':object()};self.assertTrue(gui_blocked(gui))
            gui.bookfere_ebook_translator.windows={'batch':object()};self.assertTrue(gui_blocked(gui))

    def test_remote_completion_waits_without_popping_job_while_modal_open(self):
        worker=RemoteWorker.__new__(RemoteWorker);worker.gui=self.worker.gui
        job=Mock();job.failed=False;job.result={};worker.working_jobs={job:(self.ebook,{'variants':True})}
        worker.done=Mock();worker.check_before_store=Mock();worker._store_variants=Mock();callbacks=[]
        with patch('calibre_plugins.ebook_translator.lib.remote_bridge.gui_blocked',return_value=True),patch('calibre_plugins.ebook_translator.lib.remote_bridge.QTimer.singleShot',side_effect=lambda ms,fn:callbacks.append(fn)):
            worker.translate_done(job)
        self.assertIn(job,worker.working_jobs);worker._store_variants.assert_not_called();worker.done.assert_not_called()
        with patch('calibre_plugins.ebook_translator.lib.remote_bridge.gui_blocked',return_value=False):callbacks[0]()
        worker._store_variants.assert_called_once();self.assertTrue(worker.done.call_args.args[0])

    def test_activation_refuses_cleanup_roots_and_relative_note_primitive(self):
        from ...lib.remote_bridge import guarded_note_writer
        with tempfile.TemporaryDirectory(dir=os.environ.get('WASN_TEST_TMP_ROOT')) as td:
            root=Path(td);gui=SimpleNamespace(bookfere_ebook_translator=SimpleNamespace(windows={},jobs={}))
            for config in ({'remote_bridge_root':str(root/'runtime-tmp'/'spool'),'remote_conflict_root':str(root/'backups')},
                           {'remote_bridge_root':str(root/'spool'),'remote_conflict_root':str(root/'runtime-tmp'/'backup')}):
                bridge=RemoteBridge(SimpleNamespace(gui=gui))
                with patch('calibre_plugins.ebook_translator.lib.remote_bridge.get_config',return_value=config):bridge.tick()
                self.assertIsNone(bridge.consumer);self.assertIsNotNone(bridge.last_error)
            with self.assertRaises(core.BridgeError):guarded_note_writer('migrate_h2o_kangxi_s796.py')
