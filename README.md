# tg2site

Production-сервис для публикации новостей из Telegram на nedra.kz.

```text
Telegram channel_post
        ↓ HTTPS webhook
FastAPI → SQLite queue → разбор статьи
        ↓
Закрытый Telegram-чат редакторов
        ↓ выбор категории
Laravel backend API → сайт
```

Сервис работает только через Telegram Bot API webhook и backend API сайта.
Личные Telegram-аккаунты, пользовательские сессии и доступ к браузеру не
используются.

## Возможности

- принимает публикации только от настроенного канала;
- требует внешнюю ссылку на новость;
- извлекает заголовок, текст и изображение из HTML, PDF и DOCX;
- использует прикреплённое к Telegram-посту фото в приоритетном порядке;
- фильтрует локальные адреса, редиректы и слишком большие ответы;
- сохраняет задания в SQLite и последовательно обрабатывает серии публикаций;
- показывает категории backend в закрытом Telegram-чате;
- разрешает кнопки только пользователям из `TG_ADMIN_USER_IDS`;
- генерирует изображение через OpenAI, если это явно включено;
- позволяет перегенерировать AI-изображение до публикации;
- отправляет новость в идемпотентный backend endpoint;
- выдаёт локальные изображения backend по подписанным ссылкам;
- повторяет временные ошибки разбора и backend-запросов;
- автоматически очищает устаревшие черновики и записи очереди.

## Требования

- Ubuntu 22.04/24.04;
- Python 3.11 или новее;
- nginx и действующий HTTPS-домен;
- 1–2 vCPU и 1 GB RAM;
- служебный Telegram-бот;
- закрытая Telegram-группа редакторов;
- токен внутреннего API nedra.kz.

Бот должен быть добавлен администратором исходного канала и участником
закрытой группы редакторов с возможностью отправлять и редактировать сообщения.

## Быстрый запуск тестов

Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/check_no_secrets.py
.venv/bin/python -m compileall -q app scripts
.venv/bin/ruff check app scripts tests
.venv/bin/ruff format --check app scripts tests
.venv/bin/python -m pytest
.venv/bin/python -m pip check
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\check_no_secrets.py
.\.venv\Scripts\python.exe -m compileall -q app scripts
.\.venv\Scripts\ruff.exe check app scripts tests
.\.venv\Scripts\ruff.exe format --check app scripts tests
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pip check
```

## Установка на VPS

### 1. Подготовить систему

```bash
sudo apt update
sudo apt install -y git nginx python3 python3-venv
sudo useradd --system --create-home --home-dir /opt/tg2site --shell /bin/bash tg2site
sudo mkdir -p /opt/tg2site /var/lib/tg2site
sudo chown -R tg2site:tg2site /opt/tg2site /var/lib/tg2site
```

Клонируйте приватный репозиторий в `/opt/tg2site` с помощью deploy key или
другого репозиторного credential организации. Не копируйте локальные `.env`,
`data`, `.venv`, кэши и логи.

```bash
sudo -u tg2site python3 -m venv /opt/tg2site/.venv
sudo -u tg2site /opt/tg2site/.venv/bin/python -m pip install --upgrade pip
sudo -u tg2site /opt/tg2site/.venv/bin/python -m pip install \
  -r /opt/tg2site/requirements.txt
```

### 2. Настроить окружение

```bash
sudo -u tg2site cp /opt/tg2site/.env.production.example /opt/tg2site/.env
sudo -u tg2site nano /opt/tg2site/.env
sudo chmod 600 /opt/tg2site/.env
```

Создайте два разных секрета — для webhook и подписанных изображений:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Обязательные значения:

```dotenv
TG_CHANNEL_ID=-1000000000000
TG_ADMIN_USER_IDS=111111111,222222222
BOT_TOKEN=<organization_bot_token>
NOTIFY_CHAT_ID=-1000000000001
TG_WEBHOOK_SECRET=<first_random_secret>
TELEGRAM_WEBHOOK_ENFORCE_IPS=true

NEWS_BOT_API_BASE=https://dev.nedra.kz/api/internal
NEWS_BOT_API_TOKEN=<backend_token>
ADMIN_BASE_URL=https://dev.nedra.kz/admin/news

