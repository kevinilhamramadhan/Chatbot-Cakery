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
from app.conversation import bahasa, rbac, store, tanggal
from app.conversation.states import State
from app.core.config import settings
from app.core.security import mask_phone, sanitize_relay, wa_digits
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


async def _teks_kabar_refund(order, kunci: str) -> str:
    """Kabar refund dalam bahasa sesi pelanggan. `kunci`: refund_ditransfer (admin
    selesai mentransfer manual) atau refund_otomatis (di-refund lewat backend)."""
    lang = await store.get_lang(order.wa_number)
    teks = bahasa.teks(kunci, lang, invoice=_label(order))
    email = settings.store_support_email.strip()
    if email:
        teks += bahasa.teks(kunci + "_email", lang, email=email)
    return teks + bahasa.teks("terima_kasih_menunggu", lang)


async def tutup_karena_refund(order) -> bool:
    """Kabari pelanggan, lalu tutup pesanan lokalnya. Dipakai polling DAN webhook.

    Urutannya disengaja: kalau pesannya gagal terkirim (gateway WhatsApp mati,
    sesi putus), baris pesanannya TIDAK ditandai selesai — siklus polling
    berikutnya mencoba lagi. Kalau ditandai lebih dulu, pelanggan yang uangnya
    dikembalikan tidak akan pernah diberi tahu, dan tidak ada yang mengulang.
    """
    if order.status in ("cancelled", "refunded"):
        return False
    if not await _notify(order.wa_number, await _teks_kabar_refund(order, "refund_otomatis")):
        logger.warning("Kabar refund %s gagal terkirim — dicoba lagi siklus berikutnya",
                       order.order_ref)
        return False
    await store.update_pending_order(order.id, status="refunded")
    await store.set_state(order.wa_number, State.IDLE)
    logger.info("Pesanan %s di-refund — pelanggan sudah dikabari", order.order_ref)
    return True


async def notify_refunded(order_id: int) -> bool:
    """Dipanggil backend lewat webhook internal begitu refund selesai.

    Dua keadaan berbeda memakai pintu yang sama:

    1. Pesanan masih dipantau (pending/paid/ready) — refund dilakukan admin dari
       Admin Site tanpa sepengetahuan chatbot. Pelanggan dikabari pesanannya
       dibatalkan dan dananya dikembalikan.
    2. Pesanan sudah berstatus menunggu transfer — pelanggan sendiri yang
       membatalkan lewat chat, sudah diberi tahu dananya diproses manual, dan
       ini kabar bahwa admin sudah benar-benar mentransfernya.

    Polling 30 detik tetap jalan sebagai jaring pengaman untuk keadaan pertama.
    Keadaan kedua hanya bisa datang dari webhook: dari luar, pesanan yang sudah
    di-refund terlihat sama saja sebelum dan sesudah uangnya dikirim. Karena itu
    kalau pengirimannya gagal barisnya dipindah ke KABAR_TRANSFER_TERTUNDA, bukan
    dibiarkan di refund_wait — webhook-nya tidak akan datang kedua kali.
    """
    for order in await store.list_orders_by_status(store.MENUNGGU_TRANSFER,
                                                   store.KABAR_TRANSFER_TERTUNDA):
        if str(order.order_ref) != str(order_id):
            continue
        if not await _notify(order.wa_number, await _teks_kabar_refund(order, "refund_ditransfer")):
            await store.update_pending_order(
                order.id, status=store.KABAR_TRANSFER_TERTUNDA)
            logger.warning("Kabar transfer refund %s gagal — dicoba lagi siklus "
                           "berikutnya", order.order_ref)
            return False
        await store.update_pending_order(order.id, status="refunded")
        logger.info("Refund manual %s selesai — pelanggan sudah dikabari",
                    order.order_ref)
        return True

    for order in await store.list_orders_by_status("pending", "paid", "ready"):
        if str(order.order_ref) != str(order_id):
            continue
        # Status refund-nya dipastikan dulu ke backend, sama seperti jalur
        # pembayaran: webhook itu pemicu, bukan sumber kebenaran soal uang.
        # Tanpa ini, satu panggilan keliru cukup untuk memberi tahu pelanggan
        # "dananya dikembalikan" padahal tidak ada yang dikembalikan.
        try:
            res = await backend.get_payment_status(order.order_ref, order.wa_number)
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
            res = await backend.get_payment_status(order.order_ref, order.wa_number)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cek status refund gagal untuk %s: %s", order.order_ref, exc)
            continue
        inv = str((res or {}).get("invoice_status") or "").lower()
        if inv == "paid" and order.payment_type == store.PELUNASAN:
            # Tagihan pelunasan DP sudah dibayar. Ditandai dulu supaya kabarnya
            # tidak terkirim dua kali kalau siklus berikutnya datang lebih cepat.
            await store.update_pending_order(order.id, payment_type="full")
            await _notify(order.wa_number, bahasa.teks(
                "pelunasan_diterima", await store.get_lang(order.wa_number),
                invoice=_label(order)))
            continue
        if inv != "refunded":
            continue
        await tutup_karena_refund(order)


