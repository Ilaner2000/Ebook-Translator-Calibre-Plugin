import unittest
from unittest.mock import Mock, call, patch

from ...lib.conversion import ConversionWorker
from ...lib.config import Configuration
from . import test_conversion as fixtures


MODULE = 'calibre_plugins.ebook_translator.lib.conversion'


class TestVariantMain(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.TestConversionWorker()
        fixture.setUp()
        fixture.configure_variant_ebook()
        self.worker, self.ebook = fixture.worker, fixture.ebook
        self.payload = fixture.variant_payload('bundle_data')
        for name in ('_prepare_variant_metadata', '_add_verified_extra_files',
                     '_write_manifest', '_variant_manifest'):
            patcher = patch.object(self.worker, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self._prepare_variant_metadata.side_effect = [
            (Mock(), 'Chinese title'), (Mock(), 'Bilingual title')]
        self._write_manifest.return_value = '/manifest.json'

    def select_main(self, kind):
        self.worker.config['translation_variant_main'] = kind
        self.payload['main_variant'] = kind

    def test_default_and_invalid_main_are_translation(self):
        self.assertEqual('translation', Configuration().get(
            'translation_variant_main'))
        for value in ('translation', 'invalid', None):
            self.worker.config['translation_variant_main'] = value
            self.assertEqual('translation', self.worker._variant_main())
        self.worker.config['translation_variant_main'] = 'bilingual'
        self.assertEqual('bilingual', self.worker._variant_main())

    @patch(MODULE + '.file_sha256', return_value='same-hash')
    def test_bilingual_main_stores_translation_data_and_role(self, _hash):
        self.select_main('bilingual')
        result = self.worker._store_variants(self.ebook, self.payload)
        files = self._add_verified_extra_files.call_args.args[1]
        self.assertEqual({
            'data/translation-variants/run-1/source.epub': '/source.epub',
            'data/translation-variants/run-1/translation.zh-Hant.epub':
                '/translation.epub',
            'data/translation-variants/run-1/manifest.json': '/manifest.json',
        }, files)
        self.worker.api.add_format.assert_called_once_with(
            7, 'EPUB', '/bilingual.epub', replace=True, run_hooks=False)
        self.assertEqual([
            call('tags', {7: ['Topic', 'AY_雙語', '雙語_', '附中譯_']}),
            call('identifiers', {7: {
                'isbn': '123', 'ebook-translator-variant': 'bilingual:run-1'}}),
        ], self.worker.api.set_field.call_args_list)
        self.assertEqual({'translation': None, 'bilingual': 7},
                         self._variant_manifest.call_args.args[3])
        self.assertEqual('bilingual',
                         self._variant_manifest.call_args.args[1]['main_variant'])
        self.assertEqual(('/library/translation.epub', 'Bilingual title', 0), result)
        self.worker.db.create_book_entry.assert_not_called()
        self.assertEqual([call('/bilingual.epub'),
                          call('/library/translation.epub')],
                         _hash.call_args_list)

    @patch(MODULE + '.file_sha256', return_value='same-hash')
    def test_main_selection_is_frozen_in_job_payload(self, _hash):
        self.select_main('bilingual')
        self.worker.config['translation_variant_main'] = 'translation'
        self.worker._store_variants(self.ebook, self.payload)
        self.worker.api.add_format.assert_called_once_with(
            7, 'EPUB', '/bilingual.epub', replace=True, run_hooks=False)

    def test_bilingual_replace_failure_restores_source(self):
        self.select_main('bilingual')
        self.worker.api.add_format.side_effect = [RuntimeError('replace'), True]
        with self.assertRaisesRegex(RuntimeError, 'replace'):
            self.worker._store_variants(self.ebook, self.payload)
        self.assertEqual([
            call(7, 'EPUB', '/bilingual.epub', replace=True, run_hooks=False),
            call(7, 'EPUB', '/source.epub', replace=True, run_hooks=False),
        ], self.worker.api.add_format.call_args_list)
        self.assertEqual([
            call('tags', {7: ['Topic', 'AY_雙語', '雙語_', '附中譯_']}),
            call('identifiers', {7: {'isbn': '123'}}),
            call('tags', {7: ['Topic', 'AY_雙語']}),
        ], self.worker.api.set_field.call_args_list)
        self.worker.api.remove_extra_files.assert_called_once()

    @patch(MODULE + '.file_sha256', return_value='same-hash')
    def test_bilingual_identifier_failure_restores_source_and_markers(self, _hash):
        self.select_main('bilingual')
        self.worker.api.set_field.side_effect = [None, RuntimeError('marker'),
                                                None, None]
        with self.assertRaisesRegex(RuntimeError, 'marker'):
            self.worker._store_variants(self.ebook, self.payload)
        self.assertEqual('/source.epub',
                         self.worker.api.add_format.call_args_list[-1].args[2])
        self.assertEqual(call('tags', {7: ['Topic', 'AY_雙語']}),
                         self.worker.api.set_field.call_args_list[-1])
        self.worker.api.remove_extra_files.assert_called_once()

    def test_bilingual_rejected_replace_restores_source(self):
        self.select_main('bilingual')
        self.worker.api.add_format.side_effect = [False, True]
        with self.assertRaisesRegex(RuntimeError, 'Could not replace'):
            self.worker._store_variants(self.ebook, self.payload)
        self.assertEqual('/source.epub',
                         self.worker.api.add_format.call_args_list[-1].args[2])
        self.worker.api.remove_extra_files.assert_called_once()

    @patch(MODULE + '.file_sha256', side_effect=['expected', 'bad'])
    def test_bilingual_hash_mismatch_restores_source(self, _hash):
        self.select_main('bilingual')
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            self.worker._store_variants(self.ebook, self.payload)
        self.assertEqual('/source.epub',
                         self.worker.api.add_format.call_args_list[-1].args[2])
        self.worker.api.remove_extra_files.assert_called_once()

    def test_bilingual_restore_failure_keeps_verified_source_data(self):
        self.select_main('bilingual')
        self.worker.api.add_format.side_effect = [
            RuntimeError('replace'), RuntimeError('restore')]
        with self.assertRaisesRegex(RuntimeError, 'verified source remains'):
            self.worker._store_variants(self.ebook, self.payload)
        self.worker.api.remove_extra_files.assert_not_called()

    def test_bilingual_unverified_data_never_replaces_main(self):
        self.select_main('bilingual')
        self._add_verified_extra_files.side_effect = RuntimeError('bad data')
        with self.assertRaisesRegex(RuntimeError, 'bad data'):
            self.worker._store_variants(self.ebook, self.payload)
        self.worker.api.add_format.assert_not_called()
        self.worker.api.remove_extra_files.assert_called_once()

    def test_bilingual_tags_are_chinese_only_and_b_c_unchanged(self):
        for storage in ('linked_pair', 'three_records'):
            for kind in ('bilingual', 'translation'):
                self.assertEqual(
                    self.worker._variant_role_tags(self.ebook, storage, kind),
                    self.worker._variant_role_tags(
                        self.ebook, storage, kind, 'bilingual'))
        self.ebook.lang_code = 'de'
        self.ebook.target_lang = 'German'
        for kind in ('translation', 'bilingual'):
            self.assertEqual([], self.worker._variant_role_tags(
                self.ebook, 'bundle_data', kind, 'bilingual'))

    @patch(MODULE + '.file_sha256', return_value='same-hash')
    def test_unset_language_uses_translation_target_filename(self, _hash):
        self.select_main('bilingual')
        self.ebook.lang_code = None
        self.worker._store_variants(self.ebook, self.payload)
        self.assertIn('data/translation-variants/run-1/translation.target.epub',
                      self._add_verified_extra_files.call_args.args[1])
