# Opt-in only-mode chapter trials

`only_typography` defaults to `None`. Existing above/below/left/right output and
unconfigured only output retain their renderer. A trial can explicitly configure
`line_height` (default 1.7), `paragraph_gap` in em (0.75), `text_align` (`start`),
and CSS class roles through `OnlyPolicy` or the configuration object. Body,
introduction and verse roles receive line height and alignment. Only body
paragraphs receive the ordinary gap. Publisher indentation, fonts, heading,
caption and footnote styles, and introduction/verse spacing remain intact.
Role mappings are book-specific inputs, not a general Chinese typography UI.

Set `preserve_inline: true` for A2. Extraction returns paired
`⟦ET0O⟧...⟦ET0C⟧` markers for source inline elements; existing reserve placeholders
keep footnote superscripts, images, line breaks and other non-translated fragments.
A translator or alignment stage must return each pair and reserve exactly once,
with the original nesting. Sibling runs may move with Chinese word order. Source
templates supply tags, classes, IDs and hrefs; translated text is escaped. IDs
restart per block, so A2 explicitly rejects merged translation mode.

The opening dropcap and following smallcaps text become one logical source run;
output decorates the first target grapheme while retaining the smallcaps role.
Identically styled word fragments separated by an empty page anchor likewise
become one lexical run, with the anchor at its boundary. No English character
offsets are applied to Chinese. Leading empty heading anchors remain at the
heading start; other empty anchors use explicit zero-width pairs that cannot
acquire translated content.

Missing, repeated, unknown, crossed or corrupted markers, missing/repeated
reserves, and unknown unreserved inline tags raise `InlineAlignmentError` before
source removal or color changes. There is no silent A2 flattening. A caller may
explicitly produce a separate A1 candidate, label its degraded blocks, retain the
source or retry alignment; it must report that fallback.

`lib/chapter_trial.py` reuses a read-only cache snapshot through the real
`PageElement` only renderer. It requires exact source block identities and,
for plain cache reuse, matching prepared source. The packager exclusively creates
a new EPUB with one linear chapter plus non-linear supporting notes, preserves
rendering assets, and excludes other chapter HTML. It never discovers live cache
paths, translates, installs a plugin, registers a library book or touches reading
positions/annotations. Use an isolated Calibre config/cache. Calibre Python uses
optimization, so safety gates must use explicit exceptions rather than `assert`.

S740 found all 122 translatable chapter blocks cached, plus one ignored block.
V2 uses curated semantic phrase alignment of that Chinese, with explicit target
occurrences/contexts; this does not prove automatic recovery from a plain cache.
Page-only anchors use the containing Chinese paragraph boundary when no lexical
counterpart exists. Two cached TeX powers and redundant Markdown italic stars
are normalized to source inline roles. Cached citation text is reused while
source IDs, return links and journal emphasis are restored. Translation wording
and quality are otherwise retained. There were zero external translation calls.

Exact mappings, normalization records, EPUB hashes, screens and browser checks
are in the external S740 evidence packet. CURRENT/V1 retain the original seven
broken visible note targets; V2 restores them. The additional hidden supplemental
rearnote receives structural checks, not a claimed user-visible click test.
Controlled local Chrome previews are not Content Server or physical-phone
acceptance. Typography parameters await AY's choice and explicit THOROUGH
acceptance. This branch is not a plugin installation or seven-book regeneration.
