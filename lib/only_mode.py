"""Fail-closed Chinese-only rendering policies (V2 by default in the factory).

Paired markers identify semantic runs, never source character offsets.  The
translator must return each marker once with the original nesting; malformed
output raises before the source element is removed.  Existing rendering is
unchanged when a handler explicitly disables its policy.
"""
from dataclasses import dataclass
from collections import Counter
import copy
import html
import json
import math
import re
import unicodedata
from urllib.parse import urlsplit
from lxml import etree


class InlineAlignmentError(ValueError):
    pass


@dataclass(frozen=True)
class OnlyPolicy:
    line_height: float = 1.7
    paragraph_gap: float = 0.75
    text_align: str = 'start'
    body_classes: tuple = ('TX', 'TX1', 'TXb')
    intro_classes: tuple = ('COTX',)
    quote_classes: tuple = ('V', 'V_f', 'V_l')
    heading_link_classes: tuple = ('CN', 'CT')
    preserve_inline: bool = False
    auto_roles: bool = False
    dropcap_classes: tuple = ('_idGenDropcap-1', '_idgendropcap')
    smallcaps_classes: tuple = ('first-line',)

    def __post_init__(self):
        if isinstance(self.line_height, bool) or isinstance(self.paragraph_gap, bool):
            raise ValueError('typography values must be numeric, not booleans')
        if not math.isfinite(float(self.line_height)) or not 1.2 <= float(self.line_height) <= 2.4:
            raise ValueError('only line height must be finite and in [1.2, 2.4]')
        if not math.isfinite(float(self.paragraph_gap)) or not 0 <= float(self.paragraph_gap) <= 2:
            raise ValueError('only paragraph gap must be finite and in [0, 2]')
        if self.text_align not in ('start', 'left', 'justify'):
            raise ValueError('unsupported only text alignment')
        object.__setattr__(self, 'line_height', float(self.line_height))
        object.__setattr__(self, 'paragraph_gap', float(self.paragraph_gap))
        for field in ('body_classes', 'intro_classes', 'quote_classes', 'heading_link_classes', 'dropcap_classes', 'smallcaps_classes'):
            values = getattr(self, field)
            if not isinstance(values, (tuple, list)) or any(not isinstance(x, str) or not x or re.search(r'\s', x) for x in values):
                raise ValueError('class roles must be explicit nonempty CSS tokens')
            object.__setattr__(self, field, tuple(values))

    @classmethod
    def from_config(cls, value):
        if value is None or value is False:
            return None
        if not isinstance(value, dict):
            raise ValueError('only policy must be a configuration object')
        return cls(**value)


def _local(node):
    return etree.QName(node).localname


def _classes(node):
    return {x.casefold() for x in node.get('class', '').split()}


def typography_role(node, policy):
    """Use explicit roles first, then conservative publisher class semantics.

    Never guess that every p is body text: captions, contents, and notes retain
    publisher spacing. Context is inspected on the original tree, not a detached
    output element.
    """
    classes = _classes(node)
    if policy.auto_roles:
        for ancestor in (node, *node.iterancestors()):
            if not isinstance(ancestor.tag, str):
                continue
            context = ' '.join(_classes(ancestor))
            role = ancestor.get('role', '') + ' ' + ancestor.get('{http://www.idpf.org/2007/ops}type', '')
            if (_local(ancestor) == 'aside' or re.search(r'footnote|rearnote|endnote|doc-noteref', role)
                    or re.search(r'footnote|endnote|caption|(?:^|\s)note(?:\s|$)', context)):
                return None
        if any(isinstance(x.tag, str) and _local(x) == 'blockquote'
               for x in (node, *node.iterancestors())):
            return 'quote'
    for role, values in (('intro', policy.intro_classes), ('quote', policy.quote_classes), ('body', policy.body_classes)):
        if classes & {x.casefold() for x in values}:
            return role
    if policy.auto_roles and _local(node) == 'p' and any(re.search(r'body[-_]?text', x) for x in classes):
        return 'body'
    return None


