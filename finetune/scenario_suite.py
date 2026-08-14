#!/usr/bin/env python3
"""Timed, 3x-repeated behavioural scenario suite for the fine-tuned models.

For each held-out scenario TYPE (T1-T12 tool, N1-N6 non-tool) it picks a
representative row from the frozen test split and replays the user turn through
the SAME serving path as production (ChatOllama + bind_tools + production
sampling from config.py) N times (default 3), recording:

  - latency of every run (min / median / max)
  - the model's decision: tool call (name + args) or the cleaned text reply
  - correctness vs the gold label (right tool for tool rows; no tool for
    non-tool rows), and whether args exactly matched (tool rows)

Writes finetune/scenario_<model>.json and prints a readable per-scenario log.
This complements eval_tool_calling.py (which scores all 100 rows once for the
aggregate BFCL verdict) by showing behaviour + latency with repetition.
"""

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chatbot-service"))

from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_ollama import ChatOllama  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.llm.prompt import SYSTEM_PROMPT  # noqa: E402
from app.tools.registry import ALL_TOOLS  # noqa: E402

# v4: rakitan pesan paritas-runtime + skor argumen netral-kebijakan
from eval_common import args_match, gold_of, to_lc_messages  # noqa: E402

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Human labels + expectation per type (for the report).
TYPE_LABEL = {
    "T1":  "Lihat seluruh menu            -> get_menu",
    "T2":  "Menu per kategori             -> get_menu(kategori)",
    "T3":  "Detail 1 produk               -> get_product_detail",
    "T4":  "Bandingkan produk             -> compare_products",
    "T5":  "Pesan 1 item                  -> add_to_cart",
    "T6":  "Pesan banyak item sekaligus   -> add_to_cart",
    "T7":  "Pesan lanjutan (multi-turn)   -> add_to_cart",
    "T8":  "Status pesanan                -> get_order_status",
    "T9":  "Batalkan pesanan              -> cancel_order",
    "T10": "Eskalasi ke admin             -> escalate_to_admin",
    "T11": "Laporan keuangan (owner)      -> financial_report",
    "T12": "Analisa bisnis (owner)        -> business_analytics",
    "N1":  "FAQ (grounded)                -> jawab teks, TANPA tool",
    "N2":  "Permintaan di luar layanan    -> teks, TANPA tool",
    "N3":  "Basa-basi / terima kasih      -> teks, TANPA tool",
    "N4":  "Out-of-scope                  -> tolak, TANPA tool",
    "N5":  "Ambigu                        -> tanya balik, TANPA tool",
    "N6":  "Adversarial (jebakan)         -> teks, TANPA tool",
    "R1":  "Regresi: menu fresh           -> get_menu (bukan jawab hafalan)",
    "R2":  "Regresi: detail by name       -> get_product_detail (bukan get_menu)",
    "R3":  "Regresi: menu lagi (penanda)  -> get_menu (bukan meniru history)",
    "R4":  "Regresi: detail lain (penanda)-> get_product_detail produk BARU",
    "R5":  "Regresi: order generik        -> add_to_cart verbatim 'cupcake'",
    "R6":  "Regresi: menu + history cemar -> get_menu (persis insiden live #1)",
}

ORDER = [f"T{i}" for i in range(1, 13)] + [f"N{i}" for i in range(1, 7)]


