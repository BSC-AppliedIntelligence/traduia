/**
 * Link3rs Backstage - LiveKit Integration
 */

const LiveKit = {
    room: null,
    localTrack: null,
    isConnected: false,
    onLog: null,

    /**
     * Check if LiveKit is available on server
     */
    async checkStatus() {
        try {
            const response = await fetch('/api/livekit/status');
            const data = await response.json();
            return data;
        } catch (e) {
            console.error('LiveKit status check failed:', e);
            return { configured: false, translation_ready: false };
        }
    },

    /**
     * Create a new session for an event
     */
    async createSession(eventId, eventTitle, channels, sourceLanguage = 'any', eventCode = '') {
        try {
            const response = await fetch('/api/session/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    event_id: eventId,
                    event_title: eventTitle,
                    event_code: eventCode,
                    channels: channels,
                    source_language: sourceLanguage
                })
            });
            
            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Failed to create session');
            }
            
            const data = await response.json();
            this.log('info', `Session created: ${data.room_name}`);
            return data;
        } catch (e) {
            this.log('error', `Create session error: ${e.message}`);
            throw e;
        }
    },

    /**
     * Stop a session
     */
    async stopSession(eventId) {
        try {
            const response = await fetch(`/api/session/${eventId}`, {
                method: 'DELETE'
            });
            
            if (!response.ok && response.status !== 404) {
                const error = await response.json();
                throw new Error(error.detail || 'Failed to stop session');
            }
            
            this.log('info', 'Session stopped');
            return true;
        } catch (e) {
            this.log('error', `Stop session error: ${e.message}`);
            return false;
        }
    },

    /**
     * Get session status
     */
    async getSessionStatus(eventId) {
        try {
            const response = await fetch(`/api/session/${eventId}/status`);
            return await response.json();
        } catch (e) {
            return { active: false };
        }
    },

    /**
     * Connect to LiveKit room as broadcaster
     */
    async connectAsBroadcaster(livekitUrl, token) {
        this.log('info', 'Connecting to LiveKit...');
        
        try {
            // Create room
            this.room = new LivekitClient.Room({
                adaptiveStream: true,
                dynacast: true,
            });

            // Set up event handlers
            this.room.on(LivekitClient.RoomEvent.Connected, () => {
                this.isConnected = true;
                this.log('success', 'Connected to room');
            });

            this.room.on(LivekitClient.RoomEvent.Disconnected, () => {
                this.isConnected = false;
                this.log('warning', 'Disconnected from room');
            });

            this.room.on(LivekitClient.RoomEvent.ParticipantConnected, (participant) => {
                this.log('info', `Participant joined: ${participant.identity}`);
            });

            this.room.on(LivekitClient.RoomEvent.ParticipantDisconnected, (participant) => {
                this.log('info', `Participant left: ${participant.identity}`);
            });

            // Connect
            await this.room.connect(livekitUrl, token);
            this.log('success', 'Room connected successfully');
            
            return true;
        } catch (e) {
            this.log('error', `Connection error: ${e.message}`);
            throw e;
        }
    },

    /**
     * Start publishing audio from a specific device
     */
    async startPublishing(deviceId) {
        if (!this.room || !this.isConnected) {
            throw new Error('Not connected to room');
        }

        this.log('info', 'Starting audio capture...');

        try {
            // Get audio track from device
            const constraints = {
                audio: {
                    deviceId: deviceId ? { exact: deviceId } : undefined,
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                    sampleRate: 48000,
                    channelCount: 1
                },
                video: false
            };

            const stream = await navigator.mediaDevices.getUserMedia(constraints);
            const audioTrack = stream.getAudioTracks()[0];

            // Create LiveKit track
            this.localTrack = new LivekitClient.LocalAudioTrack(audioTrack);

            // Publish to room
            await this.room.localParticipant.publishTrack(this.localTrack, {
                name: 'broadcaster_audio',
                source: LivekitClient.Track.Source.Microphone,
            });

            this.log('success', 'Audio publishing started');
            return stream;
        } catch (e) {
            this.log('error', `Publish error: ${e.message}`);
            throw e;
        }
    },

    /**
     * Stop publishing audio
     */
    async stopPublishing() {
        if (this.localTrack) {
            try {
                await this.room.localParticipant.unpublishTrack(this.localTrack);
                this.localTrack.stop();
                this.localTrack = null;
                this.log('info', 'Audio publishing stopped');
            } catch (e) {
                this.log('error', `Stop publish error: ${e.message}`);
            }
        }
    },

    /**
     * Disconnect from room
     */
    async disconnect() {
        await this.stopPublishing();
        
        if (this.room) {
            await this.room.disconnect();
            this.room = null;
            this.isConnected = false;
            this.log('info', 'Disconnected from room');
        }
    },

    /**
     * Get available audio input devices
     */
    async getAudioDevices() {
        try {
            // Request permission first
            const permissionStream = await navigator.mediaDevices.getUserMedia({ audio: true });
            
            const devices = await navigator.mediaDevices.enumerateDevices();
            permissionStream.getTracks().forEach(track => track.stop());
            return devices.filter(d => d.kind === 'audioinput');
        } catch (e) {
            this.log('error', `Get devices error: ${e.message}`);
            return [];
        }
    },

    /**
     * Log message (calls onLog callback if set)
     */
    log(level, message) {
        const timestamp = new Date().toLocaleTimeString();
        console.log(`[LiveKit] [${level.toUpperCase()}] ${message}`);
        
        if (this.onLog) {
            this.onLog({ level, message, timestamp });
        }
    }
};

console.log('✅ LiveKit module loaded');
