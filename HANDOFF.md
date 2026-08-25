# Передача проекта

Репозиторий содержит только production-flow:

`Telegram webhook → FastAPI/SQLite → выбор категории → backend API → сайт`.

## Передавать разработчикам

- исходный код `app/` и `scripts/`;
- тесты и CI-конфигурацию;
- `deploy/`;
- `.env.example` и `.env.production.example` без значений секретов;
- документацию.

## Не передавать и не коммитить

- `.env`;
- Telegram bot token и webhook secret;
- backend/OpenAI API tokens;
- содержимое `data/` и `/var/lib/tg2site`;
- виртуальные окружения, кэши и логи.

Каждая среда получает отдельные секреты через защищённый канал. Перед push:

```bash
python scripts/check_no_secrets.py
python -m compileall -q app scripts
ruff check app scripts tests
ruff format --check app scripts tests
python -m pytest
python -m pip check
```

Перед первым production-запуском используйте чеклист
`deploy/README_VPS.md`.
