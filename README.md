# Carellas Media Ads

Repository Home Assistant per gestire pubblicità audio su Sonos e playlist di immagini/video su Smart TV.

> Versione stabile 0.4.9 con interfaccia adattata a telefoni e tablet, browser completo della libreria Sonos senza URL, accesso dalla barra laterale anche per utenti non amministratori, gruppi sincronizzati, durata automatica degli spot, sequenze/collage 16:9 e canali IPTV HLS indipendenti.

Nel repository è disponibile anche **Carellas Media Ads Beta 0.5.0-beta.2** come applicazione separata sulla porta `8100`. La stabile 0.4.9 rimane Sonos-only e include un'interfaccia responsive, lo stesso browser musicale visibile nel player Sonos di Home Assistant, durata automatica degli spot, sequenze IPTV ordinabili, video completi, adattamento 16:9, collage ed effetti. La Beta mantiene anche Alexa.

## Funzioni

- Annunci Sonos sopra la sorgente in riproduzione, con ripristino automatico della musica.
- Durata di ogni spot audio rilevata automaticamente dal file: non occorre inserire manualmente i secondi.
- Scelta di più altoparlanti, limite giornaliero e fasce orarie settimanali.
- Avvio/arresto programmato della musica del locale tramite URI, URL o sorgente supportata dal media player.
- Menu automatico con radio, album e playlist salvati in “I miei Sonos”, senza copiare indirizzi o identificativi.
- Browser completo della libreria del player Sonos, con navigazione nelle cartelle, tasto Indietro e selezione del contenuto per l'avvio programmato.
- Libreria audio, immagini e video con caricamento da telefono, tablet e PC.
- Caricamento remoto a blocchi tramite Home Assistant, senza esporre la porta `8099` su Internet.
- Schermo Smart TV via browser e rete LAN: non richiede l'integrazione della TV in Home Assistant.
- Modalità DLNA/DMR con playlist, Wake-on-LAN, ritardo di avvio e spegnimento programmato.
- Modalità alternativa per TV, Chromecast o Android TV già integrati come `media_player`.
- Playlist TV con durata separata per ogni contenuto, ripetizione, adattamento allo schermo e programmazione settimanale.
- Interfaccia Ingress italiano/tedesco e registro attività.
- Voce nella barra laterale disponibile anche agli utenti Home Assistant non amministratori.

## Installazione

1. In Home Assistant aprire **Impostazioni → Applicazioni → Store applicazioni**.
2. Aprire il menu in alto a destra, scegliere **Repository** e aggiungere:
   `https://github.com/EdisonACDC/pubblicit--locali-media-ads`
3. Installare **Carellas Media Ads** per la stabile oppure **Carellas Media Ads Beta** per le funzioni in prova; attivare *Mostra nella barra laterale* e avviare.
4. Aprire il pannello e completare la configurazione.

La porta `8099` deve essere disponibile soltanto nella rete locale. Non aprirla su Internet.

## Prova audio con Sonos

1. Configurare l'integrazione **Sonos** in Home Assistant.
2. Verificare che ogni diffusore compaia come entità `media_player`.
3. In **Pubblicità audio Sonos**, selezionare uno o più diffusori.
4. Caricare o selezionare lo spot MP3. Per la prima prova disattivare **solo se la musica è già attiva**.
5. Impostare durata, volume e ripetizioni, quindi premere **Prova spot**.

Lo spot viene inviato come annuncio Sonos: la sorgente musicale in riproduzione riprende automaticamente al termine.

## Prova Smart TV via LAN

1. Collegare Home Assistant e Smart TV allo stesso router, tramite cavo LAN o Wi-Fi.
2. Nella sezione **TV** scegliere **Smart TV via browser e cavo LAN**.
3. Caricare foto o video, selezionarli nella playlist, impostare i secondi e salvare.
4. Sul browser della Smart TV aprire:
   `http://INDIRIZZO-IP-HOME-ASSISTANT:8099/screen`
5. Lasciare il browser aperto a schermo intero. La pagina aggiorna automaticamente playlist e orari.

Per la prova a casa si può usare prima lo stesso indirizzo su PC, tablet o telefono. È consigliato MP4 H.264/AAC per i video e JPG/PNG per le immagini. I video sono silenziati per impostazione predefinita così il browser può avviarli automaticamente.


## Prova Smart TV con DLNA e alimentazione automatica

1. Collegare la TV via cavo LAN e attivare nelle impostazioni della TV **DLNA/Renderer**, **avvio tramite rete**, **Wake-on-LAN** o la voce equivalente.
2. In Home Assistant aprire **Impostazioni → Dispositivi e servizi → Aggiungi integrazione** e aggiungere **DLNA Digital Media Renderer**.
3. Se si vuole usare il MAC per l'accensione, aggiungere anche l'integrazione **Wake on LAN**.
4. In Carellas Media Ads selezionare **DLNA / DMR con accensione e spegnimento** e scegliere l'entità DLNA della TV.
5. Inserire il MAC della porta LAN, impostare un'attesa iniziale di circa 20–30 secondi e usare **Prova accensione**.
6. Per lo spegnimento selezionare l'entità nativa della TV, uno switch o uno script Home Assistant e usare **Prova spegnimento**.
7. Solo dopo che entrambi i pulsanti funzionano, attivare la programmazione e impostare gli orari.

All'inizio della fascia l'add-on accende la TV, attende il tempo configurato e avvia la playlist. Alla fine invia il comando di spegnimento. DLNA standard gestisce soprattutto la riproduzione: l'accensione dipende dal Wake-on-LAN della TV e lo spegnimento dipende dall'entità o integrazione scelta.


## Demo Carellas inclusa

L'add-on include tre schermate Full HD e il video `Demo_Carellas_SmartTV_DLNA.mp4` (H.264, circa 18 secondi), creati con immagini del sito ufficiale `carellas.de`. Nelle nuove installazioni il video è già selezionato; negli aggiornamenti esistenti usare **TV → Seleziona demo Carellas → Prova**.


## IPTV multi-TV

La sezione **Canali IPTV per più TV** permette di creare zone indipendenti. Ogni canale possiede nome, playlist, giorni, orario, MAC Wake-on-LAN e comando di spegnimento.

Procedura:

1. Aggiungere una TV/zona e scegliere foto e video.
2. Salvare e premere **Crea/Aggiorna canale**; la conversione preventiva può richiedere alcuni minuti.
3. Copiare l'URL M3U del canale e inserirlo nell'app IPTV della TV.
4. Ripetere la procedura per ogni zona, senza necessità di sincronizzazione.
5. Per importare tutti i canali in una sola volta usare `http://IP-HOME-ASSISTANT:8099/iptv/channels.m3u`.

I contenuti vengono normalizzati a H.264/AAC 1280×720 e distribuiti come canali HLS in ciclo continuo. La conversione avviene solo quando si crea o aggiorna il canale; ogni TV riceve poi un flusso indipendente.
