/* Stage caption presentation. Does not change ASR or translation timing. */
(function (global) {
    'use strict';

    class StageCaptionPanel {
        constructor(element, placeholder) {
            this.element = element;
            this.placeholder = placeholder;
            this.draftLabel = element.closest('.subtitle-section')?.querySelector('.caption-draft-label') || null;
            this.reset();
        }

        reset() {
            this.streamId = null;
            this.retiredStreams = new Set();
            this.lastSequence = 0;
            this.closedRevisions = new Set();
            this.committedChunks = new Set();
            this.pending = null;
            this.partial = null;
            this.lastCommitted = null;
            if (this.draftLabel) this.draftLabel.hidden = true;
            this.element.replaceChildren();
            this.history = document.createElement('div');
            this.history.className = 'caption-history';
            this.empty = document.createElement('span');
            this.empty.className = 'caption-placeholder';
            this.empty.textContent = this.placeholder;
            this.draft = document.createElement('div');
            this.draft.className = 'caption-draft';
            this.draft.hidden = true;
            this.draftText = document.createElement('div');
            this.draft.append(this.draftText);
            this.element.append(this.empty, this.history, this.draft);
        }

        receive(data) {
            const text = typeof data.text === 'string' ? data.text.trim() : '';
            if (!text) return;
            let followLive = this.element.scrollHeight - this.element.clientHeight - this.element.scrollTop <= 24;
            if (data.stream_id && data.stream_id !== this.streamId) {
                if (this.retiredStreams.has(data.stream_id)) return;
                if (this.streamId) this.retiredStreams.add(this.streamId);
                this.streamId = data.stream_id;
                this.lastSequence = 0;
                this.closedRevisions.clear();
                this.committedChunks.clear();
                this.pending = this.partial = null;
                this.lastCommitted = null;
                followLive = true;
            }
            if (Number.isInteger(data.caption_sequence)) {
                if (data.caption_sequence <= this.lastSequence) return;
                this.lastSequence = data.caption_sequence;
            }
            const key = data.revision_key || '';
            if (key && this.closedRevisions.has(key)) return;
            this.empty.hidden = true;

            if (data.is_partial) {
                this.partial = { ...data, text, key };
            } else {
                // Older servers have no closure flag: keyed results stay drafts.
                const closed = typeof data.revision_closed === 'boolean'
                    ? data.revision_closed : !key;
                if (!closed) {
                    this.pending = { ...data, text, key };
                    this.partial = null;
                } else {
                    if (!key || this.pending?.key === key) this.pending = null;
                    if (data.closure_reason !== 'next_chunk') this.partial = null;
                    if (key) this.closedRevisions.add(key);
                    if (!data.chunk_id || !this.committedChunks.has(data.chunk_id)) {
                        this.appendCommitted(text, data);
                        if (data.chunk_id) this.committedChunks.add(data.chunk_id);
                    }
                }
            }
            // Bound deduplication metadata as well as the visible history.
            for (const set of [this.closedRevisions, this.committedChunks, this.retiredStreams]) {
                while (set.size > 100) set.delete(set.values().next().value);
            }
            this.renderDraft();
            this.trimHistory();
            this.empty.hidden = this.history.childElementCount > 0 || !this.draft.hidden;
            if (followLive) this.element.scrollTop = this.element.scrollHeight;
        }

        appendCommitted(text, data) {
            let paragraph = this.history.lastElementChild;
            const previous = paragraph?.textContent || '';
            const ending = previous.replace(/[”’"'»)\]]+$/u, '');
            const sentenceEnd = /[.!?。！？]$/u.test(ending)
                && !/\.{2,}$/u.test(ending)
                && !/\b(?:dr|dra|sr|sra|mr|mrs|prof|etc)\.$/iu.test(ending);
            const longPause = this.lastCommitted
                && Number.isFinite(this.lastCommitted.end)
                && Number.isFinite(data.audio_start_s)
                && data.audio_start_s - this.lastCommitted.end >= 2;
            if (!paragraph || !this.lastCommitted || data.translation_group?.sentence_key || sentenceEnd || longPause
                    || previous.length + text.length + 1 > 420) {
                paragraph = document.createElement('p');
                paragraph.className = 'caption-paragraph';
                this.history.append(paragraph);
            } else {
                paragraph.append(document.createTextNode(' '));
            }
            // Append immutable spans; never rebuild earlier confirmed words.
            const span = this.captionSpan(text, data);
            span.dataset.chunkId = data.chunk_id || '';
            paragraph.append(span);
            this.lastCommitted = { end: data.audio_end_s };
        }

        captionSpan(text, data) {
            const span = document.createElement('span');
            span.className = 'caption-chunk';
            if (data.translation_status === 'fallback') {
                span.classList.add('caption-fallback');
                const badge = document.createElement('span');
                badge.className = 'caption-fallback-label';
                const language = typeof data.text_language === 'string' && data.text_language
                    ? ` (${data.text_language.toUpperCase()})` : '';
                badge.textContent = `Translation unavailable · showing original${language}`;
                span.append(badge, document.createTextNode(' '));
            }
            span.append(document.createTextNode(text));
            return span;
        }

        renderDraft() {
            let texts = this.pending ? [this.pending] : [];
            if (this.partial) {
                // A partial for the same hypothesis replaces only its draft.
                if (this.pending && this.partial.key === this.pending.key) {
                    texts = [this.partial];
                } else {
                    texts.push(this.partial);
                }
            }
            this.draftText.replaceChildren(...texts.map(data => {
                const paragraph = document.createElement('p');
                paragraph.className = 'caption-paragraph';
                paragraph.append(this.captionSpan(data.text, data));
                return paragraph;
            }));
            this.draft.hidden = texts.length === 0;
            if (this.draftLabel) this.draftLabel.hidden = this.draft.hidden;
        }

        trimHistory() {
            // Retain the existing compact view rather than introducing a new
            // scrolling/history mode. Never recreate the remaining paragraphs.
            while (this.history.childElementCount > 10 ||
                   (this.history.childElementCount > 1 && this.element.clientHeight > 0 &&
                    this.element.scrollHeight > this.element.clientHeight + 1)) {
                this.history.firstElementChild.remove();
            }
        }
    }

    global.StageCaptionPanel = StageCaptionPanel;
})(window);
