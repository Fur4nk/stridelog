# StrideLog

Self-hosted web application for analyzing running workouts exported from OpenTracks (Android).

Supports GPX 1.1 and KMZ file imports with automatic parsing of distance, pace, elevation, heart rate, and cadence data.

## Quick Start

```bash
docker compose up -d
```

Open http://localhost:7842

## Features

- Drag & drop GPX/KMZ file upload (multi-file)
- Automatic deduplication via OpenTracks track ID
- Dashboard with summary cards and interactive charts
- Sortable/filterable workout table
- Dark theme UI

## Data

All data is persisted in a Docker named volume (`stridelog_data`):
- SQLite database at `/data/tracks.db`
- Uploaded files at `/data/uploads/`

## Stack

- Python Flask + SQLAlchemy
- SQLite
- Chart.js (CDN)
- Gunicorn
- Docker
