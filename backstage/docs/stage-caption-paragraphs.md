# Stage caption paragraphs and live draft

This change affects Backstage's `/stage/{event_code}` page and its subtitle
messages. It does not change ASR model selection, recognition settings,
translation settings, or the standalone Stage service on port 8082.
The latest optional two-service preview also fixes segment filtering in the
recognizer, as described at the end of this document; it retains the same model
and recognition settings.

Confirmed caption chunks append inline into sentence-like paragraphs. Partial text and
timeout-final hypotheses appear in a separate area with **Live · may change**
shown in the caption heading. A corrected hypothesis updates this draft; closure moves the latest
text into a confirmed span. Existing confirmed words are not rebuilt
on draft updates. The view remains bounded; scrollable history/follow-live
controls are a separate future change.

New paragraphs start after terminal punctuation (excluding ellipses and common
abbreviations), a source-audio pause of at least two seconds, or when appending
another chunk would exceed 420 characters. A single ASR chunk remains intact
even if longer. Confirmed spans appear immediately; grouping adds no waiting
for a complete sentence and does not alter ASR/translation/TTS timing. The
fixed-height area follows the newest text while already near the bottom; a
reader who scrolls upward is not forced back down. This is still the compact,
bounded view, not the proposed 100-line transcript history or review storage.

The player now uses 85% of viewport width up to 1000 px (the available width on
mobile). Each text area has a steady responsive height between 160 and 220 px;
the 16 px font and 1.6 line height are unchanged. Paragraph gaps are 4 px, and
captions are explicitly left-aligned. The draft badge no longer takes a line
inside the text area. Smaller surrounding margins leave more space for text.

## Caption protocol

Existing subtitle fields and topics remain compatible. New fields are:

- `finalization_reason`: `partial`, `timeout`, `stable`, `bounded_window`, or
  `eos`, using the existing recognition result's reason.
- `revision_closed`: whether this presentation revision is closed. A timeout
  or bounded-window result can have the legacy `is_final=true` while
  `revision_closed=false`.
- `closure_reason`: `next_chunk` or `stream_end` when the draft closes without
  another result; `confirmed` when an unchanged stable/EOS result closes it
  without repeating translation/TTS. Otherwise empty.
- `chunk_id`: the source ASR chunk identity, also used by translation captions.
- `stream_id` and `caption_sequence`: reject obsolete/duplicate packets without
  allowing a late partial to rewrite an already completed paragraph.
- `audio_start_s` and `audio_end_s`: source-audio span in seconds, shared by
  original and translated captions and retained on draft closure. Paragraph
  pauses use these times, not network/translation delivery delays. Older
  payloads without these fields still group using punctuation and length.

An unrelated subsequent audio span closes the preceding timeout draft before
displaying the new text, using the paragraph grouping rules above. Closing a draft does not run recognition,
translation, or TTS again. A stable/EOS correction for the same span
closes the same revision using its corrected text. Stop/stream teardown also
closes the last emitted timeout draft. Unconfirmed partials are never promoted
to completed text merely because a connection failed.

“Closed” means this caption revision will no longer be edited in place; it does
not certify a complete grammatical sentence or perfect recognition. With
silence, a timeout result can remain a draft until a closing result, another
span, or stream end. There is no new stabilization timer or buffering delay.
A bounded window closes an ASR processing window but retains recognition
context, so its caption remains a draft until confirmation, a new span, or
stream end.

`asr/caption_revisions.py` reconciles source spans before caption output and
shared translation fan-out. A correction to the retained window tail keeps
the unique sentence prefix and uses the latest recognized tail. Exact repeated
context crossing earlier closed chunks is removed only when audio ranges
overlap and a sufficient word anchor matches. Equal words spoken again in
later audio remain separate. Ambiguous alignment retains both texts rather
than guessing a deletion; this does not guarantee elimination of every possible
near-duplicate recognition result.

Short overlapping tails also have a capitalization-independent fallback. It
can join a distinctive repeated phrase (for example `mercat laboral`), complete
an incomplete word (`tecnolo` → `tecnologies`), or accept a closely spelled
corrected last word beside matching context. The old suffix must align to the
new prefix, with enough matching characters and an unambiguous location in the
earlier draft. A single complete word, short common phrases, very short word
fragments, unrelated replacements, and repeated/ambiguous anchors are not
enough. It keeps the unique prefix and takes the newer recognized tail; it
does not invent missing words. The earlier narrow proper-name fallback remains
for short surnames with completely different spelling.

These rules use relative audio intervals and text, without depending on a
recording name, event, absolute timestamp or topic. File playback and microphone
audio share this reconciler. Only overlapping, still-revisable tails are joined;
later repetitions remain separate. This is conservative alignment rather than
a general grammar or speech-repair model.

## First check: offline visual preview

This uses the complete Stage page structure, CSS and caption-rendering
JavaScript, with a sample event/channels and synthetic messages. It omits the
live service scripts and shows floating preview controls. It needs no Docker,
credentials, LiveKit, microphone, or models.

From the `backstage` directory:

```bash
python3 scripts/preview_stage_captions.py
python3 -m http.server 8765 --bind 127.0.0.1 \
  --directory /tmp/traduia-stage-captions-preview
```

Open `http://localhost:8765`, then use **Replay example** or **Next step**.
Check that partial/timeout changes affect only the draft, closed text joins
paragraphs according to punctuation/pause/length, and both languages behave
consistently. This preview does not measure ASR latency or validate live media.
Stop the preview server with Ctrl-C.

## Automated checks

Translation failures now carry per-request metadata on both caption transports:
`translation_status` (`translated` or `fallback`), `translation_fallback`,
`translation_error` (a stable reason code), and `text_language` (the detected
source language on fallback, or the target on success). An unknown source is
left empty. Existing `channel`/`language` and `is_translation` retain their
channel semantics for older clients. Identical source/target wording alone does
not imply failure. Draft closure preserves the status without another request.

Stage attaches an amber **Translation unavailable · showing original (CA)**
label to affected text, including drafts. A successful replacement revision
removes its warning. Later successful chunks carry no warning. Speaker terminal
logs also report the fallback reason and chunk ID.

## Tests

`pytest tests/test_caption_protocol.py tests/test_caption_revisions.py tests/test_stage_caption_display.py` cover the protocol, the
revision logic and the offline preview.
