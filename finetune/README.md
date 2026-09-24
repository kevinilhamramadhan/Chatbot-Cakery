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
  - 1K<n<10K
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

# Toti Cakery — Tool-Calling Fine-Tuning Dataset (Qwen3, v8)

Synthetic bilingual (Indonesian ~78% / English ~22%) SFT dataset for the Toti
Cakery WhatsApp chatbot: **13 LangChain tools** (11 for customers, +2 owner-only
reports) and grounded answers from **RAG FAQ context**. Rows are built from the
live runtime code (`SYSTEM_PROMPT`, `TOOL_REMINDER`, tool schemas via
`convert_to_openai_tool`, `_history_view`, `pertanyaan_dengan_konteks`), so the
training prompt is byte-identical to what the model receives in production.

## What v8 changes (QA 24–25 Sep 2026)

1. **The session's language is stated in the system block.** The runtime already
   decides the conversation language and makes it sticky, but v7 never told the
   model — the prompt asked it to guess from the customer's text each turn. On
   turns with no language signal at all ("ok", "iya", "sure") the guess missed:
   an Indonesian conversation answered *"Still here 😊 Type \*menu\* untuk lihat
   daftarnya ya"*, 3 of 6 times at temperature 0.7 and still 1 of 13 at 0.3.
   Every v8 row carries `bahasa.arahan(lang)` at the end of the system block,
   rendered by the same runtime function, matching `meta.lang`.
2. **Filler turns are actually trained.** The failing class was the thinnest in
   the data: 29 short (≤2 word) mid-conversation turns out of 1540 rows, and
   **zero** bare fillers — every v7 "ack" row still carried another word that
   gave the language away. v8: 53 mid-conversation filler turns, with bare
   `ok`/`oke`/`iya`/`sip`/`sure`/`noted`/`k` in both languages.
3. **Language adherence is measured.** The eval harness reports
   `wrong_language_rate` (share of text replies that miss the session language);
   v7's metrics were all tool-related, so the bug customers actually saw could
   not be caught. Three smoke cases cover turns with no language signal.
4. **Recipe unchanged on purpose.** Same hyperparameters as v6/v7 (2 epochs,
   lr 2e-4, LoRA r/alpha 16), so v7 → v8 is a clean data-only comparison.

Details: `PROMPT_FINETUNE_V8.md` in the chatbot repo. Earlier revisions are kept
as branches: `v5`, `v6`, `v7`.

## What v7 changed (QA end-to-end, 18–19 Sep 2026)

1. **FAQ context where production puts it.** The runtime prepends retrieved FAQ
   to the customer's message (`KONTEKS FAQ … Pertanyaan pelanggan: …`) on ~65%
   of turns. v6 had it on 7% of rows and inside the system block. v7: 66% of
   rows, tool rows included (the model learns to ignore irrelevant context).
2. **FAQ as a reading skill, not memory.** FAQ documents are rendered from fact
   slots (opening hours, payment deadline, deposit %, …), so the same question
   has different correct answers on different rows — only reading the context
   gets it right. Rows with context that does NOT contain the answer teach
   "I don't have that info yet". Two FAQ topics exist only in `test`. When the
   shop edits its FAQ, the model follows the new text.
3. **Every tool is trained.** `check_cart` and `resend_payment_method` had zero
   rows up to v6. v7 adds them (T15/T16) plus near-negatives seen in QA
   (non-owner asking for reports, changing payment method after the invoice,
   short messages like "order"/"oke gas"/"1", English meta questions).
4. **Tool schemas match runtime:** `get_menu` takes no arguments (v6 still
   trained a `kategori` argument).
5. **Test split regenerated** from held-out templates, products and FAQ topics
   (no longer frozen since v1); all 13 tools are tested.

## Row format

```json
{"messages": [
   {"role": "system", "content": "<SYSTEM_PROMPT>\n\n<TOOL_REMINDER><ARAHAN_BAHASA>"},
   {"role": "user", "content": "menu dong"},
   {"role": "assistant", "content": "[Aku sudah menampilkan daftar menu via tool get_menu]"},
   {"role": "user", "content": "KONTEKS FAQ (jawab pertanyaan umum berdasarkan ini):\nQ: …\nA: …\n\nPertanyaan pelanggan: lapis legit premium 1"},
   {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {
       "name": "add_to_cart", "arguments": "{\"items\":[{\"product\":\"lapis legit premium\",\"qty\":1}]}"}}]}
 ],
 "tools_json": "<11 customer tools, or 13 for owner-report rows>",
 "meta": {"type": "T5", "lang": "id", "multi_turn": true, "noised": false, "konteks": 1}}
```

- **Single-pass tool calling**: rows end at the assistant `tool_calls` turn
  (production returns tool output verbatim; no second LLM pass).
- `arguments` is a JSON string — parse it before `apply_chat_template`.
- Tool arguments are the customer's words verbatim; the code-side resolver
  decides or asks.
- **Train on the FINAL assistant turn only** (history assistant turns are context).

## Splits

| Split | Rows | Purpose |
|---|---|---|
| `train` | 1600 | weight updates |
| `validation` | 162 | same distribution as train — `eval_dataset` for val-loss / early stopping |
| `test` | 166 | held-out templates (last ~15% of every pool), 2 products (`Cake 22cm`, `Giant Cookies 15cm`), 1 flavour (`Matcha`), 2 FAQ topics — evaluation only |

Types (train): tool rows T1–T16 (menu, detail, compare, order, multi-item,
follow-up quantity, order status, cancel, custom-cake escalation, owner
reports, payment claim, complaint, **cart**, **resend payment**); non-tool rows
N1 (FAQ from context, 150), **N1x** (context without the answer, 40), N2–N13
(no-info, greetings, out-of-scope, clarifying questions, adversarial, prompt
injection, short hails, human/negotiation, **non-owner reports**, **payment
change**, **short ambiguous messages**, **language**). Exact counts in
`data/stats.json`.

`finetune/audit_dataset.py` runs 34 checks against the RUNTIME code (system
block, FAQ placement, tool list per role, argument schemas, every tool covered,
behaviour rules, grounded FAQ answers, counterfactual facts, leakage). v7 passes
all; the v6 dataset fails 16 of them.

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

The Colab notebook `finetune/finetune_toti_qwen3.ipynb` measures the base model
and the fine-tuned model on `test` under identical conditions (production
prompt, sampling 0.7/0.8, 192-token cap = production `num_predict`), prints the
before/after comparison table, and uploads it with the GGUF to the model repo
(`perbandingan_v7.md` / `.json`, also embedded in the model card). The final
verdict is the end-to-end QA on the deployed stack.

## Provenance

Generated deterministically (seed 42) by `finetune/generate_dataset.py` in the
project repo — templates + slot-filling over the real shop menu; no LLM was
used to synthesize rows. Independently audited (structure, §-rules, behavior,
statistics, leakage) before every release. v4 rules distilled from live
WhatsApp failure analysis (`finetune/PROMPT_FINETUNE_V4.md` … `PROMPT_FINETUNE_V7.md`).
