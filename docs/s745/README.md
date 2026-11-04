# S745: V2 only-mode release and first-book acceptance

This release incorporates S740 `b16b1f07ae2d061ae98ca50648d3800bfcc503cb`
into the local translation fork. Plugin version: **2.4.5** (S745 r4).

## Default behavior

Translation-only (`only`) now enables paired semantic markers, line-height
**1.7**, ordinary body paragraph gap **0.75em**, and start alignment. Publisher
fonts, indentation, headings, captions, notes, hyperlinks, and reserved
superscript/subscript/image content remain structurally preserved. Body roles
recognize explicit TX/COTX/V classes and conservative BodyText class names;
quotes retain publisher margins, and note/caption contexts exclude body gaps.
Unrecognized publisher body classes retain their layout; this is not a promise
of automatic layout classification for all books.

The bilingual renderer stays on its existing path. In paired variants the
first only render uses V2, and the cache-only bilingual pass removes protocol
markers from the shared translations before using the legacy renderer.
Missing/null `only_typography` selects V2; explicit `false` is a legacy opt-out.
No live engine/model/chunk configuration is rewritten. Merged extraction is
incompatible with V2 and fails explicitly instead of changing the user's setting.

For protected paragraphs, a per-request formatting instruction is added to
JSON chat `messages`. Unsupported request formats fail before that request;
other engine protocols have not been qualified. Source markers, exact reserve
tokens, nesting, and empty anchors are validated inside the existing retry
loop before a successful response enters the native cache. Invalid cached
responses are retried through that same pipeline. Under T187:m1651/m1654, a
paragraph that exhausts V2 retries gets **one** legacy plain-text request. A
successful fallback is explicitly marked `degraded`; its reserved placeholders
must still match. Retries do not each consume the abort allowance: only an
entire paragraph that also fails fallback counts once, and successful cache
hits reset that consecutive-failure count. Cancellation/fatal provider errors
retain their cancellation behavior. The whole output still fails if
any required paragraph is missing/failed or does not match current extraction.

Only degraded paragraphs discard inline styling. Root attributes, IDs and
existing sup/img/math reserves remain; empty location anchors move to the
paragraph start. Original nonempty links keep their href/id and original label
at the paragraph end as references (m1654), including footnote return targets.
Labels omit reserve tokens so images/math are not duplicated; a link with only
reserved content gets a reference arrow. Other discarded inline IDs remain as
empty start anchors. Ordinary V2 paragraphs do not use this path. `degraded
render` logs contain each source id/page, a summary and moved anchor/link counts
for AY to judge this visible compromise.

Fallback state is persisted by the plugin in the existing cache **info** table,
bound to source MD5 and target SHA256. It is not inferred from missing markers,
not embedded in display text, and does not require a schema or identity change.
The flag and target commit together; stale/corrupt flags do not bypass strict
validation. Reopening the cache recognizes valid degraded targets without
retranslating them. The r3 run completed 2,172 required rows, but its output
quality was rejected: nonempty coverage did not detect byte-token strings or
a runaway table label. r4 permits only the eight row exceptions listed below.

## r4 quality gate and bounded repair (T187:m1684)

The same pure quality check runs on fresh responses, fallback responses, warm
V2 targets and both paired renderers. Maximal adjacent `<0xHH>` byte runs are
decoded strictly as UTF8, including adjacent multi-byte codepoints. Partial,
invalid, overlong, surrogate and malformed sequences fail; invalid bytes are
never dropped or replaced. Illegal XML control characters also fail. Repeating
units of 1–32 characters are rejected when they repeat at least 16 times, cover
at least 64 characters and half the target, and exceed the longest literal
source repetition by more than 25%. ET/reserve tokens are excluded from this
measurement. Source mathematical/literal repetition is retained. These are
format/runaway checks, not a semantic translation certificate.

Quality errors stay inside the V2 request retry budget, including markerless
paragraphs. Marker-bearing paragraphs retain their single legacy fallback;
only a failed whole paragraph counts toward consecutive terminal failures.
Valid warm byte sequences are normalized in memory for rendering only. Their
native target bytes and hash-bound degraded flag are not written back. Fresh
normalized targets and flags still persist through the native callback.

cc explicitly approved retranslation of rows
`926,1344,1386,1428,1654,1890,1964,2209` for source book **#13490**. Only three
of the previous 1,405 rows are exceptions: `1344,1386,1428`; the other 1,402
must remain identical, and all source columns remain unchanged. Legitimate
UTF8 decoding does not correct wrong words (for example tauon rendered as pi
or a red herring rendered as a different Chinese word). The eight new results
must therefore be compared with their source by cc1 and presented to AY.

