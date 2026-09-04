#!/usr/bin/env bash
# Bootstrap satu perintah untuk VPS kosong (yang penting: idempoten — aman
# dijalankan ulang kapan saja). Yang dikerjakan:
#
#   1. cek prasyarat + file .env
#   2. siapkan file/direktori yang di-bind-mount (jebakan: Docker bikin
#      DIREKTORI kalau file SQLite-nya belum ada, lalu chatbot gagal start)
#   3. bangun model Ollama dari GGUF fine-tune (file lokal atau unduh dari HF)
#   4. tarik model embedding
#   5. build + start seluruh stack
#   6. ingest FAQ ke ChromaDB
#
# Langkah manual yang TIDAK bisa diotomatiskan (scan QR WhatsApp, isi katalog):
# lihat DEPLOY_VPS.md.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$OFF"; }
ok()   { printf '%s  ✓ %s%s\n' "$GREEN" "$*" "$OFF"; }
warn() { printf '%s  ! %s%s\n' "$YELLOW" "$*" "$OFF"; }
die()  { printf '\n%s  ✗ %s%s\n\n' "$RED" "$*" "$OFF" >&2; exit 1; }

# Baca satu key dari .env tanpa `source` — .env berisi nilai dengan spasi yang
# tidak dikutip (STORE_ADDRESS=Jl. Contoh No. 123), jadi source-nya akan pecah.
env_get() {
  [ -f .env ] || return 0
  grep -E "^[[:space:]]*$1=" .env | tail -n1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//' -e 's/\r$//'
}

# ── 1. Prasyarat ─────────────────────────────────────────────────────────────
step "Cek prasyarat"
command -v docker >/dev/null || die "docker tidak ditemukan. Install Docker dulu (DEPLOY_VPS.md §2)."
docker compose version >/dev/null 2>&1 || die "Butuh Docker Compose v2 (\`docker compose\`, bukan \`docker-compose\`)."
command -v curl >/dev/null || die "curl tidak ditemukan (butuh untuk unduh GGUF dari HuggingFace)."
[ -f .env ] || die ".env belum ada. Jalankan: cp .env.example .env, lalu isi nilainya (DEPLOY_VPS.md §4)."
ok "docker $(docker --version | awk '{print $3}' | tr -d ,), compose v2, .env ada"

LLM_MODEL="$(env_get LLM_MODEL)";           LLM_MODEL="${LLM_MODEL:-toti-qwen-1.7b-v4}"
EMBEDDING_MODEL="$(env_get EMBEDDING_MODEL)"; EMBEDDING_MODEL="${EMBEDDING_MODEL:-qwen3-embedding:0.6b}"
HF_REPO="${HF_REPO:-$(env_get HF_REPO)}";   HF_REPO="${HF_REPO:-LasagnaS/toti-qwen-gguf}"
HF_TOKEN="${HF_TOKEN:-$(env_get HF_TOKEN)}"
# Modelfile MENGIKUTI versi di LLM_MODEL, tidak dipatok: kalau di-hardcode,
# menaikkan LLM_MODEL ke -v5 sementara Modelfile tetap -v4 akan membuat model
# BERNAMA v5 dari bobot v4, tanpa satu pun pesan error. Override manual tetap
# bisa: MODELFILE=path/lain ./scripts/bootstrap.sh
MODELFILE="${MODELFILE:-finetune/Modelfile.qwen3-1.7b-${LLM_MODEL##*-}}"

# `ollama list` menuliskan tag lengkap (toti-qwen-1.7b-v4:latest), sedangkan
# LLM_MODEL di .env ditulis tanpa :latest — samakan dulu sebelum dibandingkan.
model_present() {
  docker compose exec -T ollama ollama list 2>/dev/null \
    | awk 'NR>1 { sub(/:latest$/, "", $1); print $1 }' \
    | grep -qx "${1%:latest}"
}

# ── 2. Bind mount ────────────────────────────────────────────────────────────
step "Siapkan file & direktori yang di-mount"
DB_FILE="chatbot-service/toti_chatbot.db"
if [ -d "$DB_FILE" ]; then
  die "$DB_FILE adalah DIREKTORI (dibuat Docker karena compose jalan sebelum bootstrap).
     Hapus dulu: sudo rm -rf $DB_FILE — lalu jalankan script ini lagi."
fi
[ -f "$DB_FILE" ] || { : > "$DB_FILE"; ok "buat $DB_FILE (kosong; tabel dibuat chatbot saat start)"; }
mkdir -p chatbot-service/chroma_db whatsapp-gateway/sessions
ok "chroma_db/ & sessions/ siap"

