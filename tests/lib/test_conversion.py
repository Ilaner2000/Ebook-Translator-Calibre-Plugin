import unittest
from pathlib import Path
from typing import Callable
from types import SimpleNamespace
from unittest.mock import patch, Mock, call

from ...lib.conversion import (
    ConversionWorker, convert_book, convert_variants_item, extract_book)
from ...lib.ebook import Ebook


module_name = 'calibre_plugins.ebook_translator.lib.conversion'


class TestConvertBookMethodRestoration(unittest.TestCase):
    class Reporter:
        global_min = 0

        def __call__(self, *args, **kwargs):
            pass

    class OutputPlugin:
        def __init__(self):
            self.report_progress = (
                TestConvertBookMethodRestoration.Reporter())
            self.base_convert_calls = 0

        def convert(self, *args, **kwargs):
            self.base_convert_calls += 1

    def run_convert(self, plugin, handler, fail=False):
        class FakePlumber:
            def __init__(self, *args, **kwargs):
                self.output_plugin = plugin

            def run(self):
                if fail:
                    raise RuntimeError('plumber failed')
                oeb = SimpleNamespace(
                    metadata=Mock(), toc=SimpleNamespace(nodes=[]),
                    manifest=SimpleNamespace(items=[]))
                self.output_plugin.convert(
                    oeb, '/output.epub', None, None, Mock())

        translation = Mock()
        cache = Mock()
        cache.all_paragraphs.return_value = []
        handler.prepare_original.return_value = []
        with patch(module_name + '.Plumber', FakePlumber), \
                patch(module_name + '.CompositeProgressReporter',
                      side_effect=lambda *args: self.Reporter()), \
                patch(module_name + '.get_metadata_elements',
                      return_value=[]), \
                patch(module_name + '.get_toc_elements', return_value=[]), \
                patch(module_name + '.get_page_elements', return_value=[]):
            convert_book(
                '/input.epub', '/output.epub', translation, handler, cache,
                'debug', 'utf-8', Mock())

    def test_sequential_conversions_do_not_reenter_previous_wrapper(self):
        plugin = self.OutputPlugin()
        first_handler = Mock()
        second_handler = Mock()

        self.run_convert(plugin, first_handler)
        self.run_convert(plugin, second_handler)

        self.assertEqual(1, first_handler.prepare_original.call_count)
        self.assertEqual(1, second_handler.prepare_original.call_count)
        self.assertEqual(2, plugin.base_convert_calls)
        self.assertNotIn('convert', vars(plugin))

    def test_convert_binding_is_restored_for_all_exit_paths(self):
        for instance_override in (False, True):
            for fail in (False, True):
                with self.subTest(
                        instance_override=instance_override, fail=fail):
                    plugin = self.OutputPlugin()
                    original_override = None
                    if instance_override:
                        original_override = Mock()
                        plugin.convert = original_override
                    handler = Mock()

                    if fail:
                        with self.assertRaisesRegex(
                                RuntimeError, 'plumber failed'):
                            self.run_convert(plugin, handler, fail=True)
                    else:
                        self.run_convert(plugin, handler)

                    if instance_override:
                        self.assertIs(
                            original_override, vars(plugin)['convert'])
                    else:
                        self.assertNotIn('convert', vars(plugin))

    def test_extract_book_restores_convert_binding(self):
        plugin = self.OutputPlugin()

        class FakePlumber:
            def __init__(self, *args, **kwargs):
                self.output_plugin = plugin

            def run(self):
                oeb = SimpleNamespace(
                    metadata=Mock(), toc=SimpleNamespace(nodes=[]),
                    manifest=SimpleNamespace(items=[]))
                self.output_plugin.convert(
                    oeb, '/output.epub', None, None, Mock())

        with patch(module_name + '.Plumber', FakePlumber), \
                patch(module_name + '.get_metadata_elements',
                      return_value=[]), \
                patch(module_name + '.get_toc_elements', return_value=[]), \
                patch(module_name + '.get_page_elements', return_value=[]):
            self.assertEqual([], extract_book('/input.epub', 'utf-8'))

        self.assertNotIn('convert', vars(plugin))


