"""Offline chapter trials: cache reuse through the real only-mode renderer.

No library/cache writes or translation calls. The caller owns the isolated
Calibre runtime. All candidate content is assembled before exclusive output.
"""
import copy
import json
import mimetypes
import posixpath
import re
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED
from lxml import etree

from .element import PageElement, ElementHandler
from .only_mode import OnlyPolicy
from ..engines.base import Base

X = 'http://www.w3.org/1999/xhtml'
OPF = 'http://www.idpf.org/2007/opf'
DC = 'http://purl.org/dc/elements/1.1/'


def parse(raw):
    return etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))


def xml(root):
    return etree.tostring(root, encoding='utf-8', xml_declaration=True)


def cache_node(raw):
    # Cache serializes XHTML fragments without the default namespace.
    holder = parse(('<holder xmlns="%s">%s</holder>' % (X, raw)).encode())
    return holder[0]


def render_cached_blocks(document, rows, policy, marked=None):
    """Return a fresh document; a bad marker never replaces a source block."""
    root = parse(document)
    handler = ElementHandler(Base.placeholder, Base.separator, 'only')
    handler.load_remove_rules()
    handler.load_reserve_rules()
    rendered = []
    for row in rows:
        if row['ignored']:
            continue
        source = cache_node(row['raw'])
        block_id = source.get('id')
        nodes = root.xpath('//*[@id=$id]', id=block_id)
        if len(nodes) != 1:
            raise ValueError('cache block must identify exactly one chapter node: %s' % block_id)
        old = nodes[0]
        source.tail = old.tail
        parent = old.getparent()
        parent.replace(old, source)
        element = PageElement(source)
        element.set_position('only')
        element.set_placeholder(Base.placeholder)
        element.set_remove_pattern(handler.remove_pattern)
        element.set_reserve_pattern(handler.reserve_pattern)
        # Adding a new lang value can change Chromium's CJK fallback font.
        # Keep the control's language metadata so A1 compares spacing only.
        element.set_translation_lang(old.get('lang'))
        element.set_target_direction(old.get('dir', 'auto'))
        element.set_only_policy(policy)
        prepared = element.get_content()
        if marked is None:
            if prepared != row['original']:
                raise ValueError('plain cache extraction mismatch for %s' % block_id)
            target = row['translation']
        else:
            if row['id'] not in marked:
                raise ValueError('missing aligned target for %s' % block_id)
            target = marked[row['id']]
        if not target:
            raise ValueError('empty cached target for %s' % block_id)
        element.add_translation(target)
        rendered.append(block_id)
    return root, rendered


def first_chapter_notes(document, ids):
    root = parse(document)
    body = root.find('{%s}body' % X)
    kept = []
    for block_id in ids:
        matches = root.xpath('//*[@id=$id]', id=block_id)
        if len(matches) != 1:
            raise ValueError('missing unique supporting note: %s' % block_id)
        kept.append(copy.deepcopy(matches[0]))
    for child in list(body):
        body.remove(child)
    body.text = None
    for child in kept:
        body.append(child)
    return root


def link_report(documents):
    """Validate local link target files and fragments, including footnote returns."""
    roots = {path: parse(raw) for path, raw in documents.items()}
    ids = {path: {x.get('id') for x in root.iter() if x.get('id')} for path, root in roots.items()}
    links = []
    for path, root in roots.items():
        for node in root.xpath('//*[@href]'):
            href = node.get('href')
            url = urlsplit(href)
            if url.scheme or url.netloc or not (url.path.endswith(('.xhtml', '.html')) or url.fragment):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(url.path))) if url.path else path
            valid = target in roots and (not url.fragment or unquote(url.fragment) in ids[target])
            links.append({'from': path, 'from_id': node.get('id'), 'href': href, 'target': target, 'valid': valid})
    return {'links': links, 'valid': sum(x['valid'] for x in links), 'invalid': [x for x in links if not x['valid']]}


