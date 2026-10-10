# Carellas Media Ads

## Novità stabile 0.4.34

- Nuovo pulsante **Aggiorna playlist Sonos**: rilegge immediatamente playlist, radio e preferiti dal Sonos selezionato senza riavviare l’add-on.
- L’aggiornamento esplora anche le cartelle Sonos dedicate a preferiti, playlist e radio e rende le sorgenti trovate disponibili nelle fasce orarie.
- Le fasce musicali e le modifiche non ancora salvate vengono conservate durante l’aggiornamento dell’elenco.

## Novità stabile 0.4.33

- Dissolvenza regolabile per il passaggio tra playlist e radio Sonos, senza interruzioni brusche.
- Ingresso e uscita graduali degli spot audio; al termine la musica precedente riparte con dissolvenza e con i volumi originali delle singole sale.
- Tempi tecnici di raggruppamento Sonos ridotti per limitare il silenzio prima e dopo gli annunci.
- Ogni sorgente musicale deve rientrare interamente negli orari generali di accensione; il pannello mostra subito se la durata supera il limite.

## Novità stabile 0.4.32

- Il pulsante **Apri** della Libreria usa ora il collegamento protetto Home Assistant Ingress, quindi funziona anche da un PC fuori dalla rete del ristorante.
- Gli spot audio dispongono di un lettore integrato direttamente nella Libreria, con riproduzione, pausa e avanzamento.
- Gli indirizzi locali destinati a Sonos, IPTV e Smart TV rimangono invariati.

## Novità stabile 0.4.31

- Nuova raccolta di effetti professionali applicabili a fotografie e collage: **Ken Burns diagonale**, **Ken Burns inverso**, panoramiche verso destra, sinistra, alto e basso, zoom cinematografici, dissolvenza morbida e bianco e nero.
- Tutti i movimenti vengono renderizzati realmente da FFmpeg a 3840×2160 con accelerazione progressiva e uscita 16:9 a 1280×720.
- Le vecchie impostazioni “Movimento laterale” vengono convertite automaticamente nella nuova panoramica verso destra.
- La nuova versione del formato rigenera automaticamente i canali già creati.

## Novità stabile 0.4.30

- Eliminato il tremolio di **Zoom avanti**, **Zoom indietro** e **Movimento laterale**.
- Gli effetti vengono calcolati su una superficie 3840×2160 e ridotti al formato finale 1280×720, evitando gli scatti dovuti all’arrotondamento dei pixel.
- Il movimento usa ora un’accelerazione e una decelerazione progressive, più naturali e fluide.
- La nuova versione del formato forza automaticamente la rigenerazione dei canali esistenti.

## Novità stabile 0.4.29

- La sequenza generata conserva in modo verificabile l’ordine esatto di video, foto e collage impostato nell’editor.
- Il pannello mostra la **sequenza realmente generata**, con posizione, durata ed effetto di ogni elemento.
- Zoom avanti, zoom indietro e movimento laterale sono stati resi molto più visibili; anche la dissolvenza è più lunga e riconoscibile.
- Durante la generazione lo stato si aggiorna automaticamente e il Player riceve la nuova revisione appena pronta.
- Un errore di conversione non viene più nascosto dalla presenza del vecchio video: il pannello e il Player mostrano l’errore reale.
- Eliminata la doppia generazione che poteva essere avviata dal pulsante **Crea/Aggiorna canale**.

## Novità stabile 0.4.28

- Ogni canale salva ora un'impronta della sequenza realmente convertita.
- Il Player confronta ordine, durate, effetti e formato salvati con il file generato: se non coincidono, il canale viene marcato come non aggiornato e ricostruito automaticamente.
- Il collegamento video include la revisione effettivamente generata, così il browser della TV non può continuare a usare la versione precedente dalla cache.
- Dopo l'aggiornamento, i vecchi canali privi dell'impronta vengono rigenerati una sola volta; successivamente vengono ricreati soltanto quando cambia la sequenza.

