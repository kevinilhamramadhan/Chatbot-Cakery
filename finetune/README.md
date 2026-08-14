---
license: mit
language:
  - id
  - en
task_categories:
  - text-generation
tags:
  - function-calling
  - tool-use
  - indonesian
  - unsloth
  - qwen
  - chatbot
pretty_name: Toti Cakery WhatsApp Chatbot — Tool-Calling SFT
size_categories:
  - n<1K
configs:
  - config_name: default
    data_files:
      - split: train
        path: data/train.jsonl
      - split: validation
        path: data/validation.jsonl
      - split: test
        path: data/test.jsonl
---

# Toti Cakery — Tool-Calling Fine-Tuning Dataset (Qwen3, v4)

Synthetic bilingual (Indonesian ~79% / English ~21%) SFT dataset for a WhatsApp
cake-shop chatbot with **9 LangChain tools**. Goal: sharpen tool-calling
accuracy on a small model **without degrading conversational ability**
(~63% tool-call rows : ~37% conversation/refusal/clarification rows).

Built for the production system it serves — the system prompt, tool schemas
(generated from the live code via `convert_to_openai_tool`), and single-pass
serving contract are **bit-identical to runtime** (Ollama + `ChatOllama.bind_tools`).

**v4 (2026-07-16)** closes four failure modes observed in live WhatsApp testing
of the v3 model (menu hallucinated from memorized training prices, detail
questions mis-routed to `get_menu`, history mimicry instead of tool calls,
guessed product variants):

1. **No menu/price/product content in any TRAINED assistant answer.** Data
   questions end at the tool call — there is nothing to memorize.
2. **History mirrors production exactly**: past bot replies enter the context
   through the runtime's `_history_view()` — menu/detail replies become compact
   markers (`[Aku sudah menampilkan daftar menu via tool get_menu]`), long text
   is truncated. Many rows carry such "contaminated" marker history and the
   assistant must STILL call the tool.
3. **Tool arguments are the customer's words verbatim** ("beli 4 cupcake" →
   `{"product": "cupcake", "qty": 4}`). The code-side resolver decides or asks;
   the model never invents a variant name.
4. **The runtime routing reminder is baked into the end of the system content**
   (joined with "\n\n"), exactly how Ollama collates the runtime's second
   SystemMessage into the top system block.

## Row format

```json
{"messages": [
   {"role": "system", "content": "<production system prompt>[ + KONTEKS FAQ block] + \n\n + <TOOL_REMINDER>"},
   {"role": "user", "content": "menu dong"},
   {"role": "assistant", "content": "[Aku sudah menampilkan daftar menu via tool get_menu]"},
   {"role": "user", "content": "beli 4 cupcake"},
   {"role": "assistant", "content": "",
    "tool_calls": [{"type": "function", "function": {
       "name": "add_to_cart",
       "arguments": "{\"items\":[{\"product\":\"cupcake\",\"qty\":4}]}"}}]}
 ],
 "tools_json": "<the 9 tool JSON schemas, serialized>",
 "meta": {"type": "T5", "lang": "id", "multi_turn": true, "noised": false}}
```

Key properties:

- **Single-pass tool calling**: rows end at the assistant `tool_calls` turn
  (production returns tool outputs verbatim; there is no second LLM pass, so
  there are no `tool` role turns to learn).
- `arguments` is a **JSON string** (parse it before `apply_chat_template` — see
  the Colab cell below). `tools_json` is a string for Arrow-schema stability.
- **Train on the FINAL assistant turn only.** History assistant turns (markers,
  chat small talk, cart summaries) are context, not targets — unmask only the
  last assistant segment. Training every assistant turn is how v3 learned to
  hallucinate menus (`train_on_responses_only` alone unmasks all of them).
- Non-tool rows teach the decision boundary: grounded FAQ answers, greeting
  small talk, out-of-scope refusals, truly-unresolvable ambiguity → clarifying
  question ("mau pesan kue" with no product word; "yang kedua" after a marker
  history where the list is no longer visible), and adversarial near-negatives
  ("cara batalin gimana?" must NOT call `cancel_order`).

## Splits

| Split | Rows | Purpose |
|---|---|---|
| `train` | 1005 | weight updates |
| `validation` | 102 | same distribution as train (iid) — pass as `eval_dataset` to monitor val-loss / early stopping |
| `test` | 100 | **held-out & FROZEN since v1**: ~15% of phrasing templates, 2 products (`Cake 22cm`, `Giant Cookies 15cm`), 1 flavour (`Matcha`), and 2 FAQ docs appear ONLY here. Used by the functional eval harness, never during training. Note: test rows still store the v1 message format — the eval harness reassembles them runtime-style at replay |

