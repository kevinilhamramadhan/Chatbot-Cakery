#!/usr/bin/env python3
"""Selaraskan 3 baris split `test` dengan aturan v6 (sekali jalan, idempoten).

Split `test` dibekukan sejak v1 supaya angka antar-versi sebanding, dan
`generate_dataset.py` memang tidak pernah menulis ulang file ini. Tapi v6
mengubah dua aturan yang kebetulan ada di dalamnya:

  - keluhan pelanggan BUKAN lagi escalate_to_admin, melainkan sampaikan_maaf;
  - nego harga / pesanan kantor jumlah besar BUKAN lagi alasan eskalasi
    (keputusan 11 Sep 2026: eskalasi khusus pesanan kue custom).

Dibiarkan apa adanya, v6 akan dihitung SALAH justru pada perilaku yang sengaja
diubah. Yang disentuh hanya giliran assistant terakhir + meta.type pada 3 baris;
97 baris lain tetap byte-identik, jadi perbandingan dengan v3-v5 masih berlaku
dengan satu catatan kaki.
"""

import json
from pathlib import Path

PATH = Path(__file__).resolve().parent / "data" / "test.jsonl"

KELUHAN = "Pelanggan komplain pesanan diduga salah kirim"
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
    for row in rows:
        teks = [m for m in row["messages"] if m["role"] == "user"][-1]["content"].lower()
        if row["meta"]["type"] not in ("T10", "T14", "N9"):
            continue
        akhir = row["messages"][-1]
        nama_tool = (akhir.get("tool_calls") or [{}])[0].get("function", {}).get("name")
        if "salah kirim" in teks and nama_tool != "sampaikan_maaf":
            row["messages"][-1] = tool_turn("sampaikan_maaf", {"keluhan": KELUHAN})
            row["meta"]["type"] = "T14"
            diubah.append(("keluhan -> sampaikan_maaf", teks[:60]))
        elif "nego harga" in teks and akhir.get("tool_calls"):
            row["messages"][-1] = {"role": "assistant", "content": JAWAB_NEGO}
            row["meta"]["type"] = "N9"
            diubah.append(("nego -> jawab sendiri, tanpa tool", teks[:60]))

    with PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"{len(rows)} baris test, {len(diubah)} diselaraskan ke aturan v6:")
    for alasan, teks in diubah:
        print(f"  - {alasan}: {teks!r}")
    if not diubah:
        print("  (sudah sesuai — aman dijalankan berulang)")


if __name__ == "__main__":
    main()
