#!/usr/bin/env python3
"""Functional tool-calling eval harness (BFCL-style) against a local Ollama model.

Replays every row of the held-out test split through the SAME serving path as
production (ChatOllama + bind_tools + the real tool objects) and scores the
model's decision against the gold label:

- function_selection : called the right tool (tool rows)
- param_exact        : arguments exactly match gold (canonical product, int qty)
- format_validity    : model's tool call parsed cleanly (tool rows where a call
                       was attempted)
- irrelevance        : correctly did NOT call any tool (non-tool rows)
- false_tool_rate    : share of non-tool rows where a tool was called anyway

Usage (Ollama running locally, model pulled):
    python finetune/eval_tool_calling.py --model qwen3:1.7b        # base
    python finetune/eval_tool_calling.py --model toti-qwen-1.7b-v4 # after fine-tune

Writes finetune/results_<model>.json. Compare the two side by side for the
baseline-vs-finetuned verdict.
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chatbot-service"))

from langchain_ollama import ChatOllama  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.tools.registry import ALL_TOOLS  # noqa: E402

# v4: rakitan pesan paritas-runtime (marker history + reminder) & skor argumen
# yang netral-kebijakan (verbatim vs kanonik dinilai lewat resolusi produk).
from eval_common import args_match, gold_of, to_lc_messages  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Ollama model name, e.g. toti-qwen-1.7b-v4")
    ap.add_argument("--data", default=str(ROOT / "finetune/data/test.jsonl"))
    ap.add_argument("--limit", type=int, default=0, help="only first N rows (0 = all)")
    ap.add_argument("--out", default="", help="path output JSON (default: results_<model>.json)")
    ap.add_argument("--num-ctx", type=int, default=settings.llm_num_ctx,
                    help="context window; default ikut config produksi (paritas §5). "
                         "Prompt hanya ~2.5k token — turunkan (mis. 8192) kalau butuh cepat")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.data, encoding="utf-8")]
    if args.limit:
        rows = rows[: args.limit]

    llm = ChatOllama(
        base_url=settings.ollama_base_url,
        model=args.model,
        # Production sampling params (parity). NOTE: temperature 0/greedy makes
        # small models loop forever — do not "make it deterministic".
        temperature=settings.llm_temperature,
        top_p=settings.llm_top_p,
        num_ctx=args.num_ctx,
        num_predict=settings.llm_num_predict,  # hard cap: no runaway generations on CPU
    ).bind_tools(ALL_TOOLS)

    per_type = defaultdict(lambda: {"n": 0, "sel": 0, "param": 0, "irrelevant_ok": 0,
                                    "false_tool": 0, "invalid": 0})
    t0 = time.time()
    for i, row in enumerate(rows):
        rtype = row["meta"]["type"]
        gname, gargs = gold_of(row)
        msgs = to_lc_messages(row["messages"][:-1])  # everything up to the gold turn
        try:
            resp = llm.invoke(msgs)
            calls = list(resp.tool_calls or [])
            invalid = bool(getattr(resp, "invalid_tool_calls", None))
        except Exception as exc:  # noqa: BLE001 - count as invalid output
            calls, invalid = [], True
            print(f"  [{i}] invoke error: {exc}")
        st = per_type[rtype]
        st["n"] += 1
        if gname is None:
            if calls:
                st["false_tool"] += 1
            else:
                st["irrelevant_ok"] += 1
        else:
            if invalid and not calls:
                st["invalid"] += 1
            if calls and calls[0]["name"] == gname:
                st["sel"] += 1
                if args_match(gname, calls[0]["args"], gargs):
                    st["param"] += 1
        if (i + 1) % 5 == 0:
            print(f"  {i + 1}/{len(rows)} rows ({time.time() - t0:.0f}s)", flush=True)

    # aggregate
    tool_types = [t for t in per_type if t.startswith("T")]
    non_types = [t for t in per_type if t.startswith("N")]
    tool_n = sum(per_type[t]["n"] for t in tool_types)
    non_n = sum(per_type[t]["n"] for t in non_types)
    agg = {
        "model": args.model,
        "rows": len(rows),
        "function_selection_acc": round(sum(per_type[t]["sel"] for t in tool_types) / max(1, tool_n), 3),
        "param_exact_acc": round(sum(per_type[t]["param"] for t in tool_types) / max(1, tool_n), 3),
        "invalid_call_rate": round(sum(per_type[t]["invalid"] for t in tool_types) / max(1, tool_n), 3),
        "irrelevance_acc": round(sum(per_type[t]["irrelevant_ok"] for t in non_types) / max(1, non_n), 3),
        "false_tool_rate": round(sum(per_type[t]["false_tool"] for t in non_types) / max(1, non_n), 3),
        "per_type": {t: dict(per_type[t]) for t in sorted(per_type)},
        "seconds": round(time.time() - t0, 1),
    }

    out = Path(args.out) if args.out else (
        ROOT / "finetune" / f"results_{args.model.replace(':', '_').replace('/', '_')}.json")
    out.write_text(json.dumps(agg, indent=2))
    print(json.dumps({k: v for k, v in agg.items() if k != "per_type"}, indent=2))
    print("\nper-type (n / correct-tool / exact-args | non-tool: ok / false-call):")
    for t in sorted(per_type):
        s = per_type[t]
        if t.startswith("T"):
            print(f"  {t:4} n={s['n']:3}  sel={s['sel']:3}  param={s['param']:3}")
        else:
            print(f"  {t:4} n={s['n']:3}  ok={s['irrelevant_ok']:3}  false_tool={s['false_tool']:3}")
    print(f"\nsaved -> {out}")


if __name__ == "__main__":
    main()
