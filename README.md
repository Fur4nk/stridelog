# StrideLog

Applicazione web self-hosted per analizzare allenamenti di corsa esportati da OpenTracks (Android).

Supporta import di file KMZ con parsing automatico di distanza, passo, dislivello, frequenza cardiaca e cadenza.

## Quick Start

```bash
cp .env.example .env
# modifica .env per configurare OIDC o disabilitare il login con password
docker compose up -d
```

Apri http://localhost:7842

## Funzionalita

### Import e gestione tracce
- Drag & drop di file KMZ (multi-file)
- Deduplicazione automatica tramite track ID di OpenTracks
- Creazione manuale di attivita senza dati GPS
- Tipi di attivita supportati: running, trail running, cycling, walking, hiking

### Dashboard e analisi
- Card riepilogative (distanza totale, durata, numero attivita)
- Grafici interattivi: trend distanza, istogramma passo, km settimanali, frequenza cardiaca, dislivello, distribuzione sforzo, scatter plot passo
- Heatmap calendario con intensita attivita
- Tabella ordinabile e filtrabile con colorazione passo (verde/giallo/rosso)
- Obiettivi di distanza e durata con barre di progresso

### Mappe
- Vista globale con tutte le tracce sovrapposte (Leaflet.js + Carto tiles)
- Mappa dettagliata per singola traccia
- Confronto side-by-side tra piu tracce

### Analisi dettagliata
- Split personalizzabili (100m - 10km) con passo, tempo cumulativo, HR e dislivello per split
- Record personali per sport (1km, 5km, 10km, mezza maratona, maratona)
- Tempo in movimento separato dal tempo totale
- Statistiche complete: velocita media/max, calorie, cadenza

### Meteo
- Dati meteo storici automatici da Open-Meteo (gratuito, senza API key)
- Temperatura, umidita, vento, copertura nuvolosa, precipitazioni
- Fetch automatico all'import; cache locale nel database
- Grafico meteo in dashboard

### Metadati e tag
- Modifica nome, tipo attivita, tag e note per ogni traccia
- Metadati contestuali per tipo di attivita (superficie, tipo di sessione, tecnicita, fango, sforzo percepito)
- Modifica metadati in blocco su piu tracce

### Export dati
- Export CSV e JSON di tutte le statistiche
- Backup e ripristino database SQLite (admin)

### Autenticazione e multi-utente
- Login con password (disabilitabile)
- Login OIDC (OpenID Connect)
- Il primo utente diventa admin
- Isolamento dati per utente

### Pannello admin
- Gestione utenti (creazione, eliminazione, promozione admin)
- Gestione tracce orfane (assegnazione o eliminazione)
- Backup e ripristino database

### PWA
- Installabile su mobile e desktop
- Service worker per funzionamento offline
- Share target nativo Android (condividi file KMZ direttamente da OpenTracks)
- Tema chiaro/scuro (salvato in localStorage)

## Dati

Tutti i dati persistono in un volume Docker (`stridelog_data`):
- Database SQLite: `/data/tracks.db`
- File caricati: `/data/uploads/`

## Documentazione

- [Documentazione tecnica](docs/TECHNICAL.md) — stack, configurazione, schema database, API, sicurezza

## Licenza

[GPLv3](LICENSE)
