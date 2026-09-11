"""
Туннелирование MTProto-трафика Telethon через Cloudflare Worker.

Зачем: Telethon общается с Telegram по MTProto — это "сырой" TCP, а не HTTP.
Если хостинг находится там, где прямые TCP-соединения к серверам Telegram
режутся/тротлятся DPI, обычный HTTP(S)/WSS-трафик к *.workers.dev обычно
проходит без проблем (DPI не фильтрует Cloudflare по одному только адресу).

Как это работает:
  Telethon (эта программа) --wss--> Cloudflare Worker --tcp--> сервер Telegram

Worker получает WebSocket-соединение, открывает настоящий TCP-сокет к DC
Telegram через Workers TCP Sockets API (`cloudflare:sockets`) и гоняет байты
в обе стороны. Сам код MTProto (шифрование, кадрирование) Telethon не меняется —
меняется только физический транспорт нижнего уровня.
"""

import asyncio
import logging

import websockets
from telethon.network.connection import ConnectionTcpAbridged

log = logging.getLogger("vacancy_bot")


class _WebSocketStreamAdapter:
    """
    Оборачивает websockets-соединение в интерфейс, который Telethon ожидает
    от `self._reader`/`self._writer` (readexactly/write/drain/close/wait_closed) —
    см. telethon/network/connection/connection.py, где Connection._connect()
    обычно получает эту пару от asyncio.open_connection(). Здесь вместо этого
    источник байт — WebSocket до Cloudflare Worker.
    """

    def __init__(self, ws):
        self._ws = ws
        self._in_buf = bytearray()
        self._out_queue = asyncio.Queue()
        self._closed = False
        # Отдельная очередь + таск-отправитель, а не "голый" `await ws.send()`
        # на каждый write(), гарантируют строгий порядок байт даже если
        # несколько write() пришли до того, как предыдущий send() завершился.
        self._sender_task = asyncio.ensure_future(self._sender_loop())
        self._close_task = None

    async def _sender_loop(self):
        try:
            while True:
                chunk = await self._out_queue.get()
                try:
                    if chunk is None:
                        break
                    await self._ws.send(chunk)
                finally:
                    self._out_queue.task_done()
        except Exception as e:
            log.warning(f"Cloudflare Worker WS: ошибка отправки: {e}")

    def write(self, data):
        if self._closed:
            return
        self._out_queue.put_nowait(bytes(data))

    async def drain(self):
        await self._out_queue.join()

    async def readexactly(self, n):
        while len(self._in_buf) < n:
            try:
                msg = await self._ws.recv()
            except Exception as e:
                # Контракт readexactly: если поток оборвался раньше времени,
                # нужно бросить IncompleteReadError — это то, что уже умеет
                # обрабатывать recv-цикл Connection (переподключение и т.п.).
                partial = bytes(self._in_buf)
                self._in_buf.clear()
                raise asyncio.IncompleteReadError(partial, n) from e
            if isinstance(msg, str):
                msg = msg.encode()
            self._in_buf.extend(msg)
        result = bytes(self._in_buf[:n])
        del self._in_buf[:n]
        return result

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._out_queue.put_nowait(None)
        self._close_task = asyncio.ensure_future(self._ws.close())

    async def wait_closed(self):
        for task in (self._sender_task, self._close_task):
            if task is None:
                continue
            try:
                await task
            except Exception:
                pass


class ConnectionViaCloudflareWorker(ConnectionTcpAbridged):
    """
    Connection-класс Telethon, тоннелирующий трафик через Cloudflare Worker.

    WS_URL/WS_TOKEN — атрибуты класса, а не параметры __init__: Telethon сам
    создаёт объект соединения с фиксированным набором аргументов
    (ip, port, dc_id, loggers, proxy, local_addr) и не даёт передать ничего
    своего (см. telegrambaseclient.py: `self._connection(ip, port, dc_id,
    loggers=..., proxy=..., local_addr=...)`). Поэтому URL/токен "запекаются"
    в динамический подкласс через make_cloudflare_connection() ниже.
    """
    WS_URL = None
    WS_TOKEN = None

    async def _connect(self, timeout=None, ssl=None):
        if not self.WS_URL:
            raise RuntimeError("ConnectionViaCloudflareWorker: WS_URL не задан")

        # ip/port — это фактический адрес DC Telegram, который выбрал сам
        # Telethon (в т.ч. при миграции между дата-центрами). Worker сам
        # проверяет, что этот адрес входит в диапазоны Telegram, прежде
        # чем открывать TCP-сокет — см. cloudflare-worker/src/index.js.
        url = f"{self.WS_URL}?host={self._ip}&port={self._port}"
        headers = {"X-Proxy-Token": self.WS_TOKEN} if self.WS_TOKEN else {}

        log.info(f"Подключение к Telegram DC {self._ip}:{self._port} через Cloudflare Worker")
        ws = await asyncio.wait_for(
            websockets.connect(url, additional_headers=headers, max_size=None),
            timeout=timeout,
        )

        adapter = _WebSocketStreamAdapter(ws)
        self._reader = adapter
        self._writer = adapter
        self._codec = self.packet_codec(self)
        self._init_conn()
        await self._writer.drain()


def make_cloudflare_connection(ws_url, token):
    """Возвращает Connection-класс с зашитыми URL воркера и секретным токеном."""
    return type(
        "ConnectionViaCloudflareWorkerBound",
        (ConnectionViaCloudflareWorker,),
        {"WS_URL": ws_url.rstrip("/"), "WS_TOKEN": token},
    )
