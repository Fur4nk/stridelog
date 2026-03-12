# StrideLog

Self-hosted web application for analyzing running workouts exported from OpenTracks (Android).

Supports GPX 1.1, KML 2.3, and KMZ file imports with automatic parsing of distance, pace, elevation, heart rate, and cadence data.

## Quick Start

```bash
cp .env.example .env
# edit .env to enable OIDC login or disable password login
docker compose up -d
```

Open http://localhost:7842

Set `PASSWORD_LOGIN_ENABLED=false` in `.env` to hide the password form and disable `POST /login`.
Use this only when another login method is configured, typically OIDC via `OIDC_PROVIDER_URL`, `OIDC_CLIENT_ID`, and `OIDC_CLIENT_SECRET`.
After changing `.env`, restart the app or recreate the container so the new settings are loaded.

Run details can also fetch historical weather near the workout start time.
The default provider is `WEATHER_PROVIDER=open-meteo`, which is free and does not need an API key.
StrideLog stores run weather on first lookup and then serves it from the local database cache.

## Features

- Drag & drop GPX/KML/KMZ file upload (multi-file)
- Automatic deduplication via OpenTracks track ID
- Dashboard with summary cards and interactive charts (distance, pace, weekly km, HR, cumulative)
- Map view with all runs overlaid (Leaflet.js with Carto tiles)
- Sortable/filterable workout table with pace color coding
- Dark/light theme toggle (persisted in localStorage)
- PWA support (installable on mobile and desktop)

## Data

All data is persisted in a Docker named volume (`stridelog_data`):
- SQLite database at `/data/tracks.db`
- Uploaded files at `/data/uploads/`

## Stack

- Python Flask + SQLAlchemy
- SQLite
- Chart.js + Leaflet.js (CDN)
- Gunicorn
- Docker