## Novità stabile 0.4.27

- Aggiunto il comando esplicito **Reimposta password** per ogni utente Player TV, con conferma e pulsanti per mostrare/nascondere entrambi i campi.
- Reimpostando la password vengono invalidate automaticamente le precedenti sessioni del Player TV.
- Salvando un nuovo ordine di video, foto o collage, il relativo canale viene ora ricreato automaticamente senza riavviare l'add-on.
- Il Player TV continua a mostrare il canale precedente durante la conversione e carica quello aggiornato automaticamente appena pronto.
- Se si salva nuovamente durante una conversione, viene accodata un'ulteriore ricostruzione con l'ordine più recente.
- I pulsanti IT/DE restano disponibili nelle schermate di accesso e servizio, ma scompaiono durante la riproduzione pubblicitaria.

## Novità stabile 0.4.26

- Il Player TV prepara automaticamente il canale assegnato quando il file di riproduzione non esiste ancora.
- Durante la preparazione mostra lo stato reale e distingue conversione, canale vuoto, canale eliminato ed errore.
- Gli utenti Player TV salvati sono raccolti in una tendina: si seleziona un utente alla volta per modificarlo o eliminarlo.
- Salvataggio ed eliminazione degli utenti sono immediati e mostrano una conferma chiara.

## Novità stabile 0.4.25

- Dopo un caricamento, il nuovo file compare immediatamente nella Libreria senza uscire e rientrare dall’app.
- L’aggiornamento della Libreria non cancella le modifiche di configurazione non ancora salvate.
- Il selettore del file non viene più considerato una modifica della configurazione generale.

## Novità stabile 0.4.24

- Aggiunto un pulsante **Salva utente** direttamente nella scheda di ogni Player TV.
- Dopo il salvataggio compare una conferma chiara che la password è stata registrata.
- La password resta vuota nei salvataggi successivi per sicurezza e viene cambiata solo quando se ne digita una nuova.
- Aggiunto un test completo che salva le credenziali dal pannello e verifica il login reale sulla porta 8101.

## Novità stabile 0.4.23

- Aggiunto il pulsante con l'occhio per mostrare o nascondere le password nella configurazione degli utenti TV e nella pagina di accesso del Player TV.
- Le password già salvate restano protette e non sono recuperabili: il pulsante mostra soltanto la password digitata nel campo.

## Novità stabile 0.4.22

- Nuovo **Player TV via browser** sulla porta dedicata `8101`, protetto da nome utente e password.
- Ogni account TV viene associato a uno dei canali già preparati in **IPTV multi-TV**.
- Riproduzione continua di video e audio a schermo intero; formato 16:9 adattato senza deformazioni (`contain`) oppure con ritaglio automatico (`cover`).
- L'audio originale dei video viene conservato durante la creazione del canale; ai video senza audio viene aggiunta una traccia silenziosa compatibile.
- La porta del Player non espone configurazione, libreria o comandi amministrativi dell'add-on.

## Novità stabile 0.4.21

- Il conteggio giornaliero degli spot resta corretto anche dopo un riavvio dell'add-on.
- La configurazione stabile elimina automaticamente le vecchie opzioni Alexa e di ripetizione.
- Eliminando un file, vengono rimossi anche i riferimenti IPTV non più validi e il canale viene segnato da ricreare.
- Lo stato IPTV include correttamente anche i canali non ancora generati.
- Versione interna del server allineata alla versione pubblicata.

## Novità stabile 0.4.20

- Selettore Italiano/Deutsch sempre visibile nell'intestazione su telefono, tablet e PC.
- Cambio lingua applicato immediatamente e salvato automaticamente.

## Novità stabile 0.4.19

- Navigazione mobile disposta su due colonne, senza sezioni nascoste fuori schermo.
- Schede Sonos adattate alla larghezza del telefono con nomi e identificativi leggibili su più righe.
- Pulsante di salvataggio inserito nel flusso della pagina, senza coprire le impostazioni durante lo scorrimento.

