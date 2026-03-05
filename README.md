# StrideLog

Self-hosted web application for analyzing running workouts exported from OpenTracks (Android).

Supports GPX 1.1, KML 2.3, and KMZ file imports with automatic parsing of distance, pace, elevation, heart rate, and cadence data.

## Quick Start

```bash
docker compose up -d
```

Open http://localhost:7842

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