_STATUS_TUTUP = {"delivered", "picked_up", "completed", "cancelled", "refunded",
                 "cancelled_settlement_expired"}


async def _lepas_pesanan_selesai() -> None:
    """Pesanan lunas/siap yang di backend sudah diserahkan berhenti dihitung aktif.

    Tanpa ini baris lokalnya "paid"/"ready" selamanya, dan karena satu nomor
    hanya boleh punya satu pesanan aktif, pelanggan yang pernah membeli ditolak
    memesan lagi lewat WhatsApp ("kamu masih punya pesanan yang sedang
    diproses") — terukur 13 Okt 2026.
    """
    for order in await store.list_orders_by_status("paid", "ready"):
        try:
            terbaru = await backend.get_latest_order(order.wa_number)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cek status pesanan gagal untuk %s: %s", order.order_ref, exc)
            continue
        # ponytail: backend hanya memberi pesanan TERBARU per nomor. Kalau
        # pelanggan memesan lagi lewat website, baris ini tidak terlepas sampai
        # ada endpoint status per id untuk service key.
        if not terbaru or str(terbaru.get("id")) != str(order.order_ref):
            continue
        if terbaru.get("status") not in _STATUS_TUTUP:
            continue
        await _lepas(order, terbaru.get("status"))


async def _lepas(order, status_backend: str) -> None:
    """Tutup baris lokal pesanan yang di backend sudah diserahkan/berakhir."""
    await store.update_pending_order(order.id, status="selesai")
    await store.set_state(order.wa_number, State.IDLE)
    # Backend hanya mengabari "siap" dan "completed"; delivered/picked_up hanya
    # ketahuan dari polling. Batal/refund punya kabarnya sendiri.
    if status_backend in ("delivered", "picked_up", "completed"):
        await _notify(order.wa_number, bahasa.teks(
            "pesanan_selesai", await store.get_lang(order.wa_number),
            invoice=_label(order), toko=settings.store_name))
    logger.info("Pesanan %s sudah %s di backend — dilepas", order.order_ref, status_backend)


async def _kabari_transfer_tertunda() -> None:
    """Ulangi kabar "dana sudah ditransfer" yang tadi gagal terkirim.

    Backend menembak webhook /refunded sekali saja. Kalau saat itu gateway
    WhatsApp-nya mati, tanpa pengulangan ini pelanggan tidak akan pernah tahu
    uangnya sudah kembali — dan tidak ada cara memeriksanya dari backend, karena
    pesanan yang di-refund terlihat sama sebelum dan sesudah uangnya dikirim.
    """
    for order in await store.list_orders_by_status(store.KABAR_TRANSFER_TERTUNDA):
        if not await _notify(order.wa_number, await _teks_kabar_refund(order, "refund_ditransfer")):
            continue
        await store.update_pending_order(order.id, status="refunded")
        logger.info("Refund manual %s selesai — pelanggan sudah dikabari (ulangan)",
                    order.order_ref)


