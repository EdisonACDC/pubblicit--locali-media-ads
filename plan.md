# Piano: nuova interfaccia Carellas Media Ads

Branch: `feature/ui-zone-redesign` · Mock di riferimento: [Carellas Media Ads — UI Redesign](https://claude.ai/artifact/165cSHTtH8e9j7jHXoHWHQ) (5 tavole A–E)

Questo file è la bacheca del lavoro. Ogni task ha un codice, uno stato e i criteri per considerarlo finito. Aggiorna lo stato qui quando un task cambia fase, e scrivi il tuo feedback nella sezione [Feedback](#feedback) in fondo.

## Stati

| Stato | Significato |
|---|---|
| 🔲 **Da fare** | Non ancora iniziato |
| 🔄 **In corso** | Qualcuno ci sta lavorando |
| 👀 **In revisione** | Fatto, da provare in locale e approvare |
| ✅ **Fatto** | Approvato |
| ⛔ **Bloccato** | Non può andare avanti: il motivo è scritto nel task |

Priorità: **P1** indispensabile · **P2** importante · **P3** rifinitura. Stima: **S** fino a mezza giornata · **M** 1–2 giorni · **L** 3+ giorni.

---

## 1. Obiettivo

Riorganizzare il pannello attorno ai **luoghi del ristorante** (direzione C), integrando gli altri aspetti mostrati nel mock:

| Tavola | Aspetto | Dove finisce nel pannello |
|---|---|---|
| **A · Regia, ora in onda** | Cosa succede adesso in tutto il locale | Pagina **Oggi** (nuova home) |
| **B · Palinsesto unico** | Programma settimanale di musica, spot e TV | Pagina **Palinsesto** |
| **C · Per zona e dispositivo** | Sonos e schermi di un luogo | Pagine **Zona** (Sala, Bar, Terrazza…) e menu laterale |
| **D · Configurazione guidata** | Impostare gli spot passo per passo, con frasi leggibili | **Procedura guidata** per spot, musica e schermi |
| **E · Mobile, al banco** | Uso veloce dal telefono durante il servizio | Layout **telefono** con barra in basso |

Restano invariati: colori marrone e oro, titoli Georgia, lingua IT/DE, motore Sonos e Scheduler.

## 2. Regole tecniche

- **Riutilizzare le API esistenti** del server (tabella sotto). Le nuove API sono solo quelle elencate nell'epica BE, tutte compatibili con la configurazione attuale.
- **Nessuna modifica al comportamento Sonos** (spot, gruppi, ripristino). Le zone sono un'informazione dell'interfaccia.
- Ogni testo nuovo ha la traduzione tedesca (`itDe` / `deText()` in `index.html`).
- Dopo ogni task: `python -m unittest discover -s tests`, controllo JavaScript come in CI, prova nel simulatore locale su computer e telefono.
- La vecchia interfaccia resta raggiungibile su `/classic` fino al collaudo sull'impianto (UI-014).

### API esistenti da riutilizzare

| API | Uso nella nuova interfaccia |
|---|---|
| `GET /api/state` | Stato generale, configurazione, Sonos, preferiti, libreria, registro |
| `POST /api/config` | Salvataggio (unione con la configurazione esistente) |
| `POST /api/test/audio`, `POST /api/audio/stop` | Prova e arresto spot |
| `POST /api/music/start`, `/api/music/stop`, `/api/music/refresh`, `/api/music/browse` | Musica e catalogo Sonos |
| `POST /api/test/tv`, `/api/tv/power/on`, `/api/tv/power/off` | Schermo TV LAN/DLNA |
| `GET /api/iptv/status`, `POST /api/iptv/rebuild`, `/api/iptv/power/on|off` | Canali IPTV |
| `POST /api/upload`, `/api/upload/chunk`, `DELETE /api/media/…` | Libreria contenuti |

---

## 3. Bacheca

### Epica DEV · Ambiente locale

| Codice | Task | Stato | Pri | Stima |
|---|---|---|---|---|
| DEV-001 | Simulatore Home Assistant con Sonos e TV virtuali | 👀 In revisione | P1 | M |
| DEV-002 | Istruzioni di avvio locale nel README e in AGENTS.md | 🔲 Da fare | P1 | S |

**DEV-001 · Simulatore con Sonos virtuali** — `dev/run_local.py`, `dev/simulator.html`
- Avvia il server vero con 4 Sonos virtuali (Sala, Bar, Terrazza, Bagni), 2 TV, uno script e una presa.
- Le casse rispondono a `play_media`, `join`/`unjoin`, `volume_set`, `snapshot`/`restore`, `select_source`, pausa e stop. A fine spot si fermano da sole come un vero Sonos.
- Pannello simulatore su http://127.0.0.1:8098: musica avviata "dall'app Sonos", volume, gruppi, cassa scollegata, registro dei comandi ricevuti.
- Accettazione: uno spot su Sala+Terrazza mentre Sala+Bar suonano viene riprodotto e alla fine Sala+Bar riprendono la musica con i volumi di prima. *(Verificato.)*

### Epica BE · Piccole estensioni del server

| Codice | Task | Stato | Pri | Stima |
|---|---|---|---|---|
| BE-001 | Dettagli Sonos in `/api/state` | 🔲 Da fare | P1 | S |
| BE-002 | Prossimo spot in `/api/state` | 🔲 Da fare | P1 | S |
| BE-003 | Zone nella configurazione | 🔲 Da fare | P1 | S |
| BE-004 | Prova spot su una sola zona | 🔲 Da fare | P2 | S |
| BE-005 | Servire la vecchia interfaccia su `/classic` | 🔲 Da fare | P2 | S |

- **BE-001** — Oggi `/api/state` dà solo nome e stato delle casse. Aggiungere `volume`, `group_members`, `media_title` e `source` per ogni Sonos, così Oggi e Zona mostrano cosa suona e dove. Test in `tests/test_server.py`.
- **BE-002** — Esporre in `runtime` i secondi mancanti al prossimo spot (dall'intervallo e da `Scheduler.last_audio`), se uno spot è in corso e quale sarà il prossimo nella rotazione. Serve al conto alla rovescia di Oggi e del telefono.
- **BE-003** — Nuova sezione `zones` in `default_config.json`: `[{id, name, players[], tv_entity, iptv_channels[]}]`. Alla prima apertura il pannello le crea da Sonos e canali esistenti. Lo Scheduler non le legge. Validazione: ID unici, entità esistenti.
- **BE-004** — `POST /api/test/audio` accetta `players` facoltativo per provare uno spot solo nella zona aperta, senza cambiare la configurazione.
- **BE-005** — L'`index.html` attuale copiato in `index_classic.html` e servito su `/classic`.

### Epica UI · Schermate

| Codice | Task | Tavola | Stato | Pri | Stima |
|---|---|---|---|---|---|
| UI-000 | Leggere le tavole del mock a piena risoluzione | tutte | ⛔ Bloccato | P1 | S |
| UI-001 | Struttura: menu laterale con zone, intestazione, stato motore | C | 🔲 Da fare | P1 | M |
| UI-002 | Componenti comuni (avvisi colorati, interruttore, tessere, giorni a pillola) | tutte | 🔲 Da fare | P1 | M |
| UI-003 | Barra "Modifiche non salvate" con Annulla/Salva e avviso all'uscita | tutte | 🔲 Da fare | P1 | S |
| UI-004 | Pagina **Oggi**: ora in onda, prossimo spot, giornata, zone, da sistemare, attività | A | 🔲 Da fare | P1 | L |
| UI-005 | Pagina **Zona**: musica, spot, schermo e sequenza della zona | C | 🔲 Da fare | P1 | L |
| UI-006 | Gestione zone: creare, rinominare, unire, assegnare Sonos e schermi | C | 🔲 Da fare | P1 | M |
| UI-007 | Pagina **Palinsesto**: settimana con musica, spot e TV, pannello di modifica | B | 🔲 Da fare | P2 | L |
| UI-008 | **Procedura guidata** spot: frase riepilogo, passi, ordine spot | D | 🔲 Da fare | P2 | M |
| UI-009 | Procedura guidata musica e schermo | D | 🔲 Da fare | P3 | M |
| UI-010 | **Contenuti** (ex Libreria): griglia con anteprime, filtri, "Usato in" | E/C | 🔲 Da fare | P2 | M |
| UI-011 | **Impostazioni**: indirizzo locale, server IPTV, Player TV e utenti, DLNA | — | 🔲 Da fare | P2 | M |
| UI-012 | **Registro** per giorno e per tipo, con filtri | A | 🔲 Da fare | P3 | S |
| UI-013 | **Telefono**: barra in basso, In onda, Contenuti, TV di zona | E | 🔲 Da fare | P1 | M |
| UI-014 | Collaudo finale su simulatore e poi sull'impianto reale | tutte | 🔲 Da fare | P1 | M |

**UI-000 · Bloccato** — Le tavole del mock si caricano solo dentro la tela di claude.ai e non sono leggibili come testo. Per sbloccarlo serve una di queste: il riquadro browser dell'app visibile (Ctrl+Shift+B) per ingrandire le tavole, oppure l'esportazione di ogni tavola (icona ⬇ accanto al titolo) salvata in `docs/mock/`. Finché non è risolto, i task UI seguono le miniature e la direzione C.

**UI-001 · Struttura** — Menu laterale: Oggi, Palinsesto, *Zone* (una voce per zona, con pallino di stato), Contenuti, Registro, Impostazioni. Sul telefono diventa la barra in basso (UI-013). Intestazione con selettore IT | DE a due pulsanti e "Motore attivo". Navigazione con ancore (`#zona-sala`) così il tasto Indietro funziona.

**UI-002 · Componenti comuni** — Avvisi in quattro varianti (info, ok, attenzione, errore). Interruttore principale ATTIVA/SPENTA. Altoparlanti come tessere con stato e volume, più "Tutti"/"Nessuno". Giorni come pillole con le scorciatoie "Lun–Ven" e "Tutti". Pulsanti di eliminazione con conferma nella pagina (niente `confirm()`).

**UI-004 · Oggi** — Card "In onda": playlist in corso e conto alla rovescia del prossimo spot (BE-002). Riga di numeri: spot oggi rispetto al massimo, stato musica, canali TV in onda, prossimo evento. Linea della giornata per zona. Elenco zone con stato, "Da sistemare" (casse non raggiungibili, canali da rigenerare, fasce fuori orario) e ultime attività. Comandi rapidi: prova spot, ferma, avvia/ferma musica.

**UI-005 · Zona** — Intestazione con nome zona e interruttori. Riquadro Musica: cosa suona, volume, fasce che includono la zona, avvia/ferma. Riquadro Spot: "Spot in questa zona" (aggiunge o toglie le casse da `audio.players`), frequenza in sola lettura con link al Palinsesto, prova spot in zona (BE-004). Riquadro Schermo: TV o canale IPTV, accensione e spegnimento, sequenza con anteprime e link Player TV.

**UI-007 · Palinsesto** — Griglia lunedì–domenica × 24 ore, fasce generali tratteggiate, blocchi per playlist e radio, segnale rosso per i blocchi fuori orario. Pannello laterale per modificare il blocco scelto con i campi di `music.slots`. Filtro per zona.

**UI-008 · Procedura guidata spot** — Passi: dove, quando, quali spot, volume, riepilogo. Frase sempre visibile, ad esempio "Riproduci uno spot ogni 30 minuti in Sala e Terrazza, dalle 10:00 alle 23:00, dal lunedì al venerdì, al massimo 20 volte al giorno". Scrive gli stessi campi di `audio.*`.

**UI-013 · Telefono** — Sotto 700 px: barra in basso (Oggi, Zone, Contenuti, Altro), schede compatte, pulsanti grandi, barra di salvataggio sopra la navigazione. Da provare con il simulatore aperto dal telefono (`--host 0.0.0.0`).

**UI-014 · Collaudo** — Ripetere i casi di `docs/COLLAUDO_CARELLAS_2026-10-09.md` sul simulatore, poi sull'impianto. Rimuovere `/classic` solo dopo l'approvazione.

### Epica QA · Qualità

| Codice | Task | Stato | Pri | Stima |
|---|---|---|---|---|
| QA-001 | Test per BE-001…BE-005 | 🔲 Da fare | P1 | S |
| QA-002 | Accessibilità: focus visibile, etichette, contrasto, `aria-current` | 🔲 Da fare | P2 | S |
| QA-003 | Traduzioni tedesche complete di tutte le nuove schermate | 🔲 Da fare | P1 | S |

---

## 4. Miglioramenti proposti oltre al mock

1. **Prova spot per zona** (BE-004): oggi la prova usa sempre tutte le casse configurate.
2. **Stato Sonos completo nell'API** (BE-001): permette di mostrare volume e gruppi senza aprire Home Assistant.
3. **Conto alla rovescia del prossimo spot** (BE-002): oggi non si sa quando partirà.
4. **Durata spot senza ffprobe**: in locale il simulatore la stima dall'MP3. Si potrebbe usare la stessa stima nel server come ripiego, invece di bloccare lo spot.
5. **Frequenza spot diversa per zona**: fuori da questo piano perché tocca lo Scheduler. Da valutare dopo il collaudo.
6. **Dividere `index.html`** in HTML, CSS e JS separati: renderebbe il redesign più semplice da mantenere. Oggi AGENTS.md lo vieta senza richiesta esplicita, quindi resta una proposta da decidere.

## 5. Esecuzione in locale

```bash
py dev/run_local.py
```

Su macOS o Linux: `python3 dev/run_local.py`. Opzioni: `--reset` riparte da zero, `--host 0.0.0.0` permette di aprirlo dal telefono sulla stessa rete Wi-Fi.

| Indirizzo | Cosa |
|---|---|
| http://127.0.0.1:8099 | Pannello Carellas Media Ads |
| http://127.0.0.1:8101 | Player TV via browser |
| http://127.0.0.1:8098 | Simulatore: casse e TV virtuali, comandi ricevuti |

Senza ffmpeg gli spot audio funzionano (durata stimata) ma i canali IPTV non si possono generare. Per averli: `winget install Gyan.FFmpeg`, poi riapri il terminale.

## Feedback

Scrivi qui sotto, indicando il codice del task (es. `UI-004: il conto alla rovescia è troppo piccolo`).

-
