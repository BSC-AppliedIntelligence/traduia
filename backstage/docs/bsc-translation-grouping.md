# Stable-prefix translation grouping

The `fast_text_prefix` policy confirms short source spans promptly for all three supported ASR models.
Translating every span independently loses context. Backstage now groups these
immutable prefixes for **ES/EN translation only**. Catalan captions and the ASR
confirmation policy are unchanged. Conventional timestamp-segment output retains its per-segment translation behavior. The release profile enables prefix handling for Small, Large-v3 and BSC automatically.

The authoritative trigger is the negotiated `stable_text_prefix_v1` protocol,
which the release profile exposes for Small, Large-v3 and BSC. A
saved event model name is not used: with shared model selection it may be stale.

## Flush policy

- Send a group at existing sentence punctuation (`.?!`, including closing quotes).
  Common initials/abbreviations are treated conservatively.
- Send at comma/semicolon/colon boundaries after at least six buffered words.
- Otherwise send **2 seconds after the first buffered final prefix**. Later
  prefixes/drafts do not extend the deadline; silence still triggers a flush.
- Also send when the buffer reaches 40 words, at explicit end-of-stream, or
  before a protocol/source-language change. A long individual prefix is not split.
- Preserve word order, repeated phrases and punctuation exactly as supplied by
  ASR. Drafts never enter translation. Each source ID enters one translation group.

`BSC_TRANSLATION_MAX_WAIT_S` sets the wait on Backstage (default `2.0`, range
`0..2`). Set `0` to compare against the previous per-prefix behavior. It is a
runtime setting, not a new dashboard control. Changing it requires a Backstage
restart/rebuilt preview; it does not change WhisperLive's ASR policy.

This initial buffer supplies bounded phrase context; the growing-sentence layer
below retains context across its flushes. Slow translation services and queueing can add delay beyond the buffer
budget; the limit bounds intentional grouping wait when the worker is available.
Normal stream-end/Stop drains confirmed text. Forced cancellation/timeouts cannot
guarantee delivery from an unavailable translator.

## Growing sentence translation (4 November)

For negotiated prefix ES/EN streams, each group now extends a bounded source
sentence/block. Translate that whole context again, replacing the previous target
draft under the same revision key. Commit it at an existing source sentence
boundary or stream end. This supplies grammatical context across two-second
timeouts: `hi ha una` + `altra qüestió` becomes `hay otra cuestión`, instead of
appending the independently translated `hay una` + `Otra cuestión`.

Capitalization is kept exactly as returned by the translation model. There is no
lowercasing rule or vocabulary exception. Source ASR captions/confirmation and
native segment translation remain unchanged. ES/EN use the existing **Live · may
change** display; their earlier words can change until that context closes.
New confirmed MT blocks start a paragraph, including bounded-context splits.

- `BSC_TRANSLATION_SENTENCE_CONTEXT` defaults to true; set it to `false` to restore
  independent group translation. It applies only when prefix grouping is enabled.
- Context is bounded to 80 words / 1,200 characters. Before exceeding either,
  close the already translated block and start another, preserving every source
  group. Source/language/protocol changes close the previous context too.
  A single overlong source group is kept intact and closed immediately.
- Stop closes an already translated tail from its cached result: no extra MT
  request or invented punctuation. Provisional translations are not sent to TTS;
  the closed translation enters its existing buffer once. TTS acceptance is
  outside this text-caption work.
- Drafts carry `is_partial=true`, `is_final=false`, `revision_closed=false` and a
  stable `revision_key`. They are reliable deliveries even though ASR partials
  may be lossy. Closed captions carry the matching key and `revision_closed=true`.
  This also lets older partial/final viewers identify drafts.

The two-second budget controls when **draft updates** are requested. Growing
inputs cost more to translate, and **immutable MT confirmation waits longer**
for source sentence closure. Measure both endpoints; do not present draft delay
as final confirmation or as speech-to-caption latency.

`translation_group.source_chunk_ids` still lists new source IDs once;
`context_source_chunk_ids` lists reused context. `sentence_key`, `sentence_closed`
and `sentence_close_reason` describe revision lifetime. `close_only=true` carries
no new source IDs and uses a cached translation; it is not another translation
request. `TRANSLATION_CONTEXT_CLOSE` logs these closures separately from actual
`TRANSLATION_GROUP`/`TRANSLATION_DONE` requests. Tools must count new inputs and
contextual revisions separately to avoid false duplicates.

## Measurement and testing

`TRANSLATION_GROUP` logs the reason, source chunk count, word count, actual
`buffer_wait_ms` and configured limit. Existing `TRANSLATION_DONE` logs measure
the separate translation operation when `LATENCY_LOGGING` is enabled.
Translated caption payloads include `translation_group` on both LiveKit and
WebSocket paths: ordered `source_chunk_ids`, per-source waiting times, maximum
configured wait and flush reason. Acoustic times remain null for BSC prefixes.
These are confirmation-to-translation measurements, not speech-to-caption latency.


### ASR pause-recovery provenance

The prefix client also accepts `vad_pause_recovery` for a completed utterance finalized by WhisperLive at a verified acoustic pause. It preserves this reason through CA caption delivery and fanout, instead of labelling it stable-word agreement. Closed IDs cannot change text or reason on replay. Grouping/context settings remain unchanged; any punctuation still comes from the recognizer. This ASR recovery is distinct from translation fallback. It may include a bounded correction/repetition, and needs its own incidence/quality review in the subsequent corpus evaluation.
