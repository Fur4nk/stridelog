# Documentazione tecnica

## Stack

| Componente | Tecnologia |
|------------|-----------|
| Backend | Python Flask + Flask-SQLAlchemy |
| Database | SQLite (`/data/tracks.db`) |
| Autenticazione | Flask-Login + Authlib (OIDC) |
| Server | Gunicorn (2 worker) |
| Grafici | Chart.js 4.5.0 (CDN) |
| Mappe | Leaflet.js 1.9.4 + Carto tiles (CDN) |
| Frontend | Vanilla JavaScript, CSS custom properties |
| Container | Docker multi-stage (Python 3.12-alpine) |

## Configurazione

Variabili d'ambiente (file `.env`):

| Variabile | Default | Descrizione |
|-----------|---------|-------------|
| `DATA_DIR` | `/data` | Directory root per database e upload |
| `PASSWORD_LOGIN_ENABLED` | `true` | Abilita/disabilita login con password |
| `OIDC_PROVIDER_URL` | - | URL del provider OpenID Connect |
| `OIDC_CLIENT_ID` | - | Client ID per OIDC |
| `OIDC_CLIENT_SECRET` | - | Client secret per OIDC |
| `WEATHER_PROVIDER` | `open-meteo` | Provider dati meteo |
| `WEATHER_HTTP_TIMEOUT` | `8` | Timeout API meteo in secondi |

Per disabilitare il login con password impostare `PASSWORD_LOGIN_ENABLED=false`.
Usare solo quando un altro metodo di login è configurato (tipicamente OIDC).

## Docker

Build multi-stage con immagine Alpine. Il container gira come utente non-root `appuser` (UID 1000).

```bash
cp .env.example .env
docker compose up -d
```

La porta interna 5000 viene esposta sulla 7842. I dati persistono nel volume Docker `stridelog_data`.

## Schema database

### Users

| Campo | Tipo | Note |
|-------|------|------|
| `id` | PK | |
| `email` | String | Unico, indicizzato |
| `password_hash` | String | Nullable (utenti solo OIDC) |
| `oidc_provider` | String | URL provider OIDC |
| `oidc_sub` | String | Subject OIDC |
| `is_admin` | Boolean | |
| `created_at` | String | Timestamp ISO |

### Tracks

| Campo | Tipo | Note |
|-------|------|------|
| `id` | PK | |
| `user_id` | FK → Users | |
| `track_id` | String | ID OpenTracks per deduplicazione |
| `filename`, `name` | String | |
| `activity_type` | String | running, trail_running, cycling, walking, hiking |
| `date` | String | |
| `duration_s`, `moving_time_s` | Float | Durata totale e in movimento |
| `distance_m` | Float | |
| `avg_speed_ms`, `max_speed_ms` | Float | |
| `elevation_gain_m`, `elevation_loss_m` | Float | |
| `avg_hr`, `max_hr` | Integer | |
| `avg_cadence` | Float | |
| `calories` | Float | |
| `start_lat`, `start_lon` | Float | |
| `started_at`, `ended_at` | String | |
| `coordinates` | JSON | Array coordinate per mappa |
| `trackpoints` | JSON | Dati completi per punto |
| `tags`, `notes` | String | |
| `extra_data` | JSON | Metadati contesto (superficie, sforzo, ecc.) |
| `weather_json` | JSON | Meteo cached |
| `weather_provider` | String | |
| `weather_fetched_at` | String | |

Il sistema di migrazione aggiorna automaticamente lo schema all'avvio.

## API

### Autenticazione

| Metodo | Endpoint | Descrizione |
|--------|----------|-------------|
| POST | `/login` | Login con password |
| GET | `/auth/oidc` | Avvia flusso OIDC |
| GET | `/auth/callback` | Callback OIDC |
| GET | `/logout` | Logout |

### Tracce

| Metodo | Endpoint | Descrizione |
|--------|----------|-------------|
| POST | `/upload` | Import file KMZ |
| POST | `/api/track/manual` | Crea attivita manuale |
| PUT | `/api/track/<id>` | Aggiorna metadati traccia |
| PUT | `/api/tracks/bulk-metadata` | Aggiornamento metadati in blocco |
| POST | `/delete/<id>` | Elimina traccia |

### Dati e statistiche

| Metodo | Endpoint | Descrizione |
|--------|----------|-------------|
| GET | `/api/stats` | Statistiche per dashboard |
| GET | `/api/geo` | Coordinate per mappa globale |
| GET | `/api/track/<id>` | Dettaglio traccia con split |
| GET | `/api/track/<id>/weather` | Meteo della traccia |
| GET | `/api/records` | Record personali per sport |

### Export e backup

| Metodo | Endpoint | Descrizione |
|--------|----------|-------------|
| GET | `/api/export/csv` | Export CSV |
| GET | `/api/export/json` | Export JSON |
| GET | `/api/backup` | Download backup database (admin) |
| POST | `/api/restore` | Ripristino da backup (admin) |

### Admin

| Metodo | Endpoint | Descrizione |
|--------|----------|-------------|
| POST | `/admin/users` | Crea utente |
| POST | `/admin/users/<id>/delete` | Elimina utente |
| POST | `/admin/orphan-tracks` | Assegna tracce orfane |
| POST | `/admin/orphan-tracks/delete` | Elimina tracce orfane |

## Parsing e calcoli

- **KMZ**: decompressione ZIP e parsing KML interno (`<Placemark>` con `<Track>` e array coordinate)
- **Distanza**: calcolo Haversine tra punti GPS
- **Meteo**: interpolazione lineare su dati orari Open-Meteo Archive API; selezione nearest-hour per codici meteo; calcolo precipitazioni totali sulla durata dell'attivita

## Sicurezza

- Cookie di sessione HTTPONLY con SameSite=Lax
- Hash password con Werkzeug
- Validazione formato file KMZ in upload (limite 50 MB)
- Filtraggio query per `user_id` (isolamento multi-utente)
- Decoratori di protezione admin
- Validazione redirect per prevenire open redirect
- ProxyFix per header X-Proto/X-Host (reverse proxy)
