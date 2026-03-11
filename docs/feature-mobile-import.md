# Feature: Import facile da OpenTracks (mobile)

## Problema
Servono troppi passaggi manuali per importare le tracce da OpenTracks (Android) a StrideLog.

## Opzioni valutate

### 1. PWA Web Share Target
OpenTracks → Condividi → StrideLog appare nel menu Android → upload automatico.

- Pro: nativo, un tap, zero app extra
- Con: non funziona su Firefox Android (solo Chromium e Safari iOS 15+)
- **Scartata** perché l'utente usa Firefox Android

### 2. Cartella watch + Syncthing (scelta)
StrideLog monitora `/data/import/`. Syncthing sincronizza la cartella export di OpenTracks dal telefono al server. I file vengono importati automaticamente e spostati in `/data/import/done/`.

- Pro: completamente automatico dopo il setup iniziale
- Con: richiede Syncthing su telefono e server
- Setup una tantum: configurare Syncthing su entrambi i lati

### 3. Upload manuale da browser mobile
Già funzionante: OpenTracks → Esporta → apri StrideLog in Firefox → file picker.

- Pro: niente da sviluppare
- Con: 2-3 passaggi manuali ogni volta

### 4. Cartella watch generica (senza Syncthing)
Stessa cartella watch, ma i file vengono trasferiti con scp, rsync, share di rete, ecc.

- Pro: flessibile
- Con: serve comunque un metodo di trasferimento

## Decisione
Implementare la **cartella watch** (`/data/import/`):
- StrideLog controlla periodicamente la cartella per nuovi file GPX/KML/KMZ
- Importa automaticamente i file trovati
- Sposta i file importati in `/data/import/done/`
- L'utente usa Syncthing per sincronizzare la cartella export di OpenTracks con `/data/import/`

## Stato
Da implementare.