API_HOST=127.0.0.1
API_PORT=8081
PUBLIC_API_BASE=https://dev.nedra.kz/tg
MEDIA_SIGNING_SECRET=<second_random_secret>
DATA_DIR=/var/lib/tg2site
ALLOW_INSECURE_HTTP=false
```

Проверьте настройки до запуска:

```bash
cd /opt/tg2site
sudo -u tg2site /opt/tg2site/.venv/bin/python -m scripts.check_config
```

### 3. Подключить nginx

В HTTPS `server`-блок домена добавьте:

```nginx
include /opt/tg2site/deploy/nginx-tg2site.conf;
```

Затем:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Конфигурация открывает только `/tg/webhook`, `/tg/health` и
`/tg/photo/<draft_id>`. Сам FastAPI слушает только `127.0.0.1`.

### 4. Запустить systemd

```bash
sudo cp /opt/tg2site/deploy/tg2site.service /etc/systemd/system/tg2site.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg2site
sudo systemctl status tg2site --no-pager
curl --fail http://127.0.0.1:8081/health
curl --fail https://dev.nedra.kz/tg/health
```

### 5. Зарегистрировать Telegram webhook

Скрипт читает токен из `.env` и не помещает его в командную строку:

```bash
cd /opt/tg2site
sudo -u tg2site /opt/tg2site/.venv/bin/python -m scripts.set_webhook --drop-pending
```

`--drop-pending` применяйте только при первой установке. При последующих
регистрациях запускайте команду без этого флага.

## Контракт backend API

Относительно `NEWS_BOT_API_BASE` используются:

```http
GET /news-categories
Authorization: Bearer <NEWS_BOT_API_TOKEN>
```

```http
POST /news
Authorization: Bearer <NEWS_BOT_API_TOKEN>
Content-Type: application/json
```

Публикация содержит `external_id`, `category_id`, `title`, `lead`,
`content_html`, `source_url`, `source_name`, `image_url` и `status`.

Backend обязан обеспечивать уникальность `external_id`. Значение имеет формат
`tg:<channel_id>:<message_id>` и предотвращает дубль при повторе после сетевого
таймаута.

## AI-функции

По умолчанию AI выключен. Для обложек и классификации:

```dotenv
IMAGE_FALLBACK_MODE=openai
CATEGORY_CLASSIFIER_MODE=openai
OPENAI_API_KEY=<server_key>
OPENAI_IMAGE_MODEL=gpt-image-1-mini
OPENAI_IMAGE_QUALITY=medium
OPENAI_IMAGE_SIZE=1536x1024
OPENAI_TEXT_MODEL=gpt-5.4-nano
```

OpenAI key должен принадлежать серверному проекту организации. Не переносите
личный ключ из локальной разработки.

## Проверка полного сценария

1. Опубликуйте в тестовом канале пост с внешней HTTPS-ссылкой.
2. Убедитесь, что карточка появилась в закрытом чате.
3. При необходимости обновите AI-изображение.
4. Выберите категорию разрешённым аккаунтом.
5. Проверьте, что новость появилась на сайте ровно один раз.
6. Отправьте 3–5 ссылок подряд и проверьте их последовательную обработку.
7. Нажмите кнопку с аккаунта, которого нет в `TG_ADMIN_USER_IDS`: публикация
   должна быть запрещена.

Логи:

```bash
sudo journalctl -u tg2site -n 200 --no-pager
sudo journalctl -u tg2site -f
```

## Обновление

```bash
sudo systemctl stop tg2site
cd /opt/tg2site
sudo -u tg2site git pull --ff-only
sudo -u tg2site /opt/tg2site/.venv/bin/python -m pip install \
  -r /opt/tg2site/requirements.txt
sudo -u tg2site /opt/tg2site/.venv/bin/python -m scripts.check_config
sudo systemctl start tg2site
sudo systemctl status tg2site --no-pager
```

## Данные и резервные копии

`/var/lib/tg2site` содержит SQLite, черновики и изображения. Для простой
согласованной резервной копии остановите сервис на несколько секунд:

```bash
sudo systemctl stop tg2site
sudo tar -C /var/lib -czf /var/backups/tg2site-$(date +%F-%H%M).tar.gz tg2site
sudo systemctl start tg2site
```

Не коммитьте `.env` и содержимое `data/`. Перед каждым push запускайте
`python scripts/check_no_secrets.py`.

Расширенный VPS-чеклист: [`deploy/README_VPS.md`](deploy/README_VPS.md).
