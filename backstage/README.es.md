# TraduIA Backstage

Plataforma de traducción simultánea en tiempo real para eventos en directo. Captura el audio de un ponente en catalán, lo transcribe, traduce a múltiples idiomas, sintetiza la voz y retransmite todo en tiempo real a los oyentes.

```
Ponente (catalán) → Whisper Live (ASR) → AINA Translator → Google TTS → Oyentes (6 idiomas)
```

---

## Servicios Necesarios

| Servicio | Qué es | Para qué se usa |
|----------|---------------|----------------------|
| **Supabase** | Base de datos PostgreSQL + autenticación | BD, usuarios, eventos, canales |
| **LiveKit** | Plataforma WebRTC de audio en tiempo real | Streaming de audio ponente ↔ oyentes |
| **Google Cloud TTS** | Síntesis de voz neuronal | Convertir texto traducido en audio (Chirp 3 HD) |
| **Whisper Live** | ASR self-hosted basado en OpenAI Whisper | Transcripción de audio a texto en tiempo real |
| **AINA Translator** | Traducción automática (Generalitat de Catalunya) | Traducción ca → es, en, fr, pt, it, de |
| **RunPod** | GPUs en la nube | Alojar Whisper Live y AINA Translator |

---

## Estructura del Proyecto

```
backstage/
├── main.py                   # Servidor FastAPI (toda la lógica del backend)
├── requirements.txt          # Dependencias Python
├── .env                      # Variables de entorno (secretas)
├── .env.example              # Plantilla de configuración
├──
├── asr/
│   └── whisper_client.py     # Cliente WebSocket para Whisper Live
├──
├── translation/
│   ├── base.py               # Clase abstracta BaseTranslator
│   ├── aina_translator.py    # Cliente HTTP para AINA Translator
│   └── placeholder.py        # NullTranslator (modo solo original)
├──
├── tts/
│   └── google_tts.py         # Google Cloud TTS (Chirp 3 HD / Neural2)
├──
├── utils/
│   ├── text_normalizer.py    # Normalización de números para TTS
│   └── tts_buffer_catalan.py # Buffer inteligente de texto para TTS
├──
├── static/
│   ├── css/styles.css        # Tema visual
│   ├── img/                  # Logos y banderas
│   └── js/
│       ├── config.js         # Configuración Supabase + idiomas
│       ├── auth.js           # Autenticación de usuarios
│       ├── db.js             # Operaciones CRUD (Supabase)
│       └── livekit.js        # Integración LiveKit del frontend
├──
├── templates/                # 13 páginas HTML
│   ├── index.html            # Router de entrada
│   ├── login.html            # Inicio de sesión
│   ├── signup.html           # Registro
│   ├── pending-approval.html # Esperando aprobación
│   ├── dashboard.html        # Listado de eventos
│   ├── event.html            # Gestión de un evento
│   ├── speaker.html          # Interfaz del ponente
│   ├── monitor.html          # Monitoreo con logs
│   ├── stage.html            # Página pública de oyentes
│   ├── captions.html         # Overlay de subtítulos (OBS)
│   ├── archive.html          # Eventos archivados
│   ├── account.html          # Ajustes de cuenta
│   └── admin.html            # Panel de administración
└──
├── db/                       # Inicialización de la base de datos (imagen Docker, ver docs/database-init.md)
│   ├── migrations/           # Migraciones SQL numeradas: 001_schema.sql … 005, 010_admin_bootstrap.sql
│   ├── init/                 # db-init.sh y SQL auxiliar
│   └── Dockerfile
└── sql/                      # Scripts manuales de mantenimiento
    ├── A_test_ver_usuarios.sql
    ├── B_autorizar_admin.sql # alternativa manual a ADMIN_EMAILS
    └── CLEANUP_*, SETUP_*, debug_*
```

---

## Flujo del Sistema

