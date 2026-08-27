# Backend-Cakery (FastAPI, punya teammate) — dibungkus jadi container TANPA
# menyentuh repo aslinya. Source-nya di-clone saat build pada commit yang dipin,
# jadi build selalu reproducible dan repo Nicholl2/Backend-Cakery tetap read-only
# bagi kita (aturan CLAUDE.md: jangan pernah edit backend).
#
# Update backend ke versi baru:
#   1. cek commit terbaru: git ls-remote https://github.com/Nicholl2/Backend-Cakery HEAD
#   2. ganti BACKEND_REF di .env (atau default di bawah)
#   3. docker compose build backend && docker compose up -d backend
FROM python:3.11-slim

ARG BACKEND_REPO=https://github.com/Nicholl2/Backend-Cakery.git
# Commit terverifikasi: "Fix Bug: Chatbot Payment Tempering" (2026-08-11).
ARG BACKEND_REF=3409fc7ba5dcdc53b22f1a8d57599839e61413c3

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Ambil persis satu commit (bukan clone penuh), lalu buang .git-nya supaya image
# tidak menyimpan history dan tidak bisa ter-commit balik.
RUN git init -q . \
    && git remote add origin "$BACKEND_REPO" \
    && git fetch -q --depth 1 origin "$BACKEND_REF" \
    && git checkout -q FETCH_HEAD \
    && rm -rf .git

RUN pip install --no-cache-dir -r requirements.txt

# app/main.py mount /static kalau direktori "static" ada. Direktori ini di-mount
# named volume oleh compose (upload foto produk) — dibuat di sini supaya volume
# kosong tetap punya struktur yang benar saat pertama kali dibuat.
RUN mkdir -p static/products

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
