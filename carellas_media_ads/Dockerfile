ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app
COPY app /app
COPY run.sh /run.sh
RUN cat /app/bundled_media/Carellas_Ristorante_Spot_DE_Femminile.mp3.part.* > /app/bundled_media/Carellas_Ristorante_Spot_DE_Femminile.mp3 \
    && cat /app/bundled_media/Carellas_Ristorante_Spot_DE_Maschile.mp3.part.* > /app/bundled_media/Carellas_Ristorante_Spot_DE_Maschile.mp3 \
    && find /app/bundled_media -name '*.part.*' -delete \
    && chmod a+x /run.sh \
    && mkdir -p /media/carellas_media_ads

EXPOSE 8099
CMD ["/run.sh"]
