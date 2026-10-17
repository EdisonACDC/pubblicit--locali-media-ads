# AGENTS.md

Istruzioni per agenti AI (Claude Code, Codex, Copilot, ecc.) che lavorano su questa repository.

## Progetto

**Carellas Media Ads** è un add-on per **Home Assistant** usato nel ristorante Carellas:

- spot audio pubblicitari a rotazione sui **Sonos**, con ripristino della musica, dei gruppi e dei volumi;
- programmazione di playlist e radio Sonos per fasce orarie;
- canali video **IPTV/HLS** multi-TV, Smart TV via **DLNA**, Wake-on-LAN e un **Player TV** nel browser protetto da login.

La repository è essa stessa un repository di add-on Home Assistant (`repository.yaml` nella radice).

## Struttura

| Percorso | Contenuto |
|---|---|
| `carellas_media_ads/` | Add-on **stabile** (`stage: stable`). È quello in produzione. |
| `carellas_media_ads/app/server.py` | Tutto il backend: server HTTP, Scheduler, integrazione HA/Sonos, `IPTVEngine` (FFmpeg). Un solo file Python. |
| `carellas_media_ads/app/index.html` | Pannello amministrativo "per zona" (Oggi, Zone, Palinsesto, Configurazione guidata, Contenuti, Registro, Impostazioni): HTML, CSS e JavaScript inline in un unico file. |
| `carellas_media_ads/app/index_classic.html` | Pannello precedente, servito su `/classic` fino al collaudo della nuova interfaccia. |
| `carellas_media_ads/app/player.html`, `screen.html` | Player TV nel browser e schermata Smart TV. |
| `carellas_media_ads/app/default_config.json` | Configurazione iniziale per le nuove installazioni. |
| `carellas_media_ads/app/bundled_media/` | Media demo. Gli MP3 sono divisi in `*.part.NNN` e ricomposti dal `Dockerfile`. |
| `carellas_media_ads_beta/` | Add-on **beta** (`stage: experimental`), separato, porta 8100. |
| `tests/test_server.py`, `tests/test_beta_server.py` | Test `unittest` per stabile e beta. |
| `docs/` | Note di collaudo, mockup e sorgenti del mock di riferimento (`docs/mock/`). |
| `dev/run_local.py`, `dev/simulator.html` | Avvio in locale con Home Assistant e Sonos **virtuali** (solo sviluppo, non va nell'add-on). |
| `plan.md` | Bacheca dei task del redesign, con stati e feedback. |
| `.github/workflows/` | CI (`test.yml`) e workflow di generazione demo e pubblicazione beta. |

### Porte (stabile)

- `8099`: pannello amministrativo, API e contenuti locali. Usato anche da Ingress. **Solo LAN o Ingress.**
- `8101`: Player TV protetto da login. È l'unica porta che può essere esposta su Internet.

## Comandi

Servono Python 3.13+ (la CI usa 3.13) e Node (solo per il controllo della sintassi JS). Il backend usa solo la libreria standard. `websocket-client` è opzionale in locale ed è installato nell'immagine Docker.

```bash
# Test (stessi della CI)
python -m unittest discover -s tests -v

# Un singolo test
python -m unittest tests.test_server.CarellasServerTest.test_state_and_ingress_path

# Compilazione del backend
python -m py_compile carellas_media_ads/app/server.py

# Pannello in locale con Sonos e TV virtuali (http://127.0.0.1:8099, simulatore su :8098)
python dev/run_local.py            # su Windows: py dev/run_local.py
python dev/run_local.py --reset    # riparte da zero con i dati di esempio
```

Usa il simulatore per provare ogni modifica all'interfaccia o agli spot: il pannello su `:8098` mostra ogni comando che l'add-on manda a Home Assistant e permette di simulare musica avviata dall'app Sonos, casse scollegate e gruppi.

La CI controlla anche la sintassi del JavaScript inline di `index.html` e `index_classic.html`: estrae i blocchi `<script>` ed esegue `node --check`. Dopo aver modificato `index.html`, verifica che lo script resti valido.

I test caricano `server.py` tramite `importlib`, usano directory temporanee con `CARELLAS_DATA_DIR` / `CARELLAS_MEDIA_DIR` e sostituiscono `app.ha.states`, `app.ha.config` e `app.ha.service` con dei finti. Non serve un Home Assistant reale. I nuovi test devono seguire lo stesso schema.

## Convenzioni

- **Lingua:** codice in inglese; log, messaggi utente, commenti, commit e README in **italiano**.
- **Traduzioni IT/DE:** il pannello è scritto in italiano; ogni testo passa da `L('…')` / `Lf('…', valori)` e la traduzione tedesca sta nella mappa `DE` in fondo a `index.html`. Ogni nuovo testo visibile deve avere la sua voce in `DE`. (Il pannello `/classic` usa ancora `itDe` / `deText()`.)
- **Interfaccia:** la configurazione si modifica in una bozza (`S.draft`) e si salva con `POST api/config`; la barra "Modifiche non salvate" appare da sola. Usa percorsi relativi (`api/...`, `media/...`) per funzionare dietro Ingress e niente `alert`/`confirm`: le conferme stanno nella pagina.
- **Niente dipendenze nuove** se non indispensabili: il server è volutamente in libreria standard più FFmpeg. Se ne aggiungi una, va installata nel `Dockerfile`.
- **File unici:** `server.py` e `index.html` sono monolitici per scelta. Non dividerli in moduli senza una richiesta esplicita.
- **Stabile e beta sono separati:** una modifica allo stabile non va copiata nella beta (e viceversa) se non è richiesto.
- Le impostazioni regolabili usano variabili d'ambiente `CARELLAS_*` con valore predefinito in testa a `server.py`.

## Flusso di lavoro: quando hai finito

Ogni lavoro si chiude condividendolo su GitHub:

1. Lavora su un branch dedicato, mai direttamente su `main`.
2. Prima di chiudere: test verdi (`python -m unittest discover -s tests`) e prova nel simulatore se la modifica tocca l'interfaccia, Sonos o le TV.
3. Commit, poi **push del branch**: `git push -u origin <branch>`.
4. **Crea la Pull Request con `gh`**: `gh pr create --base main --title "<titolo in italiano>" --body "<cosa cambia e come provarlo>"`. Se la PR esiste già, basta il push: si aggiorna da sola.
5. Nel resoconto finale riporta il link della PR e cosa resta **da verificare sull'impianto**.

Non fare il merge su `main` senza una richiesta esplicita.

## Rilascio di una nuova versione (stabile)

Seguire lo schema dei commit esistenti:

1. Aumentare `version` in `carellas_media_ads/config.yaml` (es. `0.4.47` → `0.4.48`). Home Assistant propone l'aggiornamento solo se la versione cambia.
2. Aggiungere in cima al `README.md` una sezione `## Novità stabile X.Y.Z` con punti brevi in italiano, rivolti all'utente finale.
3. Se cambia la generazione dei video IPTV, aumentare `IPTVEngine.BUILD_FORMAT_VERSION` in modo che i canali esistenti vengano rigenerati.
4. Messaggi di commit in italiano all'imperativo, ad esempio `Versione 0.4.48: <cosa cambia>` o `Correggi ...`.

## Attenzione

- **Sonos è la parte più delicata.** Spot, raggruppamento, ripristino della musica, dissolvenze e volumi hanno molti casi limite (vedi `docs/COLLAUDO_*.md`). Ogni modifica a `play_audio`, `restore_sonos_playback`, `fade_sonos` o allo `Scheduler` deve avere test.
- **Sicurezza:** non esporre endpoint amministrativi sulla porta 8101. Le password del Player sono salvate con `password_hash` (PBKDF2) e confrontate con `password_matches`. Non salvarle mai in chiaro.
- I file caricati passano da `safe_name()` / `safe_channel_id()`. Usali sempre per costruire percorsi a partire da input dell'utente.
- Non modificare a mano i media in `bundled_media/` né `carellas-beta-code.zip`: sono generati o pubblicati dai workflow in `.github/workflows/`.
- I test non dimostrano il funzionamento sull'impianto reale: quando una modifica tocca Sonos o le TV, segnalalo nel resoconto come **da verificare sull'impianto**.
