# Cloudflare Worker — релей для job-hunter-telegram

Нужен, когда сервер бота не имеет нормального прямого доступа к Telegram
и/или Google Gemini (например, хостинг в РФ). Воркер:

- `/mtproto` — принимает WebSocket от бота и открывает настоящий TCP-сокет
  к серверу Telegram (Workers TCP Sockets API), прозрачно гоняя байты в обе
  стороны. Разрешены только IP-диапазоны Telegram.
- `/gemini/*` — обычный reverse-proxy на `generativelanguage.googleapis.com`.

Оба маршрута защищены общим секретом (заголовок `X-Proxy-Token`).

## Деплой

Требуется Node.js и аккаунт Cloudflare с включённым Workers (нужен план,
поддерживающий исходящие TCP-сокеты — см. `wrangler.toml`).

```bash
cd cloudflare-worker
npm install -g wrangler   # если ещё не установлен
wrangler login

# Секрет для X-Proxy-Token — придумайте длинную случайную строку,
# например: openssl rand -hex 32
wrangler secret put PROXY_TOKEN

wrangler deploy
```

После деплоя wrangler выведет адрес воркера, например:
`https://job-hunter-telegram-relay.<ваш-субдомен>.workers.dev`

## Настройка бота

В `config.yaml` бота (не в этом каталоге):

```yaml
network:
  mode: "cloudflare_worker"
  cloudflare_worker:
    base_url: "https://job-hunter-telegram-relay.<ваш-субдомен>.workers.dev"
    proxy_token: "<та же строка, что в wrangler secret put PROXY_TOKEN>"
```

Чтобы вернуться на прямые соединения — обратно поставить `mode: "direct"`,
без переустановки чего-либо.

## Проверка

- `wrangler tail` во время работы бота покажет входящие запросы к воркеру
  (и `/mtproto`, и `/gemini`) — если бот подключается, здесь будет видно.
- Если бот пишет в лог `Host not allowed` — значит DC Telegram, к которому
  пытается подключиться Telethon, не попал в список `TELEGRAM_IPV4_CIDRS`
  в `src/index.js`. Актуальные диапазоны Telegram публикует сам:
  https://core.telegram.org/resources/cidr.txt — обновите список и
  передеплойте (`wrangler deploy`).
- Бот работает только с IPv4-адресами Telegram (`use_ipv6` в Telethon по
  умолчанию выключен) — IPv6 воркер сейчас не поддерживает.

## Ограничения / что стоит знать

- `wrangler dev` без флага `--remote` не даёт настоящих исходящих TCP-сокетов
  — тестировать `/mtproto` локально нужно через `wrangler dev --remote`
  или сразу через `wrangler deploy`.
- Секрет `PROXY_TOKEN` — единственное, что не даёт превратить этот воркер в
  открытый релей для посторонних. Держите его в секрете так же, как
  `api_hash`/`gemini_api_key`.
- TCP Sockets API — относительно новая возможность Cloudflare Workers; если
  после обновлений платформы `import { connect } from "cloudflare:sockets"`
  перестанет работать как ожидается, проверьте актуальную документацию:
  https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/
