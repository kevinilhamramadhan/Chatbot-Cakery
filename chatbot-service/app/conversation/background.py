"""Background worker: payment timeout + automatic paid-detection (PROMPT §10.9-10).

Runs on an interval (PAYMENT_CHECK_INTERVAL_SECONDS). For each pending order it:
- cancels + notifies if past PAYMENT_TIMEOUT_MINUTES,
- proactively notifies the customer once payment is detected as paid.

Also exposes notify_ready() for the "order is ready" proactive message (§10.13),
triggered via the internal endpoint since the backend status webhook is out of scope.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from app.backend_client import api as backend
from app.conversation import store
from app.conversation.states import State
from app.core.config import settings
from app.core.security import mask_phone
from app.tools.formatting import rupiah
from app.whatsapp_client.client import whatsapp_client

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _notify(wa_number: str, text: str) -> bool:
    """True kalau pesannya benar-benar terkirim. Pemanggilnya perlu tahu.

    Dulu kegagalan kirim cuma dicatat di log, dan pemanggil tetap menandai
    urusannya selesai — pelanggan tidak pernah diberi tahu, dan tidak ada yang
    mencoba lagi.
    """
    try:
        await whatsapp_client.send_text(wa_number, text)
        await store.log_message(wa_number, "out", text, intent="proactive")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to notify %s: %s", mask_phone(wa_number), exc)
        return False


def _label(order) -> str:
    """Name an order the way the customer was told about it.

    order_ref is the backend's internal id; checkout hands out the invoice
    number, so "Pesananmu *3* sudah siap" reads like a different order from the
    INV-… they were given. Falls back to the id for rows created before the
    invoice number was snapshotted.
    """
    return order.nomor_invoice or order.order_ref


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _teks_refund(order) -> str:
    """Kabar untuk pelanggan saat pesanannya di-refund admin."""
    baris = [
        f"Pesanan *{_label(order)}* dibatalkan dan pembayaranmu dikembalikan ✅",
        "Dananya kembali lewat metode pembayaran yang kamu pakai. Prosesnya bisa "
        "beberapa hari kerja tergantung bank atau e-wallet-nya ya 🙏",
    ]
    email = settings.store_support_email.strip()
    if email:
        baris.append(f"Kalau lewat dari itu belum masuk, kabari kami di {email}.")
    baris.append("Terima kasih sudah menunggu 😊")
    return "\n\n".join(baris)


async def tutup_karena_refund(order) -> bool:
    """Kabari pelanggan, lalu tutup pesanan lokalnya. Dipakai polling DAN webhook.

    Urutannya disengaja: kalau pesannya gagal terkirim (gateway WhatsApp mati,
    sesi putus), baris pesanannya TIDAK ditandai selesai — siklus polling
    berikutnya mencoba lagi. Kalau ditandai lebih dulu, pelanggan yang uangnya
    dikembalikan tidak akan pernah diberi tahu, dan tidak ada yang mengulang.
    """
    if order.status in ("cancelled", "refunded"):
        return False
    if not await _notify(order.wa_number, _teks_refund(order)):
        logger.warning("Kabar refund %s gagal terkirim — dicoba lagi siklus berikutnya",
                       order.order_ref)
        return False
    await store.update_pending_order(order.id, status="refunded")
    await store.set_state(order.wa_number, State.IDLE)
    logger.info("Pesanan %s di-refund — pelanggan sudah dikabari", order.order_ref)
    return True


async def notify_refunded(order_id: int) -> bool:
    """Dipanggil backend lewat webhook internal begitu refund selesai.

    Polling 30 detik tetap jalan sebagai jaring pengaman — kalau chatbot
    kebetulan sedang restart saat webhook dikirim, kabarnya tidak hilang, cuma
    telat paling lama setengah menit.
    """
    for order in await store.list_orders_by_status("pending", "paid", "ready"):
        if str(order.order_ref) != str(order_id):
            continue
        # Status refund-nya dipastikan dulu ke backend, sama seperti jalur
        # pembayaran: webhook itu pemicu, bukan sumber kebenaran soal uang.
        # Tanpa ini, satu panggilan keliru cukup untuk memberi tahu pelanggan
        # "dananya dikembalikan" padahal tidak ada yang dikembalikan.
        try:
            res = await backend.get_payment_status(order.order_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cek status refund %s gagal: %s", order.order_ref, exc)
            return False
        if str((res or {}).get("invoice_status") or "").lower() != "refunded":
            logger.info("webhook refund %s diabaikan — invoice belum refunded",
                        order.order_ref)
            return False
        return await tutup_karena_refund(order)
    return False


async def _check_refunded() -> None:
    """Pesanan yang SUDAH dibayar bisa dibatalkan admin lewat refund.

    Backend punya `POST /orders/{id}/refund` (khusus Admin/Owner): pembayaran
    jadi Refunded, invoice Refunded, pesanan induk Cancelled, dan stok kembali.
    Tanpa pemeriksaan ini chatbot tidak pernah tahu: pelanggan tidak diberi
    kabar dananya kembali, dan baris pesanan lokalnya tetap "aktif" sehingga
    dia diblokir memesan lagi ("kamu masih punya pesanan yang sedang diproses").
    """
    for order in await store.list_orders_by_status("paid", "ready"):
        try:
            res = await backend.get_payment_status(order.order_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cek status refund gagal untuk %s: %s", order.order_ref, exc)
            continue
        if str((res or {}).get("invoice_status") or "").lower() != "refunded":
            continue
        await tutup_karena_refund(order)


async def _check_once() -> None:
    await _check_refunded()
    pending = await store.list_orders_by_status("pending")
    now = datetime.now(timezone.utc)

    for order in pending:
        # 1) Timeout -> auto-cancel + notify.
        if now >= _aware(order.expires_at):
            # Cancel upstream too. Marking only the local row "expired" left the
            # order pending forever in the backend and on Admin Site, while the
            # customer had already been told it was cancelled.
            cancelled = True
            try:
                await backend.cancel_order(order.order_ref)
            except Exception as exc:  # noqa: BLE001
                cancelled = False
                logger.warning("could not cancel expired order %s upstream: %s",
                               order.order_ref, exc)
            await store.update_pending_order(order.id, status="expired")
            await store.set_state(order.wa_number, State.IDLE)
            if cancelled:
                text = (f"Pesanan *{_label(order)}* dibatalkan otomatis karena "
                        "melewati batas waktu pembayaran. Silakan pesan lagi "
                        "kapan saja ya 🙏")
            else:
                surel = settings.store_support_email.strip()
                lanjut = (f"kirim bukti transfernya ke {surel} ya" if surel
                          else "sampaikan bukti transfernya ke kontak resmi kami ya")
                text = (f"Batas waktu pembayaran pesanan *{_label(order)}* sudah "
                        "lewat, jadi pesanannya tidak kami proses. Kalau kamu "
                        f"terlanjur membayar, {lanjut} 🙏")
            await _notify(order.wa_number, text)
            continue

        # 2) Poll backend payment status (invoice: unpaid|partial|paid|refunded).
        try:
            res = await backend.get_payment_status(order.order_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Payment status check failed for %s: %s", order.order_ref, exc)
            continue

        inv_status = (res or {}).get("invoice_status")
        if inv_status in ("paid", "partial"):
            await tandai_lunas(order)


async def tandai_lunas(order) -> bool:
    """Kabari pelanggan pembayarannya masuk, lalu tandai pesanannya lunas.

    Dipakai polling DAN webhook. Sama seperti refund: kabari dulu, tandai
    belakangan — kalau pengiriman gagal, barisnya tetap dipantau dan dicoba lagi.
    """
    if order.notified_paid:
        return False
    terkirim = await _notify(
        order.wa_number,
        "Pembayaran sudah kami terima ✅\n"
        f"Jumlah: {rupiah(order.amount_due)}. Pesananmu akan segera kami proses. "
        "Terima kasih! 🎂",
    )
    if not terkirim:
        logger.warning("Kabar pembayaran %s gagal terkirim — dicoba lagi siklus berikutnya",
                       order.order_ref)
        return False
    await store.update_pending_order(order.id, status="paid", notified_paid=True)
    await store.set_state(order.wa_number, State.ORDER_ACTIVE)
    logger.info("Pembayaran %s masuk — pelanggan sudah dikabari", order.order_ref)
    return True


async def notify_paid(order_id: int) -> bool:
    """Dipanggil backend lewat webhook internal begitu pembayaran lunas/DP masuk.

    Statusnya tetap dipastikan ke backend dulu: webhook cuma pemicu supaya tidak
    menunggu siklus polling, bukan sumber kebenaran soal uang.
    """
    for order in await store.list_orders_by_status("pending"):
        if str(order.order_ref) != str(order_id):
            continue
        try:
            res = await backend.get_payment_status(order.order_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cek pembayaran %s gagal: %s", order.order_ref, exc)
            return False
        if str((res or {}).get("invoice_status") or "").lower() not in ("paid", "partial"):
            return False
        return await tandai_lunas(order)
    return False


async def notify_ready(order_id: int) -> bool:
    """Send the proactive 'order is ready' message (PROMPT §10.13)."""
    # order_id from the backend push = our pending_orders.order_ref (backend id).
    # "pending" included: the paid-poll (30s) may lag behind the admin marking
    # the order ready — the push must not be dropped in that window.
    orders = await store.list_orders_by_status("pending", "paid", "ready")
    order = next((o for o in orders if o.order_ref == str(order_id)), None)
    if order is None:
        return False
    if order.notified_ready:  # backend may re-push; don't spam the customer
        return True
    await store.update_pending_order(order.id, status="ready", notified_ready=True)

    msg = f"Kabar baik! Pesananmu *{_label(order)}* sudah *siap* 🎉\n"
    if order.delivery_method == "delivery":
        msg += (
            "\nUntuk pengiriman, silakan pesan kurir (GoSend/GrabExpress) sendiri ke "
            "alamat toko berikut:\n"
            f"*{settings.store_name}*\n{settings.store_address}\n"
            "(salin alamat di atas ke aplikasi ojol ya)"
        )
    else:
        msg += f"\nSilakan diambil di {settings.store_name}, {settings.store_address}."
    await _notify(order.wa_number, msg)
    return True


_PURGE_EVERY_SECONDS = 24 * 60 * 60


async def _purge_if_due(last_purge: float) -> float:
    """Run the personal-data purge at most once a day, on the same worker."""
    now = asyncio.get_running_loop().time()
    if now - last_purge < _PURGE_EVERY_SECONDS:
        return last_purge
    try:
        logs, orders = await store.purge_old_data()
        if logs or orders:
            logger.info(
                "Retention purge: %s transcript rows deleted, %s order identity "
                "snapshots cleared (older than %s days)",
                logs, orders, settings.data_retention_days,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Retention purge failed: %s", exc)
    return now


_last_faq_fingerprint: str | None = None


async def _refresh_faq_if_changed() -> bool:
    """Re-embed the FAQ when an admin has edited it in Admin Site.

    The bot's answers live in the backend's /faq table, and an admin who fixes a
    wrong answer there should not have to wait for a redeploy — or worse, not
    know that a redeploy is what it takes. Cheap in the common case: one GET and
    a hash comparison; embedding only runs when the text actually changed.
    """
    global _last_faq_fingerprint
    from app.rag import faq_source

    docs, asal = await faq_source.current_docs()
    if not docs:
        return False
    fingerprint = faq_source.fingerprint(docs)
    if fingerprint == _last_faq_fingerprint:
        return False
    # Compared against the marker the boot ingest wrote, not against "have I
    # looked before": the ingest container and this service can disagree about
    # the source (it once had no BACKEND_BASE_URL and silently embedded the
    # local .txt fallback while the service could see the backend's FAQ fine).
    # Reconciling against what is actually in Chroma fixes that by itself.
    if faq_source.read_marker() == fingerprint:
        _last_faq_fingerprint = fingerprint
        logger.info("FAQ aktif: %d dokumen dari %s", len(docs), asal)
        return False
    _last_faq_fingerprint = fingerprint
    chunks = await asyncio.to_thread(faq_source.ingest_documents, docs)
    faq_source.write_marker(fingerprint)
    logger.info("FAQ berubah di %s — %d dokumen di-embed ulang (%d chunk)",
                asal, len(docs), chunks)
    return True


async def _loop() -> None:
    interval = settings.payment_check_interval_seconds
    logger.info("Payment background worker started (interval=%ss)", interval)
    last_purge = 0.0
    last_faq_check = 0.0
    while True:
        try:
            await _check_once()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Background check error: %s", exc)
        last_purge = await _purge_if_due(last_purge)
        if (settings.faq_refresh_seconds > 0
                and time.monotonic() - last_faq_check >= settings.faq_refresh_seconds):
            last_faq_check = time.monotonic()
            try:
                await _refresh_faq_if_changed()
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                logger.exception("FAQ refresh failed: %s", exc)
        await asyncio.sleep(interval)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