# ── Skenario regresi 4 insiden live WA 15-16 Jul 2026 (PROMPT_FINETUNE_V4 §2) ──
# History ditulis sebagai OUTPUT RUNTIME MENTAH (menu literal, blok detail) —
# to_lc_messages menjalankannya lewat _history_view persis seperti produksi,
# sehingga model melihat penanda "history tercemar" yang sama dengan live.
def _tool_turn(name, args):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"type": "function", "function": {
                "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


_H_MENU = [
    {"role": "user", "content": "menu dong"},
    {"role": "assistant", "content": "Berikut menu Toti Cakery:\n"
     "• Cupcakes isi 6 Cokelat — Rp85.000\n• Bento Cookies 10cm Original — Rp55.000\n"
     "\nMau lihat detail salah satu kue? Sebutkan namanya ya 😊"},
]
_H_DETAIL = [
    {"role": "user", "content": "bento cookies kayak gimana ya?"},
    {"role": "assistant", "content": "*Bento Cookies 10cm Original*\n"
     "Favorit pelanggan — teksturnya lembut dengan rasa yang kaya.\nHarga: Rp55.000\n\n"
     "Mau pesan ini? Bilang aja jumlahnya ya 😊"},
]


def regression_rows():
    def row(rtype, user, gold, history=()):
        return rtype, {"messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                    *history, {"role": "user", "content": user}, gold],
                       "meta": {"type": rtype, "lang": "id"}}
    return [
        row("R1", "ada menu apa saja?", _tool_turn("get_menu", {})),
        row("R2", "bento cookies kayak gimana ya?",
            _tool_turn("get_product_detail", {"product": "bento cookies"})),
        row("R3", "tampilin menunya lagi dong", _tool_turn("get_menu", {}), _H_MENU),
        row("R4", "kalau mini cookies kayak gimana?",
            _tool_turn("get_product_detail", {"product": "mini cookies"}), _H_DETAIL),
        row("R5", "beli 4 cupcake",
            _tool_turn("add_to_cart", {"items": [{"product": "cupcake", "qty": 4}]})),
        row("R6", "ada menu apa saja?", _tool_turn("get_menu", {}), _H_MENU),
    ]


def clean_text(t):
    return _THINK_RE.sub("", t or "").strip()


def pick_rows(data_path):
    rows = [json.loads(l) for l in open(data_path, encoding="utf-8")]
    first = {}
    for r in rows:
        t = r["meta"]["type"]
        first.setdefault(t, r)
    return [(t, first[t]) for t in ORDER if t in first] + regression_rows()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--data", default=str(ROOT / "finetune/data/test.jsonl"))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--num-ctx", type=int, default=settings.llm_num_ctx)
    ap.add_argument("--out", default="", help="path output JSON (default: scenario_<model>.json)")
    args = ap.parse_args()

    llm = ChatOllama(
        base_url=settings.ollama_base_url,
        model=args.model,
        temperature=settings.llm_temperature,   # production parity (0.7)
        top_p=settings.llm_top_p,                # production parity (0.8)
        num_ctx=args.num_ctx,
        num_predict=settings.llm_num_predict,
    ).bind_tools(ALL_TOOLS)

    scenarios = pick_rows(args.data)
    print(f"\n{'=' * 72}\nMODEL: {args.model}   |   {len(scenarios)} skenario x {args.runs} run "
          f"(thinking ON, temp={settings.llm_temperature}/top_p={settings.llm_top_p})\n{'=' * 72}")

    # Warm-up: first call pays Ollama's cold model-load (~30-50s) — do it once,
    # untimed, so per-scenario latency reflects steady-state serving.
    tw = time.time()
    try:
        llm.invoke([HumanMessage(content="halo")])
    except Exception:
        pass
    print(f"(warm-up model load: {time.time() - tw:.1f}s — tidak dihitung)")

    results = []
    t_start = time.time()
    for t, row in scenarios:
        gname, gargs = gold_of(row)
        msgs = to_lc_messages(row["messages"][:-1])
        user_last = [m for m in row["messages"] if m["role"] == "user"][-1]["content"]
        runs = []
        for k in range(args.runs):
            t0 = time.time()
            try:
                resp = llm.invoke(msgs)
                dt = time.time() - t0
                calls = list(resp.tool_calls or [])
                if calls:
                    decided = f"TOOL {calls[0]['name']} {json.dumps(calls[0]['args'], ensure_ascii=False)}"
                    called_name = calls[0]["name"]
                    am = args_match(gname, calls[0]["args"], gargs) if gname else None
                else:
                    decided = "TEXT: " + (clean_text(resp.content)[:160] or "(kosong)")
                    called_name = None
                    am = None
            except Exception as exc:  # noqa: BLE001
                dt = time.time() - t0
                decided, called_name, am = f"ERROR: {exc}", "__error__", None
            # correctness
            if gname is None:
                ok = called_name is None            # non-tool: must NOT call a tool
            else:
                ok = (called_name == gname)          # tool: must call the right tool
            runs.append({"latency": round(dt, 2), "decided": decided,
                         "ok": ok, "args_match": am})

        lats = [r["latency"] for r in runs]
        oks = sum(r["ok"] for r in runs)
        argm = sum(1 for r in runs if r["args_match"]) if gname else None
        rec = {
            "type": t, "label": TYPE_LABEL.get(t, t), "lang": row["meta"]["lang"],
            "user": user_last, "gold": (f"{gname} {json.dumps(gargs, ensure_ascii=False)}"
                                        if gname else "(no tool)"),
            "runs": runs,
            "lat_min": round(min(lats), 2), "lat_med": round(statistics.median(lats), 2),
            "lat_max": round(max(lats), 2),
            "correct_runs": oks, "total_runs": args.runs,
            "args_match_runs": argm,
        }
        results.append(rec)
        tag = "OK " if oks == args.runs else ("~  " if oks else "XX ")
        extra = f" args={argm}/{args.runs}" if (gname and oks) else ""
        print(f"\n[{t:4}] {tag}{oks}/{args.runs} correct{extra}  "
              f"lat med={rec['lat_med']}s ({rec['lat_min']}-{rec['lat_max']}s)  {TYPE_LABEL.get(t,'')}")
        print(f"       USER: {user_last[:90]}")
        print(f"       GOLD: {rec['gold'][:90]}")
        for i, r in enumerate(runs):
            print(f"       run{i+1} {r['latency']:>5}s  {r['decided'][:120]}")

    # aggregate
    tool_recs = [r for r in results if not r["gold"].startswith("(no tool)")]
    non_recs = [r for r in results if r["gold"].startswith("(no tool)")]
    all_lat = [rr["latency"] for r in results for rr in r["runs"]]
    agg = {
        "model": args.model,
        "runs_per_scenario": args.runs,
        "scenarios": len(results),
        "tool_selection_correct_runs": sum(r["correct_runs"] for r in tool_recs),
        "tool_selection_total_runs": sum(r["total_runs"] for r in tool_recs),
        "args_exact_runs": sum((r["args_match_runs"] or 0) for r in tool_recs),
        "non_tool_correct_runs": sum(r["correct_runs"] for r in non_recs),
        "non_tool_total_runs": sum(r["total_runs"] for r in non_recs),
        "latency_overall_med": round(statistics.median(all_lat), 2),
        "latency_overall_mean": round(statistics.mean(all_lat), 2),
        "total_seconds": round(time.time() - t_start, 1),
        "per_scenario": results,
    }
    out = Path(args.out) if args.out else (
        ROOT / "finetune" / f"scenario_{args.model.replace(':', '_').replace('/', '_')}.json")
    out.write_text(json.dumps(agg, indent=2, ensure_ascii=False))

    # Gerbang rilis v4 (§5): SEMUA skenario regresi harus lolos di semua run.
    reg = [r for r in results if r["type"].startswith("R")]
    reg_ok = all(r["correct_runs"] == r["total_runs"] for r in reg)
    agg["regression_pass"] = reg_ok
    out.write_text(json.dumps(agg, indent=2, ensure_ascii=False))

    ts, tt = agg["tool_selection_correct_runs"], agg["tool_selection_total_runs"]
    ns, nt = agg["non_tool_correct_runs"], agg["non_tool_total_runs"]
    print(f"\n{'-' * 72}\nRINGKASAN {args.model}")
    print(f"  regresi insiden live  : {'LOLOS' if reg_ok else 'GAGAL'} "
          f"({sum(r['correct_runs'] for r in reg)}/{sum(r['total_runs'] for r in reg)} run, "
          f"gagal di: {[r['type'] for r in reg if r['correct_runs'] < r['total_runs']] or '-'})")
    print(f"  tool-selection benar : {ts}/{tt} run  ({ts/max(1,tt):.1%})")
    print(f"  args exact-match     : {agg['args_exact_runs']}/{tt} run  ({agg['args_exact_runs']/max(1,tt):.1%})")
    print(f"  non-tool benar       : {ns}/{nt} run  ({ns/max(1,nt):.1%})")
    print(f"  latency (semua run)  : median {agg['latency_overall_med']}s | mean {agg['latency_overall_mean']}s")
    print(f"  total waktu          : {agg['total_seconds']}s")
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
