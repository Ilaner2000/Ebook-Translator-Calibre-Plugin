import hashlib
import unittest
from unittest.mock import Mock
from ...lib.cache import Paragraph
from ...tools.invalidate_translation_rows import prepare, apply


class TestRowInvalidation(unittest.TestCase):
    def setup_cache(self):
        paragraphs = [Paragraph(i, 'source-md5-'+str(i), '<p/>', 'source '+str(i), translation='old '+str(i)) for i in [1, 2]]
        cache = Mock(); cache.paragraph.side_effect = paragraphs
        entries = [{'id':p.id,'source_md5':p.md5,'old_target_sha256':hashlib.sha256(p.translation.encode()).hexdigest()} for p in paragraphs]
        return cache, paragraphs, entries

    def test_bad_late_digest_or_source_does_not_invalidate_earlier_row(self):
        for field in ['source_md5', 'old_target_sha256']:
            cache, paragraphs, entries = self.setup_cache(); entries[-1][field] = 'wrong'
            with self.assertRaises(ValueError): prepare(cache, entries)
            cache.update_paragraph.assert_not_called()
            self.assertEqual(['old 1','old 2'], [p.translation for p in paragraphs])

    def test_empty_duplicate_boolean_and_negative_ids_rejected(self):
        for entries in [[], [{'id':1},{'id':1}], [{'id':True}], [{'id':-1}]]:
            cache = Mock()
            with self.assertRaises(ValueError): prepare(cache, entries)
            cache.update_paragraph.assert_not_called()

    def test_native_api_only_changes_target_and_declared_metadata(self):
        cache, paragraphs, entries = self.setup_cache()
        before = [(p.id,p.md5,p.original,p.raw) for p in paragraphs]
        planned = prepare(cache, entries); completed = []; apply(cache, planned, completed)
        self.assertEqual([1,2], completed); self.assertEqual(2, cache.update_paragraph.call_count)
        self.assertEqual(before, [(p.id,p.md5,p.original,p.raw) for p in paragraphs])
        self.assertTrue(all(p.translation is None and not p.degraded and p.degrade_reason is None for p in paragraphs))
        cache.delete.assert_not_called()

    def test_partial_api_failure_retains_precise_completed_ids(self):
        cache, paragraphs, entries = self.setup_cache(); planned = prepare(cache, entries)
        cache.update_paragraph.side_effect = [None, RuntimeError('simulated disk error')]
        completed = []
        with self.assertRaises(RuntimeError): apply(cache, planned, completed)
        self.assertEqual([1], completed)
