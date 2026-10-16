ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app
COPY app /app
COPY run.sh /run.sh
# La beta usa la stessa cartella media dello stabile: i media demo li installa già lo stabile.
# font-dejavu: testi delle pagine del menu; poppler-utils (pdftoppm): menu caricati in PDF.
RUN apk add --no-cache ffmpeg font-dejavu poppler-utils \
    && python3 -m pip install --no-cache-dir websocket-client==1.8.0 \
    && python3 -c "import websocket; print('websocket-client', websocket.__version__)" \
    && chmod a+x /run.sh \
    && mkdir -p /media/carellas_media_ads

EXPOSE 8100 8102
CMD ["/run.sh"]