# Sesi WhatsApp: dicek berkala, bukan sekali saat start.
_SESI_SEHAT = "CONNECTED"
_JEDA_NYALAKAN_SESI = 120.0
# None, BUKAN 0.0: time.monotonic() adalah uptime mesin, jadi di server yang baru
# boot nilainya masih kecil dan `sekarang - 0.0 < jeda` — percobaan pertama
# terblokir justru pada menit-menit ketika sesinya paling mungkin mati.
# Kekeliruan yang sama pernah terjadi di webhook/routes.py.
_sesi_terakhir_dinyalakan: float | None = None


async def _pastikan_sesi_wa() -> None:
    """Nyalakan lagi sesi WhatsApp kalau mati.

    Gateway menginisialisasi sesinya SEKALI saat container start. Kalau saat itu
    jaringan belum siap — persis yang terjadi tiap VM baru boot — Puppeteer gagal
    membuka web.whatsapp.com ("ERR_NAME_NOT_RESOLVED"), sesinya batal, dan tidak
    pernah dicoba lagi. Gateway tetap menjawab /ping dengan 200, jadi dari luar
    semuanya terlihat sehat sementara tidak ada satu pun pesan yang masuk.

    Pemeriksaan ini yang membuat urutan start dan kesiapan jaringan tidak lagi
    jadi soal: begitu jaringannya hidup, siklus berikutnya menyambungkan sesinya.
    """
    global _sesi_terakhir_dinyalakan

    state = await whatsapp_client.session_state()
    if state is None or state == _SESI_SEHAT:
        return

    sekarang = time.monotonic()
    if (_sesi_terakhir_dinyalakan is not None
            and sekarang - _sesi_terakhir_dinyalakan < _JEDA_NYALAKAN_SESI):
        return  # sedang dalam proses; menyalakan berulang kali tidak membantu
    _sesi_terakhir_dinyalakan = sekarang

    logger.warning("Sesi WhatsApp tidak tersambung (state=%s) — menyalakan ulang", state)
    await whatsapp_client.start_session()


_sudah_diingatkan: set[int] = set()
_terakhir_ditolak: tuple[str, ...] = ()
_gagal_sambung = 0
_TAK_TERJANGKAU = "backend tidak terjangkau"


async def _periksa_backend() -> None:
    """Kabari pemilik sistem kalau backend menolak kunci layanan chatbot.

    13 Okt 2026: backend baru memecah kuncinya per kemampuan dan pesanan lewat
    WhatsApp gagal 403 selama belasan menit tanpa ada yang tahu. Yang dikirim
    hanya PERUBAHAN keadaan (mulai ditolak, daftar berubah, pulih).
    """
    global _terakhir_ditolak, _gagal_sambung
    try:
        ditolak = tuple(await backend.periksa_kunci())
        _gagal_sambung = 0
    except Exception as exc:  # noqa: BLE001
        # Sekali gagal bisa cuma restart; dua kali berturut-turut berarti mati.
        # (14 Okt 2026: image backend baru menolak start dan berputar restart.)
        _gagal_sambung += 1
        logger.warning("backend tidak terjangkau (%s kali): %s", _gagal_sambung, exc)
        if _gagal_sambung < 2:
            return
        ditolak = (_TAK_TERJANGKAU,)
    if ditolak == _terakhir_ditolak:
        return
    _terakhir_ditolak = ditolak
    if ditolak == (_TAK_TERJANGKAU,):
        teks = ("⚠️ Chatbot tidak bisa menghubungi backend. Website dan pesanan lewat "
                "WhatsApp kemungkinan mati. Periksa container backend di server.")
    elif ditolak:
        logger.error("Backend menolak kunci layanan untuk: %s", ", ".join(ditolak))
        teks = ("⚠️ Chatbot ditolak backend (401/403) untuk: " + ", ".join(ditolak) + ".\n"
                "Pesanan lewat WhatsApp bisa gagal. Periksa SERVICE_*_KEY di .env dan "
                "perubahan kontrak backend.")
    else:
        teks = "✅ Backend kembali terjangkau dan menerima semua kunci layanan chatbot."
    tujuan = ([n.strip() for n in settings.alert_wa_numbers.split(",") if n.strip()]
              or settings.owner_wa_list)
    for nomor in tujuan:
        await _notify(nomor if "@" in nomor else f"{nomor}@c.us", teks)


