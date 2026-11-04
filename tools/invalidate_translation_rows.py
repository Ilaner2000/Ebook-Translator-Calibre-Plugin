"""Run with calibre-debug; default dry-run, --apply uses native plugin API only.

    calibre-debug -e tools/invalidate_translation_rows.py -- --manifest plan.json

Stop other jobs for the identity and back up its cache before applying. The
reviewed manifest supplies exact IDs/MD5s/old target hashes. Each native update
commits one row; a failure receipt lists already-applied IDs explicitly.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def prepare(cache, entries):
    if not entries or len({p['id'] for p in entries}) != len(entries):
        raise ValueError('empty or duplicate row allowlist')
    planned = []
    for expected in entries:
        pid = expected['id']
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
            raise ValueError('invalid row id')
        paragraph = cache.paragraph(pid)
        if (paragraph.md5 != expected['source_md5'] or not isinstance(paragraph.translation, str)
                or hashlib.sha256(paragraph.translation.encode()).hexdigest() != expected['old_target_sha256']):
            raise ValueError('row identity/old target mismatch: %s' % pid)
        planned.append(paragraph)
    return planned


def apply(cache, planned, completed):
    for paragraph in planned:
        paragraph.translation = None
        paragraph.degraded = False
        paragraph.degrade_reason = None
        cache.update_paragraph(paragraph)
        completed.append(paragraph.id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--apply', action='store_true')
    argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:]
    args = parser.parse_args(argv)
    manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8-sig'))
    if manifest.get('schema') != 'ebook-translator-row-invalidation-v1':
        raise ValueError('unknown manifest schema')
    identity = manifest['cache_identity']
    if not re.fullmatch('[0-9a-f]{32}', identity):
        raise ValueError('invalid cache identity')
    if hashlib.sha256(Path(manifest['source_path']).read_bytes()).hexdigest() != manifest['source_sha256']:
        raise ValueError('source EPUB digest mismatch')
    from calibre.customize.ui import find_plugin
    plugin = find_plugin('Ebook Translator')
    if list(plugin.version) != manifest['plugin_version']:
        raise ValueError('installed plugin version mismatch')
    from calibre_plugins.ebook_translator.lib.cache import TranslationCache
    if not (Path(TranslationCache.cache_path) / (identity + '.db')).is_file():
        raise ValueError('cache does not exist; refuse creation')
    cache = TranslationCache(identity)
    completed = []
    try:
        planned = prepare(cache, manifest['rows'])
        if args.apply:
            apply(cache, planned, completed)
        print(json.dumps({'status': 'applied' if args.apply else 'dry_run', 'cache_identity': identity,
                          'planned_ids': [p.id for p in planned], 'applied_ids': completed,
                          'source_sha256': manifest['source_sha256']}, sort_keys=True))
    except Exception as error:
        print(json.dumps({'status': 'failed', 'applied_ids': completed, 'error': str(error)}, sort_keys=True))
        raise
    finally:
        cache.close()


if __name__ == '__main__':
    main()
