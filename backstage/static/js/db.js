/**
 * Link3rs Backstage - Database Operations
 */

const DB = {
    // ============================================
    // EVENTS
    // ============================================

    /**
     * Generate unique event code (A + 5 digits)
     */
    generateEventCode() {
        const numbers = Math.floor(10000 + Math.random() * 90000);
        return `A${numbers}`;
    },

    /**
     * Create new event
     */
    async createEvent(userId, title, description = '') {
        try {
            const eventCode = this.generateEventCode();
            
            const { data, error } = await supabaseClient
                .from('events')
                .insert({
                    user_id: userId,
                    title,
                    description,
                    event_code: eventCode,
                    status: 'active'
                })
                .select()
                .single();
            
            if (error) throw error;
            console.log('✅ Event created:', data.event_code);
            return data;
        } catch (e) {
            console.error('createEvent error:', e);
            throw e;
        }
    },

    /**
     * Get all events for user
     */
    async getEvents(userId) {
        try {
            const { data, error } = await supabaseClient
                .from('events')
                .select('*')
                .eq('user_id', userId)
                .in('status', ['active', 'streaming'])
                .order('created_at', { ascending: false });
            
            if (error) throw error;
            return data || [];
        } catch (e) {
            console.error('getEvents error:', e);
            return [];
        }
    },

    /**
     * Get event by code
     */
    async getEventByCode(eventCode) {
        try {
            const { data, error } = await supabaseClient
                .from('events')
                .select('*')
                .eq('event_code', eventCode)
                .single();
            
            if (error) throw error;
            return data;
        } catch (e) {
            console.error('getEventByCode error:', e);
            return null;
        }
    },

    /**
     * Get event by ID
     */
    async getEventById(eventId) {
        try {
            const { data, error } = await supabaseClient
                .from('events')
                .select('*')
                .eq('id', eventId)
                .single();
            
            if (error) throw error;
            return data;
        } catch (e) {
            console.error('getEventById error:', e);
            return null;
        }
    },

    /**
     * Update event
     */
    async updateEvent(eventId, updates) {
        try {
            const { data, error } = await supabaseClient
                .from('events')
                .update(updates)
                .eq('id', eventId)
                .select()
                .single();
            
            if (error) throw error;
            return data;
        } catch (e) {
            console.error('updateEvent error:', e);
            throw e;
        }
    },

    /**
     * Archive event
     */
    async archiveEvent(eventId) {
        return this.updateEvent(eventId, { status: 'archived' });
    },

    /**
     * Delete event
     */
    async deleteEvent(eventId) {
        try {
            // First delete channels
            await supabaseClient
                .from('channels')
                .delete()
                .eq('event_id', eventId);
            
            // Then delete event
            const { error } = await supabaseClient
                .from('events')
                .delete()
                .eq('id', eventId);
            
            if (error) throw error;
            console.log('✅ Event deleted');
        } catch (e) {
            console.error('deleteEvent error:', e);
            throw e;
        }
    },

    // ============================================
    // CHANNELS
    // ============================================

    /**
     * Create channel
     */
    async createChannel(eventId, name, language, languageCode, isOriginal = false) {
        try {
            // If original channel, auto-set to Catalan
            const channelName = isOriginal ? 'Original' : name;
            const channelLang = isOriginal ? 'ca' : language;
            const channelLangCode = isOriginal ? 'ca-ES' : languageCode;
            
            const { data, error } = await supabaseClient
                .from('channels')
                .insert({
                    event_id: eventId,
                    name: channelName,
                    language: channelLang,
                    language_code: channelLangCode,
                    is_original: isOriginal,
                    status: 'inactive'
                })
                .select()
                .single();
            
            if (error) throw error;
            console.log('✅ Channel created:', data.name);
            return data;
        } catch (e) {
            console.error('createChannel error:', e);
            throw e;
        }
    },

    /**
     * Get channels for event
     */
    async getChannels(eventId) {
        try {
            const { data, error } = await supabaseClient
                .from('channels')
                .select('*')
                .eq('event_id', eventId)
                .order('is_original', { ascending: false })
                .order('created_at', { ascending: true });
            
            if (error) throw error;
            return data || [];
        } catch (e) {
            console.error('getChannels error:', e);
            return [];
        }
    },

    /**
     * Update channel
     */
    async updateChannel(channelId, updates) {
        try {
            const { data, error } = await supabaseClient
                .from('channels')
                .update(updates)
                .eq('id', channelId)
                .select()
                .single();
            
            if (error) throw error;
            return data;
        } catch (e) {
            console.error('updateChannel error:', e);
            throw e;
        }
    },

    /**
     * Delete channel
     */
    async deleteChannel(channelId) {
        try {
            const { error } = await supabaseClient
                .from('channels')
                .delete()
                .eq('id', channelId);
            
            if (error) throw error;
            console.log('✅ Channel deleted');
        } catch (e) {
            console.error('deleteChannel error:', e);
            throw e;
        }
    },

    // ============================================
    // ADMIN
    // ============================================

    /**
     * Get all pending users (admin only)
     */
    async getPendingUsers() {
        try {
            const { data, error } = await supabaseClient
                .from('profiles')
                .select('*')
                .eq('is_approved', false)
                .order('created_at', { ascending: true });
            
            if (error) throw error;
            return data || [];
        } catch (e) {
            console.error('getPendingUsers error:', e);
            return [];
        }
    },

    /**
     * Get all users (admin only)
     */
    async getAllUsers() {
        try {
            const { data, error } = await supabaseClient
                .from('profiles')
                .select('*')
                .order('created_at', { ascending: false });
            
            if (error) throw error;
            return data || [];
        } catch (e) {
            console.error('getAllUsers error:', e);
            return [];
        }
    },

    /**
     * Approve user (admin only)
     */
    async approveUser(userId, adminId) {
        try {
            const { data, error } = await supabaseClient
                .from('profiles')
                .update({
                    is_approved: true,
                    approved_at: new Date().toISOString(),
                    approved_by: adminId
                })
                .eq('id', userId)
                .select()
                .single();
            
            if (error) throw error;
            console.log('✅ User approved');
            return data;
        } catch (e) {
            console.error('approveUser error:', e);
            throw e;
        }
    },

    /**
     * Toggle admin status (admin only)
     */
    async toggleAdmin(userId, isAdmin) {
        try {
            const { data, error } = await supabaseClient
                .from('profiles')
                .update({ is_admin: isAdmin })
                .eq('id', userId)
                .select()
                .single();
            
            if (error) throw error;
            return data;
        } catch (e) {
            console.error('toggleAdmin error:', e);
            throw e;
        }
    },

    // ============================================
    // STATS
    // ============================================

    /**
     * Get event stats
     */
    async getEventStats(eventId) {
        try {
            const { data, error } = await supabaseClient
                .from('event_stats')
                .select('*')
                .eq('event_id', eventId)
                .single();
            
            if (error && error.code !== 'PGRST116') throw error;
            return data || { listeners: 0, duration: 0, fixed_cost: 0, variable_cost: 0 };
        } catch (e) {
            console.error('getEventStats error:', e);
            return { listeners: 0, duration: 0, fixed_cost: 0, variable_cost: 0 };
        }
    },

    /**
     * Update event stats
     */
    async updateEventStats(eventId, stats) {
        try {
            const { data, error } = await supabaseClient
                .from('event_stats')
                .upsert({
                    event_id: eventId,
                    ...stats,
                    updated_at: new Date().toISOString()
                })
                .select()
                .single();
            
            if (error) throw error;
            return data;
        } catch (e) {
            console.error('updateEventStats error:', e);
            throw e;
        }
    },

    // ============================================
    // LIVEKIT ROOMS
    // ============================================

    /**
     * Create or update LiveKit room record
     */
    async upsertLiveKitRoom(eventId, roomName, roomSid = null) {
        try {
            const { data, error } = await supabaseClient
                .from('livekit_rooms')
                .upsert({
                    event_id: eventId,
                    room_name: roomName,
                    room_sid: roomSid,
                    status: 'created'
                }, { onConflict: 'event_id' })
                .select()
                .single();
            
            if (error) throw error;
            console.log('✅ LiveKit room upserted:', roomName);
            return data;
        } catch (e) {
            console.error('upsertLiveKitRoom error:', e);
            throw e;
        }
    },

    /**
     * Update room status
     */
    async updateRoomStatus(eventId, status, participantCount = null) {
        try {
            const updates = { status };
            if (status === 'active') {
                updates.started_at = new Date().toISOString();
            } else if (status === 'closed') {
                updates.closed_at = new Date().toISOString();
            }
            if (participantCount !== null) {
                updates.participant_count = participantCount;
            }

            const { data, error } = await supabaseClient
                .from('livekit_rooms')
                .update(updates)
                .eq('event_id', eventId)
                .select()
                .single();
            
            if (error) throw error;
            return data;
        } catch (e) {
            console.error('updateRoomStatus error:', e);
            throw e;
        }
    },

    /**
     * Get room by event ID
     */
    async getRoom(eventId) {
        try {
            const { data, error } = await supabaseClient
                .from('livekit_rooms')
                .select('*')
                .eq('event_id', eventId)
                .order('created_at', { ascending: false })
                .limit(1)
                .single();
            
            if (error && error.code !== 'PGRST116') throw error;
            return data;
        } catch (e) {
            console.error('getRoom error:', e);
            return null;
        }
    },

    // ============================================
    // LIVEKIT TRACKS
    // ============================================

    /**
     * Register a new track
     */
    async registerTrack(roomId, eventId, trackData) {
        try {
            const { data, error } = await supabaseClient
                .from('livekit_tracks')
                .insert({
                    room_id: roomId,
                    event_id: eventId,
                    track_sid: trackData.track_sid,
                    track_name: trackData.track_name || null,
                    track_type: trackData.track_type || 'audio',
                    track_source: trackData.track_source || 'unknown',
                    language: trackData.language || null,
                    language_code: trackData.language_code || null,
                    channel_id: trackData.channel_id || null,
                    participant_identity: trackData.participant_identity,
                    participant_sid: trackData.participant_sid || null,
                    participant_name: trackData.participant_name || null,
                    is_agent: trackData.is_agent || false,
                    is_original: trackData.is_original || false,
                    status: 'active',
                    metadata: trackData.metadata || {}
                })
                .select()
                .single();
            
            if (error) throw error;
            console.log('✅ Track registered:', trackData.track_sid);
            return data;
        } catch (e) {
            console.error('registerTrack error:', e);
            throw e;
        }
    },

    /**
     * Update track status
     */
    async updateTrackStatus(trackSid, status) {
        try {
            const updates = { status };
            if (status === 'ended') {
                updates.ended_at = new Date().toISOString();
            }

            const { data, error } = await supabaseClient
                .from('livekit_tracks')
                .update(updates)
                .eq('track_sid', trackSid)
                .select()
                .single();
            
            if (error) throw error;
            return data;
        } catch (e) {
            console.error('updateTrackStatus error:', e);
            throw e;
        }
    },

    /**
     * Get all active tracks for an event
     */
    async getEventTracks(eventId) {
        try {
            const { data, error } = await supabaseClient
                .from('livekit_tracks')
                .select('*')
                .eq('event_id', eventId)
                .eq('status', 'active')
                .order('is_original', { ascending: false })
                .order('language');
            
            if (error) throw error;
            return data || [];
        } catch (e) {
            console.error('getEventTracks error:', e);
            return [];
        }
    },

    /**
     * End all tracks for an event
     */
    async endAllTracks(eventId) {
        try {
            const { data, error } = await supabaseClient
                .from('livekit_tracks')
                .update({ 
                    status: 'ended',
                    ended_at: new Date().toISOString()
                })
                .eq('event_id', eventId)
                .eq('status', 'active');
            
            if (error) throw error;
            console.log('✅ All tracks ended for event:', eventId);
            return true;
        } catch (e) {
            console.error('endAllTracks error:', e);
            return false;
        }
    },

    // ============================================
    // EVENT LOGS
    // ============================================

    /**
     * Add event log
     */
    async addEventLog(eventId, source, level, message, metadata = {}) {
        try {
            const { error } = await supabaseClient
                .from('event_logs')
                .insert({
                    event_id: eventId,
                    source,
                    level,
                    message,
                    metadata
                });
            
            if (error) throw error;
        } catch (e) {
            // Don't throw - logging should never break the app
            console.error('addEventLog error:', e);
        }
    },

    /**
     * Get recent logs for an event
     */
    async getEventLogs(eventId, limit = 100, source = null) {
        try {
            let query = supabaseClient
                .from('event_logs')
                .select('*')
                .eq('event_id', eventId)
                .order('created_at', { ascending: false })
                .limit(limit);
            
            if (source) {
                query = query.eq('source', source);
            }
            
            const { data, error } = await query;
            
            if (error) throw error;
            return data || [];
        } catch (e) {
            console.error('getEventLogs error:', e);
            return [];
        }
    }
};

console.log('✅ DB module loaded');
