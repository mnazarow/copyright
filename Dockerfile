# Корректор в контейнере.
#   docker build -t korrektor .
#   docker compose up -d        (см. docker-compose.yml)
# Аргументы сборки: WITH_OCR=1 — Tesseract, WITH_OFFICE=1 — LibreOffice (прием DOCX/ODT/RTF).
FROM python:3.12-slim

ARG WITH_OCR=0
ARG WITH_OFFICE=1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    KORREKTOR_DATA=/data KORREKTOR_HOST=0.0.0.0 KORREKTOR_PORT=8080 KORREKTOR_LOG=INFO TZ=Europe/Moscow

RUN apt-get update && apt-get install -y --no-install-recommends hunspell hunspell-ru fonts-dejavu-core fonts-liberation tzdata \
    && if [ "$WITH_OCR" = "1" ]; then apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus; fi \
    && if [ "$WITH_OFFICE" = "1" ]; then apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-crosextra-carlito; fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/korrektor
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY VERSION README.md ./
COPY app ./app
COPY docs ./docs
COPY scripts ./scripts

RUN useradd --system --home-dir /data --shell /usr/sbin/nologin korrektor \
    && mkdir -p /data /var/backups/korrektor && chown korrektor:korrektor /data /var/backups/korrektor /opt/korrektor/app/prompts
USER korrektor
VOLUME ["/data", "/var/backups/korrektor"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('KORREKTOR_PORT','8080')+'/api/health',timeout=4)"
ENV KORREKTOR_TRUSTED_PROXIES=127.0.0.1,::1
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${KORREKTOR_HOST} --port ${KORREKTOR_PORT} --proxy-headers --forwarded-allow-ips ${KORREKTOR_TRUSTED_PROXIES}"]
