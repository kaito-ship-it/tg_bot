# tg2site — публикация новостей из Telegram на nedra.kz

Сервис принимает публикации Telegram-канала через webhook, извлекает ссылку на
статью, подготавливает новость и после выбора категории редактором публикует её
через защищённый backend API nedra.kz.

```text
Telegram-канал
      ↓ channel_post
Telegram Bot API webhook
      ↓
FastAPI + SQLite
      ↓
Выбор категории в служебном Telegram-чате
      ↓
Laravel backend API
      ↓
Новость на nedra.kz
```

## Возможности

- приём постов через HTTPS webhook служебного Telegram-бота;
- обработка только заданного канала;
- извлечение первой внешней ссылки из поста;
- парсинг заголовка, основного текста и изображения статьи;
- поддержка HTML-страниц, документов gov.kz, PDF и DOCX;
- очистка текста без пересказа и изменения фактов;
- получение категорий с backend API;
- выбор категории, отклонение новости и обновление AI-фото кнопками в Telegram;
- публикация через `POST /api/internal/news`;
- защита от дублей по `external_id`;
- постоянная SQLite-очередь для серии из нескольких постов;
- автоматические повторы временных ошибок backend и AI API;
- endpoint состояния `/health`.

Пост должен содержать внешнюю ссылку на статью. Посты без ссылки и
неподдерживаемые вложения игнорируются.

## Как работает публикация

1. В исходном канале публикуется пост со ссылкой.
2. Telegram отправляет `channel_post` на `/tg/webhook`.
3. Сервис проверяет секрет webhook и ID канала.
4. Ссылка сохраняется в SQLite-очередь.
5. Сервис загружает статью и формирует черновик.
6. Если фото отсутствует и включён AI fallback, создаётся обложка.
7. В служебный чат приходит карточка новости с категориями.
8. Редактор выбирает категорию или отклоняет материал.
9. Новость отправляется в защищённый backend API.
10. Backend возвращает ID и URL публикации, после чего сообщение Telegram
    обновляется до состояния «Опубликовано».

## Требования

- Ubuntu 22.04/24.04 и Python 3.11+;
- 1–2 vCPU, минимум 1 GB RAM;
- HTTPS-домен и nginx;
- служебный Telegram-бот;
- исходный канал и закрытый служебный чат редакторов;
- backend API token nedra.kz;
- OpenAI API key только при использовании AI-функций.

Для продакшена не нужны номер телефона, личная Telegram-сессия, `TG_API_ID` или
`TG_API_HASH`.

## Основные файлы

- `app/main.py` — запуск FastAPI и worker;
- `app/api.py` — health и webhook endpoints;
- `app/webhook_service.py` — очередь и Telegram-кнопки;
- `app/article_service.py` — загрузка и разбор статей;
- `app/draft_builder.py` — формирование черновика;
- `app/backend.py` — интеграция с Laravel API;
- `app/moderation_db.py` — постоянная SQLite-очередь;
- `app/image_service.py` — AI-обложки;
- `deploy/` — nginx, systemd и инструкция VPS;
- `.env.production.example` — шаблон настроек сервера.

## Установка на VPS

### 1. Пользователь и каталоги

```bash
sudo useradd --system --create-home \
  --home-dir /opt/tg2site --shell /bin/bash tg2site
sudo mkdir -p /opt/tg2site /var/lib/tg2site
sudo chown -R tg2site:tg2site /opt/tg2site /var/lib/tg2site
```

Клонируйте проект в `/opt/tg2site`. Не переносите локальные `.env`, `.venv`,
`data`, `*.session`, кэши и логи.

### 2. Python и зависимости

```bash
sudo -u tg2site python3 -m venv /opt/tg2site/.venv
sudo -u tg2site /opt/tg2site/.venv/bin/python -m pip install --upgrade pip
sudo -u tg2site /opt/tg2site/.venv/bin/python -m pip install \
  -r /opt/tg2site/requirements.txt
```

### 3. Настройки

```bash
sudo -u tg2site cp \
  /opt/tg2site/.env.production.example /opt/tg2site/.env
sudo -u tg2site nano /opt/tg2site/.env
```

Минимальная конфигурация:

```dotenv
DATA_DIR=/var/lib/tg2site

TELEGRAM_INGEST_MODE=webhook
TELEGRAM_CHANNEL=@your_channel
TG_CHANNEL_ID=-1000000000000
TG_ADMIN_USER_IDS=111111111,222222222
TG_WEBHOOK_SECRET=replace_with_a_long_random_secret
TELEGRAM_WEBHOOK_ENFORCE_IPS=true
BOT_TOKEN=replace_with_service_bot_token
NOTIFY_CHAT_ID=-1000000000001

PUBLISH_MODE=backend_api
NEWS_BOT_API_BASE=https://dev.nedra.kz/api/internal
NEWS_BOT_API_TOKEN=replace_with_backend_token

API_HOST=127.0.0.1
API_PORT=8081
PUBLIC_API_BASE=https://dev.nedra.kz/tg
CORS_ORIGINS=https://dev.nedra.kz

IMAGE_FALLBACK_MODE=disabled
CATEGORY_CLASSIFIER_MODE=disabled
```

