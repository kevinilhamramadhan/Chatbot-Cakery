"""Audit dataset v6: apakah isinya benar, dan benar-benar aturan v6?

22 pemeriksaan: jumlah baris, tipe baru, daftar tool = 13 tool runtime, paritas
system prompt, keabsahan argumen, aturan perilaku v6 (keluhan tidak dieskalasi,
tidak ada tawaran admin di luar kue custom), rujukan produk pada jawaban jumlah
polos, kebocoran test ke train, dan teks kembar.

Pakai:
    chatbot-service/.venv/bin/python finetune/audit_dataset.py finetune/data

Jalankan juga terhadap salinan yang DIUNDUH DARI HF sebelum training — itu yang
benar-benar dibaca notebook, dan berkas lokal bisa saja lebih baru:

    for s in train validation test; do
      curl -sSL "https://huggingface.co/datasets/LasagnaS/toti-cakery-toolcall/resolve/main/data/$s.jsonl" -o "/tmp/hf/$s.jsonl"
    done
    chatbot-service/.venv/bin/python finetune/audit_dataset.py /tmp/hf
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/home/kevin/clcode/chatbot/chatbot-service")
from langchain_core.utils.function_calling import convert_to_openai_tool  # noqa: E402
from app.llm.prompt import SYSTEM_PROMPT, TOOL_REMINDER  # noqa: E402
from app.tools.registry import ALL_TOOLS  # noqa: E402

DIR = Path(sys.argv[1])
TOOL_RUNTIME = {t["function"]["name"] for t in (convert_to_openai_tool(t) for t in ALL_TOOLS)}

rows = {}
for split in ("train", "validation", "test"):
    rows[split] = [json.loads(l) for l in (DIR / f"{split}.jsonl").open(encoding="utf-8")]

gagal, catat = [], []


def cek(nama, ok, detail=""):
    (catat if ok else gagal).append(f"{'OK  ' if ok else 'GAGAL'} | {nama}{' — ' + detail if detail else ''}")


# 1. Jumlah baris
for split, harap in (("train", 1255), ("validation", 129), ("test", 100)):
    cek(f"jumlah baris {split} = {harap}", len(rows[split]) == harap, str(len(rows[split])))

# 2. Tipe baru ada, dan tipe lama menyusut sesuai rencana
tipe = {s: Counter(r["meta"]["type"] for r in rows[s]) for s in rows}
cek("T14 (keluhan) ada di train", tipe["train"]["T14"] == 45, str(tipe["train"]["T14"]))
cek("N9 (minta orang/nego) ada di train", tipe["train"]["N9"] == 35, str(tipe["train"]["N9"]))
cek("T7 (jawaban jumlah) naik jadi 55", tipe["train"]["T7"] == 55, str(tipe["train"]["T7"]))
cek("T10 (eskalasi) turun jadi 30", tipe["train"]["T10"] == 30, str(tipe["train"]["T10"]))
cek("test memuat 2 baris keluhan", tipe["test"]["T14"] == 2, str(tipe["test"]["T14"]))
cek("test memuat 1 baris nego (tanpa tool)", tipe["test"]["N9"] == 1, str(tipe["test"]["N9"]))

# 3. Daftar tool yang ditawarkan = 13 tool runtime, di SEMUA split
for split in rows:
    salah = [i for i, r in enumerate(rows[split])
             if {t["function"]["name"] for t in json.loads(r["tools_json"])} != TOOL_RUNTIME]
    cek(f"tools_json {split} = 13 tool runtime", not salah, f"{len(salah)} baris menyimpang")

# 4. System prompt = prompt runtime + reminder (paritas latih-sajian)
harap_sys = SYSTEM_PROMPT + "\n\n" + TOOL_REMINDER
for split in ("train", "validation"):
    beda = [i for i, r in enumerate(rows[split])
            if not r["messages"][0]["content"].startswith(SYSTEM_PROMPT.rstrip()[:200])
            or not r["messages"][0]["content"].rstrip().endswith(TOOL_REMINDER.rstrip()[-80:])]
    cek(f"system block {split} = prompt runtime + reminder", not beda, f"{len(beda)} baris beda")

# 5. Argumen tool sah menurut skema runtime
def args_of(r):
    a = r["messages"][-1]
    return [(c["function"]["name"], json.loads(c["function"]["arguments"]))
            for c in (a.get("tool_calls") or [])]


tak_dikenal = [(s, n) for s in rows for r in rows[s] for n, _ in args_of(r) if n not in TOOL_RUNTIME]
cek("semua nama tool dikenal runtime", not tak_dikenal, str(tak_dikenal[:3]))

rusak = []
for s in rows:
    for r in rows[s]:
        for n, a in args_of(r):
            if n == "add_to_cart" and not (set(a) == {"items"} and a["items"]):
                rusak.append((s, n, a))
            if n == "sampaikan_maaf" and not (set(a) == {"keluhan"} and a["keluhan"]):
                rusak.append((s, n, a))
            if n == "escalate_to_admin" and not (set(a) == {"reason"} and a["reason"]):
                rusak.append((s, n, a))
cek("argumen add_to_cart/sampaikan_maaf/escalate valid", not rusak, str(rusak[:2]))

# 6. ATURAN v6: keluhan tidak boleh escalate; eskalasi hanya kue custom
KATA_KELUHAN = ("basi", "kecewa", "salah kirim", "telat", "komplain", "penyok",
                "rusak", "ga sesuai", "tidak sesuai", "stale", "damaged", "late",
                "wrong cake", "disappointed")
salah_escalate = []
for s in rows:
    for r in rows[s]:
        for n, a in args_of(r):
            if n != "escalate_to_admin":
                continue
            teks = [m for m in r["messages"] if m["role"] == "user"][-1]["content"].lower()
            if any(k in teks for k in KATA_KELUHAN) or "ngomong sama admin" in teks \
               or "speak to a human" in teks or "nego" in teks:
                salah_escalate.append((s, teks[:60]))
cek("tidak ada keluhan/minta-orang/nego yang di-escalate", not salah_escalate,
    str(salah_escalate[:3]))

# 7. Balasan teks tidak menawarkan sambungan ke admin
TAWARAN = ("sambungkan ke admin", "kusambungkan", "teruskan ke admin", "hubungkan kamu ke admin",
           "dibantu admin", "connect you with our admin", "forward you to our admin",
           "forward it to our admin", "nanti kuteruskan")
# Kue custom memang dieskalasi, jadi kalimat "pesanan custom diteruskan ke admin"
# di jawaban FAQ bukan pelanggaran — yang dilarang adalah menawarkan admin untuk
# pertanyaan biasa.
nawarkan = []
for s in rows:
    for r in rows[s]:
        a = r["messages"][-1]
        if a.get("tool_calls"):
            continue
        low = (a.get("content") or "").lower()
        if "custom" in low:
            continue
        if any(k in low for k in TAWARAN):
            nawarkan.append((s, r["meta"]["type"], low[:70]))
cek("tidak ada balasan yang menawarkan admin di luar kue custom", not nawarkan,
    str(nawarkan[:3]))

# 8. Baris jawaban jumlah polos: riwayat memuat penanda detail, produknya sama
polos = []
for r in rows["train"] + rows["validation"]:
    if r["meta"]["type"] != "T7":
        continue
    u = [m for m in r["messages"] if m["role"] == "user"][-1]["content"]
    if len(u.split()) > 3:
        continue
    hist = [m["content"] for m in r["messages"][:-1] if m["role"] == "assistant"]
    penanda = [h for h in hist if h.startswith("[Aku sudah menampilkan detail")]
    calls = args_of(r)
    ok = bool(penanda) and calls and calls[0][0] == "add_to_cart"
    if ok:
        produk_hist = penanda[-1].split("detail ")[1].split(" + ")[0]
        ok = calls[0][1]["items"][0]["product"] == produk_hist
    if not ok:
        polos.append((u, penanda[-1][:60] if penanda else "(tanpa penanda)", calls[:1]))
cek("jawaban jumlah polos merujuk produk di penanda riwayat", not polos, str(polos[:2]))

# 9. Keluhan selalu memakai sampaikan_maaf
keluhan_salah = [(s, args_of(r)) for s in rows for r in rows[s]
                 if r["meta"]["type"] == "T14"
                 and (not args_of(r) or args_of(r)[0][0] != "sampaikan_maaf")]
cek("semua baris T14 memanggil sampaikan_maaf", not keluhan_salah, str(keluhan_salah[:2]))

# 10. Tidak ada kebocoran: teks user test tidak muncul di train/val
teks_test = {tuple(m["content"] for m in r["messages"] if m["role"] == "user")[-1]
             for r in rows["test"]}
bocor = [t for s in ("train", "validation") for r in rows[s]
         for t in [[m["content"] for m in r["messages"] if m["role"] == "user"][-1]]
         if t in teks_test]
cek("tidak ada teks user test yang bocor ke train/val", not bocor, str(bocor[:2]))

# 11. Duplikat teks user di dalam train
dup = [t for t, n in Counter(
    [m["content"] for r in rows["train"] for m in r["messages"][-2:] if m["role"] == "user"]
).items() if n > 1]
cek("tidak ada teks user kembar di train", not dup, f"{len(dup)} kembar")

print("\n".join(catat))
print()
if gagal:
    print("\n".join(gagal))
    print(f"\n=> {len(gagal)} PEMERIKSAAN GAGAL")
    sys.exit(1)
print(f"=> semua {len(catat)} pemeriksaan lolos")