## Novità stabile 0.4.18

- Con più spot audio selezionati, la programmazione ne riproduce esattamente uno a ogni intervallo.
- Gli spot avanzano nell'ordine della lista e la rotazione ricomincia dal primo solo dopo l'ultimo.
- Le vecchie impostazioni di ripetizione non possono più causare più annunci nello stesso intervallo automatico.
- Interfaccia italiana/tedesca completa, inclusi contenuti dinamici, playlist Sonos, IPTV, collage e messaggi operativi.
- Riepilogo visibile dell'ordine automatico degli spot selezionati.

## Novità stabile 0.4.17

- Orari generali di accensione della musica separati dalle sorgenti.
- Più fasce giornaliere, per esempio 10:00–14:00 e 17:00–22:00.
- Ora di partenza e durata indipendenti per ogni playlist o radio Sonos.
- Calcolo automatico dell'ora di fine e passaggio alla sorgente successiva.
- Selezione diretta della playlist o radio Sonos dentro ogni fascia, anche da telefono.
- Ripristino affidabile della musica avviata direttamente dall'app Sonos dopo ogni spot.

Repository Home Assistant per gestire pubblicità audio su Sonos e playlist di immagini/video su Smart TV.

> Versione stabile 0.4.15 con programmazione musicale multi-fascia: ogni orario può avviare una playlist, radio o preferito Sonos differente, con giorni, volume e altoparlanti propri. Include inoltre conteggio degli spot, arresto immediato, sincronizzazione Sonos, interfaccia responsive, browser musicale, sequenze/collage 16:9 e canali IPTV HLS indipendenti.

Nel repository è disponibile anche **Carellas Media Ads Beta 0.5.0-beta.2** come applicazione separata sulla porta `8100`. La stabile 0.4.15 rimane Sonos-only e include fasce musicali con sorgenti indipendenti, conteggio corretto degli spot, arresto manuale, riproduzione sincronizzata soltanto sui diffusori scelti, streaming MP3 con richieste parziali, un'interfaccia responsive, browser musicale, durata automatica degli spot, sequenze IPTV ordinabili, video completi, adattamento 16:9, collage ed effetti. La Beta mantiene anche Alexa.

## Funzioni

- Annunci Sonos sopra la sorgente in riproduzione, con ripristino automatico della musica.
- Durata di ogni spot audio rilevata automaticamente dal file: non occorre inserire manualmente i secondi.
- Scelta di più altoparlanti, limite giornaliero e fasce orarie settimanali.
- Fasce musicali indipendenti: per ciascun orario si scelgono playlist/radio, giorni, volume e altoparlanti Sonos, con passaggio automatico alla sorgente successiva.
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
5. Impostare volume e ripetizioni, quindi premere **Prova spot**. La durata viene letta automaticamente dal file.

Lo spot viene riprodotto come unico flusso dal coordinatore del gruppo temporaneo formato esclusivamente dai Sonos selezionati. Prima dello spot vengono salvati musica, volume e gruppi; al termine tutto viene ripristinato automaticamente.

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


## Player TV diretto nel browser

Non è necessaria un'app IPTV a pagamento. In **IPTV multi-TV → Player TV via browser**:

1. Creare o aggiornare il canale con i video e le foto desiderati.
2. Aggiungere un utente, impostare una password di almeno 6 caratteri e assegnargli il canale.
3. Salvare la configurazione.
4. Aprire nel browser della TV `http://IP-HOME-ASSISTANT:8101`, accedere e premere una volta **Avvia video e audio** se il browser blocca l'avvio automatico con suono.

Per l'accesso da Internet, il sottodominio pubblico deve inoltrare esclusivamente al Player sulla porta `8101`. La porta amministrativa `8099` deve rimanere accessibile soltanto dalla LAN o tramite l'accesso protetto di Home Assistant.