def apply_typography(node, policy, source=None):
    if policy is None:
        return
    classes = _classes(node)
    if policy.preserve_inline and classes & {x.casefold() for x in policy.heading_link_classes}:
        # Chapter-number/title anchors are navigation, not body references.
        # Keep their destination/ID but inherit the publisher heading color.
        # Footnotes and other links retain their original styling.
        for link in node.iterdescendants():
            if not isinstance(link.tag, str) or _local(link) != 'a':
                continue
            target = urlsplit(link.get('href', ''))
            if target.scheme or target.netloc or target.path.rsplit('/', 1)[-1].casefold() not in ('toc.xhtml', 'nav.xhtml'):
                continue
            style = link.get('style', '').strip().rstrip(';')
            link.set('style', ';'.join(x for x in (style, 'color:inherit', 'text-decoration:none') if x))
    role = typography_role(source if source is not None else node, policy)
    if role is None:
        return
    # No text-indent/font/heading/caption/footnote reset. Intro and verse keep
    # their publisher margins; ordinary body paragraphs get the selected gap.
    styles = [node.get('style', '').strip().rstrip(';'),
              'line-height:%g' % policy.line_height,
              'text-align:%s' % policy.text_align]
    if role == 'body':
        styles.extend(('margin-top:%gem' % policy.paragraph_gap,
                       'margin-bottom:0'))
    node.set('style', ';'.join(x for x in styles if x))


SEMANTIC = frozenset(('a', 'em', 'strong', 'i', 'b', 'small', 'span', 'u',
                      's', 'cite', 'q', 'del', 'ins', 'mark',
                      'ul', 'ol', 'li', 'dl', 'dt', 'dd',
                      # Extraction can select a whole quote/list/table. Pair
                      # its static child structure too, rather than flatten it.
                      'p', 'div', 'blockquote', 'pre', 'h1', 'h2', 'h3', 'h4',
                      'h5', 'h6', 'table', 'thead', 'tbody', 'tfoot', 'tr',
                      'th', 'td', 'caption'))
TOKEN = re.compile(r'⟦ET(\d+)(O|C)⟧')
MARKER_ATTR = 'data-et-inline-marker'


INLINE_INSTRUCTION = (
    'Formatting protocol: Translate only the prose. Copy every protected token '
    'exactly once, unchanged: paired tokens such as ⟦ET0O⟧ and ⟦ET0C⟧, and '
    'reserve tokens such as {{id_00000}} or <m id=00000 />. Keep each pair '
    'around the corresponding translated semantic phrase, with the same nesting '
    'and parent pair. Sibling phrases may move naturally. Empty pairs must stay '
    'empty. Do not invent tokens, explanations, markdown fences, or a preamble. '
    'Return only the translation with these tokens.'
)


def prepare_inline_body(body, source):
    """Add a per-request protocol to JSON chat messages without editing config.

    Unknown wire formats fail before a protected paragraph is sent. No engine,
    model, options, chunk size, endpoint, headers, or persistent prompt changes.
    """
    if '⟦ET' not in source and '{{id_' not in source and '<m id=' not in source:
        return body
    was_bytes = isinstance(body, bytes)
    was_string = isinstance(body, str)
    try:
        value = json.loads(body) if was_bytes or was_string else copy.deepcopy(body)
    except (ValueError, TypeError):
        raise InlineAlignmentError('V2 protected text requires a supported JSON chat request')
    messages = value.get('messages') if isinstance(value, dict) else None
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        raise InlineAlignmentError('V2 protected text requires JSON messages; this engine is not qualified')
    # Append to an existing string system message, or prepend a new one.
    system = next((m for m in messages if m.get('role') == 'system' and isinstance(m.get('content'), str)), None)
    if system is None:
        messages.insert(0, {'role': 'system', 'content': INLINE_INSTRUCTION})
    else:
        system['content'] += '\n\n' + INLINE_INSTRUCTION
    if was_bytes or was_string:
        result = json.dumps(value, ensure_ascii=False)
        return result.encode('utf-8') if was_bytes else result
    return value


