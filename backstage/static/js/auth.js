/**
 * Link3rs Backstage - Authentication Module
 */

const Auth = {
    /**
     * Get current session
     */
    async getSession() {
        try {
            const { data, error } = await supabaseClient.auth.getSession();
            if (error) throw error;
            return data.session;
        } catch (e) {
            console.error('getSession error:', e);
            return null;
        }
    },

    /**
     * Get current user
     */
    async getUser() {
        try {
            const { data, error } = await supabaseClient.auth.getUser();
            if (error) throw error;
            return data.user;
        } catch (e) {
            console.error('getUser error:', e);
            return null;
        }
    },

    /**
     * Get user profile from database
     */
    async getProfile(userId) {
        try {
            const { data, error } = await supabaseClient
                .from('profiles')
                .select('*')
                .eq('id', userId)
                .single();
            
            if (error) {
                console.error('getProfile error:', error);
                return null;
            }
            return data;
        } catch (e) {
            console.error('getProfile error:', e);
            return null;
        }
    },

    /**
     * Sign up new user
     */
    async signUp(email, password, fullName) {
        try {
            const { data, error } = await supabaseClient.auth.signUp({
                email,
                password,
                options: {
                    data: { full_name: fullName },
                    emailRedirectTo: `${APP_URL}/pending-approval`
                }
            });
            
            if (error) throw error;
            return { success: true, data };
        } catch (e) {
            console.error('signUp error:', e);
            return { success: false, error: e.message };
        }
    },

    /**
     * Sign in user
     */
    async signIn(email, password) {
        try {
            const { data, error } = await supabaseClient.auth.signInWithPassword({
                email,
                password
            });
            
            if (error) throw error;
            return { success: true, data };
        } catch (e) {
            console.error('signIn error:', e);
            return { success: false, error: e.message };
        }
    },

    /**
     * Sign out user
     */
    async signOut() {
        try {
            await supabaseClient.auth.signOut();
            window.location.href = '/login';
        } catch (e) {
            console.error('signOut error:', e);
            window.location.href = '/login';
        }
    },

    /**
     * Check if user is authenticated and approved
     * Returns: { session, profile, approved, admin } or redirects
     */
    async requireAuth() {
        console.log('🔐 Checking authentication...');
        
        const session = await this.getSession();
        
        if (!session) {
            console.log('❌ No session, redirecting to login');
            window.location.href = '/login';
            return null;
        }
        
        console.log('✅ Session found');
        
        // Get profile
        const profile = await this.getProfile(session.user.id);
        
        if (!profile) {
            console.log('❌ No profile found');
            // Profile might not exist yet, redirect to pending
            window.location.href = '/pending-approval';
            return null;
        }
        
        // Check approval status
        if (!profile.is_approved) {
            console.log('⏳ User not approved');
            window.location.href = '/pending-approval';
            return null;
        }
        
        console.log('✅ User authenticated and approved');
        return {
            session,
            profile,
            approved: profile.is_approved,
            admin: profile.is_admin
        };
    },

    /**
     * Check pending approval status
     */
    async checkPendingStatus() {
        const session = await this.getSession();
        
        if (!session) {
            window.location.href = '/login';
            return null;
        }
        
        const profile = await this.getProfile(session.user.id);
        
        if (profile && profile.is_approved) {
            window.location.href = '/dashboard';
            return null;
        }
        
        return { session, profile };
    },

    /**
     * Reset password request
     */
    async resetPassword(email) {
        try {
            const { data, error } = await supabaseClient.auth.resetPasswordForEmail(email, {
                redirectTo: `${APP_URL}/reset-password`
            });
            
            if (error) throw error;
            return { success: true };
        } catch (e) {
            console.error('resetPassword error:', e);
            return { success: false, error: e.message };
        }
    },

    /**
     * Update password
     */
    async updatePassword(newPassword) {
        try {
            const { data, error } = await supabaseClient.auth.updateUser({
                password: newPassword
            });
            
            if (error) throw error;
            return { success: true };
        } catch (e) {
            console.error('updatePassword error:', e);
            return { success: false, error: e.message };
        }
    },

    /**
     * Update profile
     */
    async updateProfile(userId, updates) {
        try {
            const { data, error } = await supabaseClient
                .from('profiles')
                .update(updates)
                .eq('id', userId)
                .select()
                .single();
            
            if (error) throw error;
            return { success: true, data };
        } catch (e) {
            console.error('updateProfile error:', e);
            return { success: false, error: e.message };
        }
    }
};

console.log('✅ Auth module loaded');
