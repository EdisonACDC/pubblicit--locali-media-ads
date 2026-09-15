ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app
COPY app /app
COPY run.sh /run.sh
RUN apk add --no-cache ffmpeg \
    && chmod a+x /run.sh \
    && mkdir -p /media/carellas_media_ads

EXPOSE 8100
CMD ["/run.sh"]
