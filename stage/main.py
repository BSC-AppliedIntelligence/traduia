"""
Stage Linkers v5.0 - Complete React Rewrite
============================================
Flow:
1. Stage loads channels (initially gray if inactive)
2. Supabase Realtime notifies when channels become active
3. Each channel has: livekit_room_name, livekit_url, track_name
4. User clicks blue button → Stage requests token from Backstage
5. Stage connects to LiveKit and subscribes to specific track

Run: python main.py
"""

import os
import time
import uuid
import json
from typing import Optional
from datetime import datetime

import httpx
import jwt
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# ============================================
# CONFIGURATION
# ============================================

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# LiveKit - Stage generates its own listener tokens
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

SUPABASE_KEY = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY

# ============================================
# FASTAPI APP
# ============================================

app = FastAPI(title="Stage Linkers", version="5.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
os.makedirs("static/assets", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ============================================
# SUPABASE CLIENT
# ============================================

class SupabaseClient:
    def __init__(self):
        self.url = SUPABASE_URL
        self.key = SUPABASE_KEY
        self.headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json"
        }

    async def get_event_by_code(self, code: str) -> Optional[dict]:
        if not self.url or not self.key:
            return None
        async with httpx.AsyncClient() as client:
            try:
                url = f"{self.url}/rest/v1/events?event_code=eq.{code.upper()}&select=id,title,description,event_code,status"
                response = await client.get(url, headers=self.headers)
                if response.status_code == 200:
                    events = response.json()
                    return events[0] if events else None
            except Exception as e:
                print(f"Supabase error: {e}")
        return None

    async def get_channels_for_event(self, event_id: str) -> list:
        if not self.url or not self.key:
            return []
        async with httpx.AsyncClient() as client:
            try:
                url = f"{self.url}/rest/v1/channels?event_id=eq.{event_id}&select=id,name,language,language_code,is_original,status,livekit_room_name,livekit_url,track_name&order=is_original.desc,name.asc"
                response = await client.get(url, headers=self.headers)
                if response.status_code == 200:
                    return response.json()
            except Exception as e:
                print(f"Channels error: {e}")
        return []

supabase = SupabaseClient()

# ============================================
# API ENDPOINTS
# ============================================

@app.get("/api/event/{event_code}")
async def get_event(event_code: str):
    code = event_code.upper()
    event = await supabase.get_event_by_code(code)

    if not event:
        return {"exists": False, "error": "not_found"}

    if event.get("status") not in ("active", "streaming"):
        return {"exists": True, "accessible": False, "error": "not_active"}

    channels = await supabase.get_channels_for_event(event["id"])

    return {
        "exists": True,
        "accessible": True,
        "event": {
            "id": event["id"],
            "title": event.get("title", ""),
            "description": event.get("description", ""),
            "code": event.get("event_code"),
            "status": event.get("status"),
        },
        "channels": [
            {
                "id": ch["id"],
                "name": ch.get("name", ""),
                "language": ch.get("language", "en"),
                "language_code": ch.get("language_code", "en-US"),
                "is_original": ch.get("is_original", False),
                "active": ch.get("status") == "active",
                "livekit_room_name": ch.get("livekit_room_name"),
                "livekit_url": ch.get("livekit_url"),
                "track_name": ch.get("track_name"),
            }
            for ch in channels
        ]
    }


@app.get("/api/config")
async def get_config():
    return {
        "supabase_url": SUPABASE_URL,
        "supabase_anon_key": SUPABASE_ANON_KEY,
    }


class TokenRequest(BaseModel):
    room_name: str
    identity: str

@app.post("/api/livekit/token")
async def get_livekit_token(request: TokenRequest):
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(500, "LiveKit not configured")

    now = int(time.time())
    exp = now + 6 * 3600

    payload = {
        "exp": exp,
        "iss": LIVEKIT_API_KEY,
        "nbf": now,
        "sub": request.identity,
        "name": request.identity,
        "video": {
            "roomJoin": True,
            "room": request.room_name,
            "canPublish": False,
            "canSubscribe": True,
        }
    }

    token = jwt.encode(payload, LIVEKIT_API_SECRET, algorithm="HS256")
    return {"token": token}


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "5.0.0"}


# ============================================
# HTML FRONTEND - Complete React Rewrite v5
# ============================================

