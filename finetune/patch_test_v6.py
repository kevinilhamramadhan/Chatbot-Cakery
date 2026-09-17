#!/usr/bin/env python3
"""Selaraskan 3 baris split `test` dengan aturan v6 (sekali jalan, idempoten).

Split `test` dibekukan sejak v1 supaya angka antar-versi sebanding, dan
`generate_dataset.py` memang tidak pernah menulis ulang file ini. Tapi v6
mengubah dua aturan yang kebetulan ada di dalamnya:

  - keluhan pelanggan BUKAN lagi escalate_to_admin, melainkan send_apology;
  - nego harga / pesanan kantor jumlah besar BUKAN lagi alasan eskalasi
    (keputusan 11 Sep 2026: eskalasi khusus pesanan kue custom).

Dibiarkan apa adanya, v6 akan dihitung SALAH justru pada perilaku yang sengaja
diubah. Yang disentuh hanya giliran assistant terakhir + meta.type pada 3 baris;
97 baris lain tetap byte-identik, jadi perbandingan dengan v3-v5 masih berlaku
dengan satu catatan kaki.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chatbot-service"))

from langchain_core.utils.function_calling import convert_to_openai_tool  # noqa: E402

from app.tools.registry import ALL_TOOLS, TOOLS_UMUM  # noqa: E402

PATH = ROOT / "finetune" / "data" / "test.jsonl"

# Daftar tool yang ditawarkan ke model saat evaluasi. Notebook memakai
# `tools_json` milik tiap baris, dan baris test masih membawa daftar 9 tool dari
# v1 — dua versi tertinggal di belakang runtime. Akibatnya tool yang lebih baru
# (termasuk send_apology) tidak pernah bisa dipanggil saat eval, sehingga dua
# baris keluhan di bawah mustahil dijawab benar. Daftar ini disegarkan dari kode
# runtime, sama seperti yang dilakukan harness lokal. Tool Owner hanya tersedia
# di baris evaluasi Owner (T11/T12); pelanggan biasa menerima TOOLS_UMUM.
TOOLS_JSON = {
    False: json.dumps([convert_to_openai_tool(t) for t in TOOLS_UMUM], ensure_ascii=False),
    True: json.dumps([convert_to_openai_tool(t) for t in ALL_TOOLS], ensure_ascii=False),
}

KELUHAN = "Pelanggan komplain pesanan diduga salah kirim"

# Balasan lama yang menawarkan sambungan ke admin untuk pertanyaan biasa. Di v6
# tawaran itu dihapus (eskalasi khusus kue custom), dan meski teks baris non-tool
# TIDAK ikut dinilai harness — yang dihitung cuma "memanggil tool atau tidak" —
# membiarkannya berarti berkas acuan mengajarkan hal yang sudah tidak berlaku.
TAWARAN_ADMIN = ("mau dibantu admin", "sambungkan ke admin", "kusambungkan",
                 "kuteruskan ke admin", "forward you to our admin",
                 "connect you with our admin")
GANTI_TAWARAN = {
    "id": "Info soal itu belum aku pegang kak 🙏 Ada lagi yang bisa kubantu soal menu atau pesanan?",
    "en": "I'm not sure about that one 🙏 Happy to help with the menu or your order, though!",
}
JAWAB_NEGO = ("Harga kami mengikuti daftar menu ya kak 🙏 Tapi untuk pesanan banyak, "
              "sebutkan saja kuenya dan jumlahnya — nanti kubuatkan ringkasan totalnya 😊")


def tool_turn(nama: str, args: dict) -> dict:
    return {"role": "assistant", "content": "",
            "tool_calls": [{"type": "function",
                            "function": {"name": nama,
                                         "arguments": json.dumps(args, ensure_ascii=False)}}]}


def main() -> None:
    rows = [json.loads(l) for l in PATH.open(encoding="utf-8")]
    diubah = []
    disegarkan = sum(
        1 for row in rows
        if row["tools_json"] != TOOLS_JSON[row["meta"]["type"] in ("T11", "T12")]
    )
    for row in rows:
        row["tools_json"] = TOOLS_JSON[row["meta"]["type"] in ("T11", "T12")]
    for row in rows:
        teks = [m for m in row["messages"] if m["role"] == "user"][-1]["content"].lower()
        if row["meta"]["type"] not in ("T10", "T14", "N9"):
            continue
        akhir = row["messages"][-1]
        nama_tool = (akhir.get("tool_calls") or [{}])[0].get("function", {}).get("name")
        if "salah kirim" in teks and nama_tool != "send_apology":
            row["messages"][-1] = tool_turn("send_apology", {"keluhan": KELUHAN})
            row["meta"]["type"] = "T14"
            diubah.append(("keluhan -> send_apology", teks[:60]))
        elif "nego harga" in teks and akhir.get("tool_calls"):
            row["messages"][-1] = {"role": "assistant", "content": JAWAB_NEGO}
            row["meta"]["type"] = "N9"
            diubah.append(("nego -> jawab sendiri, tanpa tool", teks[:60]))

    # Balasan non-tool yang masih menawarkan admin.
    ditawar = 0
    for row in rows:
        akhir = row["messages"][-1]
        if akhir.get("tool_calls"):
            continue
        isi = (akhir.get("content") or "")
        if any(k in isi.lower() for k in TAWARAN_ADMIN):
            akhir["content"] = GANTI_TAWARAN[row["meta"].get("lang", "id")]
            ditawar += 1

    with PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    n_tool_umum = len(json.loads(TOOLS_JSON[False]))
    n_tool_owner = len(json.loads(TOOLS_JSON[True]))
    print(f"{len(rows)} baris test | tools_json disegarkan ke {n_tool_umum} tool pelanggan "
          f"/ {n_tool_owner} tool Owner "
          f"pada {disegarkan} baris")
    print(f"{ditawar} balasan yang menawarkan admin diganti")
    print(f"{len(diubah)} baris diselaraskan ke aturan v6:")
    for alasan, teks in diubah:
        print(f"  - {alasan}: {teks!r}")
    if not diubah:
        print("  (sudah sesuai — aman dijalankan berulang)")


if __name__ == "__main__":
    main()
