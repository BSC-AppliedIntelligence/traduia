/**
 * Link3rs Backstage - Configuration
 *
 * Supabase credentials are injected by the server via template variables.
 * See the <script> block in each HTML template that sets window.__SUPABASE_URL__
 * and window.__SUPABASE_ANON_KEY__ before this file loads.
 *
 * APP_URL is also injected for redirect purposes (email confirmation, etc.)
 */

// ============================================
// SUPABASE CONFIGURATION (from server)
// ============================================

// Injected by the server (SUPABASE_URL / SUPABASE_ANON_KEY environment variables). Never hardcode a project here:
// the same image must work with Supabase cloud and with a self-hosted instance.
const SUPABASE_URL = (window.__SUPABASE_URL__ || '').trim();
const SUPABASE_ANON_KEY = (window.__SUPABASE_ANON_KEY__ || '').trim();
const APP_URL = window.__APP_URL__ || window.location.origin;

// ============================================
// VALIDATION
// ============================================

window.SUPABASE_CONFIG_ERROR = false;

function validateConfig() {
    const errors = [];

    // Check URL
    if (!SUPABASE_URL || SUPABASE_URL === 'YOUR_SUPABASE_URL') {
        errors.push('SUPABASE_URL not configured - check your .env file on the server');
    } else if (!/^https?:\/\/[^\/\s]+/.test(SUPABASE_URL)) {
        // Any http(s) URL is valid: Supabase cloud (https://xxxxx.supabase.co) or a self-hosted instance
        errors.push('SUPABASE_URL should be a full URL such as https://xxxxx.supabase.co or https://supabase.example.org');
    }

    // Check Key
    if (!SUPABASE_ANON_KEY || SUPABASE_ANON_KEY === 'YOUR_SUPABASE_ANON_KEY') {
        errors.push('SUPABASE_ANON_KEY not configured - check your .env file on the server');
    } else if (!SUPABASE_ANON_KEY.startsWith('eyJ')) {
        errors.push('SUPABASE_ANON_KEY should start with eyJ...');
    }

    return errors;
}

function showConfigError(errors) {
    window.SUPABASE_CONFIG_ERROR = true;

    document.body.innerHTML = `
        <div style="
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #050505;
            color: #fff;
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif;
            padding: 24px;
        ">
            <div style="
                max-width: 500px;
                text-align: center;
                background: rgba(12, 12, 12, 0.9);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 24px;
                padding: 48px;
            ">
                <div style="font-size: 48px; margin-bottom: 24px;">⚠️</div>
                <h1 style="font-size: 24px; font-weight: 600; margin-bottom: 16px;">
                    Configuration Required
                </h1>
                <div style="
                    text-align: left;
                    background: rgba(239, 68, 68, 0.1);
                    border: 1px solid rgba(239, 68, 68, 0.3);
                    border-radius: 12px;
                    padding: 16px;
                    margin-bottom: 24px;
                ">
                    ${errors.map(e => `<p style="color: #ef4444; margin: 8px 0; font-size: 14px;">• ${e}</p>`).join('')}
                </div>
                <p style="color: #777; font-size: 14px; margin-bottom: 24px;">
                    Configure <code style="background: rgba(255,255,255,0.1); padding: 2px 8px; border-radius: 4px;">SUPABASE_URL</code>
                    and <code style="background: rgba(255,255,255,0.1); padding: 2px 8px; border-radius: 4px;">SUPABASE_ANON_KEY</code>
                    in your server's <code style="background: rgba(255,255,255,0.1); padding: 2px 8px; border-radius: 4px;">.env</code> file.
                </p>
                <div style="
                    text-align: left;
                    background: #111;
                    border-radius: 8px;
                    padding: 16px;
                    font-family: 'SF Mono', monospace;
                    font-size: 12px;
                    color: #999;
                ">
                    <div style="color: #666;">// In your .env file:</div>
                    <div style="margin-top: 8px;">
                        SUPABASE_URL=<span style="color: #4ade80;">https://xxx.supabase.co</span>
                    </div>
                    <div>
                        SUPABASE_ANON_KEY=<span style="color: #4ade80;">eyJhbGci...</span>
                    </div>
                    <div>
                        APP_URL=<span style="color: #4ade80;">https://yourdomain.com</span>
                    </div>
                </div>
            </div>
        </div>
    `;
}

// Run validation
console.log('🔧 Checking Supabase configuration...');
const configErrors = validateConfig();

if (configErrors.length > 0) {
    console.error('❌ Configuration errors:', configErrors);
    showConfigError(configErrors);
} else {
    console.log('✅ Supabase URL:', SUPABASE_URL);
    console.log('✅ App URL:', APP_URL);
    console.log('✅ Configuration valid!');

    // Initialize Supabase client
    window.supabaseClient = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
    console.log('✅ Supabase client initialized');
}

// ============================================
// LANGUAGE FLAGS MAPPING
// ============================================

window.LANGUAGE_FLAGS = {
    'en': '🇬🇧',
    'es': '🇪🇸',
    'fr': '🇫🇷',
    'de': '🇩🇪',
    'it': '🇮🇹',
    'pt': '🇧🇷',
    'zh': '🇨🇳',
    'ja': '🇯🇵',
    'ko': '🇰🇷',
    'ru': '🇷🇺',
    'ar': '🇸🇦',
    'hi': '🇮🇳',
    'nl': '🇳🇱',
    'pl': '🇵🇱',
    'ca': '<img src="/static/img/banderaCatEmoji.webp" alt="CA" style="height:1.2em;width:auto;vertical-align:middle;">',
    'any': '🌐'
};

window.LANGUAGE_NAMES = {
    'en': 'English',
    'es': 'Spanish',
    'fr': 'French',
    'de': 'German',
    'it': 'Italian',
    'pt': 'Portuguese',
    'zh': 'Chinese',
    'ja': 'Japanese',
    'ko': 'Korean',
    'ru': 'Russian',
    'ar': 'Arabic',
    'hi': 'Hindi',
    'nl': 'Dutch',
    'pl': 'Polish',
    'ca': 'Catalan'
};

window.getFlag = (langCode) => window.LANGUAGE_FLAGS[langCode] || '🌐';
window.getLangName = (langCode) => window.LANGUAGE_NAMES[langCode] || langCode.toUpperCase();