HTML_APP = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Stage Linkers</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>
  <script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
    html, body { height: 100%; width: 100%; margin: 0; padding: 0; }
    #root { height: 100%; width: 100%; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #fff; }
    .big-noodle { font-family: 'BigNoodle', Impact, sans-serif; }
    @keyframes scaleIn { from { transform: scale(0.9); opacity: 0; } to { transform: scale(1); opacity: 1; } }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    @keyframes expandFromCorner {
      from { transform: scale(0); opacity: 0; }
      to { transform: scale(1); opacity: 1; }
    }
    .pop-in { animation: scaleIn 0.2s ease-out; }
    /* Menu expands from bottom-right (for language button) */
    .expand-from-br {
      animation: expandFromCorner 0.2s ease-out forwards;
      transform-origin: bottom right;
    }
    .btn-sh { box-shadow: 0 3px 14px rgba(0,0,0,0.12), 0 2px 6px rgba(0,0,0,0.08); }
    .card-sh { box-shadow: 0 4px 20px rgba(0,0,0,0.12), 0 2px 8px rgba(0,0,0,0.06); }
    .code-box { width: 52px; height: 68px; display: flex; align-items: center; justify-content: center; font-size: 40px; border-radius: 14px; background: #f0f0f0; box-shadow: 0 3px 10px rgba(0,0,0,0.1); }
    .code-box-a { border: 3px solid #bbb; background: transparent; }
    .code-box input { width: 100%; height: 100%; text-align: center; background: transparent; border: none; font-size: 40px; color: #333; outline: none; }
    input[type="range"] { -webkit-appearance: none; height: 4px; background: #ddd; border-radius: 2px; }
    input[type="range"]::-webkit-slider-thumb { -webkit-appearance: none; width: 20px; height: 20px; background: #000; border-radius: 50%; cursor: pointer; }
    input[type="color"] { -webkit-appearance: none; border: 1px solid #ddd; cursor: pointer; }
    input[type="color"]::-webkit-color-swatch-wrapper { padding: 0; }
    input[type="color"]::-webkit-color-swatch { border: none; border-radius: 4px; }
    .animate-pulse { animation: pulse 2s infinite; }

    /* Simple native scroll - no fancy stuff */
    .native-scroll {
      overflow-y: auto;
      -webkit-overflow-scrolling: touch;
    }
  </style>
</head>
<body>
<div id="root"></div>
<script type="text/babel">
const { useState, useEffect, useRef, useCallback, useMemo, memo } = React;

// ════════════════════════════════════════════════════════════════
// GLOBAL STATE & LIVEKIT (outside React)
// ════════════════════════════════════════════════════════════════
let supabaseClient = null;
let lkRoom = null;
let audioElement = null;
let currentTrackName = null;

// Store callbacks globally so LiveKit can call them
window.dataCallbacks = {
  onSubtitle: null
};
window.selectedChannel = 'es';
window.isOriginalChannel = false;

async function initConfig() {
  try {
    const res = await fetch('/api/config');
    const config = await res.json();
    if (config.supabase_url && config.supabase_anon_key && window.supabase) {
      supabaseClient = window.supabase.createClient(config.supabase_url, config.supabase_anon_key);
    }
    return config;
  } catch (e) {
    console.error('Config error:', e);
    return {};
  }
}

async function connectToLiveKit(channel, eventId, onStatus) {
  try {
    onStatus('connecting');
    if (!channel.livekit_room_name || !channel.livekit_url) {
      onStatus('error');
      return false;
    }

    currentTrackName = channel.track_name;
    window.selectedChannel = channel.language || 'es';
    window.isOriginalChannel = channel.is_original || false;

    const identity = `listener-${Date.now()}-${Math.random().toString(36).substr(2, 5)}`;
    const tokenRes = await fetch('/api/livekit/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room_name: channel.livekit_room_name, identity })
    });

    if (!tokenRes.ok) {
      onStatus('error');
      return false;
    }

    const { token } = await tokenRes.json();
    if (!window.LivekitClient) {
      onStatus('error');
      return false;
    }

    const { Room, RoomEvent, Track } = window.LivekitClient;

    if (lkRoom) {
      try { await lkRoom.disconnect(); } catch(e) {}
      lkRoom = null;
    }

    if (!audioElement) {
      audioElement = document.createElement('audio');
      audioElement.autoplay = true;
      document.body.appendChild(audioElement);
    }

    lkRoom = new Room({ adaptiveStream: true, dynacast: true });

    lkRoom.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
      const trackName = publication.trackName;
      if (track.kind === Track.Kind.Audio) {
        if (currentTrackName && trackName !== currentTrackName) return;
        track.attach(audioElement);
        onStatus('connected');
      }
    });

    lkRoom.on(RoomEvent.TrackUnsubscribed, (track, publication) => {
      if (track.kind === Track.Kind.Audio && publication?.trackName === currentTrackName) {
        try { track.detach(audioElement); } catch(e) {}
      }
    });

    // DATA RECEIVED - Handle topics
    lkRoom.on(RoomEvent.DataReceived, (payload, participant, kind, topic) => {
      try {
        const data = JSON.parse(new TextDecoder().decode(payload));
        const selectedLang = window.selectedChannel || 'es';
        const isOriginal = window.isOriginalChannel || false;

        // Handle subtitle topics
        if (topic?.startsWith('subtitle.') || topic?.startsWith('transcription.')) {
          const topicType = topic.split('.')[0]; // 'subtitle' or 'transcription'
          const lang = topic.replace(`${topicType}.`, '');

          // For original channel: accept 'original' topic, or matching language with is_original flag
          if (isOriginal) {
            const shouldShow = lang === 'original' ||
                               lang === selectedLang ||
                               data.is_original === true;
            if (shouldShow && window.dataCallbacks.onSubtitle) {
              window.dataCallbacks.onSubtitle(data.text, data.is_partial);
            }
          } else {
            // For translated channels: match language exactly, exclude originals
            if (lang === selectedLang && data.is_original !== true && window.dataCallbacks.onSubtitle) {
              window.dataCallbacks.onSubtitle(data.text, data.is_partial);
            }
          }
          return;
        }

        // Legacy fallback (no topic)
        if (!topic && (data.type === 'subtitle' || data.type === 'transcription')) {
          const subtitleLang = data.channel || data.language;
          const dataIsOriginal = data.is_original || data.channel === 'original';

          // For original channel: show original transcriptions
          // For translated channels: show matching language, non-original
          const shouldShow = (isOriginal && dataIsOriginal) ||
                             (!isOriginal && subtitleLang === selectedLang && !dataIsOriginal);
          if (shouldShow && window.dataCallbacks.onSubtitle) {
            window.dataCallbacks.onSubtitle(data.text, data.is_partial);
          }
        }

      } catch (e) {
        console.error('Data parse error:', e);
      }
    });

    await lkRoom.connect(channel.livekit_url, token);
    try { await lkRoom.startAudio(); } catch (e) {}

    onStatus('connected');
    return true;
  } catch (err) {
    console.error('LiveKit error:', err);
    onStatus('error');
    return false;
  }
}

function disconnectLiveKit() {
  if (lkRoom) {
    try { lkRoom.disconnect(); } catch(e) {}
    lkRoom = null;
  }
  if (audioElement) {
    audioElement.pause();
    audioElement.srcObject = null;
  }
  currentTrackName = null;
}

function setMuted(muted) {
  if (audioElement) audioElement.muted = muted;
}

async function tryStartAudio() {
  if (lkRoom) {
    try { await lkRoom.startAudio(); return true; } catch { return false; }
  }
  return false;
}

function switchTrack(newTrackName) {
  if (!lkRoom || !audioElement) return;
  currentTrackName = newTrackName;
  audioElement.srcObject = null;
  lkRoom.remoteParticipants.forEach(p => {
    p.audioTrackPublications.forEach(pub => {
      if (pub.trackName === newTrackName && pub.track) {
        pub.track.attach(audioElement);
      }
    });
  });
}

// ════════════════════════════════════════════════════════════════
// CONSTANTS
// ════════════════════════════════════════════════════════════════
const T = {
  en: { title: 'Real-time Translation', sub: 'With your headphones.', enter: 'Enter the Room Code', or: 'or', scan: 'Scan a QR Code', notFound: 'Event not found', notActive: 'This event is not active', back: 'Go back', loading: 'Loading...', noChannels: 'No channels streaming', waiting: 'Waiting for stream...', connecting: 'Connecting...', connected: 'Connected', error: 'Connection error', original: 'Original' },
  es: { title: 'Traducción en tiempo real', sub: 'Con tus auriculares.', enter: 'Introduce el código de sala', or: 'o', scan: 'Escanea un código QR', notFound: 'Evento no encontrado', notActive: 'Este evento no está activo', back: 'Volver', loading: 'Cargando...', noChannels: 'No hay canales transmitiendo', waiting: 'Esperando transmisión...', connecting: 'Conectando...', connected: 'Conectado', error: 'Error de conexión', original: 'Original' },
  ca: { title: 'Traducció en temps real', sub: 'Amb els teus auriculars.', enter: 'Introdueix el codi de sala', or: 'o', scan: 'Escaneja un codi QR', notFound: 'Esdeveniment no trobat', notActive: 'Aquest esdeveniment no està actiu', back: 'Tornar', loading: 'Carregant...', noChannels: 'No hi ha canals emetent', waiting: "Esperant emissió...", connecting: 'Connectant...', connected: 'Connectat', error: 'Error de connexió', original: 'Original' }
};

const FLAGS = {
  'en': '🇬🇧', 'es': '🇪🇸', 'ca': '🏴󠁥󠁳󠁣󠁴󠁿', 'fr': '🇫🇷', 'de': '🇩🇪',
  'it': '🇮🇹', 'pt': '🇵🇹', 'ja': '🇯🇵', 'zh': '🇨🇳', 'ko': '🇰🇷',
  'en-US': '🇺🇸', 'en-GB': '🇬🇧', 'es-ES': '🇪🇸', 'es-MX': '🇲🇽',
  'ca-ES': '🏴󠁥󠁳󠁣󠁴󠁿', 'fr-FR': '🇫🇷', 'de-DE': '🇩🇪', 'it-IT': '🇮🇹',
  'pt-PT': '🇵🇹', 'pt-BR': '🇧🇷', 'ja-JP': '🇯🇵', 'zh-CN': '🇨🇳', 'ko-KR': '🇰🇷',
};

const NAMES = {
  'en': 'English', 'es': 'Español', 'ca': 'Català', 'fr': 'Français',
  'de': 'Deutsch', 'it': 'Italiano', 'pt': 'Português', 'ja': '日本語',
  'zh': '中文', 'ko': '한국어',
};

// For Catalan, use the image component; otherwise use emoji
const getFlag = (ch) => {
  if (!ch) return '🏳️';
  if (ch.is_original) return <CatalanFlag />;
  const lang = ch.language_code || ch.language;
  if (lang === 'ca' || lang === 'ca-ES') return <CatalanFlag />;
  return FLAGS[ch.language_code] || FLAGS[ch.language] || '🏳️';
};
const getLangName = (ch) => ch ? (NAMES[ch.language] || ch.name || ch.language || '') : '';
const getLang = () => { const l = navigator.language.split('-')[0]; return ['en','es','ca'].includes(l) ? l : 'en'; };

// ════════════════════════════════════════════════════════════════
// ICONS (pure functions, no state)
// ════════════════════════════════════════════════════════════════
const PlayIcon = ({c='currentColor'}) => <svg width="32" height="32" viewBox="0 0 24 24" fill={c}><path d="M8 5v14l11-7z"/></svg>;
const VolOn = () => <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>;
const VolOff = () => <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>;
const Close = ({s=18}) => <svg width={s} height={s} viewBox="0 0 24 24" fill="currentColor"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>;
const QR = () => <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="3" height="3"/><rect x="18" y="18" width="3" height="3"/></svg>;
const Info = () => <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="#29ABE2" strokeWidth="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>;
// Catalan flag component using image
const CatalanFlag = () => <img src="/static/assets/emojiBanderaCAT.webp" alt="Català" width="28" height="28" style={{ objectFit: 'contain', borderRadius: '4px' }} />;

// ════════════════════════════════════════════════════════════════
// MEMOIZED COMPONENTS (defined OUTSIDE App)
// ════════════════════════════════════════════════════════════════

// Subtitles View (no partial preview - only final text)
const SubtitlesView = memo(function SubtitlesView({ history, settings, t, onTap }) {
  const scrollRef = useRef(null);
  const lastLengthRef = useRef(0);
  const isAtBottomRef = useRef(true);
  const bgColor = settings.bgColor || '#fff';

  const handleScroll = useCallback((e) => {
    const el = e.target;
    isAtBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 50;
  }, []);

  useEffect(() => {
    if (history.length > lastLengthRef.current && scrollRef.current && isAtBottomRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
    lastLengthRef.current = history.length;
  }, [history]);

  return (
    <div style={{
      position: 'relative',
      flex: 1,
      display: 'flex',
      flexDirection: 'column',
      background: bgColor,
      minHeight: 0,
      overflow: 'hidden'
    }}>
      {/* Gradient */}
      <div style={{
        position: 'absolute', top: 0, left: 0, right: 0, height: 80, zIndex: 10, pointerEvents: 'none',
        background: `linear-gradient(to bottom, ${bgColor} 0%, ${bgColor} 30%, transparent 100%)`
      }} />

      {/* SCROLL AREA - native overflow */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        style={{
          flex: 1,
          overflowY: 'auto',
          overflowX: 'hidden',
          WebkitOverflowScrolling: 'touch',
          padding: '24px',
          paddingTop: '24px'
        }}
      >
        {!history ? (
          <p style={{ color: '#9ca3af', textAlign: 'center', marginTop: 80 }}>{t.waiting}</p>
        ) : (
          <div style={{ color: settings.fontColor, fontSize: settings.fontSize, lineHeight: 1.7 }}>
            {history.split('\n\n').filter(Boolean).map((p, i) => (
              <p key={i} style={{ marginBottom: 16 }}>{p}</p>
            ))}
          </div>
        )}
      </div>
    </div>
  );
});


// Language Selector Modal - Expands from the flag button (bottom-right aligned)
const LangMenu = memo(function LangMenu({ channels, currentChannel, onSwitch, onClose, useAnim }) {
  const activeChannels = channels.filter(c => c.active);
  return (
    <div className="fixed inset-0 z-50" onClick={onClose}>
      {/* Menu at same position as button, expands from bottom-right corner */}
      <div
        className={`absolute right-4 bg-white btn-sh ${useAnim ? 'expand-from-br' : ''}`}
        style={{ bottom: '40px', borderRadius: '28px', minWidth: '180px' }}
        onClick={e => e.stopPropagation()}
      >
        {/* Language options */}
        <div className="p-2">
          {activeChannels.map(ch => (
            <button
              key={ch.id}
              onClick={() => { onSwitch(ch); onClose(); }}
              className={`flex items-center gap-3 w-full px-3 py-2.5 hover:bg-gray-100 rounded-xl ${currentChannel?.id === ch.id ? 'bg-blue-50' : ''}`}
            >
              <span className="text-xl">{getFlag(ch)}</span>
              <span className="font-medium text-sm">{getLangName(ch)}</span>
              {currentChannel?.id === ch.id && <span className="ml-auto text-blue-500 text-sm">✓</span>}
            </button>
          ))}
        </div>
        {/* X button to close - at bottom right to align with original button */}
        <div className="p-2 pt-0 flex justify-end">
          <button onClick={onClose} className="w-12 h-12 rounded-full bg-gray-100 flex items-center justify-center hover:bg-gray-200 transition-colors">
            <Close s={18} />
          </button>
        </div>
      </div>
    </div>
  );
});

// ════════════════════════════════════════════════════════════════
// SETTINGS
// ════════════════════════════════════════════════════════════════
const SETTINGS_KEY = 'stage-settings-v6';
const loadSettings = () => { try { return JSON.parse(localStorage.getItem(SETTINGS_KEY)) || null; } catch { return null; } };
const saveSettings = (s) => { try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch {} };
const defaultSettings = { fontSize: 18, fontColor: '#000000', bgColor: '#ffffff', anim: true };

// ════════════════════════════════════════════════════════════════
// MAIN APP
// ════════════════════════════════════════════════════════════════
function App() {
  // Core state
  const [screen, setScreen] = useState('code');
  const [event, setEvent] = useState(null);
  const [channels, setChannels] = useState([]);
  const [channel, setChannel] = useState(null);
  const [settings, setSettings] = useState(() => loadSettings() || defaultSettings);
  const [lang] = useState(getLang);
  const t = T[lang];

  // UI state
  const [code, setCode] = useState(['','','','','']);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [muted, setMutedState] = useState(true);
  const [lkStatus, setLkStatus] = useState('idle');
  const [configLoaded, setConfigLoaded] = useState(false);

  // Modal state
  const [langMenuOpen, setLangMenuOpen] = useState(false);

  // Content state (updated by LiveKit callbacks)
  const [subtitleHistory, setSubtitleHistory] = useState('');
  const [subtitlePartial, setSubtitlePartial] = useState('');

  // Refs
  const inputRefs = useRef([]);
  const realtimeChannel = useRef(null);

  // Initialize
  useEffect(() => {
    initConfig().then(() => setConfigLoaded(true));
  }, []);

  // Save settings
  useEffect(() => { saveSettings(settings); }, [settings]);

  // URL check
  useEffect(() => {
    if (!configLoaded) return;
    const m = window.location.pathname.match(/^\/([A-Za-z]\d{5})/);
    if (m) loadEvent(m[1].toUpperCase());
  }, [configLoaded]);

  // Cleanup
  useEffect(() => {
    return () => {
      disconnectLiveKit();
      if (realtimeChannel.current && supabaseClient) {
        supabaseClient.removeChannel(realtimeChannel.current);
      }
    };
  }, []);

  // Mute sync
  useEffect(() => { setMuted(muted); }, [muted]);

  // Register LiveKit callbacks (these update React state from outside)
  useEffect(() => {
    window.dataCallbacks.onSubtitle = (text, isPartial) => {
      if (isPartial) {
        setSubtitlePartial(text);
      } else {
        setSubtitlePartial('');
        setSubtitleHistory(prev => {
          const trimmed = text.trim();
          if (!trimmed) return prev;
          const needsBreak = prev && /[.!?]$/.test(prev.trim());
          return prev + (needsBreak ? '\n\n' : (prev ? ' ' : '')) + trimmed;
        });
      }
    };

    return () => {
      window.dataCallbacks.onSubtitle = null;
    };
  }, []);

  // ════════════════════════════════════════
  // CALLBACKS (stable references)
  // ════════════════════════════════════════

  const loadEvent = useCallback(async (c) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/event/${c}`);
      const data = await res.json();
      if (!data.exists) {
        setError('notFound');
        setScreen('error');
      } else if (!data.accessible) {
        setError('notActive');
        setScreen('error');
      } else {
        setEvent(data.event);
        setChannels(data.channels || []);
        setScreen('list');
        history.pushState({}, '', `/${c}`);

        // Realtime subscription
        if (realtimeChannel.current && supabaseClient) {
          supabaseClient.removeChannel(realtimeChannel.current);
        }
        if (supabaseClient && data.event?.id) {
          realtimeChannel.current = supabaseClient
            .channel(`ch-${data.event.id}`)
            .on('postgres_changes', {
              event: 'UPDATE',
              schema: 'public',
              table: 'channels',
              filter: `event_id=eq.${data.event.id}`
            }, (payload) => {
              const u = payload.new;
              setChannels(prev => prev.map(ch => ch.id === u.id ? {
                ...ch,
                active: u.status === 'active',
                livekit_room_name: u.livekit_room_name,
                livekit_url: u.livekit_url,
                track_name: u.track_name,
              } : ch));
            })
            .subscribe();
        }
      }
    } catch (e) {
      setError('notFound');
      setScreen('error');
    }
    setLoading(false);
  }, []);

  const selectChannel = useCallback(async (ch) => {
    if (!ch.active || !ch.livekit_room_name) return;
    setSubtitleHistory('');
    setSubtitlePartial('');
    setChannel(ch);
    setMutedState(false);
    setScreen('view');
    await connectToLiveKit(ch, event?.id, setLkStatus);
  }, [event?.id]);

  const switchChannel = useCallback(async (ch) => {
    if (!ch.active) return;
    window.selectedChannel = ch.language || 'es';
    window.isOriginalChannel = ch.is_original || false;
    setSubtitleHistory('');
    setSubtitlePartial('');

    if (channel?.livekit_room_name === ch.livekit_room_name) {
      switchTrack(ch.track_name);
      setChannel(ch);
    } else if (ch.livekit_room_name) {
      setChannel(ch);
      await connectToLiveKit(ch, event?.id, setLkStatus);
    }
  }, [channel?.livekit_room_name, event?.id]);

  const goBack = useCallback(() => {
    disconnectLiveKit();
    if (realtimeChannel.current && supabaseClient) {
      supabaseClient.removeChannel(realtimeChannel.current);
      realtimeChannel.current = null;
    }
    setScreen('code');
    setCode(['','','','','']);
    setError(null);
    setEvent(null);
    setChannels([]);
    setChannel(null);
    setLkStatus('idle');
    history.pushState({}, '', '/');
  }, []);

  const goToList = useCallback(() => {
    disconnectLiveKit();
    setScreen('list');
    setChannel(null);
    setLkStatus('idle');
  }, []);

  const handleTap = useCallback(async () => {
    await tryStartAudio();
    setMutedState(false);
  }, []);

  const handleInput = useCallback((i, v) => {
    if (!/^\d*$/.test(v)) return;
    const d = v.slice(-1);
    setCode(prev => {
      const nc = [...prev];
      nc[i] = d;
      if (d && i < 4) setTimeout(() => inputRefs.current[i+1]?.focus(), 10);
      if (d && i === 4) {
        const full = 'A' + nc.join('');
        if (/^A\d{5}$/.test(full)) setTimeout(() => loadEvent(full), 100);
      }
      return nc;
    });
  }, [loadEvent]);

  const handleKey = useCallback((i, e) => {
    if (e.key === 'Backspace' && !code[i] && i > 0) {
      inputRefs.current[i-1]?.focus();
    }
  }, [code]);

  // ════════════════════════════════════════
  // RENDER
  // ════════════════════════════════════════

  const cls = '';

  if (!configLoaded) {
    return <div className="min-h-screen flex items-center justify-center"><p className="text-gray-400">Loading...</p></div>;
  }

  // Error Screen
  if (screen === 'error') {
    return (
      <div className={`min-h-screen flex flex-col items-center justify-center px-6 ${cls}`}>
        <Info />
        <h1 className="text-2xl font-bold mt-6 mb-2" style={{ color: '#29ABE2' }}>{t[error] || t.notFound}</h1>
        <button onClick={goBack} className="mt-8 px-8 py-4 bg-[#29ABE2] text-white rounded-full font-medium active:scale-95">{t.back}</button>
      </div>
    );
  }

  // Code Entry Screen
  if (screen === 'code') {
    return (
      <div className={`min-h-screen flex flex-col items-center justify-center px-6 ${cls}`}>
        {/* Round blue image above title */}
        <img src="/static/assets/bscTranslatorLogo.png" alt="" style={{ width: 80, height: 80, objectFit: 'contain', marginBottom: 16 }} />
        <div className="text-center mb-10">
          <h1 className="text-3xl font-bold mb-2">{t.title}</h1>
          <p className="text-gray-400 text-lg">{t.sub}</p>
        </div>
        <div className="bg-white rounded-3xl p-8 card-sh w-full max-w-sm">
          <h2 className="text-center text-xl font-medium mb-6">{t.enter}</h2>
          <div className="flex justify-center gap-2 mb-4">
            <div className="code-box code-box-a big-noodle">A</div>
            {code.map((d, i) => (
              <div key={i} className="code-box big-noodle">
                <input
                  ref={el => inputRefs.current[i] = el}
                  type="tel"
                  inputMode="numeric"
                  maxLength="1"
                  value={d}
                  onChange={e => handleInput(i, e.target.value)}
                  onKeyDown={e => handleKey(i, e)}
                  className="big-noodle"
                />
              </div>
            ))}
          </div>
          {loading && <p className="text-center text-gray-400 text-sm">{t.loading}</p>}
        </div>
        <p className="text-gray-400 my-6">{t.or}</p>
        <button className="flex items-center gap-3 px-8 py-4 border-2 border-gray-200 rounded-full">
          <QR /><span>{t.scan}</span>
        </button>
        {/* BSC Link3rs branding */}
        <div style={{ marginTop: 48 }}>
          <img src="/static/assets/BSCLink3rs.png" alt="BSC Link3rs" style={{ height: 20, objectFit: 'contain' }} />
        </div>
      </div>
    );
  }

  // Channel List Screen
  if (screen === 'list') {
    const hasActive = channels.some(c => c.active);
    return (
      <div className={cls} style={{ height: '100vh', display: 'flex', flexDirection: 'column', background: settings.bgColor }}>
        <div style={{ flexShrink: 0, padding: '56px 24px 16px' }}>
          <h1 style={{ fontSize: 30, fontWeight: 'bold', color: settings.fontColor }}>{event?.title || 'Event'}</h1>
          {event?.description && <p style={{ color: '#6b7280', fontSize: 18, marginTop: 4 }}>{event.description}</p>}
        </div>

        <div style={{ flex: 1, overflowY: 'auto', WebkitOverflowScrolling: 'touch', padding: '0 24px' }}>
          {!hasActive && (
            <div style={{ background: '#eff6ff', border: '1px solid #bfdbfe', borderRadius: 16, padding: 16, marginBottom: 24 }}>
              <p style={{ color: '#2563eb', textAlign: 'center' }}>{t.noChannels}</p>
              <p style={{ color: '#60a5fa', textAlign: 'center', fontSize: 14, marginTop: 4 }}>Waiting for stream to start...</p>
            </div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            {channels.map(ch => (
              <div key={ch.id} className="card-sh" style={{
                background: 'white', borderRadius: 24, padding: 20,
                display: 'flex', alignItems: 'center',
                opacity: ch.active ? 1 : 0.6
              }}>
                <div style={{ flex: 1 }}>
                  <h3 style={{ fontWeight: 'bold', fontSize: 18 }}>{ch.name}</h3>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
                    <span style={{ fontSize: 20 }}>{getFlag(ch)}</span>
                    {ch.is_original && <span style={{ fontSize: 12, background: '#f3f4f6', padding: '2px 8px', borderRadius: 12 }}>Català</span>}
                    {ch.active && <span className="animate-pulse" style={{ fontSize: 12, background: '#dcfce7', color: '#16a34a', padding: '2px 8px', borderRadius: 12 }}>● Live</span>}
                  </div>
                </div>
                <button
                  onClick={() => selectChannel(ch)}
                  disabled={!ch.active}
                  style={{
                    width: 64, height: 64, borderRadius: 16, border: 'none',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    background: ch.active ? '#29ABE2' : '#e5e7eb',
                    cursor: ch.active ? 'pointer' : 'not-allowed'
                  }}
                >
                  <PlayIcon c={ch.active ? 'white' : '#9ca3af'} />
                </button>
              </div>
            ))}
          </div>

          {/* BSC Link3rs branding - inside scroll area, after channels */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 24, paddingBottom: 32 }}>
            <span style={{ fontSize: 14, color: 'black' }}>by</span>
            <img src="/static/assets/BSCLink3rs.png" alt="BSC Link3rs" style={{ height: 20, objectFit: 'contain' }} />
          </div>
        </div>
      </div>
    );
  }

  // View Screen (main content)
  if (screen === 'view') {
    const viewBg = settings.bgColor || '#fff';
    return (
      <div className={`h-screen flex flex-col ${cls}`} style={{ background: viewBg }}>
        {/* Header */}
        <div className="flex-shrink-0 px-4 pt-10 pb-4" style={{ background: viewBg }}>
          <button onClick={goToList} className="flex items-center gap-2">
            <div className="w-3 h-3 rounded-full bg-[#29ABE2]" />
            <span className="font-bold text-lg" style={{ color: settings.fontColor }}>{event?.title}</span>
          </button>
        </div>

        {/* Content - only subtitles mode */}
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <SubtitlesView
            history={subtitleHistory}
            settings={settings}
            t={t}
            onTap={handleTap}
          />
        </div>

        {/* Bottom Bar - only language and volume buttons */}
        <div className="flex-shrink-0 flex items-center justify-end px-4 py-5 pb-10" style={{ background: viewBg }}>
          <div className="flex gap-3">
            <button
              onClick={() => setLangMenuOpen(true)}
              className="w-14 h-14 rounded-full flex items-center justify-center text-2xl btn-sh active:scale-95 bg-white"
            >
              {getFlag(channel)}
            </button>
            <button
              onClick={() => { setMutedState(!muted); tryStartAudio(); }}
              className="w-14 h-14 rounded-full flex items-center justify-center btn-sh active:scale-95 bg-white"
              style={{ color: settings.fontColor }}
            >
              {muted ? <VolOff /> : <VolOn />}
            </button>
          </div>
        </div>

        {/* Modals - only language menu */}
        {langMenuOpen && (
          <LangMenu
            channels={channels}
            currentChannel={channel}
            onSwitch={switchChannel}
            onClose={() => setLangMenuOpen(false)}
            useAnim={settings.anim}
          />
        )}
      </div>
    );
  }

  return null;
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>
</body>
</html>'''

@app.get("/", response_class=HTMLResponse)
@app.get("/{code}", response_class=HTMLResponse)
async def serve(code: str = None):
    if code and code.startswith("api"):
        raise HTTPException(404)
    return HTMLResponse(HTML_APP)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))

    print("\n" + "="*60)
    print("  STAGE LINKERS v5.0 - Complete React Rewrite")
    print("="*60)
    print(f"\n  🌐 http://localhost:{port}")
    print(f"\n  📦 Supabase: {'✅ Ready' if SUPABASE_URL else '❌ Not configured'}")
    print(f"  🎥 LiveKit:  {'✅ Ready' if LIVEKIT_API_KEY else '❌ Not configured'}")
    print("\n  Changes:")
    print("  - All components defined OUTSIDE App (no re-mounting)")
    print("  - React.memo for all view components")
    print("  - useCallback for all handlers")
    print("  - Proper scroll containers with CSS")
    print("  - Global LiveKit callbacks (no closure issues)")
    print("\n" + "="*60 + "\n")

    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