Composition (train, **v4**): tool rows 630 (T1 menu 70, T2 kategori 30, T3
detail 90, T4 compare 40, T5 single order 140 — incl. ~25% generic verbatim
orders, T6 multi-item 35, T7 follow-up 25, T8 status 50, T9 cancel 30, T10
escalate 60, T11/T12 owner reports 30+30); non-tool 375 (N1 FAQ-grounded 90,
N2 no-info 25, N3 greetings 50, N4 out-of-scope 60, N5 clarify 70, N6
adversarial 80). Validation keeps the same proportions. Zero verbatim (even
punctuation-normalized) **final user-message** overlap across splits.

**v4 (2026-07-16)**: marker history + verbatim args + reminder-in-system (the
four rules above); detail T3 70→90, single order T5 90→140 (absorbs the former
under-specified-order clarify cases — the resolver asks now), clarify N5
110→70, follow-up T7 35→25. **v3 (2026-07-06)**: T5 60→90, owner reports T11
12→30 & T12 13→30. **v2 (2026-07-05)**: clarify N5 50→110, adversarial N6
45→80, escalate T10 35→60 (v1 models were trigger-happy on ambiguous requests —
fixed: false_tool 0.20→0.025). The `test` split is byte-identical to v1 across
all versions, so every eval stays comparable.

## Using with the Unsloth Colab (Qwen3)

```python
from datasets import load_dataset
import json

ds = load_dataset("LasagnaS/toti-cakery-toolcall")  # 3 splits

def to_text(ex):
    msgs = ex["messages"]
    for m in msgs:
        for tc in (m.get("tool_calls") or []):
            if isinstance(tc["function"]["arguments"], str):
                tc["function"]["arguments"] = json.loads(tc["function"]["arguments"])
    return {"text": tokenizer.apply_chat_template(
        msgs, tools=json.loads(ex["tools_json"]),
        tokenize=False, add_generation_prompt=False)}

ds = ds.map(to_text)
print(ds["train"][0]["text"][:2000])  # sanity: <tools> block + <tool_call> render
```

Training notes:

- Pass `eval_dataset=ds["validation"]` to `SFTTrainer` and watch val-loss
  (rising val-loss while train-loss falls = overfitting → stop earlier).
- Use `train_on_responses_only` (Unsloth util) with the Qwen instruction/
  response markers, **then additionally mask everything before the LAST
  `<|im_start|>assistant` segment** (final-turn-only, v4 rule). The ready-made
  pipeline — incl. this masking, render sanity checks, and identical
  before/after eval — is `finetune/finetune_toti_qwen3.ipynb` in the project repo.
- Suggested start: LoRA r=16, alpha=16, lr=2e-4, 2 epochs, effective batch 8.

## After training: export + measure

1. Export GGUF from Unsloth (`q4_k_m`, quantized from the f16 source), create
   the Ollama model with the **same chat template as the base model** (a
   mismatched template is the #1 cause of "worse after export"), e.g.
   `ollama create toti-qwen-1.7b-v4 -f Modelfile.qwen3-1.7b-v4`.
2. Run the functional eval harness (BFCL-style) on the held-out test split
   against the REAL serving path, plus the regression scenario suite:

```bash
python finetune/eval_tool_calling.py --model toti-qwen-1.7b-v4
python finetune/scenario_suite.py    --model toti-qwen-1.7b-v4   # + regresi R1-R6
```

Metrics reported: function-selection accuracy, parameter match (product
references are compared by resolution, so verbatim v4 output and canonical
gold both count), invalid-call rate, irrelevance detection, false-tool-call
rate — per category and aggregate, plus a regression pass/fail gate for the
live incidents (incl. marker-contaminated history). Compare against the
same-harness baseline; that comparison (not training loss) is the verdict.
Then point the chatbot's `.env` `LLM_MODEL` at the new model.

## Provenance

Generated deterministically (seed 42) by `finetune/generate_dataset.py` in the
project repo — templates + slot-filling over the real shop menu; no LLM was
used to synthesize rows. Independently audited (structure, §-rules, behavior,
statistics, leakage) before every release. v4 rules distilled from live
WhatsApp failure analysis (`finetune/PROMPT_FINETUNE_V4.md` §2–§3).