```
                         BACKSTAGE SERVER (main.py)
                    ┌──────────────────────────────────────────┐
                    │                                          │
 ┌──────────┐       │  ┌─────────────────────────┐             │
 │          │ audio │  │     TranslationSession  │             │
 │  Ponente │──────→│  │                         │             │
 │ (Browser)│       │  │  Audio 48kHz → 16kHz    │             │
 └──────────┐       │  │       │                 │             │
                    │  │       ↓                 │             │
                    │  │  Whisper Live (ASR)     │             │
                    │  │  "Bon dia a tothom"     │             │
                    │  │       │                 │             │
                    │  │       ↓                 │             │
                    │  │  AINA Translator        │             │
                    │  │  ca→es: "Buenos días"   │             │
                    │  │  ca→en: "Good morning"  │             │
                    │  │  ca→fr: "Bonjour"       │             │
                    │  │       │                 │             │
                    │  │       ↓                 │             │
                    │  │  Google TTS (Chirp 3 HD)│             │
                    │  │  Texto → Audio 24kHz    │             │
                    │  │       │                 │             │
                    │  │       ↓                 │             │
                    │  │  AudioPublisher         │             │
                    │  │  24kHz → 48kHz + fade   │             │
                    │  └─────────────────────────┐             │
                    │       │ tracks (tts-es, tts-en, tts-fr...)
                    │       ↓                                  │
                    │   LiveKit Room                           │
                    └──────────────────────────────────────────
                         │              │                │
                    ┌─────────┐    ┌─────────┐    ┌─────────┐
                    │ Oyente  │    │ Oyente  │    │ Captions│
                    │ (es)    │    │ (en)    │    │ (OBS)   │
                    └─────────┐    └─────────┐    └─────────┐
```

### Pipeline de Audio

| Etapa | Sample Rate | Formato | Notas |
|-------|-------------|---------|-------|
| Captura del navegador | 48 kHz | WebRTC (Opus) | Navigator.mediaDevices |
| Transporte LiveKit | 48 kHz | Opus | Adaptive bitrate |
| Whisper Live (ASR) | 16 kHz | PCM float32 | Resampled desde 48kHz |
| Google TTS salida | 24 kHz | PCM int16 | Chirp 3 HD |
| Publicación LiveKit | 48 kHz | Opus | Resampleado con fade-in 500ms |

---

## Setup Paso a Paso

### 1. Supabase (Base de Datos)

