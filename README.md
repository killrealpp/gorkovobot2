# admin_niz_mvp

Компактный MVP Telegram-бота бронирования.

AI используется только как JSON-парсер. Сценарий бронирования, проверка доступности, оплата и запись в YCLIENTS выполняются backend-ом.

## Что внутри

- `main.py` - точка запуска.
- `app/bot/telegram.py` - Telegram polling.
- `app/dialog/engine.py` - step-based сценарий бронирования.
- `app/ai/parser.py` - один JSON-контракт для DeepSeek.
- `app/integrations/yclients.py` - проверка/создание/обновление записей YCLIENTS.
- `app/integrations/yookassa.py` - создание и проверка платежей.
- `app/data/services.yaml` - услуги, цены, вместимость и YCLIENTS ids.
- `bot.sqlite3` - локальное состояние диалогов, сообщений, hold-резервов, заявок и уведомлений админу.

## Критичный контур

- Все входящие и исходящие сообщения сохраняются в `messages`.
- Перед оплатой создается временный hold на слот, чтобы другой клиент не занял то же время.
- После оплаты hold конвертируется в бронь, а backend создает запись в YCLIENTS.
- Ошибки и ручные ситуации пишутся в `system_logs` и попадают в очередь `admin_notifications`.
- Если задан `ADMIN_TELEGRAM_CHAT_ID`, бот отправляет админу уведомления.

## Run

```powershell
cd D:\admin_niz_mvp
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
python main.py
```

Если `.env` уже скопирован из старого проекта, повторять `copy .env.example .env` не нужно.

## Smoke

```powershell
cd D:\admin_niz_mvp
python tests\smoke.py
python -m compileall .
```
