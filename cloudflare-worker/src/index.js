/**
 * Cloudflare Worker: релей для job-hunter-telegram, когда прямой доступ
 * к Telegram/Google с хостинга бота ограничен (например, сервер в РФ).
 *
 * Два маршрута:
 *   /mtproto  — WebSocket-туннель до сервера Telegram. Клиент (Telethon,
 *               см. ../../cloudflare_transport.py) шлёт MTProto-байты через
 *               WebSocket; воркер открывает настоящий TCP-сокет к Telegram
 *               (Workers TCP Sockets API, `cloudflare:sockets`) и гоняет
 *               байты в обе стороны. Разрешены только IP из диапазонов
 *               Telegram — иначе воркер превратился бы в открытый TCP-релей
 *               в любую точку интернета.
 *   /gemini/* — обычный HTTP reverse-proxy на generativelanguage.googleapis.com.
 *   /telegram-bot/* — обычный HTTP reverse-proxy на api.telegram.org, для
 *               Bot API (python-telegram-bot). Нужен отдельно от /mtproto,
 *               т.к. Bot API — это обычный HTTPS с чётким SNI api.telegram.org,
 *               который многие DPI блокируют независимо от MTProto.
 *
 * Все маршруты защищены общим секретом (заголовок X-Proxy-Token), который
 * задаётся через `wrangler secret put PROXY_TOKEN` и должен совпадать со
 * значением network.cloudflare_worker.proxy_token в config.yaml бота.
 */

import { connect } from "cloudflare:sockets";

// Официальные диапазоны Telegram (https://core.telegram.org/resources/cidr.txt).
// Список может со временем меняться — если после деплоя воркер начнёт
// отклонять подключения с "Host not allowed", сверьтесь с этой страницей.
const TELEGRAM_IPV4_CIDRS = [
  "149.154.160.0/20",
  "91.108.4.0/22",
  "91.108.8.0/22",
  "91.108.12.0/22",
  "91.108.16.0/22",
  "91.108.20.0/22",
  "91.108.56.0/22",
  "109.239.140.0/24",
  "95.161.64.0/20",
];

function ipToInt(ip) {
  const parts = ip.split(".").map(Number);
  if (parts.length !== 4 || parts.some((p) => Number.isNaN(p) || p < 0 || p > 255)) {
    return null;
  }
  return ((parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]) >>> 0;
}

function isTelegramIp(ip) {
  const ipInt = ipToInt(ip);
  if (ipInt === null) return false;
  return TELEGRAM_IPV4_CIDRS.some((cidr) => {
    const [range, bitsStr] = cidr.split("/");
    const bits = Number(bitsStr);
    const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
    return (ipInt & mask) === (ipToInt(range) & mask);
  });
}

function isAuthorized(request, env) {
  const token = request.headers.get("X-Proxy-Token");
  return Boolean(token) && Boolean(env.PROXY_TOKEN) && token === env.PROXY_TOKEN;
}

async function handleMtproto(request, env) {
  if (!isAuthorized(request, env)) {
    return new Response("Forbidden", { status: 403 });
  }
  if (request.headers.get("Upgrade") !== "websocket") {
    return new Response("Expected websocket upgrade", { status: 400 });
  }

  const url = new URL(request.url);
  const host = url.searchParams.get("host");
  const port = Number(url.searchParams.get("port"));

  if (!host || !port || !isTelegramIp(host)) {
    return new Response("Host not allowed", { status: 403 });
  }

  let socket;
  try {
    socket = connect({ hostname: host, port });
  } catch (e) {
    return new Response(`Upstream connect failed: ${e}`, { status: 502 });
  }

  const pair = new WebSocketPair();
  const client = pair[0];
  const server = pair[1];
  server.accept();

  const writer = socket.writable.getWriter();

  // Telegram -> клиент бота
  (async () => {
    const reader = socket.readable.getReader();
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        server.send(value);
      }
    } catch (e) {
      // соединение оборвалось — закрываем WS ниже
    } finally {
      try { server.close(1000, "upstream closed"); } catch (e) {}
    }
  })();

  // Клиент бота -> Telegram
  server.addEventListener("message", async (event) => {
    try {
      const data =
        typeof event.data === "string"
          ? new TextEncoder().encode(event.data)
          : new Uint8Array(event.data);
      await writer.write(data);
    } catch (e) {
      try { server.close(1011, "write failed"); } catch (e2) {}
    }
  });

  server.addEventListener("close", () => {
    writer.close().catch(() => {});
    socket.close().catch(() => {});
  });

  return new Response(null, { status: 101, webSocket: client });
}

async function proxyTo(request, env, { stripPrefix, upstreamOrigin }) {
  if (!isAuthorized(request, env)) {
    return new Response("Forbidden", { status: 403 });
  }

  const url = new URL(request.url);
  const upstreamPath = url.pathname.replace(stripPrefix, "") || "/";
  const upstreamUrl = upstreamOrigin + upstreamPath + url.search;

  const headers = new Headers(request.headers);
  headers.delete("X-Proxy-Token");
  headers.delete("Host");

  const init = {
    method: request.method,
    headers,
    body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
  };

  const upstreamResponse = await fetch(upstreamUrl, init);
  // Отдаём тело как есть, потоково — не нужно буферизовать в памяти воркера.
  return new Response(upstreamResponse.body, {
    status: upstreamResponse.status,
    statusText: upstreamResponse.statusText,
    headers: upstreamResponse.headers,
  });
}

async function handleGemini(request, env) {
  return proxyTo(request, env, {
    stripPrefix: /^\/gemini/,
    upstreamOrigin: "https://generativelanguage.googleapis.com",
  });
}

async function handleTelegramBot(request, env) {
  return proxyTo(request, env, {
    stripPrefix: /^\/telegram-bot/,
    upstreamOrigin: "https://api.telegram.org",
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/mtproto") {
      return handleMtproto(request, env);
    }
    if (url.pathname.startsWith("/gemini")) {
      return handleGemini(request, env);
    }
    if (url.pathname.startsWith("/telegram-bot")) {
      return handleTelegramBot(request, env);
    }
    return new Response("Not found", { status: 404 });
  },
};