Создать webhook secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Защитить `.env`:

```bash
sudo chown tg2site:tg2site /opt/tg2site/.env
sudo chmod 600 /opt/tg2site/.env
```

## Backend API

Сервис использует два endpoint относительно `NEWS_BOT_API_BASE`:

```http
GET /news-categories
Authorization: Bearer <NEWS_BOT_API_TOKEN>
Accept: application/json
```

```http
POST /news
Authorization: Bearer <NEWS_BOT_API_TOKEN>
Content-Type: application/json
Accept: application/json
```

В публикацию передаются `external_id`, `category_id`, `title`, `lead`,
`content_html`, `source_url`, `source_name`, `image_url` и `status=published`.

`external_id` имеет формат `tg:<channel_id>:<message_id>`. Backend должен
обеспечивать идемпотентность по этому значению, чтобы повтор после таймаута не
создавал дубль.

## nginx

Добавьте `deploy/nginx-tg2site.conf` в HTTPS server-блок:

```nginx
location /tg/ {
    proxy_pass http://127.0.0.1:8081/;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
    client_max_body_size 2m;
}
```

```bash
sudo nginx -t
sudo systemctl reload nginx
```

`X-Real-IP` обязателен при включённой проверке IP Telegram.

## systemd

```bash
sudo cp /opt/tg2site/deploy/tg2site.service \
  /etc/systemd/system/tg2site.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg2site
sudo systemctl status tg2site
```

Проверка:

```bash
curl http://127.0.0.1:8081/health
curl https://dev.nedra.kz/tg/health
```

Ответ должен содержать `status: ok` и `moderation_queue`.

## Регистрация Telegram webhook

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

`drop_pending_updates=true` используйте только при первой регистрации. При
повторной настройке установите `false`.

## AI-обложки и категория

AI-функции по умолчанию выключены. Для генерации изображения при отсутствии
фото:

```dotenv
IMAGE_FALLBACK_MODE=openai
OPENAI_API_KEY=replace_with_new_server_key
OPENAI_IMAGE_MODEL=gpt-image-1-mini
OPENAI_IMAGE_QUALITY=medium
OPENAI_IMAGE_SIZE=1536x1024
```

Для AI-классификации при неоднозначном результате:

```dotenv
CATEGORY_CLASSIFIER_MODE=openai
OPENAI_TEXT_MODEL=gpt-5.4-nano
```

Редактор может обновить сгенерированную обложку до выбора категории.
`PUBLIC_API_BASE` должен быть публичным HTTPS-адресом `/tg`, иначе backend не
сможет скачать подготовленное изображение.

## Проверка полного сценария

1. Опубликуйте в тестовом канале пост с одной внешней ссылкой.
2. Проверьте карточку новости в служебном чате.
3. При необходимости обновите AI-обложку.
4. Выберите категорию.
5. Убедитесь, что сообщение изменилось на «Опубликовано».
6. Проверьте заголовок, текст, источник и изображение на сайте.
7. Отправьте 3–5 ссылок подряд и проверьте последовательную обработку.

Логи и SQLite:

```bash
sudo journalctl -u tg2site -f
sudo ls -lh /var/lib/tg2site/state.db
```

## Тесты

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/check_no_secrets.py
.venv/bin/python -m pytest
```

## Обновление

```bash
sudo systemctl stop tg2site
cd /opt/tg2site
sudo -u tg2site git pull --ff-only
sudo -u tg2site /opt/tg2site/.venv/bin/python -m pip install \
  -r /opt/tg2site/requirements.txt
sudo systemctl start tg2site
sudo journalctl -u tg2site -n 100 --no-pager
```

## Безопасность

- используйте служебного бота и чат организации;
- не храните секреты в Git и README;
- ограничьте редакторов через `TG_ADMIN_USER_IDS`;
- используйте случайный `TG_WEBHOOK_SECRET`;
- оставляйте `TELEGRAM_WEBHOOK_ENFORCE_IPS=true`;
- храните `.env` с правами `600`;
- создавайте backup `/var/lib/tg2site`;
- перед push запускайте `python scripts/check_no_secrets.py`.

Дополнительные детали находятся в
[`deploy/README_VPS.md`](deploy/README_VPS.md).
