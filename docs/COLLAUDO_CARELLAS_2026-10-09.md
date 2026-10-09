# Collaudo e correzioni prima della consegna commerciale — 2026-10-09

Stato: **da verificare sull'impianto**. Il README 0.4.18 dichiara rotazione di uno spot per intervallo e ripristino Sonos, ma ciò non dimostra il funzionamento installato.

## P0 — Sonos
- Riprodurre caso reale: musica avviata **direttamente dall'app Sonos**, non dall'add-on; avviare spot e assicurare ripresa automatica di sorgente, coda, punto e volume quando supportati.
- Gestire race condition, altoparlanti raggruppati, radio streaming non seekable, perdita rete e spegnimento durante spot; registrare motivi di fallimento e fallback sicuri.
- Con più spot e intervallo 30 minuti deve partire **esattamente uno** spot ogni 30 minuti, a rotazione e senza sovrapposizioni; arresto manuale efficace.

## P1 — Programmazione/UI
- Collaudare fasce musica 10:00–14:00 e 17:00–22:00; sorgente distinta per fascia, giorni, fuso orario, passaggio tra playlist e gestione riavvio.
- Verificare selettori delle playlist Sonos da iPhone e PC e traduzione IT/DE in tutte le schermate.
- Collaudare upload di video grandi, sequenze 16:9, IPTV/HLS e riproduzione Smart TV; non promettere universalità dei formati.

## Accettazione
20 cicli consecutivi spot/ripristino con musica Sonos esterna; 3 cicli con rete temporaneamente instabile; 2 spot su 6 intervalli (A-B-A-B-A-B); verifica playlist in entrambe le fasce e dopo riavvio; filmati/foto visibili su TV target.
