# VPS-чеклист tg2site

## До запуска

- [ ] Репозиторий клонирован в `/opt/tg2site` от пользователя `tg2site`.
- [ ] `/var/lib/tg2site` принадлежит `tg2site:tg2site`.
- [ ] Установлены зависимости из `requirements.txt`.
- [ ] `.env` создан из `.env.production.example` и имеет права `600`.
- [ ] Бот, канал, чат и API-токен принадлежат организации.
- [ ] Бот является администратором исходного канала и может писать в чат редакторов.
- [ ] `TG_ADMIN_USER_IDS` содержит только разрешённых редакторов.
- [ ] `TG_WEBHOOK_SECRET` и `MEDIA_SIGNING_SECRET` разные и случайные.
- [ ] Все внешние URL используют HTTPS.
- [ ] `python -m scripts.check_config` завершается успешно.
- [ ] `nginx -t` завершается успешно.

## Запуск

```bash
sudo cp /opt/tg2site/deploy/tg2site.service /etc/systemd/system/tg2site.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg2site
curl --fail http://127.0.0.1:8081/health
curl --fail https://dev.nedra.kz/tg/health
sudo -u tg2site /opt/tg2site/.venv/bin/python -m scripts.set_webhook --drop-pending
```

## Приёмка

- [ ] Пост из заданного канала появляется в очереди.
- [ ] Пост из другого канала игнорируется.
- [ ] Пост без внешней ссылки игнорируется.
- [ ] Неавторизованный пользователь не может нажать категорию.
- [ ] Авторизованный редактор публикует новость один раз.
- [ ] Изображение доступно backend по подписанному URL.
- [ ] Серия из пяти постов обрабатывается последовательно.
- [ ] После `systemctl restart tg2site` незавершённая очередь сохраняется.
- [ ] В `journalctl` отсутствуют секреты и полные Telegram Bot API URL.

## Эксплуатация

```bash
sudo systemctl status tg2site --no-pager
sudo journalctl -u tg2site -n 200 --no-pager
sudo du -sh /var/lib/tg2site
```

Настройте:

- ежедневную резервную копию `/var/lib/tg2site`;
- мониторинг `https://dev.nedra.kz/tg/health`;
- оповещение при перезапусках `tg2site.service`;
- регулярную установку проверенных Dependabot-обновлений.

Полная инструкция установки и обновления находится в корневом `README.md`.
