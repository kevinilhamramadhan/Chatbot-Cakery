"""QA percakapan: dorong skenario lewat handle_message asli. Sesi TIDAK pernah di-reset."""
import asyncio, json, logging, sys, time, traceback

from app.whatsapp_client import client as wa_client
_SENT = []
async def _t(self, wa, text): _SENT.append(("text", wa)); return {"ok": True}
async def _i(self, wa, url, caption=None): _SENT.append(("image", wa)); return {"ok": True}
wa_client.WhatsAppClient.send_text = _t
wa_client.WhatsAppClient.send_image = _i

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from app.conversation import store                       # noqa: E402
from app.conversation.orchestrator import handle_message # noqa: E402
from app.core.database import init_db                    # noqa: E402
from app.tools.registry import ALL_TOOLS                 # noqa: E402
import app.llm.agent as agent_mod                        # noqa: E402

_CALLS = []
class _Spy:
    def __init__(self, t): self._t = t
    async def ainvoke(self, args, *a, **k):
        _CALLS.append((self._t.name, args))
        return await self._t.ainvoke(args, *a, **k)
agent_mod.TOOLS_BY_NAME = {t.name: _Spy(t) for t in ALL_TOOLS}

_RAG = []
class _Cap(logging.Handler):
    def emit(self, rec):
        m = rec.getMessage()
        if m.startswith("RAG best_sim"): _RAG.append(m)
lg = logging.getLogger("app.llm.agent"); lg.setLevel(logging.INFO); lg.addHandler(_Cap())


async def main():
    scenarios = json.load(open(sys.argv[1]))
    await init_db()
    out = []
    for sc in scenarios:
        wa = sc["wa"]
        print(f"\n{'#'*76}\n# {sc['id']}: {sc['title']}\n{'#'*76}", flush=True)
        turns = []
        for msg in sc["turns"]:
            t0 = time.time(); _CALLS.clear(); _RAG.clear(); _SENT.clear()
            try:
                reply = await handle_message(wa, msg)
                text = "[BUNGKAM — takeover aktif]" if reply.suppressed else (reply.text or "")
                media = [m.image_url for m in reply.media]; err = None
            except Exception:
                text, media, err = "", [], traceback.format_exc()
            dt = time.time() - t0
            s = await store.get_or_create_session(wa)
            o = await store.get_active_pending(wa)
            cart = [(c["nama"], c["qty"]) for c in json.loads(s.cart_json or "[]")]
            print(f"\n[{dt:5.1f}s] USER> {msg}")
            print("         BOT > " + text.replace("\n", "\n                "))
            if _CALLS: print(f"         TOOL> {_CALLS}")
            if media:  print(f"         IMG > {media}")
            if err:    print(f"         !!! {err}")
            print(f"         ~st={s.state} cart={cart} order={(o.order_ref, o.status) if o else None}")
            turns.append({"user": msg, "bot": text, "tools": [c[0] for c in _CALLS],
                          "args": [c[1] for c in _CALLS], "rag": list(_RAG),
                          "seconds": round(dt, 2), "state": str(s.state), "cart": cart,
                          "error": err})
        out.append({**{k: sc[k] for k in ("id", "title", "wa")}, "turns": turns})
        json.dump(out, open("/work/finetune/qa_live_v5.json", "w"), ensure_ascii=False, indent=1)
    print("\n=== SELESAI -> finetune/qa_live_v5.json ===")

asyncio.run(main())
