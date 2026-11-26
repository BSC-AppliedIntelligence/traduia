/* Shared-model controls stay independent from event-specific endpoint saves. */
class AsrModelSelection {
    constructor({eventCode}) {
        this.url = `/api/event/${encodeURIComponent(eventCode)}/asr-model`;
        this.select = document.getElementById('whisperModel');
        this.button = document.getElementById('saveAsrModel');
        this.status = document.getElementById('asrModelStatus');
        this.running = document.getElementById('asrRunningModel');
        this.host = document.getElementById('whisperHost');
        this.port = document.getElementById('whisperPort');
        this.state = null;
        this.dirty = false;
        this.saving = false;
        this.formSaving = false;
        this.saveError = null;
        this.revision = null;
        this.request = 0;
        this.select.addEventListener('change', () => { this.dirty = true; this.saveError = null; this.render(); });
        for (const input of [this.host, this.port]) input.addEventListener('input', () => this.render());
        this.button.addEventListener('click', () => this.save());
        this.timer = setInterval(() => { if (!document.hidden) this.refresh(); }, 3000);
        window.addEventListener('pagehide', () => clearInterval(this.timer), {once: true});
    }

    endpointMatches() {
        return this.state && this.host.value.trim() === this.state.endpoint.host &&
            Number(this.port.value) === Number(this.state.endpoint.port);
    }

    label(id) {
        return this.state.models.find(model => model.id === id)?.label || id;
    }

    apply(state) {
        const selected = this.select.value;
        if (this.state && JSON.stringify(this.state.endpoint) !== JSON.stringify(state.endpoint)) {
            this.dirty = false; // A draft for another ASR service must not be carried over.
        }
        this.state = state;
        const entries = state.models.map(model => [model.id, model.label]);
        const existing = Array.from(this.select.options, option => [option.value, option.text]);
        if (JSON.stringify(entries) !== JSON.stringify(existing)) {
            this.select.replaceChildren(...state.models.map(model => new Option(model.label, model.id)));
        }
        this.select.value = this.dirty ? selected : state.selected_model;
        if (!this.dirty) this.revision = state.selection_revision;
        this.render();
    }

    render() {
        const available = Boolean(this.state);
        const matching = available && this.endpointMatches();
        this.select.disabled = !matching || this.saving || this.formSaving || this.state.loading;
        this.button.disabled = this.select.disabled || !this.dirty;
        if (!available) return;
        this.running.textContent = `Running model: ${this.state.loaded_model ? this.label(this.state.loaded_model) : 'not loaded'}`;
        if (!matching) {
            this.status.textContent = 'Save the endpoint host and port first, then choose its shared model.';
        } else if (this.saving) {
            this.status.textContent = 'Saving the shared model selection…';
        } else if (this.state.loading) {
            this.status.textContent = 'Downloading/loading the selected ASR model. Wait before starting another broadcast.';
        } else if (this.state.last_error) {
            this.status.textContent = `${this.state.last_error} Saved selection: ${this.label(this.state.selected_model)}.`;
        } else if (this.saveError) {
            this.status.textContent = this.saveError;
        } else if (this.dirty) {
            this.status.textContent = 'Unsaved model choice. Saving applies to all events using this server.';
        } else if (this.state.pending) {
            this.status.textContent = `Saved selection: ${this.label(this.state.selected_model)}. ` +
                ((this.state.active_broadcasts || this.state.active_sessions) ? 'Pending: existing broadcasts must finish; new starts are refused until then.' : 'Will load on the next broadcast Start.');
        } else {
            this.status.textContent = `Saved selection: ${this.label(this.state.selected_model)}. Ready for broadcasts.`;
        }
    }

    async refresh({fromForm = false} = {}) {
        if (this.saving || (this.formSaving && !fromForm)) return;
        const request = ++this.request;
        try {
            const response = await fetch(this.url);
            const state = await response.json();
            if (!response.ok) throw new Error(typeof state.detail === 'string' ? state.detail :
                'Shared model status is unavailable. This server may not support model selection.');
            if (request === this.request) this.apply(state);
        } catch (error) {
            if (request !== this.request) return;
            this.state = null;
            this.select.disabled = this.button.disabled = true;
            this.running.textContent = 'Running model: unavailable';
            this.status.textContent = error.message;
        }
    }

    async save({fromForm = false} = {}) {
        if (!this.dirty || !this.endpointMatches() || this.state.loading || this.saving ||
            (this.formSaving && !fromForm)) return false;
        ++this.request; // Ignore an older poll completing after this save.
        this.saving = true;
        this.saveError = null;
        this.render();
        try {
            const response = await fetch(this.url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({model: this.select.value, selection_revision: this.revision})});
            const state = await response.json();
            if (!response.ok) {
                if (response.status === 409) this.dirty = false; // Refresh a stale revision before retrying.
                throw new Error(typeof state.detail === 'string' ? state.detail : 'The shared selection could not be saved.');
            }
            this.dirty = false;
            this.saving = false;
            this.apply(state);
            return true;
        } catch (error) {
            this.saving = false;
            await this.refresh({fromForm: true});
            this.saveError = error.message;
            this.render();
            this.status.textContent = error.message;
            return false;
        }
    }

    async saveEndpointForm(config) {
        if (this.formSaving || this.saving) throw new Error('A save is already in progress.');
        const saveModel = this.dirty && this.endpointMatches();
        const changedEndpoint = this.state && !this.endpointMatches();
        ++this.request;
        this.formSaving = true;
        this.render();
        try {
            const response = await fetch(this.url.replace(/\/asr-model$/, '/endpoints'), {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(config)
            });
            if (!response.ok) throw new Error('Endpoints could not be saved. The model choice was not saved.');
            if (saveModel) {
                if (!await this.save({fromForm: true})) {
                    throw new Error('Endpoints saved, but the ASR model was not saved. ' +
                        (this.saveError || 'Review the shared model status and try again.'));
                }
                return 'Endpoints and shared ASR model saved.';
            }
            await this.refresh({fromForm: true});
            return changedEndpoint ? 'Endpoints saved. Choose and save the model for this server.' : 'Endpoints saved.';
        } finally {
            this.formSaving = false;
            this.render();
        }
    }
}
window.AsrModelSelection = AsrModelSelection;
