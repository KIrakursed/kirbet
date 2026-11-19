# Kirbet — эксплуатация, подписки, платежи

## Компоненты

| Компонент | Где | Что делает |
|---|---|---|
| Сканер | `app/scheduler/scanner.py`, `app/main.py` | Плановый скан по сетке времени Asia/Almaty (`SCAN_INTERVAL_SECONDS`). Общий кеш 90 с и single-flight: параллельные запросы пользователей ждут один скан |
| Адаптеры | `app/providers/**` | Интерфейс `BaseProvider`: `health_check()`, `fetch_markets()`, `refresh_market()`, `status`. DOM-адаптеры (`common/dom_provider.py`): Olimpbet, Fonbet, Parimatch, Winline, Tennisi. Ubet читает данные, которые загрузила сама страница. 1xBet/Oinabet сначала проверяют публичную страницу (`access_gate.py`) |
| Браузер | `app/providers/browser/zendriver_client.py` | Один Chrome на процесс: robots.txt, тайм-ауты, HTTP-статус, проверка часового пояса страницы, перезапуск Chrome при >1,5 ГБ или каждые 300 страниц |
| Статусы источников | `app/providers/status.py` | WORKING / PARTIAL / DEGRADED / BLOCKED / UNAVAILABLE / DISABLED |
| Сопоставление | `app/matching/engine.py`, `app/normalization/names.py` | Вид спорта, тип рынка, период, время, оба участника (RU↔EN, алиасы, маркеры жен./мол./рез./люб., кибер-версии). Порог `MIN_MATCH_CONFIDENCE` (0,9). Принятые совпадения с причинами — в `matched_events` |
| Вилки | `app/arbitrage/*`, `app/validation/engine.py` | Decimal, комиссии, лимиты, округление, повторная загрузка цен перед уведомлением |
| Подписки | `app/billing/service.py` | Тарифы (данные в `plans`), лимиты по дню Алматы (`usage_events`), подписки, платежи, аудит |
| Telegram | `app/telegram/bot.py`, `app/telegram/commerce.py` | Меню, команды, оплата Telegram Stars, админ-панель |
| Прокси | `app/proxy_manager/` | Выключен. Никогда не используется для обхода блокировок и для Telegram/API-ключей |

## CLI

```
python -m app.cli status       # статус каждого источника по данным БД
python -m app.cli health       # БД, миграция, Telegram API, Chrome, диск, RAM, служба (код выхода = число ошибок)
python -m app.cli stats        # пользователи, подписки, платежи, сканы, совпадения, вилки
python -m app.cli bookmakers [--only winline] [--persist] [--json f.json]   # живая проверка DOM-адаптеров
python -m app.cli scan         # один полный скан с выводом
python -m app.cli providers    # health-check всех источников
python -m app.cli bot          # бот + плановый сканер (так запускает systemd)
```

## Тарифы и подписки

* Тарифы лежат в таблице `plans` (FREE/BASIC/PRO/VIP создаются при первом запуске). Цены и лимиты меняются командой `/setplan CODE FIELD VALUE` (нужен `/auth PIN`). Поля: `price_stars`, `price_kzt`, `period_days`, `scans_per_day`, `notifications_per_day`, `history_days`, `filters`, `details`, `active`, `title`.
* Что реально даёт тариф: число сканирований и уведомлений в день (сброс в 00:00 Asia/Almaty), глубина истории, свои фильтры букмекеров и видов спорта (BASIC+), подробный расчёт (PRO+). Рынки сверх 1X2/победителя собираются и хранятся, но в поиск вилок пока не идут, поэтому в тарифах не продаются.
* Подписка продлевается от даты окончания текущей. Истечение и очистка данных — раз в день Алматы, напоминание за 3 дня до окончания.

## Платежи

* Провайдер: **Telegram Stars (XTR)** — официальный механизм Telegram для цифровых услуг в ботах. Подписка активируется только по `successful_payment` от Telegram; `telegram_payment_charge_id` хранится как UNIQUE, повторная доставка не активирует подписку второй раз. Перед оплатой `pre_checkout_query` сверяет счёт, пользователя и сумму. Возврат: `/refund CHARGE_ID` (вызывает `refundStarPayment`).
* **По умолчанию выключено** (`PAYMENTS_ENABLED=false`): кнопка «Оформить» отвечает, что оплата ещё не включена. Включает только владелец, после юридической проверки.
* Kaspi Pay / платёжные шлюзы в тенге требуют договора с банком или эквайером (ИП/ТОО). Код под них не написан, чтобы не имитировать оплату. Для подключения нужны договор, реквизиты и документация API провайдера.

## Админ-панель (в Telegram)

`BOT_ADMIN_IDS` — кто видит `/admin`. Чувствительные команды требуют `/auth <ADMIN_PIN>`: PIN проверяется constant-time, сообщение с PIN удаляется, сессия 15 минут. Все действия пишутся в `audit_logs`.

Просмотр: `/admin /users /user ID /payments /errors /sources /scans`.
С PIN: `/grant ID PLAN DAYS`, `/cancel ID`, `/block ID`, `/unblock ID`, `/setplan CODE FIELD VALUE`, `/refund CHARGE_ID`, `/source NAME on|off`.

## Сервер (boxd `telegrambot`, Франкфурт)

* Служба `arb-bot` (systemd, `Restart=always`, `MemoryMax=3G`). Логи: `sudo journalctl -u arb-bot -n 100`. Журнал systemd ротируется автоматически (journald).
* Обновление: `powershell -ExecutionPolicy Bypass -File deploy\deploy_boxd.ps1 -Machine telegrambot -Region fra`. `.env` на сервере не перезаписывается — секреты (пароль БД, ADMIN_PIN) живут только там.
* PostgreSQL: локально на сервере, только 127.0.0.1. Перенос с SQLite: `bash deploy/setup_postgres.sh` (копия SQLite сохраняется, откат — `cp .env.before-postgres .env`).
* Резервное копирование: `boxd machine checkpoint save telegrambot <имя>` (вся машина) и дамп БД
  `sudo -u postgres pg_dump -Fc kirbet > ~/backup/kirbet-$(date -u +%F).dump`.
* Восстановление: `boxd machine checkpoint restore telegrambot <имя>` или `pg_restore -c -d kirbet файл.dump`.
* Удержание данных: снимки коэффициентов 7 дней, статусы источников 14, отклонённые вилки 60, счётчики 120. Пользователи, подписки, платежи и аудит не удаляются.

## Переменные окружения

См. `.env.example`. Секреты (`TELEGRAM_BOT_TOKEN`, `ODDS_API_KEY`, пароль в `DATABASE_URL`, `ADMIN_PIN`, `BETFAIR_*`, `PROXY_TOOLS_API_KEY`) — только в `.env` на сервере, файл с правами 600, в логи не выводятся.