class TestConvertVariantsItem(unittest.TestCase):
    @patch(module_name + '.convert_book')
    @patch(module_name + '.get_translation')
    @patch(module_name + '.get_cache')
    @patch(module_name + '.get_element_handler')
    @patch(module_name + '.get_translator')
    def test_reuses_one_translation_context_for_two_renders(
            self, mock_get_translator, mock_get_handler, mock_get_cache,
            mock_get_translation, mock_convert_book):
        translator = mock_get_translator.return_value
        translator.placeholder = ('placeholder', 'pattern')
        translator.separator = '<sep>'
        translator.name = 'Test Engine'
        translator.get_iso639_target_code.return_value = 'zh'
        translation = mock_get_translation.return_value
        cache = mock_get_cache.return_value
        cache.is_persistence.return_value = True
        translation_handler = Mock()
        translation_handler.get_merge_length.return_value = 1800
        bilingual_handler = Mock()
        mock_get_handler.side_effect = [
            translation_handler, bilingual_handler]
        notification = Mock()

        convert_variants_item(
            'Title', '/input.epub', '/translation.epub',
            '/bilingual.epub', 'English', 'Chinese', False, False,
            'epub', 'utf-8', 'auto', 1.0, notification)

        mock_get_translator.assert_called_once_with()
        mock_get_translation.assert_called_once()
        mock_get_cache.assert_called_once()
        self.assertEqual('only', translation_handler.position)
        translation_handler.set_vertical_gap.assert_called_once_with(0.0)
        self.assertEqual('below', bilingual_handler.position)
        bilingual_handler.set_vertical_gap.assert_called_once_with(1.0)
        self.assertEqual([call(False), call(True)],
                         cache.set_cache_only.call_args_list)
        self.assertEqual(2, mock_convert_book.call_count)
        first, second = mock_convert_book.call_args_list
        self.assertIs(translation, first.args[2])
        self.assertIs(translation, second.args[2])
        self.assertIs(cache, first.args[4])
        self.assertIs(cache, second.args[4])
        self.assertEqual(0, translation.total)
        cache.done.assert_called_once_with()


