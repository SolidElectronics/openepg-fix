FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=America/Toronto

# tzdata so zoneinfo works regardless of base-image contents
RUN pip install --no-cache-dir tzdata \
 && useradd --uid 1000 --create-home app \
 && mkdir /data && chown app:app /data

WORKDIR /app
COPY openepg_fix.py server.py ./

USER app
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=5m --timeout=10s --start-period=60s \
  CMD python -c "import urllib.request as u; u.urlopen('http://localhost:80/guide.xml', timeout=5)" || exit 1

CMD ["python", "server.py"]
