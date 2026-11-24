# Applicazione autocontenuta: l'LLM (Ollama o endpoint compatibile OpenAI) si sceglie dall'interfaccia.
#   docker build -t traduttore .
#   docker run -d -p 8080:8080 --add-host=host.docker.internal:host-gateway traduttore
FROM python:3.12-slim AS build
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

FROM python:3.12-slim
# Font usati per ricomporre i PDF: sostituti metricamente compatibili di Calibri (Carlito),
# Cambria (Caladea), Arial/Helvetica, Times, Courier (Liberation), più DejaVu e Noto per gli
# altri alfabeti (arabo, ebraico, devanagari, cinese, giapponese, coreano...).
# Per un'immagine più leggera: --build-arg EXTRA_FONTS=""
ARG EXTRA_FONTS="fonts-noto-core fonts-noto-cjk"
RUN apt-get update \
 && apt-get install -y --no-install-recommends fontconfig fonts-liberation fonts-crosextra-carlito \
      fonts-crosextra-caladea fonts-dejavu-core $EXTRA_FONTS \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOCALHOST_ALIAS=host.docker.internal \
    LLM_PROVIDER=ollama \
    LLM_BASE_URL=http://localhost:11434 \
    LLM_MODEL="" \
    CHUNK_CHARS=2000 \
    OLLAMA_NUM_CTX=8192 \
    NO_THINK=1 \
    LLM_READ_TIMEOUT=900 \
    CONCURRENCY=1 \
    MAX_UPLOAD_MB=50
COPY --from=build /opt/venv /opt/venv
WORKDIR /srv
COPY app ./app
RUN useradd --system --uid 10001 --no-create-home app && chown -R app /srv
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
