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

## Public API для Viksa Online

Public API — отдельное read-only FastAPI-приложение. Его можно запускать рядом с MaxBot в polling-mode; переключать MAX в webhook-mode не нужно.

Локальный backend base URL для фронтенда:

```text
http://127.0.0.1:8090
```

Запуск только public API:

```powershell
cd D:\AI\max-bot4
.\.venv\Scripts\python.exe -m app.api.server
```

Endpoints:

- `GET /health`
- `GET /api/public/catalog`
- `GET /api/public/media/{media_key}`
- локально и только при `CATALOG_ADMIN_LOCAL_ENABLED=true`:
  - `GET /api/admin/catalog/draft`
  - `PUT /api/admin/catalog/draft`
  - `PATCH /api/admin/catalog/facilities/{facility_id}`
  - `GET /api/admin/catalog/bot-preview?facility_id={facility_id}`
  - `POST /api/admin/catalog/validate`

Default CORS origins для локальной разработки:

- `http://127.0.0.1:5173`
- `http://localhost:5173`

Настройки:

```env
PUBLIC_API_HOST=127.0.0.1
PUBLIC_API_PORT=8090
PUBLIC_API_CORS_ORIGINS=http://127.0.0.1:5173,http://localhost:5173
CATALOG_ADMIN_LOCAL_ENABLED=false
CATALOG_OVERRIDES_PATH=data/catalog_overrides.local.json
```

JSON contract: `public_catalog.v1`. Стабильные поля для frontend:

- `business.contact_phone`
- `categories[].id`, `categories[].title`
- `facilities[].id`, `facilities[].title`, `facilities[].service_id`, `facilities[].capacity.max`, `facilities[].mediaRefs`, `facilities[].tariff_ids`, `facilities[].availability_state`
- `tariffs[].id`, `tariffs[].facility_id`, `tariffs[].price.amount`, `tariffs[].duration_minutes`
- `media[].key`, `media[].title`, `media[].publicUrl`, `media[].path`, `media[].storage`, `media[].exists`
- `availability_state.cacheState`
- `publicBookingPolicy.mode`, `publicBookingPolicy.readOnly`

Фото для браузера доступны через `media[].publicUrl`; frontend не должен читать локальные filesystem paths MaxBot.

Public catalog intentionally does not expose top-level internal `payment` or `post_payment` copies. Payment and confirmed-booking texts stay inside the MaxBot booking/payment flow.
Public catalog also does not expose `integration_refs`, YCLIENTS service/staff IDs, or post-payment instruction refs. Public tariff IDs are stable frontend IDs like `tariff:gazebo-1:1440:all:1`, not YCLIENTS-derived IDs.

Local draft/overrides:

- default file: `data/catalog_overrides.local.json`
- file is gitignored and intended only for local editable catalog work
- public catalog is built from current `admin_profile`/`app/data/services.yaml`, then the local draft is merged on top
- allowed draft sections: `categories`, `facilities`, `tariffs`, `media`
- allowed changes: category `title`/`visible`/`sort`; facility `title`/`publicTitle`/`description`/`warning`/`capacity.max`/`mediaRefs`/`visible`/`sort`; tariff `title`/`price.amount`/`duration_minutes`/`visible`/`sort`; media `title`/`aliases`/`path`/`storage`/`contentType`/`visible`/`sort`

Example draft:

```json
{
  "facilities": {
    "gazebo-1": {
      "publicTitle": "Беседка №1 у воды",
      "capacity": {
        "max": 45
      },
      "warning": "Локальный черновик"
    }
  },
  "tariffs": {
    "tariff:gazebo-1:1440:all:1": {
      "price": {
        "amount": 11000
      }
    }
  }
}
```

Включение локального admin draft API без изменения боевого `.env`:

```powershell
$env:CATALOG_ADMIN_LOCAL_ENABLED="true"
$env:CATALOG_OVERRIDES_PATH="data/catalog_overrides.local.json"
.\.venv\Scripts\python.exe -m app.api.server
```

Admin endpoints never create bookings, payments, YCLIENTS records, or Supabase writes; they only read/write the local JSON draft and validate its shape.

Frontend-friendly facility patch:

```http
PATCH /api/admin/catalog/facilities/gazebo-1
Content-Type: application/json
```

Bot preview uses the same resolved catalog layer as `GET /api/public/catalog` and shows the read-only facts/text MaxBot will use in informational answers:

```powershell
Invoke-RestMethod "http://127.0.0.1:8090/api/admin/catalog/bot-preview?facility_id=gazebo-1"
```

Quick local check:

```powershell
# terminal 1
$env:CATALOG_ADMIN_LOCAL_ENABLED="true"
$env:CATALOG_OVERRIDES_PATH="data/catalog_overrides.local.json"
.\.venv\Scripts\python.exe -m app.api.server

# terminal 2
Invoke-RestMethod "http://127.0.0.1:8090/health"
Invoke-RestMethod "http://127.0.0.1:8090/api/public/catalog"
Invoke-RestMethod "http://127.0.0.1:8090/api/admin/catalog/bot-preview?facility_id=gazebo-1"
```

```json
{
  "publicTitle": "Беседка №1 у воды",
  "description": "Описание для сайта",
  "capacity": {
    "max": 45
  },
  "mediaRefs": ["Беседка №1"],
  "tariffs": {
    "tariff:gazebo-1:1440:all:1": {
      "price": {
        "amount": 11000
      }
    }
  }
}
```

Successful response shape:

```json
{
  "ok": true,
  "mode": "maxbot-local-admin",
  "facility_id": "gazebo-1",
  "draft": {
    "facilities": {
      "gazebo-1": {
        "publicTitle": "Беседка №1 у воды",
        "description": "Описание для сайта",
        "capacity": {
          "max": 45
        },
        "mediaRefs": ["Беседка №1"]
      }
    },
    "tariffs": {
      "tariff:gazebo-1:1440:all:1": {
        "price": {
          "amount": 11000
        }
      }
    }
  },
  "validation": {
    "ok": true,
    "errors": [],
    "warnings": []
  }
}
```

Generated fixture для frontend:

```powershell
.\.venv\Scripts\python.exe scripts\generate_public_catalog_fixture.py
```

Fixture path:

```text
tests/fixtures/public_catalog.v1.json
```

## Run

```powershell
cd D:\AI\max-bot4
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\python.exe main.py
```

Если `.env` уже скопирован из старого проекта, повторять `copy .env.example .env` не нужно.

## Smoke

```powershell
cd D:\AI\max-bot4
.\scripts\validate.ps1 -IncludeSmoke
```

