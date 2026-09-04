# syntax=docker/dockerfile:1
#
# Image chatbot-service. Dibangun sendiri oleh repo ini (CI: .github/workflows/ci.yaml)
# dan dipublish ke ghcr.io/kevinilhamramadhan/chatbot-capstone — repo deploy cukup
# menarik image itu lewat compose, tidak perlu ikut membawa source.
#
#   docker build -t toti-chatbot ./chatbot-service
#   docker compose build chatbot-service
#
# Dua stage: dependency (pip, lapisan paling mahal) dipisah dari kode supaya
# perubahan kode tidak memicu install ulang chromadb/langchain.

# ── Stage 1: dependency ──────────────────────────────────────────────────────
# Tag minor sengaja tidak dipin ke patch: 3.11 mengikuti versi yang dites di
# requirements.txt, patch-nya biar ikut update keamanan Debian/Python.
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Venv terpisah supaya stage runtime cukup menyalin satu direktori — tidak ada
# sisa cache pip atau toolchain build yang ikut ke image akhir.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="toti-chatbot-service" \
      org.opencontainers.image.description="Toti Cakery WhatsApp chatbot service (FastAPI + LangChain + Ollama)" \
      org.opencontainers.image.source="https://github.com/kevinilhamramadhan/chatbot-capstone"

# UID/GID bisa ditimpa saat build (--build-arg APP_UID=…). Default 1000 karena
# compose bind-mount chroma_db/ dan toti_chatbot.db dari host: kalau UID
# container tidak sama dengan pemilik folder itu, prosesnya gagal menulis.
ARG APP_UID=1000
ARG APP_GID=1000

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --gid "${APP_GID}" app \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

# /app harus dimiliki app, bukan cuma isinya: SQLite membuat berkas baru
# (toti_chatbot.db plus -journal/-wal) di direktori ini, dan direktori yang
# dibuat WORKDIR tetap milik root — startup langsung mati "unable to open
# database file" kalau langkah ini dilewat.
RUN mkdir -p /app && chown app:app /app
WORKDIR /app
COPY --chown=app:app . .

# Dua titik mount data di repo deploy (chatbot_data:/app/data dan
# chroma_data:/app/chroma_db). Keduanya HARUS sudah ada di image dan dimiliki
# app: saat named volume dibuat pertama kali, Docker menyalin isi + kepemilikan
# direktori image ke volume itu. Kalau direktorinya tidak ada, volume lahir
# milik root dan container non-root gagal menulis — SQLite mati "unable to open
# database file", ingest Chroma kena "read-only"/permission denied.
RUN mkdir -p /app/data /app/chroma_db && chown app:app /app/data /app/chroma_db

# Bukan root: webhook WhatsApp adalah permukaan yang terbuka ke internet.
USER app

EXPOSE 8000

# /health tidak menyentuh backend/Ollama, jadi murah dipanggil tiap 30 detik.
# start-period longgar: startup memanggil validate_runtime + init_db dulu.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