def validate_inline_translation(source, target, placeholder, degraded=False):
    """Validate provider/cache text before committing it to the native cache."""
    if not isinstance(source, str) or not isinstance(target, str) or not target.strip():
        raise InlineAlignmentError('empty or invalid translated paragraph')
    if degraded:
        # An explicit, persisted per-paragraph decision permits legacy text.
        # Reserve placeholders still have to match; arbitrary markerless cache
        # responses never acquire this permission by inference.
        source = TOKEN.sub('', source)

    def pairs(text):
        stack, seen, parents, empty = [], set(), {}, set()
        last = 0
        for match in TOKEN.finditer(text):
            key, kind = match.groups()
            if (key, kind) in seen:
                raise InlineAlignmentError('duplicate inline token')
            seen.add((key, kind))
            if kind == 'O':
                parents[key] = stack[-1] if stack else None
                stack.append(key)
            elif not stack or stack.pop() != key:
                raise InlineAlignmentError('crossed or unbalanced inline tokens')
            elif last == match.start() and text[last - len('⟦ET' + key + 'O⟧'):last] == '⟦ET' + key + 'O⟧':
                empty.add(key)
            last = match.end()
        if stack or seen != {(key, kind) for key in parents for kind in ('O', 'C')} or '⟦ET' in TOKEN.sub('', text):
            raise InlineAlignmentError('missing or corrupted inline token')
        return parents, empty

    expected, empty = pairs(source)
    actual, target_empty = pairs(target)
    if expected != actual or not empty <= target_empty:
        raise InlineAlignmentError('inline token set, parent, or empty anchor changed')
    if placeholder:
        pattern = re.compile(placeholder[1].format(r'\d+'))
        counts = lambda text: Counter(m.group(0) for m in pattern.finditer(text))
        if counts(source) != counts(target):
            raise InlineAlignmentError('reserve placeholder set changed')


