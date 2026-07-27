# Business Profile

Эта папка в корне проекта хранит изменяемые бизнес-данные бота без секретов.

## Файлы

- `admin_profile.yaml` - активный профиль бизнеса: тематика, ассистент, анкета v1, услуги, цены, длительности, YCLIENTS service/staff ids, медиа, допы, правила оплаты/отмены, post-payment тексты, FAQ, knowledge и prompt rules.

## Что не хранить здесь

- токены MAX/Telegram;
- OpenAI/OpenRouter ключи;
- YooKassa/YCLIENTS секреты;
- пароли базы данных;
- локальные SQLite базы, логи, временные файлы.

Секреты и технические режимы запуска остаются в `.env`.

## Активный путь

По умолчанию приложение читает:

```text
business_profile/admin_profile.yaml
```

Если нужно временно подключить другой профиль, можно указать `ADMIN_PROFILE_PATH` в `.env`.

## Фото И Медиа

Фото хранятся как обычные файлы в `app/images/`, а связи между услугами и фото задаются в `business_profile/admin_profile.yaml`.

Чтобы заменить или добавить фото:

1. Положите файл в `app/images/`, например `app/images/massage-room.jpg`.
2. Добавьте или измените запись в `media.items`:

```yaml
media:
  items:
    massage_room:
      title: "Кабинет массажа"
      path: "app/images/massage-room.jpg"
      aliases: ["кабинет", "фото кабинета", "массажный кабинет"]
```

3. Привяжите фото к услуге через `media_key`:

```yaml
services:
  classic_massage:
    title: "Классический массаж"
    media_key: "massage_room"
```

Если у услуги есть варианты, `media_key` можно указывать внутри конкретного варианта. Ключ в `media_key` должен точно совпадать с ключом из `media.items`.

Важные правила:

- `path` сейчас должен вести на локальный файл, который реально существует в проекте.
- Для MAX и Telegram используется один и тот же каталог `media.items`.
- `aliases` нужны, чтобы бот понял просьбы клиента вроде "покажи фото бани" или "скинь фото кабинета".
- Если фото больше не нужно, уберите `media_key` у услуги/варианта и удалите старую запись из `media.items`.
- После замены фото проверьте профиль командой:

```powershell
.\.venv\Scripts\python.exe tests\admin_profile_smoke.py
```
