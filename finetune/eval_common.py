"""Shared helpers for the eval harnesses (v4): runtime-parity message assembly
and policy-neutral argument scoring.

Assembly: replay must mirror app/llm/agent.py run_agent() exactly — production
SYSTEM_PROMPT, history passed through _history_view() (markers/truncation),
TOOL_REMINDER as a second SystemMessage, then the question. v7 rows store the
final question exactly as the runtime builds it (FAQ context included, via
pertanyaan_dengan_konteks), so it is replayed verbatim. Ollama collates the
system messages into one top block, so the model sees byte-identical prompts.

Argument scoring: v4 trains VERBATIM customer words while the frozen test split
stores canonical gold names. Both refer to the same product, so product fields
are compared by resolution (offline replica of app/tools/formatting.py
resolve_product over the real menu) instead of string equality. Everything
else stays exact-match.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chatbot-service"))
sys.path.insert(0, str(ROOT / "finetune"))

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

from app.llm.agent import _history_view  # noqa: E402
from app.llm.prompt import SYSTEM_PROMPT, TOOL_REMINDER  # noqa: E402
from app.conversation import bahasa  # noqa: E402
from app.tools.formatting import _tokens  # noqa: E402

from generate_dataset import MENU  # noqa: E402  (single menu truth)

# ── Runtime-parity assembly ───────────────────────────────────────────────────

def _arahan_baris(system_msg: dict) -> str:
    """Arahan bahasa yang dipakai baris ini, diambil dari blok system-nya."""
    isi = system_msg.get("content", "")
    return bahasa.arahan(bahasa.EN if isi.endswith(bahasa.arahan(bahasa.EN)) else bahasa.ID)


def to_lc_messages(messages: list[dict]):
    """messages = row["messages"][:-1] (everything up to the gold turn)."""
    out = [SystemMessage(content=SYSTEM_PROMPT)]
    body = messages[1:]
    hist, question = body[:-1], body[-1]
    assert question["role"] == "user", "last pre-gold message must be the user question"
    for m in hist:
        if m["role"] == "user":
            out.append(HumanMessage(content=m["content"]))
        else:
            c = m.get("content") or ""
            # _history_view is idempotent except on already-truncated text
            out.append(AIMessage(content=c if c.endswith("…(dipotong)") else _history_view(c)))
    # Bahasa baris dibaca dari blok system-nya sendiri (dataset menempelkannya
    # di sana), supaya eval merakit prompt yang sama persis dengan runtime.
    out.append(SystemMessage(content=TOOL_REMINDER + _arahan_baris(messages[0])))
    out.append(HumanMessage(content=question["content"]))
    return out


def gold_of(row: dict):
    final = row["messages"][-1]
    if final.get("tool_calls"):
        fn = final["tool_calls"][0]["function"]
        raw = fn["arguments"]
        return fn["name"], (json.loads(raw) if isinstance(raw, str) else raw)
    return None, None


# ── Product-reference matching (policy-neutral) ──────────────────────────────
# Canonical labels = real menu × flavours (plus bare product names, which the
# older gold labels use when no flavour was specified).
LABELS = []
for _p, (_cat, _flavs) in MENU.items():
    LABELS.append(_p)
    LABELS.extend(f"{_p} {_f}" for _f in _flavs)

_FOLD = {"vanila": "vanilla", "coklat": "cokelat", "chocolate": "cokelat",
         "choco": "cokelat", "kukis": "cookie"}
_CM_RE = re.compile(r"\b(\d+)\s+cm\b")


def _norm_tokens(s) -> frozenset:
    s = _CM_RE.sub(r"\1cm", str(s).lower())
    return frozenset(_FOLD.get(t, t) for t in _tokens(s))


_LABEL_TOKENS = [(lbl, _norm_tokens(lbl)) for lbl in LABELS]


def _resolve(s) -> str | None:
    """Offline replica of resolve_product's token scoring: unique best label,
    or None when ambiguous/not found."""
    q = _norm_tokens(s)
    if not q:
        return None
    for lbl in LABELS:
        if lbl.lower() == str(s).strip().lower():
            return lbl
    best_score, best = -1.0, []
    for lbl, lt in _LABEL_TOKENS:
        ov = q & lt
        if not ov:
            continue
        score = len(ov) * 2 + len(ov) / len(lt) + len(ov) / len(q)
        if score > best_score + 1e-9:
            best_score, best = score, [lbl]
        elif score >= best_score - 1e-9:
            best.append(lbl)
    return best[0] if len(best) == 1 else None


def same_product_ref(a, b) -> bool:
    """True when two product strings refer to the same thing: identical after
    normalization, or both resolve to the same unique menu item."""
    na, nb = _norm_tokens(a), _norm_tokens(b)
    if na == nb:
        return True
    ra, rb = _resolve(a), _resolve(b)
    return ra is not None and ra == rb


def _canon(obj):
    if isinstance(obj, dict):
        return {k: _canon(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_canon(v) for v in obj]
    return obj


def args_match(name: str, pred: dict, gold: dict) -> bool:
    """Exact-match everywhere except product references, which are compared by
    resolution so canonical gold ("Cupcakes isi 6 Cokelat") and verbatim v4
    output ("cupcakes yang isi 6 cokelat") both count as correct."""
    pred, gold = pred or {}, gold or {}
    try:
        if name == "add_to_cart":
            p_items, g_items = pred.get("items") or [], gold.get("items") or []
            if set(pred) != {"items"} or len(p_items) != len(g_items):
                return False
            remaining = list(p_items)
            for g in g_items:
                hit = next((p for p in remaining
                            if isinstance(p, dict)
                            and same_product_ref(p.get("product", ""), g["product"])
                            and int(p.get("qty", -1)) == int(g["qty"])), None)
                if hit is None:
                    return False
                remaining.remove(hit)
            return True
        if name == "get_product_detail":
            return set(pred) == {"product"} and same_product_ref(pred["product"], gold["product"])
        if name == "compare_products":
            p_list, g_list = pred.get("products") or [], gold.get("products") or []
            if set(pred) != {"products"} or len(p_list) != len(g_list):
                return False
            remaining = list(p_list)
            for g in g_list:
                hit = next((p for p in remaining if same_product_ref(p, g)), None)
                if hit is None:
                    return False
                remaining.remove(hit)
            return True
    except (TypeError, ValueError, KeyError):
        return False
    return _canon(pred) == _canon(gold)


if __name__ == "__main__":  # self-check
    assert same_product_ref("cupcakes yang isi 6 cokelat", "Cupcakes isi 6 Cokelat")
    assert same_product_ref("cake 22 cm vanilla", "Cake 22cm Vanilla")
    assert same_product_ref("bento cookies", "Bento Cookies 10cm")
    assert same_product_ref("cupcake", "cupcakes")
    assert not same_product_ref("cupcake", "Cupcakes isi 6 Cokelat")
    assert not same_product_ref("cake 15cm", "Cake 18cm")
    assert args_match("add_to_cart",
                      {"items": [{"product": "cupcake isi 9 rasa coklat", "qty": 2}]},
                      {"items": [{"product": "Cupcakes isi 9 Cokelat", "qty": 2}]})
    assert not args_match("add_to_cart",
                          {"items": [{"product": "cupcake isi 9 rasa coklat", "qty": 1}]},
                          {"items": [{"product": "Cupcakes isi 9 Cokelat", "qty": 2}]})
    assert args_match("get_menu", {}, {})
    tanya = "KONTEKS FAQ (jawab pertanyaan umum berdasarkan ini):\nQ: a\nA: b\n\nPertanyaan pelanggan: ada menu apa aja?"
    msgs = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n" + TOOL_REMINDER},
            {"role": "user", "content": "menu dong"},
            {"role": "assistant", "content": "Berikut menu Toti Cakery:\n• X — Rp10.000"},
            {"role": "user", "content": tanya}]
    lc = to_lc_messages(msgs)
    assert lc[0].content == SYSTEM_PROMPT
    assert lc[2].content == "[Aku sudah menampilkan daftar menu via tool get_menu]"
    assert lc[-2].content == TOOL_REMINDER + bahasa.arahan(bahasa.ID)
    assert lc[-1].content == tanya
    msgs_en = [dict(msgs[0], content=msgs[0]["content"] + bahasa.arahan(bahasa.EN))] + msgs[1:]
    assert to_lc_messages(msgs_en)[-2].content == TOOL_REMINDER + bahasa.arahan(bahasa.EN)
    print("eval_common self-check OK")
