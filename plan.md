# Piano: nuova interfaccia Carellas Media Ads

Branch: `feature/ui-zone-redesign` · Mock di riferimento: [Carellas Media Ads — UI Redesign](https://claude.ai/artifact/165cSHTtH8e9j7jHXoHWHQ) (5 tavole A–E, sorgenti salvati in `docs/mock/project/`)

Questo file è la bacheca del lavoro. Ogni task ha un codice, uno stato e i criteri per considerarlo finito. Aggiorna lo stato qui quando un task cambia fase e scrivi il tuo feedback nella sezione [Feedback](#feedback) in fondo.

## Stati

| Stato | Significato |
|---|---|
| 🔲 **Da fare** | Non ancora iniziato |
| 🔄 **In corso** | Qualcuno ci sta lavorando |
| 👀 **In revisione** | Fatto e provato nel simulatore: da provare tu e approvare |
| ✅ **Fatto** | Approvato |
| ⛔ **Bloccato** | Non può andare avanti: il motivo è scritto nel task |

Priorità: **P1** indispensabile · **P2** importante · **P3** rifinitura. Stima: **S** fino a mezza giornata · **M** 1–2 giorni · **L** 3+ giorni.

---

## 1. Obiettivo

Riorganizzare il pannello attorno ai **luoghi del ristorante** (direzione C), integrando gli altri aspetti del mock:

| Tavola | Aspetto | Dove si trova ora | File sorgente del mock |
|---|---|---|---|
| **A · Regia, ora in onda** | Cosa succede adesso in tutto il locale | Pagina **Oggi** (home) | `Main.dc.html` |
| **B · Palinsesto unico** | Programma settimanale di musica, spot e TV | Pagina **Palinsesto** | `Palinsesto.dc.html` |
| **C · Per zona e dispositivo** | Sonos e schermi di un luogo | Menu laterale + pagine **Zona** | `Zone.dc.html` |
| **D · Configurazione guidata** | Impostare tutto passo per passo, con frasi leggibili | **Configurazione guidata** (6 passi) | `Guidata.dc.html` |
| **E · Mobile, al banco** | Uso veloce dal telefono durante il servizio | Layout **telefono** (sotto 820 px) | `Mobile.dc.html` |

### Scelte rispetto al mock

- **Una sola palette per tutto il pannello**: quella della tavola A (marrone scuro e oro, titoli Fraunces, testi IBM Plex). Le tavole B, C e D usano colori diversi perché erano direzioni alternative: qui ne ho preso la struttura, non i colori.
- **Palinsesto senza frecce tra le settimane**: il programma si ripete uguale ogni settimana, quindi le frecce avrebbero mostrato sempre lo stesso contenuto. La pagina dice "Settimana tipo".
- **"Uno spot" nella frase della procedura guidata non è modificabile**: la versione stabile trasmette sempre uno spot per intervallo.
- **Zone proposte in automatico**: se non ne hai ancora, il pannello ne propone una per ogni Sonos (e una "Schermi" per i canali TV senza zona). Diventano definitive con "Conferma queste zone" o al primo salvataggio di una zona.

## 2. Regole tecniche

- Riutilizzare le API esistenti. Quelle nuove (epica BE) sono compatibili con la configurazione attuale.
- **Nessuna modifica al comportamento Sonos** (spot, gruppi, ripristino). Le zone sono un'informazione dell'interfaccia.
- Ogni testo nuovo ha la traduzione tedesca (mappa `DE` in fondo a `index.html`).
- Dopo ogni task: `python -m unittest discover -s tests`, controllo del JavaScript come in CI, prova nel simulatore su computer e telefono.
- L'interfaccia precedente resta su `/classic` (`index_classic.html`) fino al collaudo sull'impianto (UI-014).

### API usate dalla nuova interfaccia

| API | Uso | Nuova? |
|---|---|---|
| `GET /api/state` | Stato, configurazione, Sonos, preferiti, libreria, registro | estesa (BE-001, BE-002) |
| `POST /api/config` | Salvataggio della bozza | — |
| `POST /api/test/audio` | "Riproduci ora", prova spot in una zona | estesa (BE-004) |
| `POST /api/audio/stop`, `/api/audio/skip`, `/api/audio/pause` | Ferma, salta, pausa pubblicità | skip e pause nuove (BE-006) |
| `POST /api/sonos/volume`, `/api/sonos/refresh` | Volume dal pannello, "Riprova collegamento" | nuove (BE-007) |
| `POST /api/music/start`, `/api/music/stop` | Musica del palinsesto, prova di una fascia, stop di una zona | estese (BE-008) |
| `POST /api/music/refresh` | "Aggiorna da Sonos" | — |
| `POST /api/tv/power/*`, `/api/iptv/power/*`, `/api/test/tv` | Accensione e prova schermi | — |
| `GET /api/iptv/status`, `POST /api/iptv/rebuild` | "Genera e invia alla TV" | — |
| `POST /api/upload`, `/api/upload/chunk`, `DELETE /api/media/…` | Contenuti | — |

---

## 3. Bacheca

### Epica DEV · Ambiente locale

| Codice | Task | Stato | Pri | Stima |
|---|---|---|---|---|
| DEV-001 | Simulatore Home Assistant con Sonos e TV virtuali | 👀 In revisione | P1 | M |
| DEV-002 | Istruzioni di avvio locale (questo file, README, AGENTS.md) | 👀 In revisione | P1 | S |
| DEV-003 | Dati di esempio al primo avvio del simulatore | 👀 In revisione | P2 | S |

- **DEV-001** — `dev/run_local.py` avvia il server vero con 4 Sonos virtuali (Sala, Bar, Terrazza, Bagni), 2 TV, uno script e una presa. Le casse rispondono a `play_media`, `join`/`unjoin`, `volume_set`, `snapshot`/`restore`, `select_source`, pausa e stop, e a fine spot si fermano da sole come un vero Sonos. Il pannello del simulatore (http://127.0.0.1:8098) permette di far partire musica "dall'app Sonos", cambiare volume e gruppi, scollegare una cassa e vedere ogni comando ricevuto. *Verificato: spot su Sala+Terrazza mentre Sala+Bar suonano, poi ripristino della musica e dei volumi.*
- **DEV-003** — Al primo avvio (o con `--reset`) carica zone, orari, tre fasce musicali, due spot, il canale "TV Sala" e l'utente TV `tv-sala` / `carellas`. Con `--empty` parte vuoto.

### Epica BE · Estensioni del server

| Codice | Task | Stato | Pri | Stima |
|---|---|---|---|---|
| BE-001 | Dettagli Sonos in `/api/state` (volume, gruppo, titolo, offline) | 👀 In revisione | P1 | S |
| BE-002 | Prossimo spot, conto alla rovescia e motivo dell'attesa in `/api/state` | 👀 In revisione | P1 | S |
| BE-003 | Zone nella configurazione, con validazione | 👀 In revisione | P1 | S |
| BE-004 | Prova spot solo sui Sonos di una zona | 👀 In revisione | P2 | S |
| BE-005 | Interfaccia precedente su `/classic` | 👀 In revisione | P2 | S |
| BE-006 | Pausa pubblicità (30 min, 1 ora, fino a fine giornata) e "Salta questo" | 👀 In revisione | P1 | S |
| BE-007 | Volume di un Sonos e "Riprova collegamento" | 👀 In revisione | P2 | S |
| BE-008 | Prova di una sola fascia; stop della musica di una zona o di tutte le fasce | 👀 In revisione | P2 | S |
| BE-009 | Errori delle prove spot scritti nel Registro; `favicon.ico` senza errori | 👀 In revisione | P3 | S |

Note:
- **BE-006**: la pausa resta in memoria. Se l'add-on si riavvia, la pubblicità riprende.
- **BE-008**: prima `/api/music/stop` fermava solo `music.players`, che con le fasce è spesso vuoto. Ora "Ferma tutto" ferma anche i Sonos delle fasce.
- **BE-009**: prima l'errore di una prova manuale ("Riproduci ora") si perdeva in silenzio.

### Epica UI · Schermate

| Codice | Task | Tavola | Stato | Pri | Stima |
|---|---|---|---|---|---|
| UI-000 | Leggere le tavole del mock | tutte | ✅ Fatto | P1 | S |
| UI-001 | Struttura: menu laterale con zone, barra superiore, lingua IT/DE | C | 👀 In revisione | P1 | M |
| UI-002 | Componenti comuni: interruttori, chip, avvisi colorati, conferme nella pagina | tutte | 👀 In revisione | P1 | M |
| UI-003 | Barra "Modifiche non salvate" con Annulla/Salva e avviso all'uscita | tutte | 👀 In revisione | P1 | S |
| UI-004 | Pagina **Oggi** | A | 👀 In revisione | P1 | L |
| UI-005 | Pagina **Zona** con musica, spot, schermo, sequenza e impostazioni avanzate | C | 👀 In revisione | P1 | L |
| UI-006 | **Tutte le zone**: creare, rinominare, assegnare Sonos e TV, eliminare | C | 👀 In revisione | P1 | M |
| UI-007 | Pagina **Palinsesto** con editor dei blocchi, orari di apertura e regola spot | B | 👀 In revisione | P2 | L |
| UI-008 | **Configurazione guidata**, passo 3 "Pubblicità audio" con frase e anteprima di oggi | D | 👀 In revisione | P2 | M |
| UI-009 | Configurazione guidata, passi 1, 2, 4, 5, 6 | D | 👀 In revisione | P3 | M |
| UI-010 | **Contenuti**: caricamento a trascinamento, anteprime, filtri, "In uso", conferma eliminazione | E | 👀 In revisione | P2 | M |
| UI-011 | **Impostazioni**: indirizzo locale, canali TV, Player TV e utenti, schermo principale | — | 👀 In revisione | P2 | M |
| UI-012 | **Registro** per giorno e per tipo | A | 👀 In revisione | P3 | S |
| UI-013 | **Telefono**: barra in basso, pausa pubblicità, volumi, sequenza verticale | E | 👀 In revisione | P1 | M |
| UI-014 | Collaudo sull'impianto reale e rimozione di `/classic` | tutte | 🔲 Da fare | P1 | M |

Dettagli e criteri di accettazione:

- **UI-004 · Oggi** — Musica in corso con avanzamento della fascia (anche se avviata dall'app Sonos fuori dal palinsesto). Prossimo spot con conto alla rovescia aggiornato senza ridisegnare la pagina, "Riproduci ora", "Salta questo", "Ferma lo spot". Riquadri: spot oggi su massimo, musica, canali in onda, prossimo cambio. Linea della giornata (musica, spot, TV, ora attuale). Zone con stato e volume. "Da sistemare": Sonos offline, sequenze da inviare, accesso Player TV mancante, spot non scelti, fasce fuori orario, indirizzo locale non numerico.
- **UI-005 · Zona** — Interruttore Musica (aggiunge o toglie la zona dalle fasce), Pubblicità audio (aggiunge o toglie i Sonos da `audio.players`), Schermo (attiva il canale). Sequenza con anteprime, frecce e trascinamento, modifica di durata, effetto, formato e collage. "Genera e invia alla TV" salva e rigenera il canale, poi segue lo stato fino a pronto o errore.
- **UI-007 · Palinsesto** — Settimana o giorno, filtro per zona, blocchi fuori orario a strisce rosse. Editor laterale: sorgente con ricerca (doppioni uniti), altoparlanti, orari, giorni, volume, dissolvenza, "Prova ora", "Elimina" con conferma.
- **UI-013 · Telefono** — Verificato a 375 px: nessuno scorrimento orizzontale su tutte le pagine, barra in basso Oggi · Palinsesto · Contenuti · Altro, pulsante "Carica" fluttuante.

### Epica QA · Qualità

| Codice | Task | Stato | Pri | Stima |
|---|---|---|---|---|
| QA-001 | Test per le estensioni del server e per la nuova interfaccia (114 test) | 👀 In revisione | P1 | S |
| QA-002 | Accessibilità: focus visibile, etichette, `aria-current`, interruttori `role="switch"` | 🔄 In corso | P2 | S |
| QA-003 | Traduzioni tedesche di tutte le nuove schermate (448 testi, 0 mancanti) | 👀 In revisione | P1 | S |
| QA-004 | CI: controllo del JavaScript sia della nuova interfaccia sia di `/classic` | 👀 In revisione | P2 | S |

QA-002 resta aperto: va ancora provato con lettore di schermo e solo tastiera.

---

## 4. Miglioramenti proposti (non ancora fatti)

1. **Frequenza spot diversa per zona**: oggi intervallo, volume e rotazione sono uguali per tutto il locale. Richiede modifiche allo Scheduler.
2. **Pausa pubblicità salvata su disco**, così sopravvive a un riavvio dell'add-on.
3. **Durata spot senza ffprobe anche nel server**, come fa il simulatore, invece di bloccare lo spot.
4. **Anteprima "Ecco come andrà oggi" anche nella pagina Zona**.
5. **Dividere `index.html`** in HTML, CSS e JS: la nuova interfaccia è circa 180 KB in un solo file. AGENTS.md oggi lo sconsiglia, quindi è una decisione da prendere insieme.
6. **Caratteri offline**: Fraunces e IBM Plex arrivano da Google Fonts. Senza Internet il pannello usa Georgia e i caratteri di sistema; si possono includere nell'add-on.

## 5. Esecuzione in locale

```bash
py dev/run_local.py
```

Su macOS o Linux: `python3 dev/run_local.py`.

| Indirizzo | Cosa |
|---|---|
| http://127.0.0.1:8099 | Pannello nuovo |
| http://127.0.0.1:8099/classic | Pannello precedente |
| http://127.0.0.1:8101 | Player TV (utente di esempio `tv-sala`, password `carellas`) |
| http://127.0.0.1:8098 | Simulatore: casse e TV virtuali, comandi ricevuti |

Opzioni: `--reset` riparte da zero con i dati di esempio, `--empty` (insieme a `--reset`) parte vuoto, `--host 0.0.0.0` permette di aprirlo dal telefono sulla stessa rete Wi-Fi (`http://IP-DEL-PC:8099`).

Senza FFmpeg gli spot audio funzionano (durata stimata) ma i canali TV non si possono generare. Per averli: `winget install Gyan.FFmpeg`, poi riapri il terminale.

## Feedback

Scrivi qui sotto indicando il codice del task, ad esempio `UI-004: il conto alla rovescia è troppo piccolo`.

-
