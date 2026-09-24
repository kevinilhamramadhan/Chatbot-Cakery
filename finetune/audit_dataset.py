"""Audit dataset v7: isinya benar, dan bentuknya sama dengan yang dilihat model di produksi?

Diperiksa terhadap KODE RUNTIME (bukan terhadap generator), supaya generator yang
salah tidak bisa lolos dengan meluluskan dirinya sendiri:

- paritas: system block, letak konteks FAQ (pesan pelanggan, dirakit
  agent.pertanyaan_dengan_konteks), daftar tool per peran, argumen vs skema tool
- cakupan: setiap tool runtime punya contoh latihan (v6 tidak punya satu pun untuk
  check_cart dan resend_payment_method — tidak ada pemeriksaan yang menangkapnya)
- aturan perilaku v6 dan v7
- FAQ sebagai keterampilan: jawaban N1 berasal dari konteks, ada fakta tandingan,
  dan topik khusus test tidak bocor
- kebocoran test dan teks kembar

Pakai:
    chatbot-service/.venv/bin/python finetune/audit_dataset.py finetune/data

Jalankan juga terhadap salinan yang DIUNDUH DARI HF sebelum training — itu yang
benar-benar dibaca notebook:

    for s in train validation test; do
      curl -sSL "https://huggingface.co/datasets/LasagnaS/toti-cakery-toolcall/resolve/main/data/$s.jsonl" -o "/tmp/hf/$s.jsonl"
    done
    chatbot-service/.venv/bin/python finetune/audit_dataset.py /tmp/hf
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chatbot-service"))
sys.path.insert(0, str(ROOT / "finetune"))
from langchain_core.utils.function_calling import convert_to_openai_tool  # noqa: E402

from app.llm.agent import pertanyaan_dengan_konteks  # noqa: E402
from app.llm.prompt import SYSTEM_PROMPT, TOOL_REMINDER  # noqa: E402
from app.conversation import bahasa  # noqa: E402
from app.tools.registry import ALL_TOOLS, TOOLS_UMUM  # noqa: E402
import faq_topik  # noqa: E402

DIR = Path(sys.argv[1])
SKEMA = {t["function"]["name"]: t["function"]["parameters"]
         for t in (convert_to_openai_tool(t) for t in ALL_TOOLS)}
TOOL_RUNTIME = set(SKEMA)
TOOL_UMUM = {t["function"]["name"] for t in (convert_to_openai_tool(t) for t in TOOLS_UMUM)}
SYSTEM_BLOCK = SYSTEM_PROMPT + "\n\n" + TOOL_REMINDER
PREFIX = pertanyaan_dengan_konteks("", "X").split("X")[0]
PENANYA = pertanyaan_dengan_konteks("Y", "X").split("X", 1)[1].rsplit("Y", 1)[0]

rows = {s: [json.loads(line) for line in (DIR / f"{s}.jsonl").open(encoding="utf-8")]
        for s in ("train", "validation", "test")}
gagal, catat = [], []


def cek(nama, ok, detail=""):
    (catat if ok else gagal).append(f"{'OK  ' if ok else 'GAGAL'} | {nama}{' — ' + detail if detail else ''}")


def args_of(r):
    a = r["messages"][-1]
    return [(c["function"]["name"], json.loads(c["function"]["arguments"]))
            for c in (a.get("tool_calls") or [])]


def user_akhir(r):
    return r["messages"][-2]["content"]


def pisah_konteks(teks):
    """(dokumen[], pertanyaan mentah) dari pesan pelanggan terakhir."""
    if not teks.startswith(PREFIX):
        return [], teks
    isi, tanya = teks[len(PREFIX):].split(PENANYA, 1)
    return isi.split("\n\n---\n\n"), tanya


# 1. Ukuran
for split, minimal in (("train", 1400), ("validation", 140), ("test", 140)):
    cek(f"jumlah baris {split} >= {minimal}", len(rows[split]) >= minimal, str(len(rows[split])))

# 2. Paritas system block — v8: dua bentuk sah, satu per bahasa. Runtime
# menempel arahan bahasa (agent.py: TOOL_REMINDER + bahasa.arahan(lang)), jadi
# baris yang TIDAK memuatnya melatih model dengan prompt yang tidak pernah ia
# terima. Bahasanya pun harus cocok dengan meta baris itu, bukan asal ada.
SYSTEM_BLOK = {l: SYSTEM_BLOCK + bahasa.arahan(l) for l in (bahasa.ID, bahasa.EN)}
for split in rows:
    beda = sum(1 for r in rows[split] if r["messages"][0]["content"] not in SYSTEM_BLOK.values())
    cek(f"system block {split} identik dengan runtime", not beda, f"{beda} baris beda")
    salah_bahasa = sum(
        1 for r in rows[split]
        if r["messages"][0]["content"] != SYSTEM_BLOK[bahasa.normalkan(r["meta"].get("lang"))]
    )
    cek(f"arahan bahasa {split} cocok dengan meta baris", not salah_bahasa,
        f"{salah_bahasa} baris salah bahasa")

# 3. Konteks FAQ: hanya di pesan pelanggan TERAKHIR, bentuknya = fungsi runtime
for split in rows:
    salah = 0
    for r in rows[split]:
        for m in r["messages"][1:-2]:
            if m["role"] == "user" and m["content"].startswith(PREFIX):
                salah += 1
        docs, tanya = pisah_konteks(user_akhir(r))
        if docs and pertanyaan_dengan_konteks(tanya, "\n\n---\n\n".join(docs)) != user_akhir(r):
            salah += 1
    cek(f"konteks FAQ {split} dirakit persis seperti runtime", not salah, f"{salah} baris")
bagian = sum(1 for r in rows["train"] if user_akhir(r).startswith(PREFIX)) / len(rows["train"])
cek("porsi baris ber-konteks train mendekati runtime (±65%)", 0.55 <= bagian <= 0.75, f"{bagian:.2f}")
tool_berkonteks = sum(1 for r in rows["train"] if args_of(r) and user_akhir(r).startswith(PREFIX))
cek("baris tool juga membawa konteks (model belajar mengabaikan konteks tak relevan)",
    tool_berkonteks >= 300, str(tool_berkonteks))

# 4. Daftar tool per peran
for split in rows:
    salah = [i for i, r in enumerate(rows[split])
             if {t["function"]["name"] for t in json.loads(r["tools_json"])}
             != (TOOL_RUNTIME if r["meta"]["type"] in ("T11", "T12") else TOOL_UMUM)]
    cek(f"tools_json {split} sesuai peran (11 umum / 13 Owner)", not salah, f"{len(salah)} baris")

# 5. Argumen sesuai SKEMA runtime (nama, parameter wajib, tidak ada parameter karangan)
rusak = []
for s in rows:
    for r in rows[s]:
        for n, a in args_of(r):
            sk = SKEMA.get(n)
            if sk is None:
                rusak.append((s, n, "tool tak dikenal"))
                continue
            props = sk.get("properties", {})
            if set(a) - set(props):
                rusak.append((s, n, f"parameter karangan {set(a) - set(props)}"))
            if set(sk.get("required") or []) - set(a):
                rusak.append((s, n, "parameter wajib hilang"))
            if n == "add_to_cart":
                if not (set(a) == {"items"} and a["items"]
                        and all(set(i) == {"product", "qty"} and isinstance(i["qty"], int)
                                and i["qty"] >= 1 for i in a["items"])):
                    rusak.append((s, n, a))
cek("argumen tool cocok dengan skema runtime", not rusak, str(rusak[:3]))

# 6. Cakupan: setiap tool punya contoh di train
jumlah_tool = Counter(n for r in rows["train"] for n, _ in args_of(r))
kurang = {t: jumlah_tool[t] for t in TOOL_RUNTIME if jumlah_tool[t] < 25}
cek("setiap tool runtime punya >= 25 contoh di train", not kurang, str(kurang))
kurang_test = [t for t in TOOL_RUNTIME if not any(n == t for r in rows["test"] for n, _ in args_of(r))]
cek("setiap tool runtime diuji di split test", not kurang_test, str(kurang_test))

# 7. Aturan v6: keluhan -> send_apology, eskalasi hanya kue custom, tanpa tawaran admin
KELUHAN = ("basi", "kecewa", "salah kirim", "telat", "komplain", "penyok", "rusak",
           "ga sesuai", "tidak sesuai", "stale", "damaged", "late", "wrong cake", "disappointed")
salah_esc = [(s, pisah_konteks(user_akhir(r))[1][:50]) for s in rows for r in rows[s]
             for n, _ in args_of(r) if n == "escalate_to_admin"
             and any(k in pisah_konteks(user_akhir(r))[1].lower() for k in KELUHAN)]
cek("tidak ada keluhan yang di-escalate", not salah_esc, str(salah_esc[:3]))
t14 = [s for s in rows for r in rows[s] if r["meta"]["type"] == "T14"
       and (not args_of(r) or args_of(r)[0][0] != "send_apology")]
cek("semua keluhan (T14) memanggil send_apology", not t14, str(t14[:2]))
TAWARAN = ("sambungkan ke admin", "kusambungkan", "teruskan ke admin", "dibantu admin",
           "connect you with our admin", "forward you to our admin", "nanti kuteruskan")
nawar = [(s, r["meta"]["type"]) for s in rows for r in rows[s] if not args_of(r)
         and "custom" not in r["messages"][-1]["content"].lower()
         and any(k in r["messages"][-1]["content"].lower() for k in TAWARAN)]
cek("tidak ada tawaran admin di luar kue custom", not nawar, str(nawar[:3]))


# 8. Aturan v7 (QA E2E 18-19 Sep)
def langgar(t):
    return [(s, args_of(r)) for s in rows for r in rows[s]
            if r["meta"]["type"] == t and args_of(r)]


cek("N10: bukan Owner minta laporan -> teks, tanpa tool", not langgar("N10"))
cek("N10: baris itu tidak ditawari tool Owner",
    all("financial_report" not in r["tools_json"] for s in rows for r in rows[s]
        if r["meta"]["type"] == "N10"))
cek("N12: pesan pendek ('order', 'oke gas', '1') tidak memanggil tool", not langgar("N12"))
cek("N11: ganti cara bayar -> teks, tanpa tool", not langgar("N11"))
t15 = [r for r in rows["train"] if r["meta"]["type"] == "T15"]
t16 = [r for r in rows["train"] if r["meta"]["type"] == "T16"]
cek("T15 -> check_cart", bool(t15) and all(args_of(r)[0][0] == "check_cart" for r in t15),
    str(len(t15)))
cek("T16 -> resend_payment_method, selalu sesudah tagihan terbit",
    bool(t16) and all(args_of(r)[0][0] == "resend_payment_method"
                      and any("Pesanan kamu sudah dibuat" in (m.get("content") or "")
                              for m in r["messages"][1:-1]) for r in t16), str(len(t16)))
aneh = [(s, r["messages"][-1]["content"][:50]) for s in rows for r in rows[s] if not args_of(r)
        and re.search(r"#|KONTEKS|Pertanyaan pelanggan", r["messages"][-1]["content"])]
cek("balasan bebas penanda internal (#FAQ_…, KONTEKS)", not aneh, str(aneh[:2]))

# 9. FAQ sebagai keterampilan membaca konteks
# Kata fungsi, bukan kata fakta. Pemeriksaan ini menanyakan "apakah FAKTA di
# jawaban ada di konteks", jadi kata perangkai tidak boleh ikut dihitung: tanpa
# tambahan v8 di baris kedua, parafrase pendek yang benar-benar bersumber dari
# konteks ("Bisa lewat QRIS saja kak — semuanya non-tunai") dihitung salah
# hanya karena "lewat" dan "semuanya" tidak muncul di dokumen.
STOP = set("yang di ke dari dan atau untuk pada dengan kami kamu kak ya adalah bisa juga "
           "itu ini nya akan sudah belum tidak ga hanya saja kalau jika hari jam "
           "lewat semuanya semua cuma aja pakai buat dulu nanti langsung silakan "
           "tinggal mohon maaf kok deh sih pun agar supaya".split())
tak_berdasar = []
for s in rows:
    for r in rows[s]:
        if r["meta"]["type"] != "N1" or r["meta"]["lang"] != "id":
            continue
        docs, _ = pisah_konteks(user_akhir(r))
        kata = [w for w in re.findall(r"[a-z0-9.\-]+", r["messages"][-1]["content"].lower())
                if w not in STOP and len(w) > 2]
        teks = " ".join(docs).lower()
        if not docs or sum(w in teks for w in kata) / max(1, len(kata)) < 0.6:
            tak_berdasar.append((s, r["messages"][-1]["content"][:50]))
cek("jawaban N1 bersumber dari konteks di baris itu", not tak_berdasar, str(tak_berdasar[:2]))
jawab_per_tanya = defaultdict(set)
for r in rows["train"]:
    if r["meta"]["type"] == "N1":
        for d in pisah_konteks(user_akhir(r))[0]:
            q, a = d.split("\nA: ", 1)
            jawab_per_tanya[q].add(a)
satu_versi = [q for q, a in jawab_per_tanya.items() if len(a) < 2]
cek("fakta tandingan: tiap dokumen FAQ muncul dengan >= 2 versi jawaban", not satu_versi,
    str(satu_versi[:2]))
tanpa_jawab = [r for r in rows["train"] if r["meta"]["type"] == "N1x"]
cek("ada baris konteks-tanpa-jawaban (N1x) -> jujur belum tahu", len(tanpa_jawab) >= 30,
    str(len(tanpa_jawab)))
doc_test = [t["doc_q"] for t in faq_topik.TOPIK_TEST.values()]
bocor_topik = [s for s in ("train", "validation") for r in rows[s]
               if any(q in user_akhir(r) for q in doc_test)]
cek("topik FAQ khusus test tidak muncul di train/val", not bocor_topik, str(len(bocor_topik)))
cek("split test memakai topik FAQ khusus test",
    any(q in user_akhir(r) for r in rows["test"] for q in doc_test))

# 10. Kebocoran & duplikat
tanya_test = {pisah_konteks(user_akhir(r))[1] for r in rows["test"]}
bocor = [t for s in ("train", "validation") for r in rows[s]
         for t in [pisah_konteks(user_akhir(r))[1]] if t in tanya_test]
cek("tidak ada teks pelanggan test yang bocor ke train/val", not bocor, str(bocor[:2]))
dup = [t for t, n in Counter(pisah_konteks(user_akhir(r))[1] for r in rows["train"]).items() if n > 1]
cek("tidak ada teks pelanggan kembar di train", not dup, f"{len(dup)} kembar")

print("\n".join(catat))
print()
if gagal:
    print("\n".join(gagal))
    print(f"\n=> {len(gagal)} PEMERIKSAAN GAGAL")
    sys.exit(1)
print(f"=> semua {len(catat)} pemeriksaan lolos")
