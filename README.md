# Carellas Media Ads

Repository Home Assistant per la gestione professionale della pubblicità audio su Sonos e di immagini/video su TV.

## Funzioni

- Annunci Sonos sopra qualsiasi sorgente già in riproduzione, con ripristino automatico della musica.
- Scelta di più diffusori, volume annuncio, intervallo, ripetizioni e limite giornaliero.
- Avvio/arresto programmato della musica del locale tramite URI, URL o Preferito Sonos.
- Libreria audio, immagini e video con caricamento da telefono, tablet e PC.
- Playlist TV con durata separata per ogni contenuto, ripetizione e programmazione settimanale.
- Interfaccia Ingress italiano/tedesco e registro attività.

## Installazione

1. In Home Assistant aprire **Impostazioni → Applicazioni → Store applicazioni**.
2. Aprire il menu in alto a destra, scegliere **Repository** e aggiungere:
   `https://github.com/EdisonACDC/pubblicit--locali-media-ads`
3. Installare **Carellas Media Ads**, attivare *Mostra nella barra laterale* e avviare.
4. Aprire il pannello e completare la configurazione guidata.

Per consentire a Sonos e alla TV di raggiungere i file, la porta `8099` deve essere disponibile nella rete locale. Non è necessario aprirla su Internet.