async def _check_once() -> None:
    await _pastikan_sesi_wa()
    await _check_refunded()
    await _lepas_pesanan_selesai()
    await _kabari_transfer_tertunda()
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
                await backend.cancel_order(order.order_ref, order.wa_number)
            except Exception as exc:  # noqa: BLE001
                cancelled = False
                logger.warning("could not cancel expired order %s upstream: %s",
                               order.order_ref, exc)
            await store.update_pending_order(order.id, status="expired")
            await store.set_state(order.wa_number, State.IDLE)
            lang = await store.get_lang(order.wa_number)
            if cancelled:
                text = bahasa.teks("batal_otomatis", lang, invoice=_label(order))
            else:
                surel = settings.store_support_email.strip()
                lanjut = (bahasa.teks("batas_lewat_bukti_email", lang, email=surel) if surel
                          else bahasa.teks("batas_lewat_bukti_kontak", lang))
                text = bahasa.teks("batas_lewat", lang, invoice=_label(order), lanjut=lanjut)
            await _notify(order.wa_number, text)
            continue

        # 1b) Sekali diingatkan sebelum hangus.
        # ponytail: penandanya di memori; kalau service restart di jendela
        # pengingat, pelanggan bisa menerimanya dua kali. Kolom baru kalau itu
        # mengganggu.
        sisa_detik = (_aware(order.expires_at) - now).total_seconds()
        if (0 < sisa_detik <= settings.payment_reminder_minutes * 60
                and order.id not in _sudah_diingatkan):
            _sudah_diingatkan.add(order.id)
            await _notify(order.wa_number, bahasa.teks(
                "pengingat_bayar", await store.get_lang(order.wa_number),
                invoice=_label(order), jumlah=rupiah(order.amount_due),
                menit=max(1, round(sisa_detik / 60))))

        # 2) Poll backend payment status (invoice: unpaid|partial|paid|refunded).
        try:
            res = await backend.get_payment_status(order.order_ref, order.wa_number)
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
    # Klaim dulu, baru kirim. Urutan sebaliknya -- yang dipakai sebelumnya --
    # membiarkan polling dan webhook sama-sama lolos pemeriksaan notified_paid
    # lalu sama-sama mengirim; pelanggan menerima kabar identik dua kali.
    # Klaim yang gagal DILEPAS lagi di bawah, jadi janji "kabari dulu, tandai
    # belakangan" tetap berlaku: baris yang gagal dikirim tetap dicoba ulang.
    if not await store.klaim_kabar_lunas(order.id):
        return False
    lang = await store.get_lang(order.wa_number)
    teks = bahasa.teks("bayar_diterima", lang, jumlah=rupiah(order.amount_due))
    if order.payment_type == "dp" and order.total_amount > order.amount_due:
        teks += bahasa.teks("sisa_pelunasan", lang,
                            sisa=rupiah(order.total_amount - order.amount_due))
    terkirim = await _notify(order.wa_number, teks)
    if not terkirim:
        await store.update_pending_order(order.id, notified_paid=False)  # lepas klaim
        logger.warning("Kabar pembayaran %s gagal terkirim — dicoba lagi siklus berikutnya",
                       order.order_ref)
        return False
    await store.update_pending_order(order.id, status="paid")
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
            res = await backend.get_payment_status(order.order_ref, order.wa_number)
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

    lang = await store.get_lang(order.wa_number)
    msg = bahasa.teks("pesanan_siap", lang, label=_label(order))
    kunci = ("pesanan_siap_kirim" if order.delivery_method == "delivery"
             else "pesanan_siap_ambil")
    msg += bahasa.teks(kunci, lang, nama_toko=settings.store_name,
                       alamat_toko=settings.store_address)
    await _notify(order.wa_number, msg)
    return True



