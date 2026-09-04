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


async def _notify(wa_number: str, text: str) -> None:
    try:
        await whatsapp_client.send_text(wa_number, text)
        await store.log_message(wa_number, "out", text, intent="proactive")
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to notify %s: %s", mask_phone(wa_number), exc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _check_once() -> None:
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
                text = (f"Pesanan *{order.order_ref}* dibatalkan otomatis karena "
                        "melewati batas waktu pembayaran. Silakan pesan lagi "
                        "kapan saja ya 🙏")
            else:
                text = (f"Batas waktu pembayaran pesanan *{order.order_ref}* sudah "
                        "lewat, jadi pesanannya tidak kami proses. Kalau kamu "
                        "terlanjur membayar, hubungi admin ya 🙏")
            await _notify(order.wa_number, text)
            continue

        # 2) Poll backend payment status (invoice: unpaid|partial|paid|refunded).
        try:
            res = await backend.get_payment_status(order.order_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Payment status check failed for %s: %s", order.order_ref, exc)
            continue

        inv_status = (res or {}).get("invoice_status")
        if inv_status in ("paid", "partial") and not order.notified_paid:
            await store.update_pending_order(order.id, status="paid", notified_paid=True)
            await store.set_state(order.wa_number, State.ORDER_ACTIVE)
            await _notify(
                order.wa_number,
                "Pembayaran sudah kami terima ✅\n"
                f"Jumlah: {rupiah(order.amount_due)}. Pesananmu akan segera kami proses. "
                "Terima kasih! 🎂",
            )


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

    msg = f"Kabar baik! Pesananmu *{order.order_ref}* sudah *siap* 🎉\n"
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


async def _loop() -> None:
    interval = settings.payment_check_interval_seconds
    logger.info("Payment background worker started (interval=%ss)", interval)
    last_purge = 0.0
    while True:
        try:
            await _check_once()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Background check error: %s", exc)
        last_purge = await _purge_if_due(last_purge)
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
