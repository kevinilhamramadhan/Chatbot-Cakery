"""Thin wrapper around the wwebjs-api REST API for sending WhatsApp messages.

We only configure/consume wwebjs-api here — we never reimplement it (PROMPT §13).
"""

import logging

import httpx

from app.core.config import settings
from app.core.security import mask_phone

logger = logging.getLogger(__name__)


def to_chat_id(wa_number: str) -> str:
    """Normalize a bare phone number into a wweb.js chatId (`<digits>@c.us`).

    Pass-through if it already looks like a chatId.
    """
    if "@" in wa_number:
        return wa_number
    digits = "".join(ch for ch in wa_number if ch.isdigit())
    return f"{digits}@c.us"


class WhatsAppClient:
    def __init__(self) -> None:
        self._base = settings.wwebjs_base_url.rstrip("/")
        self._session = settings.wwebjs_session_id
        self._headers = {"x-api-key": settings.wwebjs_api_key}

    async def _post(self, payload: dict) -> dict:
        url = f"{self._base}/client/sendMessage/{self._session}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload, headers=self._headers)
            resp.raise_for_status()
            return resp.json()

    async def send_text(self, wa_number: str, text: str) -> dict:
        payload = {
            "chatId": to_chat_id(wa_number),
            "contentType": "string",
            "content": text,
        }
        if settings.log_message_bodies:
            logger.info("WA out -> %s: %s", mask_phone(wa_number), text[:120])
        else:
            logger.info("WA out -> %s (%d chars)", mask_phone(wa_number), len(text))
        return await self._post(payload)

    async def resolve_phone(self, chat_id: str) -> str | None:
        """Nomor telepon asli di balik sebuah alamat `@lid`, atau None.

        WhatsApp kini mengirim sebagian pengirim sebagai `@lid` — identitas
        privasi yang BUKAN nomor telepon. Terekam di produksi:
        `10278007771379@lid` untuk nomor yang sebenarnya `6281283838610`.

        Angka LID tidak boleh sampai ke backend. Di sana nomor WhatsApp adalah
        kunci yang menyambungkan pesanan lewat chat dengan akun Buyer Site, dan
        dipakai untuk OTP serta reset kata sandi. Menyimpan LID sebagai nomor
        berarti pesanannya tidak pernah bisa dicocokkan dengan akunnya, dan
        adminnya tidak bisa menghubungi pelanggan itu sama sekali.
        """
        url = f"{self._base}/client/getContactById/{self._session}"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(url, headers=self._headers,
                                         json={"contactId": chat_id})
            kontak = (resp.json() or {}).get("contact") or {}
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Gagal menerjemahkan %s: %s: %s",
                           mask_phone(chat_id), type(exc).__name__, exc)
            return None

        # Balasannya memuat dua hal: `id` yang sudah berbentuk `62…@c.us`, dan
        # `number` yang justru berisi angka LID-nya. Yang dipakai `id`.
        nomor = str((kontak.get("id") or {}).get("user") or "")
        if nomor.isdigit() and len(nomor) >= 10:
            return nomor
        logger.warning("Kontak %s tidak memuat nomor telepon yang sah",
                       mask_phone(chat_id))
        return None

    # ── Kesehatan sesi ───────────────────────────────────────────────────────
    async def session_state(self) -> str | None:
        """State sesi WhatsApp di gateway, atau None kalau gateway tak terjawab.

        Nilai yang mungkin dari wwebjs-api: CONNECTED saat siap dipakai, dan
        pesan `session_not_found` saat sesinya belum/gagal diinisialisasi.
        """
        url = f"{self._base}/session/status/{self._session}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=self._headers)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Gateway WhatsApp tidak terjawab: %s: %s",
                           type(exc).__name__, exc)
            return None
        if data.get("success") and data.get("state"):
            return str(data["state"])
        return str(data.get("message") or "unknown")

    async def start_session(self) -> bool:
        """Minta gateway menginisialisasi ulang sesinya.

        Dipanggil saat sesinya mati. Kredensial yang tersimpan di volume dipakai
        lagi kalau masih sah, jadi biasanya tidak perlu scan QR ulang.
        """
        url = f"{self._base}/session/start/{self._session}"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(url, headers=self._headers)
            ok = bool(resp.json().get("success"))
        except httpx.TimeoutException:
            # Gateway membuka Chromium dan memulihkan kredensialnya sebelum
            # menjawab, dan itu bisa lebih lama dari batas di atas. Timeout di
            # sini berarti "masih berjalan", bukan gagal: siklus berikutnya
            # melihat hasilnya, dan jeda antar percobaan sudah mencegah tumpukan.
            logger.info("Sesi WhatsApp masih diinisialisasi — menunggu siklus berikutnya")
            return False
        except (httpx.HTTPError, ValueError) as exc:
            # Tipe exception ikut dicatat: httpx.ReadTimeout dan beberapa
            # saudaranya memiliki str() kosong, sehingga log yang hanya memuat
            # pesannya berbunyi "Gagal menyalakan sesi WhatsApp: " tanpa isi.
            logger.error("Gagal menyalakan sesi WhatsApp: %s: %s",
                         type(exc).__name__, exc)
            return False
        logger.info("Permintaan menyalakan sesi WhatsApp terkirim (sukses=%s)", ok)
        return ok

    # ── Ganti nomor dari Admin Site ──────────────────────────────────────────
    async def _get(self, path: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=20.0) as client:
            return await client.get(self._base + path.format(s=self._session),
                                    headers=self._headers)

    async def akun(self) -> dict:
        """Nomor dan nama profil yang sedang tertaut. Hanya sah saat CONNECTED.

        Kuncinya `profile_name`, bukan `nama_profil`: backend meneruskan balasan
        ini apa adanya ke Admin Site, dan halaman WhatsApp di sana membacanya
        dengan nama itu (src/services/whatsappService.ts).
        """
        info = (await self._get("/client/getClassInfo/{s}")).json().get("sessionInfo") or {}
        return {"nomor": (info.get("wid") or {}).get("user"),
                "profile_name": info.get("pushname")}

    async def qr_png(self) -> bytes | None:
        """Gambar QR yang sedang berlaku, atau None kalau tidak sedang menunggu scan.

        Gateway membalas JSON `{success: false}` dengan status 200 saat QR belum
        ada atau sudah discan, jadi yang dibedakan adalah jenis isinya.
        """
        resp = await self._get("/session/qr/{s}/image")
        if resp.headers.get("content-type", "").startswith("image/png"):
            return resp.content
        return None

    async def logout(self) -> None:
        """Putus nomor yang tertaut dan hapus kredensialnya dari volume.

        Sesudahnya sesi tidak ada sama sekali; start_session() membuat sesi baru
        yang menampilkan QR. Loop penyembuh di background.py tidak mengganggu QR
        itu: /session/start pada sesi yang sudah ada ditolak gateway (422), bukan
        memulai ulang.
        """
        resp = await self._get("/session/terminate/{s}")
        resp.raise_for_status()
        logger.warning("Nomor WhatsApp diputus: %s", resp.text[:120])

    async def send_image(
        self, wa_number: str, image_url: str, caption: str | None = None
    ) -> dict:
        """Send an image by URL (used for product photos — PROMPT §10.2)."""
        payload: dict = {
            "chatId": to_chat_id(wa_number),
            "contentType": "MessageMediaFromURL",
            "content": image_url,
        }
        if caption:
            payload["options"] = {"caption": caption}
        logger.info("WA out (image) -> %s: %s", mask_phone(wa_number), image_url)
        return await self._post(payload)


whatsapp_client = WhatsAppClient()
