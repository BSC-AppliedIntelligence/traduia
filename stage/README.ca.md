# TraduIA Stage — Client d'Escolta en Temps Real

<p align="center">
  <img src="static/assets/bscTranslatorLogo.png" alt="BSC Translator" width="100" />
</p>

**Stage** és el client web orientat als **oients** de la plataforma TraduIA. Permet que els assistents d'un esdeveniment escoltin la traducció simultània i llegeixin subtítols en temps real des del seu mòbil, simplement introduint un codi de sala.

> Forma part de l'ecosistema **TraduIA** (Backstage + AINA Translator + Stage).

---

## Taula de Continguts

- [Visió General](#visió-general)
- [Arquitectura](#arquitectura)
- [Estructura del Projecte](#estructura-del-projecte)
- [Requisits](#requisits)
- [Instal·lació](#installació)
- [Configuració](#configuració)
- [Execució](#execució)
- [API Endpoints](#api-endpoints)
- [Frontend](#frontend)
- [Flux d'un Usuari](#flux-dun-usuari)
- [Desplegament](#desplegament)
- [Troubleshooting](#troubleshooting)

---

## Visió General

Stage és un servidor **FastAPI** lleuger amb un frontend **React 18** embegut en un sol fitxer (`main.py`). Funciona com a interfície pública: els oients obren l'aplicació, introdueixen el codi d'un event, trien l'idioma i reben àudio + subtítols en directe.

```
Orador  ──▶  Backstage  ──▶  LiveKit  ──▶  Stage (oients)
               (ASR + traducció + TTS)         │
                                               ├─ Àudio WebRTC
                                               └─ Subtítols (Data Channels)
```

La càrrega real de streaming la gestiona **LiveKit**. Stage únicament:

1. Serveix el frontend (HTML/React)
2. Consulta events i canals a **Supabase**
3. Genera **JWT tokens** de lectura per a LiveKit
4. S'actualitza en temps real via **Supabase Realtime**

Això el fa completament **stateless** i molt fàcil d'escalar.

---

## Arquitectura

| Capa | Tecnologia | Rol |
|------|-----------|-----|
| Backend | FastAPI + Uvicorn | API REST, servir HTML, generar tokens JWT |
| Frontend | React 18 (embegut inline) | SPA mòbil, reproductor d'àudio, subtítols |
| Base de dades | Supabase (PostgreSQL) | Events, canals, configuració |
| Temps real | Supabase Realtime | Notificacions d'activació de canals |
| Streaming | LiveKit (WebRTC) | Àudio traduït + dades (subtítols) |
| Autenticació | PyJWT | Tokens de lectura per LiveKit |

### Serveis externs necessaris

| Servei | Ús | Compte necessari |
|--------|-----|-----------------|
| **Supabase** | BD + Realtime | Sí (pla gratuït vàlid) |
| **LiveKit** | Streaming àudio i dades | Sí (Cloud o self-hosted) |

---

## Estructura del Projecte

```
stage_bsc/
├── main.py                 # Aplicació completa (backend + frontend)
├── requirements.txt        # Dependències Python
├── .env                    # Variables d'entorn (no inclòs al repo)
├── .env.example            # Plantilla de variables d'entorn
├── README.md               # Aquest fitxer
└── static/
    └── assets/
        ├── bscTranslatorLogo.png   # Logo principal (pantalla inicial)
        ├── BSCLink3rs.png          # Logo BSC Link3rs (branding inferior)
        └── emojiBanderaCAT.webp    # Bandera catalana (canal original)
```

### `main.py` — Fitxer únic

El fitxer `main.py` conté **tot** el backend i frontend:

- Imports i configuració d'entorn
- Client Supabase (`SupabaseClient`) amb mètodes `get_event_by_code` i `get_channels_for_event`
- Endpoints FastAPI (event, config, token, health)
- Frontend React 18 complet embegut com a string HTML (`HTML_APP`)
  - Gestió LiveKit global (connexió, tracks, data channels)
  - Constants d'idioma (T), banderes (FLAGS), noms (NAMES)
  - Components d'icones SVG + CatalanFlag
  - Components de vista memoitzats (SubtitlesView, LangMenu)
  - Component principal `App` amb tota la lògica d'estat
- Rutes FastAPI i arrencada del servidor

---

## Requisits

### Software

- Python 3.9 o superior
- pip

### Dependències

```
fastapi>=0.100.0
uvicorn[standard]>=0.23.0
httpx>=0.24.0
python-dotenv>=1.0.0
pydantic>=2.0.0
PyJWT>=2.8.0
```

### CDN (carregats pel frontend)

El frontend carrega automàticament des de CDN:

- React 18 + ReactDOM (`unpkg.com`)
- Babel Standalone (`unpkg.com`)
- Tailwind CSS (`cdn.tailwindcss.com`)
- Supabase JS v2 (`cdn.jsdelivr.net`)
- LiveKit Client SDK (`cdn.jsdelivr.net`)
- Font OpenDyslexic (`fonts.cdnfonts.com`)

---

## Instal·lació

```bash
# 1. Clonar el repositori
git clone <url-del-repo>
cd stage_bsc

# 2. Crear entorn virtual (recomanat)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# 3. Instal·lar dependències
pip install -r requirements.txt

# 4. Crear fitxer .env
cp .env.example .env
# Editar .env amb les credencials reals
```

---

## Configuració

### Variables d'entorn (`.env`)

```env
# Supabase
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOi...
SUPABASE_SERVICE_KEY=eyJhbGciOi...   # Opcional, utilitza anon si no hi és

# LiveKit
LIVEKIT_URL=wss://xxx-xxx.livekit.cloud
LIVEKIT_API_KEY=APIhixxxxxxxxxxx
LIVEKIT_API_SECRET=erSiE1xeNNkptxxxxxxxxx
```

> **Nota:** Les credencials de Supabase i LiveKit han de ser les **mateixes** que utilitza el Backstage, ja que comparteixen la mateixa base de dades i les mateixes sales de LiveKit.

### Taules de Supabase

Stage només **llegeix** — no escriu a la base de dades. Necessita que existeixin dues taules:

#### `events`

| Columna | Tipus | Descripció |
|---------|-------|------------|
| `id` | UUID (PK) | Identificador únic |
| `event_code` | VARCHAR(6) | Codi de sala (format `A12345`) |
| `title` | VARCHAR(255) | Nom de l'event |
| `description` | TEXT | Descripció |
| `status` | VARCHAR(20) | `inactive`, `active`, `streaming`, `ended` |

#### `channels`

| Columna | Tipus | Descripció |
|---------|-------|------------|
| `id` | UUID (PK) | Identificador únic |
| `event_id` | UUID (FK) | Referència a `events.id` |
| `name` | VARCHAR(100) | Nom del canal (ex: "Español", "English") |
| `language` | VARCHAR(10) | Codi d'idioma (`es`, `en`, `ca`, `fr`...) |
| `language_code` | VARCHAR(10) | Codi regional (`es-ES`, `en-US`, `ca-ES`...) |
| `is_original` | BOOLEAN | Si és el canal de l'idioma original |
| `status` | VARCHAR(20) | `inactive` o `active` |
| `livekit_room_name` | VARCHAR(100) | Nom de la sala LiveKit |
| `livekit_url` | VARCHAR(255) | URL WebSocket de LiveKit |
| `track_name` | VARCHAR(100) | Nom del track d'àudio específic |

#### Realtime

La taula `channels` ha de tenir **Realtime activat** a Supabase perquè Stage rebi actualitzacions d'estat en directe (quan Backstage activa o desactiva canals).

---

## Execució

```bash
python main.py
```

Sortida esperada:

```
============================================================
  STAGE LINKERS v5.0 - Complete React Rewrite
============================================================

  🌐 http://localhost:8080

  📦 Supabase: ✅ Ready
  🎥 LiveKit:  ✅ Ready
============================================================
```

L'aplicació estarà disponible a **http://localhost:8080**.

---

## API Endpoints

### `GET /api/event/{event_code}`

Retorna la informació d'un event i els seus canals.

**Request:** `GET /api/event/A12345`

**Resposta (èxit):**
```json
{
  "exists": true,
  "accessible": true,
  "event": {
    "id": "uuid",
    "title": "Conferència Barcelona",
    "description": "Descripció",
    "code": "A12345",
    "status": "active"
  },
  "channels": [
    {
      "id": "uuid",
      "name": "Català",
      "language": "ca",
      "language_code": "ca-ES",
      "is_original": true,
      "active": true,
      "livekit_room_name": "conf-bcn-2026",
      "livekit_url": "wss://livekit.example.com",
      "track_name": "audio-original"
    }
  ]
}
```

**Errors:**
- `{"exists": false, "error": "not_found"}` — Codi no trobat
- `{"exists": true, "accessible": false, "error": "not_active"}` — Event no actiu

### `POST /api/livekit/token`

Genera un token JWT de **només lectura** per connectar a LiveKit.

**Body:**
```json
{
  "room_name": "conf-bcn-2026",
  "identity": "listener-1707312345-abc12"
}
```

**Resposta:**
```json
{
  "token": "eyJhbGciOiJIUzI1NiIs..."
}
```

El token generat té permisos `canPublish: false, canSubscribe: true` i expira en 6 hores.

### `GET /api/config`

Retorna la configuració pública de Supabase perquè el frontend pugui inicialitzar el client Realtime.

```json
{
  "supabase_url": "https://xxxxx.supabase.co",
  "supabase_anon_key": "eyJhbGciOi..."
}
```

### `GET /api/health`

Health check per a monitorització.

```json
{
  "status": "ok",
  "version": "5.0.0"
}
```

---

## Frontend

### Pantalles

L'aplicació té quatre pantalles gestionades per l'estat `screen`:

| Pantalla | Descripció |
|----------|-----------|
| `code` | Entrada del codi d'event (A + 5 dígits). Mostra el logo BSC Translator i el branding BSCLink3rs. |
| `error` | Missatge d'error si l'event no existeix o no està actiu. |
| `list` | Llista de canals disponibles amb estat en directe. El canal original es mostra amb la bandera catalana i l'etiqueta "Català". |
| `view` | Pantalla principal de subtítols amb barra inferior (botó idioma + botó volum). |

### Mode de visualització

L'únic mode de visualització és **Subtítols** — text continu de la traducció amb auto-scroll.

### Barra inferior (pantalla `view`)

La barra inferior conté únicament dos botons:

- **Bandera (idioma):** Obre un menú per canviar de canal/idioma sense desconnectar-se.
- **Volum:** Silencia/activa l'àudio.

### Topics de LiveKit Data Channels

Stage escolta les següents dades enviades per Backstage:

| Topic | Format | Descripció |
|-------|--------|-----------|
| `subtitle.{lang}` | `{text, is_partial}` | Subtítols traduïts per idioma |
| `subtitle.original` | `{text, is_partial}` | Transcripció de l'idioma original |
| `transcription.{lang}` | `{text, is_partial}` | Àlies per subtítols |

### Idiomes suportats

La interfície està disponible en tres idiomes (detecció automàtica del navegador):

- Castellà (`es`)
- Anglès (`en`)
- Català (`ca`)

### Persistència de preferències

Les preferències de l'usuari (mida de font, colors, etc.) es guarden a `localStorage` amb la clau `stage-settings-v6`.

---

## Flux d'un Usuari

```
1. Obre l'app → GET / (Stage serveix HTML amb React)

2. Introdueix codi "A12345"
   └─▶ GET /api/event/A12345
       └─▶ Stage consulta Supabase → retorna event + canals

3. Stage s'subscriu a Supabase Realtime
   └─▶ Escolta canvis a la taula "channels" per aquest event

4. Selecciona canal "Español"
   └─▶ POST /api/livekit/token
       └─▶ Stage genera JWT read-only → retorna token

5. Connexió WebRTC directa a LiveKit
   └─▶ Àudio streaming (WebRTC track)
   └─▶ Subtítols (DataReceived events)
   └─▶ Stage ja NO intervé fins que l'usuari canviï de canal

6. Canvia d'idioma
   └─▶ Si mateixa sala: switchTrack() (instantani)
   └─▶ Si sala diferent: POST /api/livekit/token + reconnectar
```

---

## Desplegament

### Docker (local)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY static/ static/
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

```bash
docker build -t stage-bsc:latest .
docker run -p 8080:8080 --env-file .env stage-bsc:latest
```

La imatge publicada és `ghcr.io/bsc-appliedintelligence/traduia/stage`. `scripts/docker_build_and_push.sh` construeix (i opcionalment publica) amb buildx; es configura amb les variables `IMAGE_NAME`, `DOCKERFILE_PATH`, `PLATFORMS`, `PUSH` i `LOAD`.

### Escalabilitat

**Resum d'escalabilitat:** Stage és completament stateless. Amb 3–5 pods i LiveKit Cloud, pot servir desenes de milers d'usuaris simultanis, cosa que realment no necesita ni fará mai, ja que nomes ho fa per el Backstage traductor i les traduccions es fan per sala i després s'escala l'enviament amb Livekit, no es generan per usuari. Es a dir, amb una RTX 5090 amb un preu aproximat de 0.89€/hora, funciona de sobres per una audiencia pràcticament infinita.

---

## Troubleshooting

| Problema | Solució |
|----------|---------|
| L'àudio no es reprodueix | Els navegadors requereixen interacció de l'usuari. Tocar la pantalla activa `startAudio()`. Verificar que el canal té `livekit_url` i `track_name`. |
| Subtítols no apareixen | Verificar que Backstage envia al topic `subtitle.{lang}`. Obrir DevTools per veure errors. |
| Codi d'event no funciona | Format correcte: `A` + 5 dígits. L'event ha de tenir status `active` o `streaming` a Supabase. |
| Canals no s'actualitzen | Verificar que Realtime està activat a la taula `channels` de Supabase. Verificar polítiques RLS. |
| No es veu la bandera catalana | Verificar que `emojiBanderaCAT.webp` existeix a `static/assets/`. |

---

## Llicència

Apache-2.0: vegeu `LICENSE` a l'arrel del repositori.
