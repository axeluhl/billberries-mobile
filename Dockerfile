FROM python:3.12-alpine

RUN apk add --no-cache tzdata && adduser -D -H -u 10001 app && mkdir /data && chown app:app /data
WORKDIR /app
COPY server.py index.html ./

USER app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8080
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
CMD ["python3", "/app/server.py"]