class InlinePlan:
    def __init__(self, source, policy):
        self.templates = {}
        self.parents = {}
        self.opening = {}
        self.leading_anchors = []
        self.source_links = []
        self.zero_width = set()
        root = copy.deepcopy(source)
        if any(MARKER_ATTR in x.attrib for x in root.iter() if isinstance(x.tag, str)):
            raise InlineAlignmentError('reserved renderer attribute occurs in source')
        if TOKEN.search(''.join(root.itertext())):
            raise InlineAlignmentError('reserved marker occurs in source text')
        self._join_split_words(root)
        self._join_opening_run(root, policy)
        self.marked_source = self._content(root, None)

    @staticmethod
    def _join_split_words(root):
        # Publishers sometimes split a word around a zero-width page anchor:
        # <span class=ital>Drosoph</span><a id=page/><span class=ital>ila</span>.
        # Give the translator one lexical run; keep the anchor at that word's
        # boundary. Never allocate Chinese characters by English offsets.
        for parent in list(root.iter()):
            children = list(parent)
            for first, anchor, last in zip(children, children[1:], children[2:]):
                if (first.getparent() is not parent or anchor.getparent() is not parent
                        or last.getparent() is not parent
                        or not all(isinstance(x.tag, str) for x in (first, anchor, last))
                        or _local(first) != 'span' or first.tag != last.tag
                        or dict(first.attrib) != dict(last.attrib)
                        or _local(anchor) != 'a' or not anchor.get('id')
                        or anchor.get('href') or len(anchor) or anchor.text
                        or len(first) or len(last) or first.tail or anchor.tail
                        or not (first.text or '').isalpha()
                        or not (last.text or '').isalpha()):
                    continue
                first.text += last.text
                first.tail = last.tail
                index = parent.index(first)
                parent.remove(anchor)
                parent.insert(index, anchor)
                parent.remove(last)

    def _join_opening_run(self, root, policy):
        children = list(root)
        if len(children) < 2:
            return
        first, second = children[:2]
        if (_local(first) != 'span' or _local(second) != 'span'
                or not (_classes(first) & {x.casefold() for x in policy.dropcap_classes})
                or not (_classes(second) & {x.casefold() for x in policy.smallcaps_classes})
                or len(first) or len(second) or len(first.text or '') != 1
                or (first.tail or '').strip()):
            return
        # Source "I" + "n many airports..." is a single lexical run. Translate
        # it intact, then decorate the target run's first grapheme. This does
        # not map English offsets onto Chinese offsets.
        original_dropcap = copy.deepcopy(first)
        second.text = (first.text or '') + (first.tail or '') + (second.text or '')
        first.tail = None
        root.remove(first)
        self._opening_node = second
        self._opening_template = original_dropcap

    def _content(self, node, parent):
        text = node.text or ''
        for child in node:
            if not isinstance(child.tag, str):
                text += child.tail or ''
                continue
            name = _local(child)
            if name not in SEMANTIC:
                # Reserved img/sup/etc should already be placeholders in the
                # fork's normal extraction. Unknown remaining markup is not
                # silently discarded by A2.
                raise InlineAlignmentError('unreserved inline element: ' + name)
            empty_anchor = name == 'a' and not ''.join(child.itertext()).strip() and not len(child)
            href = child.get('href', '').strip()
            if re.match(r'(?i)\s*(?:javascript|data|vbscript):', href):
                raise InlineAlignmentError('unsafe source hyperlink scheme')
            if name == 'a' and not empty_anchor:
                self.source_links.append(copy.deepcopy(child))
            if empty_anchor and parent is None and not text.strip():
                anchor = copy.deepcopy(child)
                anchor.tail = None
                self.leading_anchors.append(anchor)
                text += child.tail or ''
                continue
            key = str(len(self.templates))
            template = copy.deepcopy(child)
            template.clear(keep_tail=False)
            template.attrib.update(child.attrib)
            self.templates[key] = template
            self.parents[key] = parent
            if empty_anchor:
                self.zero_width.add(key)
            if child is getattr(self, '_opening_node', None):
                self.opening[key] = self._opening_template
            text += '⟦ET%sO⟧%s⟦ET%sC⟧' % (key, self._content(child, key), key)
            text += child.tail or ''
        return text

    def restore_xml(self, escaped_translation):
        stack, seen, parts, last = [], set(), [], 0
        for match in TOKEN.finditer(escaped_translation):
            parts.append(escaped_translation[last:match.start()])
            key, kind = match.groups()
            if key not in self.templates or (key, kind) in seen:
                raise InlineAlignmentError('unknown or duplicate inline marker')
            seen.add((key, kind))
            if kind == 'O':
                if (stack[-1] if stack else None) != self.parents[key]:
                    raise InlineAlignmentError('inline marker nesting changed')
                node = copy.deepcopy(self.templates[key])
                node.set(MARKER_ATTR, key)
                raw = etree.tostring(node, encoding='unicode', with_tail=False)
                if not raw.endswith('/>'):
                    raise InlineAlignmentError('invalid marker template')
                parts.append(raw[:-2] + '>')
                stack.append(key)
            else:
                if not stack or stack.pop() != key:
                    raise InlineAlignmentError('crossed or unbalanced inline markers')
                node = self.templates[key]
                tag = (node.prefix + ':' if node.prefix else '') + _local(node)
                parts.append('</' + tag + '>')
            last = match.end()
        parts.append(escaped_translation[last:])
        expected = {(key, kind) for key in self.templates for kind in ('O', 'C')}
        if stack or seen != expected or '⟦ET' in TOKEN.sub('', escaped_translation):
            raise InlineAlignmentError('missing or corrupted inline marker')
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        try:
            root = etree.fromstring(('<wrapper>' + ''.join(parts) + '</wrapper>').encode(), parser)
        except etree.XMLSyntaxError as err:
            raise InlineAlignmentError('invalid translated fragment') from err
        for node in root.iter():
            key = node.attrib.pop(MARKER_ATTR, None)
            if key in self.zero_width and (node.text or len(node)):
                raise InlineAlignmentError('zero-width source anchor gained translated content')
            if key in self.opening:
                text = node.text or ''
                i = next((i for i, c in enumerate(text) if c.isalnum()), None)
                if i is None:
                    raise InlineAlignmentError('opening run has no target grapheme')
                end = i + 1
                while end < len(text) and (unicodedata.combining(text[end]) or text[end] in '\ufe0e\ufe0f'):
                    end += 1
                cap = copy.deepcopy(self.opening[key])
                cap.text, cap.tail = text[i:end], text[end:]
                node.text = text[:i]
                node.insert(0, cap)
        for anchor in reversed(self.leading_anchors):
            # Heading anchors remain at the start of the translated heading.
            anchor.tail = root.text
            root.text = None
            root.insert(0, copy.deepcopy(anchor))
        return (html.escape(root.text or '', quote=False) + ''.join(
            etree.tostring(x, encoding='unicode', with_tail=True) for x in root))