# ── 3. Model fine-tune ───────────────────────────────────────────────────────
step "Siapkan model Ollama: $LLM_MODEL"
docker compose up -d ollama >/dev/null
printf '  menunggu container ollama siap'
for i in $(seq 1 60); do
  if docker compose exec -T ollama ollama list >/dev/null 2>&1; then break; fi
  printf '.'; sleep 2
  if [ "$i" = 60 ]; then die "ollama tidak siap setelah 120 detik. Cek: docker compose logs ollama"; fi
done
printf '\n'

if model_present "$LLM_MODEL"; then
  ok "$LLM_MODEL sudah ada di model store — dilewati"
else
  [ -f "$MODELFILE" ] || die "Modelfile tidak ditemukan: $MODELFILE
     Diturunkan dari LLM_MODEL=$LLM_MODEL di .env. Perbaiki salah satu:
       a) samakan nama, mis. LLM_MODEL=toti-qwen-1.7b-v5 butuh
          finetune/Modelfile.qwen3-1.7b-v5, atau
       b) tunjuk manual: MODELFILE=finetune/Modelfile.lain ./scripts/bootstrap.sh"
  # Nama file GGUF diambil dari baris FROM Modelfile supaya keduanya tidak
  # pernah lepas sinkron saat versi model naik.
  GGUF="$(awk 'tolower($1)=="from"{print $2; exit}' "$MODELFILE" | sed 's#.*/##')"
  [ -n "$GGUF" ] || die "Tidak bisa membaca baris FROM dari $MODELFILE"

  if [ -f "finetune/$GGUF" ]; then
    ok "GGUF sudah ada: finetune/$GGUF"
  elif [ -n "$HF_TOKEN" ]; then
    warn "GGUF belum ada — unduh dari HuggingFace ($HF_REPO, private)"
    curl -fL --progress-bar \
      -H "Authorization: Bearer $HF_TOKEN" \
      -o "finetune/$GGUF.part" \
      "https://huggingface.co/$HF_REPO/resolve/main/$GGUF" \
      || die "Unduhan gagal. Cek HF_TOKEN (butuh akses Read ke $HF_REPO) dan nama file $GGUF."
    mv "finetune/$GGUF.part" "finetune/$GGUF"
    ok "terunduh: finetune/$GGUF"
  else
    die "GGUF tidak ada dan HF_TOKEN kosong. Dua pilihan:
       a) isi HF_TOKEN di .env (fine-grained token, akses Read ke $HF_REPO), atau
       b) salin manual dari mesin dev:
          scp finetune/$GGUF <user>@<vps>:$ROOT/finetune/"
  fi

  # `ollama create` di dalam container: path FROM harus absolut ke mount /finetune.
  sed "s#^[Ff][Rr][Oo][Mm] .*#FROM /finetune/$GGUF#" "$MODELFILE" > finetune/Modelfile.generated
  docker compose exec -T ollama ollama create "$LLM_MODEL" -f /finetune/Modelfile.generated \
    || die "ollama create gagal. Cek: docker compose logs ollama"
  ok "model $LLM_MODEL dibuat"
fi

# ── 4. Model embedding ───────────────────────────────────────────────────────
step "Siapkan model embedding: $EMBEDDING_MODEL"
if model_present "$EMBEDDING_MODEL"; then
  ok "sudah ada — dilewati"
else
  docker compose exec -T ollama ollama pull "$EMBEDDING_MODEL" || die "gagal pull $EMBEDDING_MODEL"
  ok "$EMBEDDING_MODEL siap"
fi

# ── 5. Build & start ─────────────────────────────────────────────────────────
step "Build & start seluruh stack (build backend meng-clone repo, bisa beberapa menit)"
docker compose up -d --build
printf '  menunggu chatbot sehat'
for i in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then break; fi
  printf '.'; sleep 2
  if [ "$i" = 90 ]; then die "chatbot tidak sehat setelah 180 detik. Cek: docker compose logs chatbot-service"; fi
done
printf '\n'
ok "chatbot sehat (model di-preload di background, balasan pertama tetap lambat)"

# ── 6. Ingest FAQ ────────────────────────────────────────────────────────────
step "Ingest FAQ ke ChromaDB"
docker compose exec -T chatbot-service python knowledge_base/ingest.py \
  || die "ingest gagal. Cek: docker compose logs chatbot-service"
ok "knowledge base siap (ulangi perintah ini tiap kali file FAQ diubah)"

cat <<EOF

${BOLD}Stack jalan.${OFF} Status: docker compose ps

Sisa langkah manual:
  1. Scan QR WhatsApp        → DEPLOY_VPS.md §5 (butuh SSH tunnel ke port 3000)
  2. Isi katalog & user admin → DEPLOY_VPS.md §6 (DB backend masih kosong)

EOF