def build_epub(input_epub, output_epub, chapter_path, chapter, notes_path, notes, title):
    """Create a new one-chapter EPUB; preserve publisher CSS/fonts/images.

    Supporting notes are non-linear. Other source chapter HTML is excluded.
    The library input is only opened for reading; output must not exist.
    """
    output = Path(output_epub)
    if Path(input_epub).resolve() == output.resolve():
        raise ValueError('candidate output cannot be its input')
    nav_path = posixpath.join(posixpath.dirname(chapter_path), 'toc.xhtml')
    nav = etree.Element('{%s}html' % X, nsmap={None:X, 'epub':'http://www.idpf.org/2007/ops'})
    nav.set('lang', 'zh-Hant')
    head = etree.SubElement(nav, '{%s}head' % X)
    etree.SubElement(head, '{%s}title' % X).text = title
    body = etree.SubElement(nav, '{%s}body' % X)
    n = etree.SubElement(body, '{%s}nav' % X)
    n.set('{http://www.idpf.org/2007/ops}type', 'toc')
    ol = etree.SubElement(n, '{%s}ol' % X)
    for anchor, dest, label in (('toc8', posixpath.basename(chapter_path)+'#ji_93', '第一章：非基因所定'),
                                ('toc21', posixpath.basename(notes_path)+'#ji_965', '第一章註解')):
        li = etree.SubElement(ol, '{%s}li' % X, id=anchor)
        etree.SubElement(li, '{%s}a' % X, href=dest).text = label
    documents = {chapter_path:xml(chapter), notes_path:xml(notes), nav_path:xml(nav)}
    links = link_report(documents)
    with ZipFile(input_epub) as source:
        container = parse(source.read('META-INF/container.xml'))
        old_opf = container.xpath('//*[local-name()="rootfile"]')[0].get('full-path')
        old_package = parse(source.read(old_opf))
        old_dir = posixpath.dirname(old_opf)
        media = {}
        for item in old_package.xpath('//*[local-name()="manifest"]/*'):
            media[posixpath.normpath(posixpath.join(old_dir,item.get('href')))] = item.get('media-type')
        assets = {}
        for name in source.namelist():
            if name.endswith('/') or name in ('mimetype','META-INF/container.xml') or name.startswith('META-INF/'):
                continue
            if name in documents or name.lower().endswith(('.xhtml','.html','.htm','.opf','.ncx')):
                continue
            # Preserve rendering assets, not old navigation/reading-position data.
            if not (name.lower().endswith(('.css','.jpg','.jpeg','.png','.gif','.svg','.webp','.ttf','.otf','.woff','.woff2'))):
                continue
            assets[name] = source.read(name)
    package = etree.Element('{%s}package' % OPF, nsmap={None:OPF,'dc':DC}, version='3.0', attrib={'unique-identifier':'uid'})
    metadata = etree.SubElement(package,'{%s}metadata' % OPF)
    etree.SubElement(metadata,'{%s}identifier' % DC,id='uid').text='urn:uuid:'+str(uuid.uuid4())
    etree.SubElement(metadata,'{%s}title' % DC).text=title
    etree.SubElement(metadata,'{%s}language' % DC).text='zh-Hant'
    # A fixed trial timestamp keeps rebuilds independent of live reader state.
    etree.SubElement(metadata,'{%s}meta' % OPF,property='dcterms:modified').text='2026-11-03T00:00:00Z'
    manifest=etree.SubElement(package,'{%s}manifest' % OPF)
    all_files={**assets,**documents}
    item_ids={}
    for index,name in enumerate(sorted(all_files)):
        item_id='item%d' % index;item_ids[name]=item_id
        mime='application/xhtml+xml' if name in documents else media.get(name) or mimetypes.guess_type(name)[0]
        if not mime:raise ValueError('unknown EPUB media type: '+name)
        item=etree.SubElement(manifest,'{%s}item' % OPF,id=item_id,href=name,attrib={'media-type':mime})
        if name==nav_path:item.set('properties','nav')
    spine=etree.SubElement(package,'{%s}spine' % OPF)
    etree.SubElement(spine,'{%s}itemref' % OPF,idref=item_ids[chapter_path])
    etree.SubElement(spine,'{%s}itemref' % OPF,idref=item_ids[notes_path],linear='no')
    container=b'<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    # No output is opened until all rendering and package checks have finished.
    with ZipFile(output,'x') as target:
        target.writestr('mimetype',b'application/epub+zip',compress_type=ZIP_STORED)
        target.writestr('META-INF/container.xml',container,compress_type=ZIP_DEFLATED)
        target.writestr('content.opf',xml(package),compress_type=ZIP_DEFLATED)
        for name,data in sorted(all_files.items()):target.writestr(name,data,compress_type=ZIP_DEFLATED)
    return {'path':str(output),'files':len(all_files)+3,'linear_chapters':1,'supporting_notes':1,
            'links':links,'documents':documents}