class TestConversionWorker(unittest.TestCase):
    def setUp(self):
        self.gui = Mock()
        self.icon = Mock()
        self.worker = ConversionWorker(self.gui, self.icon)
        self.worker.db = Mock()
        self.worker.api = Mock()

        self.ebook = Mock(Ebook)
        self.job = Mock()
        self.worker.working_jobs = {
            self.job: (self.ebook, str(Path('/path/to/test.epub')))}

    def test_create_worker(self):
        self.assertIsInstance(self.worker, ConversionWorker)

    @patch(module_name + '.temporary_path')
    @patch(module_name + '.temporary_copy')
    @patch(module_name + '.question_dialog')
    def test_translate_variants_changes_a_to_b_for_annotations(
            self, mock_question, mock_copy, mock_temp_path):
        self.worker.config = {
            'to_library': True,
            'translation_variant_storage': 'bundle_data',
            'vertical_gap': 0.0,
        }
        self.worker.working_jobs = {}
        self.ebook.id = 7
        self.ebook.title = 'Title'
        self.ebook.input_format = 'epub'
        self.ebook.output_format = 'epub'
        self.ebook.source_lang = 'English'
        self.ebook.target_lang = 'Chinese'
        self.ebook.encoding = 'utf-8'
        self.ebook.target_direction = 'auto'
        self.worker.api.annotation_count_for_book.return_value = 1
        self.worker.api.get_last_read_positions.return_value = []
        mock_question.return_value = False
        mock_copy.return_value = '/source.epub'
        mock_temp_path.side_effect = [
            '/translation.epub', '/bilingual.epub']
        job = Mock()
        self.worker.gui.job_manager.run_job.return_value = job

        result = self.worker.translate_variants(
            self.ebook, '/input.epub')

        self.assertIs(job, result)
        payload = self.worker.working_jobs[job][1]
        self.assertEqual('bundle_data', payload['requested_storage'])
        self.assertEqual('linked_pair', payload['effective_storage'])
        worker_args = self.worker.gui.job_manager.run_job.call_args.kwargs[
            'args'][2]
        self.assertEqual(1.0, worker_args[-1])
        mock_question.assert_called_once()
        self.assertFalse(mock_question.call_args.kwargs['default_yes'])
        self.assertEqual(
            'Keep preset A', mock_question.call_args.kwargs['yes_text'])
        self.assertEqual(
            'Use preset B (safe)',
            mock_question.call_args.kwargs['no_text'])
        self.assertEqual(
            'bundle_data',
            self.worker.config['translation_variant_storage'])

    @patch(module_name + '.temporary_path')
    @patch(module_name + '.temporary_copy')
    @patch(module_name + '.question_dialog')
    def test_translate_variants_changes_a_to_b_for_last_read_position(
            self, mock_question, mock_copy, mock_temp_path):
        self.worker.config = {
            'to_library': True,
            'translation_variant_storage': 'bundle_data',
            'vertical_gap': 1.0,
        }
        self.worker.working_jobs = {}
        self.ebook.id = 7
        self.ebook.title = 'Title'
        self.ebook.input_format = 'epub'
        self.ebook.output_format = 'epub'
        self.ebook.source_lang = 'English'
        self.ebook.target_lang = 'Chinese'
        self.ebook.encoding = 'utf-8'
        self.ebook.target_direction = 'auto'
        self.worker.api.annotation_count_for_book.return_value = 0
        self.worker.api.get_last_read_positions.return_value = [{}]
        mock_question.return_value = False
        mock_copy.return_value = '/source.epub'
        mock_temp_path.side_effect = [
            '/translation.epub', '/bilingual.epub']
        job = Mock()
        self.worker.gui.job_manager.run_job.return_value = job

        self.worker.translate_variants(self.ebook, '/input.epub')

        payload = self.worker.working_jobs[job][1]
        self.assertEqual('linked_pair', payload['effective_storage'])
        mock_question.assert_called_once()

    @patch(module_name + '.temporary_path')
    @patch(module_name + '.temporary_copy')
    @patch(module_name + '.question_dialog', return_value=True)
    def test_translate_variants_keeps_a_when_user_confirms(
            self, mock_question, mock_copy, mock_temp_path):
        self.worker.config = {
            'to_library': True,
            'translation_variant_storage': 'bundle_data',
            'translation_variant_gap': 1.0,
        }
        self.worker.working_jobs = {}
        self.ebook.id = 7
        self.ebook.title = 'Title'
        self.ebook.input_format = 'epub'
        self.ebook.output_format = 'epub'
        self.ebook.source_lang = 'English'
        self.ebook.target_lang = 'Chinese'
        self.ebook.encoding = 'utf-8'
        self.ebook.target_direction = 'auto'
        self.worker.api.annotation_count_for_book.return_value = 1
        self.worker.api.get_last_read_positions.return_value = []
        mock_copy.return_value = '/source.epub'
        mock_temp_path.side_effect = [
            '/translation.epub', '/bilingual.epub']
        job = Mock()
        self.worker.gui.job_manager.run_job.return_value = job

        result = self.worker.translate_variants(
            self.ebook, '/input.epub')

        self.assertIs(job, result)
        payload = self.worker.working_jobs[job][1]
        self.assertEqual('bundle_data', payload['requested_storage'])
        self.assertEqual('bundle_data', payload['effective_storage'])
        mock_question.assert_called_once()

    @patch(module_name + '.temporary_path')
    @patch(module_name + '.temporary_copy')
    @patch(module_name + '.question_dialog')
    def test_translate_variants_handles_unverified_reading_assets_choice(
            self, mock_question, mock_copy, mock_temp_path):
        self.worker.config = {
            'to_library': True,
            'translation_variant_storage': 'bundle_data',
            'translation_variant_gap': 1.0,
        }
        self.ebook.id = 7
        self.ebook.title = 'Title'
        self.ebook.input_format = 'epub'
        self.ebook.output_format = 'epub'
        self.ebook.source_lang = 'English'
        self.ebook.target_lang = 'Chinese'
        self.ebook.encoding = 'utf-8'
        self.ebook.target_direction = 'auto'
        mock_copy.return_value = '/source.epub'

        for keep_a, expected_storage in (
                (True, 'bundle_data'), (False, 'linked_pair')):
            with self.subTest(keep_a=keep_a):
                self.worker.working_jobs = {}
                self.worker.api.annotation_count_for_book.side_effect = (
                    Exception('reading asset status unavailable'))
                mock_question.return_value = keep_a
                mock_temp_path.side_effect = [
                    '/translation.epub', '/bilingual.epub']
                job = Mock()
                self.worker.gui.job_manager.run_job.return_value = job

                result = self.worker.translate_variants(
                    self.ebook, '/input.epub')

                self.assertIs(job, result)
                payload = self.worker.working_jobs[job][1]
                self.assertEqual('bundle_data', payload['requested_storage'])
                self.assertEqual(expected_storage, payload['effective_storage'])
                mock_question.assert_called_once()
                mock_question.reset_mock()

    @patch(module_name + '.error_dialog')
    def test_translate_variants_rejects_unsupported_scope(self, mock_error):
        self.worker.config = {'to_library': False}
        self.ebook.id = 7
        self.ebook.input_format = 'epub'
        self.ebook.output_format = 'epub'

        result = self.worker.translate_variants(
            self.ebook, '/input.epub')

        self.assertIsNone(result)
        mock_error.assert_called_once()
        self.worker.gui.job_manager.run_job.assert_not_called()

    def variant_payload(self, storage):
        return {
            'variants': True,
            'source_path': '/source.epub',
            'translation_path': '/translation.epub',
            'bilingual_path': '/bilingual.epub',
            'requested_storage': storage,
            'effective_storage': storage,
            'run_id': 'run-1',
        }

    def configure_variant_ebook(self):
        self.worker.config = {
            'ebook_metadata': {},
            'engine_preferences': {},
            'vertical_gap': 1.0,
        }
        self.ebook.id = 7
        self.ebook.lang_code = 'zh-Hant'
        self.ebook.source_lang = 'English'
        self.ebook.target_lang = 'Chinese'
        self.worker.api.field_for.side_effect = lambda field, book_id: {
            'uuid': 'source-uuid',
            'identifiers': {'isbn': '123'},
        }.get(field)
        self.worker.api.add_format.return_value = True
        self.worker.api.format_abspath.return_value = '/library/translation.epub'

    @patch(module_name + '.file_sha256', return_value='hash')
    @patch(module_name + '.get_translator')
    def test_manifest_records_actual_default_engine(
            self, mock_get_translator, mock_hash):
        self.configure_variant_ebook()
        mock_get_translator.return_value.name = 'Default Engine'

        manifest = self.worker._variant_manifest(
            self.ebook, self.variant_payload('linked_pair'),
            'source-uuid', {'translation': 21, 'bilingual': None})

        self.assertEqual('Default Engine', manifest['translation_engine'])
        self.assertEqual('calibre-data-v1', manifest['storage_layout'])
        mock_get_translator.assert_called_once_with()

    @patch(module_name + '.file_sha256', return_value='hash')
    @patch.object(ConversionWorker, '_variant_manifest')
    @patch.object(ConversionWorker, '_write_manifest')
    @patch.object(ConversionWorker, '_add_verified_extra_files')
    @patch.object(ConversionWorker, '_prepare_variant_metadata')
    def test_store_variants_a_replaces_main_after_verified_data(
            self, mock_metadata, mock_add_extra, mock_manifest_file,
            mock_manifest, mock_hash):
        self.configure_variant_ebook()
        translation_metadata = Mock()
        bilingual_metadata = Mock()
        mock_metadata.side_effect = [
            (translation_metadata, 'Chinese title'),
            (bilingual_metadata, 'Bilingual title')]
        mock_manifest_file.return_value = '/manifest.json'

        result = self.worker._store_variants(
            self.ebook, self.variant_payload('bundle_data'))

        files = mock_add_extra.call_args.args[1]
        self.assertEqual(7, mock_add_extra.call_args.args[0])
        self.assertEqual({
            'data/translation-variants/run-1/source.epub',
            'data/translation-variants/run-1/bilingual.zh-Hant.epub',
            'data/translation-variants/run-1/manifest.json',
        }, set(files))
        self.assertEqual('/source.epub', files[
            'data/translation-variants/run-1/source.epub'])
        self.assertEqual('/bilingual.epub', files[
            'data/translation-variants/run-1/bilingual.zh-Hant.epub'])
        self.worker.api.add_format.assert_called_once_with(
            7, 'EPUB', '/translation.epub', replace=True, run_hooks=False)
        self.worker.api.set_field.assert_called_once_with(
            'identifiers', {7: {
                'isbn': '123',
                'ebook-translator-variant': 'translation:run-1',
            }})
        self.worker.db.create_book_entry.assert_not_called()
        self.assertEqual(
            ('/library/translation.epub', 'Chinese title', 0), result)

    @patch(module_name + '.file_sha256', side_effect=[
        'expected-hash', 'different-hash'])
    @patch.object(ConversionWorker, '_variant_manifest')
    @patch.object(ConversionWorker, '_write_manifest')
    @patch.object(ConversionWorker, '_add_verified_extra_files')
    @patch.object(ConversionWorker, '_prepare_variant_metadata')
    def test_store_variants_a_restores_source_on_replace_hash_mismatch(
            self, mock_metadata, mock_add_extra, mock_manifest_file,
            mock_manifest, mock_hash):
        self.configure_variant_ebook()
        mock_metadata.side_effect = [
            (Mock(), 'Chinese title'), (Mock(), 'Bilingual title')]
        mock_manifest_file.return_value = '/manifest.json'

        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            self.worker._store_variants(
                self.ebook, self.variant_payload('bundle_data'))

        self.assertEqual([
            call(7, 'EPUB', '/translation.epub',
                 replace=True, run_hooks=False),
            call(7, 'EPUB', '/source.epub',
                 replace=True, run_hooks=False),
        ], self.worker.api.add_format.call_args_list)
        self.worker.api.set_field.assert_called_once_with(
            'identifiers', {7: {'isbn': '123'}})
        self.worker.api.remove_extra_files.assert_called_once()

    @patch.object(ConversionWorker, '_variant_manifest')
    @patch.object(ConversionWorker, '_write_manifest')
    @patch.object(ConversionWorker, '_add_verified_extra_files')
    @patch.object(ConversionWorker, '_prepare_variant_metadata')
    def test_store_variants_b_creates_one_linked_book(
            self, mock_metadata, mock_add_extra, mock_manifest_file,
            mock_manifest):
        self.configure_variant_ebook()
        mock_metadata.side_effect = [
            (Mock(), 'Chinese title'), (Mock(), 'Bilingual title')]
        mock_manifest_file.return_value = '/manifest.json'
        self.worker.db.create_book_entry.return_value = 21

        result = self.worker._store_variants(
            self.ebook, self.variant_payload('linked_pair'))

        files = mock_add_extra.call_args.args[1]
        self.assertEqual(21, mock_add_extra.call_args.args[0])
        self.assertEqual({
            'data/translation-variants/run-1/bilingual.zh-Hant.epub',
            'data/translation-variants/run-1/manifest.json',
        }, set(files))
        self.assertNotIn(
            'data/translation-variants/run-1/source.epub', files)
        self.assertIn(
            'data/translation-variants/run-1/bilingual.zh-Hant.epub', files)
        self.worker.api.add_format.assert_called_once_with(
            21, 'EPUB', '/translation.epub', run_hooks=False)
        self.worker.gui.library_view.model().books_added \
            .assert_called_once_with(1)
        self.assertEqual(
            ('/library/translation.epub', 'Chinese title', 1), result)

    @patch.object(ConversionWorker, '_variant_manifest')
    @patch.object(ConversionWorker, '_write_manifest')
    @patch.object(ConversionWorker, '_add_verified_extra_files')
    @patch.object(ConversionWorker, '_prepare_variant_metadata')
    def test_store_variants_c_creates_two_books(
            self, mock_metadata, mock_add_extra, mock_manifest_file,
            mock_manifest):
        self.configure_variant_ebook()
        mock_metadata.side_effect = [
            (Mock(), 'Chinese title'), (Mock(), 'Bilingual title')]
        mock_manifest_file.return_value = '/manifest.json'
        self.worker.db.create_book_entry.side_effect = [21, 22]

        result = self.worker._store_variants(
            self.ebook, self.variant_payload('three_records'))

        files = mock_add_extra.call_args.args[1]
        self.assertEqual(21, mock_add_extra.call_args.args[0])
        self.assertEqual({
            'data/translation-variants/run-1/manifest.json': '/manifest.json'
        }, files)
        self.assertEqual([
            call(21, 'EPUB', '/translation.epub', run_hooks=False),
            call(22, 'EPUB', '/bilingual.epub', run_hooks=False),
        ], self.worker.api.add_format.call_args_list)
        self.worker.gui.library_view.model().books_added \
            .assert_called_once_with(2)
        self.assertEqual(
            ('/library/translation.epub', 'Chinese title', 2), result)

    @patch.object(ConversionWorker, '_variant_manifest')
    @patch.object(ConversionWorker, '_write_manifest')
    @patch.object(ConversionWorker, '_add_verified_extra_files')
    @patch.object(ConversionWorker, '_prepare_variant_metadata')
    def test_store_variants_a_restores_source_when_replace_fails(
            self, mock_metadata, mock_add_extra, mock_manifest_file,
            mock_manifest):
        self.configure_variant_ebook()
        mock_metadata.side_effect = [
            (Mock(), 'Chinese title'), (Mock(), 'Bilingual title')]
        mock_manifest_file.return_value = '/manifest.json'
        self.worker.api.add_format.side_effect = [RuntimeError('fail'), True]

        with self.assertRaises(RuntimeError):
            self.worker._store_variants(
                self.ebook, self.variant_payload('bundle_data'))

        self.assertEqual([
            call(7, 'EPUB', '/translation.epub',
                 replace=True, run_hooks=False),
            call(7, 'EPUB', '/source.epub',
                 replace=True, run_hooks=False),
        ], self.worker.api.add_format.call_args_list)
        self.worker.api.remove_extra_files.assert_called_once()

    @patch.object(ConversionWorker, '_variant_manifest')
    @patch.object(ConversionWorker, '_write_manifest')
    @patch.object(ConversionWorker, '_add_verified_extra_files')
    @patch.object(ConversionWorker, '_prepare_variant_metadata')
    def test_store_variants_a_keeps_source_data_when_restore_fails(
            self, mock_metadata, mock_add_extra, mock_manifest_file,
            mock_manifest):
        self.configure_variant_ebook()
        mock_metadata.side_effect = [
            (Mock(), 'Chinese title'), (Mock(), 'Bilingual title')]
        mock_manifest_file.return_value = '/manifest.json'
        self.worker.api.add_format.side_effect = [
            RuntimeError('replace failed'), RuntimeError('restore failed')]

        with self.assertRaisesRegex(
                RuntimeError, 'verified source remains'):
            self.worker._store_variants(
                self.ebook, self.variant_payload('bundle_data'))

        self.worker.api.remove_extra_files.assert_not_called()

    @patch.object(ConversionWorker, '_variant_manifest')
    @patch.object(ConversionWorker, '_write_manifest')
    @patch.object(ConversionWorker, '_add_verified_extra_files')
    @patch.object(ConversionWorker, '_prepare_variant_metadata')
    def test_store_variants_a_never_replaces_when_data_verification_fails(
            self, mock_metadata, mock_add_extra, mock_manifest_file,
            mock_manifest):
        self.configure_variant_ebook()
        mock_metadata.side_effect = [
            (Mock(), 'Chinese title'), (Mock(), 'Bilingual title')]
        mock_manifest_file.return_value = '/manifest.json'
        mock_add_extra.side_effect = RuntimeError('hash mismatch')

        with self.assertRaisesRegex(RuntimeError, 'hash mismatch'):
            self.worker._store_variants(
                self.ebook, self.variant_payload('bundle_data'))

        self.worker.api.add_format.assert_not_called()
        self.worker.api.remove_extra_files.assert_called_once()

    @patch.object(ConversionWorker, '_variant_manifest')
    @patch.object(ConversionWorker, '_write_manifest')
    @patch.object(ConversionWorker, '_add_verified_extra_files')
    @patch.object(ConversionWorker, '_prepare_variant_metadata')
    def test_store_variants_b_trashes_new_book_on_data_failure(
            self, mock_metadata, mock_add_extra, mock_manifest_file,
            mock_manifest):
        self.configure_variant_ebook()
        mock_metadata.side_effect = [
            (Mock(), 'Chinese title'), (Mock(), 'Bilingual title')]
        mock_manifest_file.return_value = '/manifest.json'
        self.worker.db.create_book_entry.return_value = 21
        mock_add_extra.side_effect = RuntimeError('fail')

        with self.assertRaises(RuntimeError):
            self.worker._store_variants(
                self.ebook, self.variant_payload('linked_pair'))

        self.worker.api.remove_books.assert_called_once_with(
            [21], permanent=False)
        self.assertNotIn(
            7, self.worker.api.remove_books.call_args.args[0])

    def test_create_variant_book_trashes_record_when_format_add_fails(self):
        self.worker.db.create_book_entry.return_value = 21
        self.worker.api.add_format.side_effect = RuntimeError('fail')

        with self.assertRaisesRegex(RuntimeError, 'fail'):
            self.worker._create_variant_book(Mock(), '/translation.epub')

        self.worker.api.remove_books.assert_called_once_with(
            [21], permanent=False)

    @patch.object(ConversionWorker, '_prepare_variant_metadata')
    def test_store_variants_c_trashes_both_if_second_format_fails(
            self, mock_metadata):
        self.configure_variant_ebook()
        mock_metadata.side_effect = [
            (Mock(), 'Chinese title'), (Mock(), 'Bilingual title')]
        self.worker.db.create_book_entry.side_effect = [21, 22]
        self.worker.api.add_format.side_effect = [
            True, RuntimeError('second format failed')]

        with self.assertRaisesRegex(RuntimeError, 'second format failed'):
            self.worker._store_variants(
                self.ebook, self.variant_payload('three_records'))

        self.assertEqual([
            call([22], permanent=False),
            call([21], permanent=False),
        ], self.worker.api.remove_books.call_args_list)

    @patch(module_name + '.file_sha256')
    def test_add_verified_extra_files_rejects_hash_mismatch(self, mock_hash):
        self.worker.api.list_extra_files.return_value = [SimpleNamespace(
            relpath='data/translation-variants/run/source.epub',
            file_path='/stored/source.epub')]
        mock_hash.side_effect = ['source-hash', 'different-hash']

        with self.assertRaisesRegex(RuntimeError, 'hash mismatch'):
            self.worker._add_verified_extra_files(7, {
                'data/translation-variants/run/source.epub': '/source.epub'})

    def test_translate_done_job_failed_debug(self):
        self.job.failed = True
        with patch(module_name + '.DEBUG', True):
            self.worker.translate_done(self.job)
            self.gui.job_exception.assert_not_called()

    def test_translate_done_job_failed_not_debug(self):
        with patch(module_name + '.DEBUG', False):
            self.worker.translate_done(self.job)
            self.gui.job_exception.assert_called_once_with(
                self.job, dialog_title='Translation job failed')

    @patch(module_name + '.os')
    @patch(module_name + '.open')
    @patch(module_name + '.get_metadata')
    @patch(module_name + '.set_metadata')
    def test_translate_done_ebook_to_library(
            self, mock_set_metadata, mock_get_metadata, mock_open, mock_os):
        self.job.failed = False
        self.job.description = 'test description'
        self.job.log_path = '/path/to/log'
        metadata_config = {
            'subjects': ['test subject 1', 'test subject 2'],
            'lang_code': True,
            'lang_mark': True,
        }
        self.worker.config = {
            'ebook_metadata': metadata_config,
            'to_library': True,
        }
        self.ebook.is_extra_format.return_value = False
        self.ebook.title = 'test title'
        self.ebook.input_format = 'epub'
        self.ebook.output_format = 'epub'
        self.ebook.custom_title = 'test custom title'
        self.ebook.target_lang = 'German'
        self.ebook.lang_code = 'de'
        file = Mock()
        mock_open.return_value.__enter__.return_value = file
        metadata = Mock()
        metadata.title = 'test title'
        metadata.tags = []
        metadata.language = 'en'
        mock_get_metadata.return_value = metadata

        self.worker.db.create_book_entry.return_value = 89
        self.worker.api.format_abspath.return_value = '/path/to/test[m].epub'

        self.worker.translate_done(self.job)

        mock_open.assert_called_once_with(
            str(Path('/path/to/test.epub')), 'r+b')
        mock_get_metadata.assert_called_once_with(file, 'epub')
        mock_set_metadata.assert_called_once_with(file, metadata, 'epub')
        self.assertEqual('test custom title [German]', metadata.title)
        self.assertEqual('de', metadata.language)
        self.assertEqual([
            'test subject 1', 'test subject 2', 'Translated by Ebook '
            'Translator: https://translator.bookfere.com'], metadata.tags)

        self.worker.db.create_book_entry.assert_called_once_with(metadata)
        self.worker.api.add_format.assert_called_once_with(
            89, 'epub', str(Path('/path/to/test.epub')), run_hooks=False)
        self.worker.gui.library_view.model.assert_called_once()
        self.worker.gui.library_view.model().books_added \
            .assert_called_once_with(1)
        self.worker.api.format_abspath.assert_called_once_with(89, 'epub')

        self.worker.gui.status_bar.show_message.assert_called_once_with(
            'test description completed', 5000)
        arguments = self.worker.gui.proceed_question.mock_calls[0].args
        self.assertIsInstance(arguments[0], Callable)
        self.assertIs(self.worker.gui.job_manager.launch_gui_app, arguments[1])
        self.assertEqual('/path/to/log', arguments[2])
        self.assertEqual('Ebook Translation Log', arguments[3])
        self.assertEqual('Translation Completed', arguments[4])
        self.assertEqual(
            'The translation of "test custom title [German]" was completed. '
            'Do you want to open the book?',
            arguments[5])

        mock_payload = Mock()
        arguments[0](mock_payload)
        mock_payload.assert_called_once_with(
            'ebook-viewer',
            kwargs={'args': ['ebook-viewer', '/path/to/test[m].epub']})

        arguments = self.worker.gui.proceed_question.mock_calls[0].kwargs
        self.assertEqual(True, arguments.get('log_is_file'))
        self.assertIs(self.icon, arguments.get('icon'))


    @patch(module_name + '.open')
    @patch(module_name + '.open_path')
    @patch(module_name + '.os.rename')
    @patch(module_name + '.get_metadata')
    @patch(module_name + '.set_metadata')
    def test_translate_done_ebook_to_path(
            self, mock_set_metadata, mock_get_metadata, mock_os_rename,
            mock_open_path, mock_open):
        self.job.failed = False
        self.job.description = 'test description'
        self.job.log_path = str(Path('/path/to/log'))
        metadata_config = {
            'subjects': ['test subject 1', 'test subject 2'],
            'lang_code': True,
            'lang_mark': True,
        }
        self.worker.config = {
            'ebook_metadata': metadata_config,
            'to_library': False,
        }
        self.ebook.is_extra_format.return_value = False
        self.ebook.title = 'test title'
        self.ebook.input_format = 'epub'
        self.ebook.output_format = 'epub'
        self.ebook.custom_title = 'test: custom title*'
        self.ebook.target_lang = 'German'
        self.ebook.lang_code = 'de'
        file = Mock()
        mock_open.return_value.__enter__.return_value = file
        metadata = Mock()
        metadata.title = 'test title'
        metadata.tags = []
        metadata.language = 'en'
        mock_get_metadata.return_value = metadata

        self.worker.translate_done(self.job)

        original_path = str(Path('/path/to/test.epub'))
        new_path = str(Path('/path/to/test_ custom title_ [German].epub'))

        mock_open.assert_called_once_with(original_path, 'r+b')
        mock_os_rename.assert_called_once_with(original_path, new_path)
        self.worker.gui.status_bar.show_message.assert_called_once_with(
            'test description ' + 'completed', 5000)
        arguments = self.worker.gui.proceed_question.mock_calls[0].args
        self.assertIsInstance(arguments[0], Callable)
        self.assertIs(self.worker.gui.job_manager.launch_gui_app, arguments[1])
        self.assertEqual(str(Path('/path/to/log')), arguments[2])
        self.assertEqual('Ebook Translation Log', arguments[3])
        self.assertEqual('Translation Completed', arguments[4])
        self.assertEqual(
            'The translation of "test: custom title* [German]" was completed. '
            'Do you want to open the book?',
            arguments[5])

        mock_payload = Mock()
        arguments[0](mock_payload)
        mock_payload.assert_called_once_with(
            'ebook-viewer', kwargs={'args': [
                'ebook-viewer',
                str(Path('/path/to/test_ custom title_ [German].epub'))]})

        arguments = self.worker.gui.proceed_question.mock_calls[0].kwargs
        self.assertEqual(True, arguments.get('log_is_file'))
        self.assertIs(self.icon, arguments.get('icon'))


    @patch(module_name + '.open_path')
    @patch(module_name + '.os.rename')
    @patch(module_name + '.open')
    def test_translate_done_other_to_library(
            self, mock_open, mock_os_rename, mock_open_path):
        self.job.failed = False
        self.job.description = 'test description'
        self.job.log_path = str(Path('/path/to/log'))
        metadata_config = {'lang_mark': True}
        self.worker.config = {
            'ebook_metadata': metadata_config,
            'to_library': True,
        }
        self.ebook.is_extra_format.return_value = True
        self.ebook.id = 89
        self.ebook.title = 'test title'
        self.ebook.custom_title = 'test custom title'
        self.ebook.input_format = 'srt'
        self.ebook.output_format = 'srt'
        self.ebook.custom_title = 'test custom title'
        self.ebook.target_lang = 'German'
        self.worker.working_jobs = {
            self.job: (self.ebook, str(Path('/path/to/test.srt')))}
        metadata = Mock()
        self.worker.api.get_metadata.return_value = metadata
        self.worker.api.format_abspath.return_value = \
            str(Path('/path/to/test[m].srt'))
        self.worker.db.create_book_entry.return_value = 90

        self.worker.translate_done(self.job)

        self.worker.api.get_metadata.assert_called_once_with(89)
        self.worker.db.create_book_entry.assert_called_once_with(metadata)
        self.worker.api.add_format.assert_called_once_with(
            90, 'srt', str(Path('/path/to/test.srt')), run_hooks=False)
        self.worker.gui.library_view.model.assert_called_once()
        self.worker.gui.library_view.model().books_added \
            .assert_called_once_with(1)
        self.worker.api.format_abspath.assert_called_once_with(90, 'srt')
        self.worker.gui.status_bar.show_message.assert_called_once_with(
            'test description ' + 'completed', 5000)
        self.assertEqual('test custom title [German]', metadata.title)

        arguments = self.worker.gui.proceed_question.mock_calls[0].args
        self.assertIsInstance(arguments[0], Callable)
        self.assertIs(self.worker.gui.job_manager.launch_gui_app, arguments[1])
        self.assertEqual(str(Path('/path/to/log')), arguments[2])
        self.assertEqual('Ebook Translation Log', arguments[3])
        self.assertEqual('Translation Completed', arguments[4])
        self.assertEqual(
            'The translation of "test custom title [German]" was completed. '
            'Do you want to open the book?',
            arguments[5])

        mock_payload = Mock()
        arguments[0](mock_payload)
        mock_open_path.assert_called_once_with(
            str(Path('/path/to/test[m].srt')))

        arguments = self.worker.gui.proceed_question.mock_calls[0].kwargs
        self.assertEqual(True, arguments.get('log_is_file'))
        self.assertIs(self.icon, arguments.get('icon'))

    @patch(module_name + '.open_path')
    @patch(module_name + '.os.rename')
    @patch(module_name + '.open')
    def test_translate_done_other_to_path(
            self, mock_open, mock_os_rename, mock_open_path):
        self.job.failed = False
        self.job.description = 'test description'
        self.job.log_path = str(Path('/path/to/log'))
        metadata_config = {'lang_mark': True}
        self.worker.config = {
            'ebook_metadata': metadata_config,
            'to_library': False,
        }
        self.ebook.is_extra_format.return_value = True
        self.ebook.id = 89
        self.ebook.title = 'test title'
        self.ebook.custom_title = 'test custom title'
        self.ebook.input_format = 'srt'
        self.ebook.output_format = 'srt'
        self.ebook.custom_title = 'test: custom title*'
        self.ebook.target_lang = 'German'
        self.worker.working_jobs = {
            self.job: (self.ebook, str(Path('/path/to/test.srt')))}
        metadata = Mock()
        self.worker.api.get_metadata.return_value = metadata

        self.worker.translate_done(self.job)

        self.worker.api.get_metadata.assert_called_once_with(89)
        mock_os_rename.assert_called_once_with(
            str(Path('/path/to/test.srt')),
            str(Path('/path/to/test_ custom title_ [German].srt')))
        self.worker.gui.status_bar.show_message.assert_called_once_with(
            'test description ' + 'completed', 5000)
        arguments = self.worker.gui.proceed_question.mock_calls[0].args
        self.assertIsInstance(arguments[0], Callable)
        self.assertIs(self.worker.gui.job_manager.launch_gui_app, arguments[1])
        self.assertEqual(str(Path('/path/to/log')), arguments[2])
        self.assertEqual('Ebook Translation Log', arguments[3])
        self.assertEqual('Translation Completed', arguments[4])
        self.assertEqual(
            'The translation of "test: custom title* [German]" was completed. '
            'Do you want to open the book?',
            arguments[5])

        mock_payload = Mock()
        arguments[0](mock_payload)
        mock_open_path.assert_called_once_with(
            str(Path('/path/to/test_ custom title_ [German].srt')))

        arguments = self.worker.gui.proceed_question.mock_calls[0].kwargs
        self.assertEqual(True, arguments.get('log_is_file'))
        self.assertIs(self.icon, arguments.get('icon'))