1. Crear cuenta en [supabase.com](https://supabase.com) y crear un nuevo proyecto.
2. Ir a **Settings > API** y copiar:
   - **Project URL** → `SUPABASE_URL`
   - **anon/public key** → `SUPABASE_ANON_KEY` (para el frontend)
   - **service_role key** → `SUPABASE_SERVICE_KEY` (para el backend)
3. Inicializar la base de datos. **Supabase autoalojado** (Postgres + GoTrue + PostgREST): usar la imagen de
   inicialización, que aplica una sola vez cada migración de `db/migrations/` (001…005 esquema, 010 administradores
   iniciales) y configura contraseñas de roles y JWT; es idempotente y se puede ejecutar en cada despliegue:
   ```bash
   docker pull ghcr.io/bsc-appliedintelligence/traduia/db-init:2026.11.26   # o: docker build -f db/Dockerfile -t traduia-db-init .
   docker run --rm --network <red-de-supabase> -e PGHOST=<host-postgres> -e POSTGRES_PASSWORD=... -e JWT_SECRET=... \
     -e ADMIN_EMAILS=admin@ejemplo.org ghcr.io/bsc-appliedintelligence/traduia/db-init:2026.11.26
   ```
   Detalles, variables y publicación de la imagen en [docs/database-init.md](docs/database-init.md).
   **Supabase cloud** (sin acceso de superusuario): ejecutar en el **SQL Editor** los ficheros de `db/migrations/`
   en orden numérico (001, 002, 003, 004, 005 y 010).
4. Con la app corriendo, hacer **Sign Up** con un email incluido en `ADMIN_EMAILS`: queda aprobado y como admin
   automáticamente. Sin `ADMIN_EMAILS` (p. ej. Supabase cloud), usar `sql/A_test_ver_usuarios.sql` para ver los usuarios
   pendientes y `sql/B_autorizar_admin.sql` (cambiar el email en el script) para aprobar y hacer admin.
5. `SUPABASE_URL` y `SUPABASE_ANON_KEY` solo se configuran en el servidor (`.env`): el servidor los inyecta en las
   páginas y `static/js/config.js` no se edita.

### 2. LiveKit (Streaming de Audio)

#### LiveKit Cloud

1. Crear cuenta en [livekit.io](https://livekit.io).
2. Crear un proyecto y copiar:
   - **URL** (ej: `wss://mi-proyecto.livekit.cloud`) → `LIVEKIT_URL`
   - **API Key** → `LIVEKIT_API_KEY`
   - **API Secret** → `LIVEKIT_API_SECRET`

#### LiveKit self-hosted de BSC

Backstage usa dos direcciones cuando se ejecuta en Docker junto a
`bsc-livekit`:

```dotenv
# Dirección pública devuelta a los navegadores y guardada en Supabase.
LIVEKIT_URL=ws://<Spark-address>:7880

# Dirección interna usada por la API y la sesión servidor de Backstage.
LIVEKIT_INTERNAL_URL=ws://livekit:7880
```

El navegador debe poder alcanzar `7880/TCP`, `7881/TCP` y `7882/UDP` en el
Spark. No usar un túnel SSH como sustituto del puerto UDP de WebRTC.

### 3. Google Cloud TTS (Síntesis de Voz)

1. Ir a [console.cloud.google.com](https://console.cloud.google.com), crear/seleccionar un proyecto.
2. Activar la API **Cloud Text-to-Speech**.
3. Ir a **IAM & Admin > Service Accounts**, crear una cuenta de servicio.
4. Generar una clave JSON y descargarla.
5. Configurar en `.env`:
   ```
   GOOGLE_APPLICATION_CREDENTIALS=/ruta/al/archivo.json
   TTS_PROVIDER=google_chirp
   TTS_VOICE=Charon
   ```

La voz **Charon** (masculina, Chirp 3 HD) está disponible en es-ES, en-US, fr-FR, it-IT, de-DE y pt-BR. El sistema construye automáticamente el nombre completo como `{idioma}-Chirp3-HD-{voz}` (ej: `es-ES-Chirp3-HD-Charon`).

### 4. Whisper Live (Reconocimiento de Voz)

Whisper Live es un servidor self-hosted de ASR. Se despliega típicamente en **RunPod**:

1. Crear cuenta en [runpod.io](https://runpod.io).
2. Crear un Pod con GPU (mínimo 8GB VRAM).
3. Desplegar el contenedor Docker de Whisper Live (expone puerto WebSocket, normalmente `9090`).
4. En la app web, pestaña **Endpoints** del evento, configurar host y puerto.

Whisper se configura **por evento**, no globalmente. Cada evento puede apuntar a un servidor diferente.

### 5. AINA Translator (Traducción)

También se despliega en **RunPod**:

1. Crear un Pod con GPU.
2. Desplegar el contenedor Docker de AINA Translator (expone puerto `8000`).
3. RunPod genera una URL pública: `https://{POD-ID}-8000.proxy.runpod.net`
4. En la app web, pestaña **Endpoints** del evento, introducir solo el **Pod ID** (la URL se genera automáticamente).

Idiomas soportados: **es** (castellano), **en** (inglés), **fr** (francés), **pt** (portugués), **it** (italiano), **de** (alemán).

### 6. Instalar y Ejecutar

```bash
# Crear entorno virtual
python -m venv venv
source venv/bin/activate        # Linux/Mac
# venv\Scripts\activate         # Windows

# Instalar dependencias
pip install -r requirements.txt

# Copiar y rellenar configuración
cp .env.example .env
# Editar .env con los valores de cada servicio

# Ejecutar
python main.py
```

El servidor arranca en `http://localhost:8000`.

---

## Variables de Entorno (.env)

| Variable | Servicio | Descripción |
|----------|----------|-------------|
| `SUPABASE_URL` | Supabase | URL del proyecto (https://xxxxx.supabase.co) |
| `SUPABASE_ANON_KEY` | Supabase | Clave anon/public; el servidor la inyecta en el frontend |
| `SUPABASE_SERVICE_KEY` | Supabase | Clave service_role (solo backend) |
| `LIVEKIT_URL` | LiveKit | URL pública WebSocket entregada a navegadores; Cloud o `ws://<Spark-address>:7880` en BSC self-hosted |
| `LIVEKIT_INTERNAL_URL` | LiveKit | URL interna de Backstage; `ws://livekit:7880` cuando Backstage y LiveKit comparten red Docker. Por defecto usa `LIVEKIT_URL` |
| `LIVEKIT_API_KEY` | LiveKit | Clave API |
| `LIVEKIT_API_SECRET` | LiveKit | Secreto API |
| `GOOGLE_APPLICATION_CREDENTIALS` | Google Cloud | Ruta al JSON de credenciales |
| `TTS_PROVIDER` | Google TTS | `google_chirp` (Chirp 3 HD) o `google_regular` (Neural2) |
| `TTS_VOICE` | Google TTS | Nombre de la voz (ej: Charon, Aoede, Kore) |

`SUPABASE_URL` y `SUPABASE_ANON_KEY` se inyectan en las páginas desde estas variables: `static/js/config.js` ya no
contiene ningún proyecto fijo. Si faltan, la aplicación muestra la pantalla «Configuration Required».

---

## Interfaz Web

### Flujo de usuario

1. **`/login`** — Inicio de sesión con email y contraseña (Supabase Auth).
2. **`/signup`** — Registro. El usuario queda pendiente de aprobación por un admin.
3. **`/pending-approval`** — Pantalla de espera hasta que un admin apruebe la cuenta.
4. **`/dashboard`** — Listado de eventos activos. Botón para crear nuevos eventos.
5. **`/event/{code}`** — Gestión de un evento con 5 pestañas:
   - **Channels**: Configurar canales de audio (original + traducciones).
   - **Share**: URL pública del evento para compartir con la audiencia.
   - **Terminal**: Logs en tiempo real vía WebSocket con filtros por fuente.
   - **Endpoints**: Configurar Whisper Live (host, puerto, modelo) y AINA Translator (Pod ID).
   - **Settings**: Editar título/descripción, archivar o eliminar evento.
6. **`/speaker/{code}`** — Interfaz del ponente: selector de micro, visualizador de onda, botón de inicio, terminal de logs. Al pulsar inicio ejecuta los preflight checks y arranca la sesión.
7. **`/monitor/{code}`** — Tres columnas: canales de audio, subtítulos en directo (original + traducción), y logs en tiempo real.
8. **`/stage/{code}`** — Página pública para oyentes. Selección de idioma con banderas, subtítulos en directo, control de volumen. No requiere autenticación.
9. **`/event/{code}/publicdomain/captions`** — Overlay de subtítulos para OBS. Personalizable: líneas, fuente, tamaño, color, tema. Atajos de teclado: `C` captura, `T` tema, `H` panel, flechas tamaño, `Esc` limpiar.
10. **`/admin`** — Aprobar usuarios y gestionar roles (solo admins).
11. **`/account`** — Cambiar nombre y contraseña.
12. **`/archive`** — Eventos archivados con opción de restaurar o eliminar.

### Preflight Checks

Antes de iniciar una sesión, el sistema verifica automáticamente 5 puntos. Si alguno falla, la sesión no arranca y se muestra el error en el terminal:

1. Que la configuración de Whisper existe (host y puerto).
2. Que la URL del traductor está configurada si hay canales de traducción.
3. Que los idiomas solicitados están soportados por AINA.
4. Que se puede conectar al servidor Whisper Live (test WebSocket).
5. Que AINA Translator responde al health check.

---

## API REST

### Sesiones

| Método | Ruta | Descripción |
|--------|------|-------------|
| `POST` | `/api/session/create` | Crea sesión de traducción (preflight checks + LiveKit + agentes) |
| `POST` | `/api/session/{id}/join` | Unirse como oyente. Devuelve token LiveKit |
| `POST` | `/api/session/{id}/source-language` | Cambiar idioma fuente |
| `DELETE` | `/api/session/{id}` | Detener sesión y limpiar recursos |
| `GET` | `/api/session/{id}/status` | Estado actual: agentes activos, idiomas |

### Configuración

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET` | `/api/livekit/status` | Verificar que LiveKit y Google TTS están configurados |
| `GET` | `/api/event/{code}/endpoints` | Obtener config Whisper/Translator del evento |
| `POST` | `/api/event/{code}/endpoints` | Guardar config Whisper/Translator del evento |

### Públicos (sin autenticación)

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET` | `/api/public/event/{code}` | Info del evento y canales (para página stage) |
| `GET` | `/api/public/event/{code}/tracks` | Tracks LiveKit activos (para servicios externos) |

### WebSocket

| Ruta | Descripción |
|------|-------------|
| `/ws/logs/{event_id}` | Stream de logs en tiempo real (terminal del ponente y monitor) |
| `/ws/captions/{event_code}/{channel}` | Stream de subtítulos por canal (página captions) |

---

## Tablas de la Base de Datos

| Tabla | Descripción |
|-------|-------------|
| `profiles` | Usuarios. Se crea automáticamente al registrarse. Campos: email, nombre, is_approved, is_admin |
| `events` | Eventos. Cada uno tiene event_code único, estado, whisper_config (JSONB) |
| `channels` | Canales de audio por evento. Un canal original (ca) + canales de traducción |
| `livekit_tracks` | Tracks de audio publicados en LiveKit. room_name, track_name, idioma, is_active |
| `event_logs` | Logs por evento con nivel, fuente y mensaje |
| `event_stats` | Estadísticas: oyentes, duración, costes |

---

## Descripción de Archivos

### Backend (Python)

| Archivo | Descripción |
|---------|-------------|
| `main.py` | Servidor FastAPI. Contiene toda la lógica: API REST, WebSockets, sesiones, conexión con LiveKit, Whisper, AINA y TTS. Clases: `TranslationSession`, `AudioPublisher`, `PaceMeter`, `WhisperAgent` |
| `asr/whisper_client.py` | Cliente WebSocket para Whisper Live. Envía audio PCM 16kHz, recibe transcripciones. Lógica de segmentos estables (FINAL) vs activo (último, siempre PARTIAL). Timeout de 2s para finalizar el último segmento. Deduplicación de emisiones |
| `translation/base.py` | Clase abstracta `BaseTranslator` con método `translate(text)` |
| `translation/aina_translator.py` | Cliente HTTP para AINA Translator. POST a `/translate`, GET a `/health`. Idiomas: en, es, fr, pt, it, de |
| `translation/placeholder.py` | `NullTranslator`: devuelve el texto original sin traducir. Se usa para el canal original sin traducción |
| `tts/google_tts.py` | Integra Google Cloud TTS. Soporta Chirp 3 HD y Neural2. Lee `TTS_PROVIDER` y `TTS_VOICE` del .env. Genera audio PCM a 24kHz. Velocidad dinámica 0.8x-1.5x |
| `utils/text_normalizer.py` | Normaliza números para TTS: `14.000` → `14000`, `36,5` → `36.5` |
| `utils/tts_buffer_catalan.py` | Acumula texto traducido y decide cuándo enviar al TTS. Espera frases completas (punto, interrogación) para mejor prosodia |

### Frontend (JavaScript)

| Archivo | Descripción |
|---------|-------------|
| `static/js/config.js` | Inicializa cliente Supabase con la URL y clave inyectadas por el servidor. Define `LANGUAGE_FLAGS` y `LANGUAGE_NAMES`. Funciones: `getFlag()`, `getLangName()` |
| `static/js/auth.js` | Autenticación completa: login, signup, logout, getProfile, requireAuth, resetPassword, updatePassword. Gestiona aprobación de usuarios |
| `static/js/db.js` | CRUD contra Supabase: eventos, canales, usuarios, rooms, tracks, logs, estadísticas |
| `static/js/livekit.js` | Integración LiveKit: createSession, connectAsBroadcaster, startPublishing, stopPublishing, getAudioDevices. Usa livekit-client@2.0.4 |

### Assets Estáticos

| Archivo | Uso |
|---------|-----|
| `static/css/styles.css` | Tema visual de toda la app |
| `static/img/bscTranslatorLogo.png` | Logo principal (login, signup, dashboard) |
| `static/img/builtByLink3rs.png` | Badge "Built by Link3rs" (footer) |
| `static/img/banderaCatEmoji.webp` | Bandera catalana (speaker, captions) |

---

## Docker

El `Dockerfile` usa `python:3.11-slim` y fija `setuptools<82` (necesario porque `openai-whisper` usa `pkg_resources`, eliminado en setuptools 82+). Se usa `PIP_CONSTRAINT` para que los entornos de build aislados de pip también respeten esta restricción.

La imagen publicada es `ghcr.io/bsc-appliedintelligence/traduia/backstage`. Para construirla en local, desde este directorio:

```bash
docker build -t traduia-backstage .
docker run -p 8000:8000 --env-file .env traduia-backstage
```

`scripts/docker_build_and_push.sh` construye (y opcionalmente publica) con buildx; se configura con las variables `IMAGE_NAME`, `DOCKERFILE_PATH`, `PLATFORMS`, `PUSH` y `LOAD`. El despliegue completo (Swarm, Supabase incluido) está descrito en [`../deploy/docs/quickstart.md`](../deploy/docs/quickstart.md).

---

## Quick Start

1. **Supabase**: Crear proyecto (o desplegar uno propio) → Copiar URL + claves → Inicializar la base de datos con `db/` (o ejecutar `db/migrations/*.sql` en orden en el SQL Editor).
2. **LiveKit**: Crear cuenta → Crear proyecto → Copiar URL + API Key + Secret.
3. **Google Cloud**: Activar TTS API → Crear cuenta de servicio → Descargar JSON.
4. **RunPod**: Desplegar Whisper Live y AINA Translator en pods con GPU.
5. **`.env`**: Rellenar todas las variables con los valores obtenidos.
6. **Instalar**: `pip install -r requirements.txt`
7. **Ejecutar**: `python main.py`
8. **Primer admin**: Registrarse con un email de `ADMIN_EMAILS` (o ejecutar `sql/A_` y `sql/B_` a mano) → Ya puedes aprobar usuarios.
9. **Crear evento**: Dashboard → New Event → Añadir canales → Configurar Endpoints → Speaker → Hablar.

---

Licencia Apache-2.0: véase `LICENSE` en la raíz del repositorio.