`tools/invalidate_translation_rows.py` is an operator utility run via
`calibre-debug -e ... -- --manifest <reviewed-plan.json>`, defaulting to a
dry run. `--apply` verifies the source EPUB SHA, installed plugin version,
cache identity, and every requested row's ID/MD5/old target SHA before any
update. It uses only `cache.paragraph()` and `cache.update_paragraph()` to
clear the target and its flag; it never deletes source rows. Native updates
commit one row at a time, so a failure reports already-applied IDs. Stop jobs
using that identity, verify the manifest SHA and back up the cache first.
Do not expand the reviewed manifest or directly edit SQL/cache/config files.

The run3 problem EPUBs/logs and snapshot are retained as evidence. Before the
same official `run.py` writes its output paths again, cc1 must preserve those
files and record matching SHA256s. The eight-row real model run, zero byte-token
hits in body and NCX, source/other-cache invariants and fresh output review are
release gates. Offline identity-response replays only prove mechanics.

V2 uses the plugin's ordinary persistent cache with a `|only-inline-v2` identity
suffix. Its marked originals must not be confused with legacy plain originals.
This neither deletes nor rewrites old caches. Retries resume the V2 cache.
Custom engines retain only numeric provider token counters in memory; logged
usage counts include retries and distinguish reported from unreported responses.

## Installation and restart

cx1 builds a source-sealed ZIP without touching the installed plugin. cc1
verifies the supplied SHA256 and installs with:

```powershell
& 'C:/Program Files/Calibre2/calibre-customize.exe' -a '<sealed-S745-plugin.zip>'
```

cc1 reports installation to cc, who owns `/admin/restart-calibre`. Confirm
version 2.4.5 and its module origin after restart before translating. Do not
install/restart while another Calibre job is running. Keep the previous ZIP
available before installation. No push is part of this delivery.

## First official translation (cc1)

Only **#13490, Our Mathematical Universe (Max Tegmark)** is authorized. Use the
installed plugin's normal GUI job, or `lib.conversion.convert_item` through
`calibre-debug`, with its real selected engine, native cache and current chunk
settings. Select `only` for this job without changing the persistent JSON.
Do not replace the provider, use an offline translator, or alter the user's
engine/model/options/merge preferences. An equivalent debug entry may supply
an in-memory configuration view for job-only position selection; all other
keys delegate to the real plugin configuration unchanged.

Read the library path through `wasn_paths.get_calibre_library_path()`. Dump
source formats, original EPUB SHA256, annotations and last-read positions
before and after. Import the result as a **new book** using supported Calibre
APIs/tools, leaving the original EPUB and original book record intact. Ingestion
still requires the separate decision specified by m1614; do not use the current
GUI storage preset to overwrite the source. Never edit `metadata.db` directly.
All library/product writes are owned by cc1.

Capture exact installed source/ZIP SHA, selected engine/model (no secrets),
provider-reported token counters and retries, translated chapter/paragraph
counts, elapsed time, failed rows and output SHA/path/new book ID. Cache DBs
and plugin JSON are read-only to agents; only the running plugin writes them.
Do not print keys, headers, endpoint credentials or complete config objects.
Seven previously translated books are explicitly out of scope.

Provide first-chapter and a footnote-bearing chapter at 412 and 1440 widths,
with functional footnote return and heading navigation checks. Offline browser
screenshots are labeled as such and do not stand for a physical phone or CS
reading session. cc presents the result to AY; only AY's explicit 驗收 closes
the THOROUGH acceptance. cx1 retains review/fix/handback ownership throughout.

## Rollback

Before r4 installation, retain the currently installed 2.4.4 r3b ZIP with its SHA256
in the authorized backup layer. A ZIP rebuilt from sealed source `b01169f4a23b6c43a9618b1df78a9be52b5ac259`
is a source-level rollback and must not be labeled a byte-identical copy of the
old installed ZIP. cc1 confirms the actual pre-install baseline.

On a release regression: stop the S745 job, retain its evidence and native
cache, reinstall the recorded previous ZIP with `calibre-customize -a`, then
have cc restart Calibre and verify the previous version/module. Do not delete
cache DBs, modify annotations/positions or regenerate old books. The new book
can remain for diagnosis; removal needs a separate explicit decision.

## Evidence and limits

`D:/WASN-Tools/logs/cx1-s745/` holds safe preflight metadata, isolated Calibre
test receipts and full-book identity-provider structural probes. These probes
make **zero network/model requests** and are **not Chinese translations**.
The real model/cache/installation and translated reading result require cc5's
official run, followed by cross-family and independent ARRL review. A test
count or dry EPUB alone does not satisfy those gates.
