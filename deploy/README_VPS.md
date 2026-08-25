# Развёртывание tg2site на Ubuntu VPS

Продакшен-схема: Telegram webhook → FastAPI/SQLite → выбор категории в
служебном чате → защищённый Laravel API. Playwright и Tampermonkey не нужны.

## 1. Требования

- Ubuntu 22.04/24.04, Python 3.11+;
- 1–2 vCPU, 1 GB RAM (2 GB рекомендуется для генерации изображений);
- HTTPS-сайт `dev.nedra.kz` и возможность добавить location в nginx;
- отдельный системный пользователь `tg2site`.

```bash
sudo useradd --system --create-home --home-dir /opt/tg2site --shell /bin/bash tg2site
sudo mkdir -p /opt/tg2site /var/lib/tg2site
sudo chown -R tg2site:tg2site /opt/tg2site /var/lib/tg2site
```

Скопируйте проект в `/opt/tg2site`, не копируя `.env`, `.venv`, `data`,
`__pycache__` и локальные `*.session`.

```bash
sudo -u tg2site python3 -m venv /opt/tg2site/.venv
sudo -u tg2site /opt/tg2site/.venv/bin/python -m pip install --upgrade pip
sudo -u tg2site /opt/tg2site/.venv/bin/python -m pip install \
  -r /opt/tg2site/requirements.txt
```

## 2. Настройки

Создайте `/opt/tg2site/.env` из `.env.production.example`. Секретные значения
создавайте или передавайте только через защищённый канал и добавляйте напрямую
на сервер — не коммитьте их в Git и не присылайте в обычном чате.

```dotenv
DATA_DIR=/var/lib/tg2site

TELEGRAM_INGEST_MODE=webhook
TELEGRAM_CHANNEL=@имя_канала
TG_CHANNEL_ID=<числовой_id_канала>
TG_ADMIN_USER_IDS=<telegram_user_id_редактора>
TG_WEBHOOK_SECRET=<случайный_секрет>
TELEGRAM_WEBHOOK_ENFORCE_IPS=true
BOT_TOKEN=<bot_token>
NOTIFY_CHAT_ID=<id_служебной_группы>

PUBLISH_MODE=backend_api
NEWS_BOT_API_BASE=https://dev.nedra.kz/api/internal
NEWS_BOT_API_TOKEN=<выданный_backend_токен>

API_HOST=127.0.0.1
API_PORT=8081
PUBLIC_API_BASE=https://dev.nedra.kz/tg

IMAGE_FALLBACK_MODE=openai
OPENAI_API_KEY=<openai_key>
CATEGORY_CLASSIFIER_MODE=openai
```

`TG_API_ID`, `TG_API_HASH` и файлы `*.session` на VPS не нужны: webhook работает
от служебного бота, а не от личного Telegram-аккаунта. Желательно, чтобы бот и
служебный чат принадлежали организации. Не копируйте на сервер локальные `.env`,
`data/`, `.venv`, `.pytest_cache` и `__pycache__`.

Защитите файл:

```bash
sudo chown tg2site:tg2site /opt/tg2site/.env
sudo chmod 600 /opt/tg2site/.env
```

## 3. nginx

Добавьте содержимое `deploy/nginx-tg2site.conf` внутрь HTTPS server-блока
`dev.nedra.kz`, затем:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

`X-Real-IP` обязателен: приложение проверяет Telegram IP после того, как nginx
заменил этот заголовок реальным адресом клиента.

## 4. systemd

```bash
sudo cp /opt/tg2site/deploy/tg2site.service /etc/systemd/system/tg2site.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg2site
sudo systemctl status tg2site
curl http://127.0.0.1:8081/health
curl https://dev.nedra.kz/tg/health
```

Внешний health должен вернуть `status: ok` и состояние `moderation_queue`.

## 5. Регистрация Telegram webhook

Выполните на VPS после успешной проверки HTTPS. Значения читаются из `.env` и
не должны попадать в историю команд в открытом виде.

```bash
cd /opt/tg2site
set -a
source .env
set +a

curl -sS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -H 'Content-Type: application/json' \
  --data "{\"url\":\"https://dev.nedra.kz/tg/webhook\",\"secret_token\":\"${TG_WEBHOOK_SECRET}\",\"allowed_updates\":[\"channel_post\",\"callback_query\"],\"drop_pending_updates\":true,\"max_connections\":20}"

curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

`drop_pending_updates=true` используйте только при первой настройке. При
повторной регистрации ставьте `false`, иначе Telegram удалит ожидающие события.
Проверьте отсутствие `last_error_message` и значение `pending_update_count`.

После установки webhook нельзя параллельно запускать `getUpdates` для того же
бота. На VPS должен быть включён именно `TELEGRAM_INGEST_MODE=webhook`.

## 6. Проверка

Сначала используйте тестовый канал:

1. опубликуйте пост с одной ссылкой на статью;
2. проверьте появление сообщения с категориями в служебном чате;
3. нажмите категорию;
4. убедитесь, что новость появилась на сайте ровно один раз;
5. проверьте текст, источник и изображение;
6. повторно отправьте тот же webhook и убедитесь, что дубль не появился.

Логи и SQLite:

```bash
sudo journalctl -u tg2site -f
sudo ls -lh /var/lib/tg2site/state.db
```

Backend использует `external_id=tg:<channel_id>:<message_id>`, поэтому повтор
после сетевого таймаута безопасен. Сделайте ежедневный backup
`/var/lib/tg2site`; там находятся SQLite, черновики и фотографии.

## 7. Обновление

```bash
sudo systemctl stop tg2site
sudo -u tg2site /opt/tg2site/.venv/bin/python -m pip install \
  -r /opt/tg2site/requirements.txt
sudo systemctl start tg2site
sudo journalctl -u tg2site -n 100 --no-pager
```