# Kabar "pembayaran diterima" sudah berbunyi "pesananmu akan segera kami
# proses", dan backend memindahkan pesanan ke in_process tepat saat lunas — jadi
# /paid dan /processing tiba berdempetan. Kabar "sedang diproses" hanya dikirim
# kalau seller memindahkannya belakangan.
# ponytail: dibedakan dari umur kabar lunas, bukan dari penyebab perpindahan;
# kalau backend suatu saat mengirim penyebabnya, pakai itu.
_JEDA_SESUDAH_LUNAS = 5 * 60


async def notify_processing(order_id: int) -> bool:
    """Kabari pelanggan pesanannya mulai dibuat (webhook backend /processing)."""
    orders = await store.list_orders_by_status("paid")
    order = next((o for o in orders if o.order_ref == str(order_id)), None)
    if order is None:
        return False
    umur = (datetime.now(timezone.utc) - _aware(order.updated_at)).total_seconds()
    if umur < _JEDA_SESUDAH_LUNAS:
        return True
    lang = await store.get_lang(order.wa_number)
    return await _notify(order.wa_number, bahasa.teks("pesanan_diproses", lang,
                                                      label=_label(order)))


async def notify_completed(order_id: int) -> bool:
    """Pesanan diselesaikan seller (webhook backend /completed).

    Sama dengan yang dilakukan polling _lepas_pesanan_selesai, hanya tanpa
    menunggu siklusnya.
    """
    orders = await store.list_orders_by_status("pending", "paid", "ready")
    order = next((o for o in orders if o.order_ref == str(order_id)), None)
    if order is None:
        return False
    await _lepas(order, "completed")
    return True


def _teks_pesanan_baru(o: dict) -> str:
    """Ringkasan pesanan baru untuk seller. Nama dan alamat diketik pelanggan
    (website), jadi diperlakukan sama seperti kutipan takeover."""
    inv = (o.get("invoice") or {}).get("nomor_invoice") or f"#{o.get('id')}"
    asal = "WhatsApp" if o.get("created_via") == "chatbot" else "website"
    cust = o.get("customer") or {}
    baris = [f"🧾 Pesanan baru masuk: *{inv}* (lewat {asal})",
             f"Pelanggan: {sanitize_relay(cust.get('nama') or '-')}"
             f" ({wa_digits(cust.get('nomor_wa') or '') or '-'})"]
    for it in o.get("order_items") or []:
        nama = it.get("product_name") or it.get("custom_product_name") or "?"
        baris.append(f"- {it.get('jumlah', 1)}× {sanitize_relay(nama)}")
    if o.get("fulfillment_date"):
        waktu = datetime.fromisoformat(o["fulfillment_date"]).astimezone(tanggal.WIB)
        cara = "Dikirim" if o.get("metode_pengiriman") == "delivery" else "Diambil"
        baris.append(f"{cara}: {tanggal.tampil(waktu.isoformat())}")
    bayar = "sudah dibayar" if o.get("payment_status") == "paid" else "belum dibayar"
    baris.append(f"Total: {rupiah(o.get('total_harga_pesanan'))} ({bayar})")
    return "\n".join(baris)


async def notify_created(order_id: int) -> bool:
    """Kabari seller ada pesanan baru (webhook backend /created).

    Penerimanya sama dengan penerima takeover: yang bertugas melayani pelanggan,
    diatur dari Admin Site. Dikirim ke semuanya, tidak digilir.
    """
    try:
        order = await backend.get_order(order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("detail pesanan baru %s gagal diambil: %s", order_id, exc)
        return False
    if order is None:
        return False
    teks = _teks_pesanan_baru(order)
    terkirim = False
    for nomor in await rbac.nomor_penerima_takeover():
        try:
            await whatsapp_client.send_text(nomor, teks)
            terkirim = True
        except Exception as exc:  # noqa: BLE001
            logger.error("Kabar pesanan baru ke %s gagal: %s", mask_phone(nomor), exc)
    return terkirim

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
    last_probe = 0.0
    while True:
        if (settings.backend_probe_seconds > 0
                and time.monotonic() - last_probe >= settings.backend_probe_seconds):
            last_probe = time.monotonic()
            try:
                await _periksa_backend()
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                logger.warning("pemeriksaan kunci backend gagal: %s", exc)
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
