# Точки входа и карта модулей

Статическая инвентаризация 07.09.2026. Пути ниже относительно `WalletHunterV05_final/`. Это описание имеющегося кода, не подтверждение корректности и не инструкция немедленно запускать торговлю. Runtime, сеть и production в этой документирующей задаче не запускались.

## Процессы

| Точка входа | Роль | Побочные эффекты / запуск |
| --- | --- | --- |
| `desktop/main.py` | Telethon-бот, Telegram сообщения, копирование, AI-наблюдатели | Источник запуска — `python desktop/main.py` из APP. Уже импорт создаёт Settings/Storage/SQLite/Telegram client; `main():727–746` авторизует бота, обновляет menu button и запускает фоновые задачи. |
| `webapp/server.py` | FastAPI, Mini App static и API | `uvicorn webapp.server:app`; import-level создание Storage/SQLite-сервисов (`:46–57`). Linux unit привязывает к loopback 8090. Запуск API не равен запуску bot watcher. |
| `scripts/*.py`, `scripts/*.sh`, `deploy_v05.sh` | Backup, миграция, деплой и аудит | Не библиотечные «безопасные проверки». Часть создаёт state/DB на импорте, часть останавливает/запускает службы и меняет режимы. Разбор в [RECOVERY.md](RECOVERY.md). |

В боте стартуют шесть задач (`desktop/main.py:740–745`): `watcher` — реальное/PAPER-копирование согласно его флагам; `ai_trader_watcher` — независимый виртуальный трейдер; `ai_learning_watcher` — публичные данные/обучение; `ai_review_watcher` — старое исследовательское review; `ai_position_watcher` и `ai_entry_watcher` — подготовка подтверждаемых предложений и уведомления. Новые observers используют публичный reader, не подписывают ордера в фоне. Реальное исполнение подтверждённых форм находится в web decision API.

Copy watcher обходит пользователей последовательно, а обработчик исключения охватывает весь проход (`desktop/main.py:671–692`): отказ одного профиля может пропустить следующих. Интервал — пауза после прохода, не SLA. Другие AI-петли работают с собственными паузами/смещениями и сохраняют часть статусов; завершение отдельной задачи не контролируется `/health`.

## Авторизация и границы действий

`webapp/server.py:75–101` принимает `X-Telegram-Init-Data`, проверяет HMAC подпись с bot token через constant-time compare, допускает возраст до 24 часов и часы не более чем на 60 секунд в будущем. User ID берётся из подписанного `user`, не из произвольного тела запроса. Профили адресуются этим Telegram ID; мутации используют профильные/account locks.

Ограничения: `TELEGRAM_OWNER_ID` не используется как allowlist. Подпись Telegram доказывает Telegram-сессию, **не контроль Hyperliquid-адреса**: привязка аккаунта пока проверяет формат, непустой key и конфликт адреса с другим профилем, но не агентские права/контроль на бирже (`server.py:459–486`, P1-08). Обработка Telegram message/callback и групповое использование требуют отдельной проверки контрактов; HMAC web-auth не защищает эти отдельные пути автоматически.

`/`, `/static/*`, `/health` не требуют Mini App auth. FastAPI docs/redoc выключены, но `openapi_url` не отключён явно. Health возвращает успешный ответ без проверки watcher, биржи, свежести данных, БД или исполнения (`server.py:117–119`); это liveness web, не readiness торговли.

| API-группа | Маршруты | Характер действия |
| --- | --- | --- |
| Главная и настройки | `GET /api/dashboard`; `PUT /api/settings` | Чтение профиля/баланса и изменение настроек. |
| Копирование/слоты | `POST /api/copy`; `POST /api/wallet/{slot}`; `POST /api/wallet`; `DELETE /api/wallet/{slot}`; `POST /api/ai/slot` | Меняют профиль/владение слотами; продолжение копирования может повлиять на реальные позиции. |
| Аккаунт | `PUT`, `DELETE /api/account`; `GET /api/account/open-positions`; `GET /api/account/report`; `GET /api/wallet/{slot}/open-positions` | Привязка секрета, detach, позиции и отчёт. Preview открытых позиций — не доказательство контроля аккаунта. |
| Ручная торговля | `POST /api/position/close`; `POST`, `DELETE /api/position/stop-loss`; `POST /api/emergency` | Потенциальные реальные закрытия, trigger orders и отмены. Даже скрытая в UI аварийная кнопка не означает отсутствия endpoint. |
| Аналитика/рынок | `GET /api/analyse/{slot}`; `POST /api/analyse`; `GET /api/chart`, `/api/price`, `/api/markets` | Публичные биржевые данные с пользовательской авторизацией. Анализ произвольного адреса не добавляет слот; web analysis не записывает Telegram `leader_models`. |
| AI-сводка/режимы | `GET /api/ai`; `POST /api/ai/modes` | Сводка виртуального трейдера, модели, reviews, pending/history, переключатели подготовки/наблюдения. GET не отправляет реальный ордер, но summary может обслуживать/инвалидировать сохранённое состояние. |
| Новый AI-вход | `POST /api/ai/orders/prepare`; `POST /api/ai/orders/{proposal_id}/decision` | Prepare — публичные чтения + сохранение предложения. Строгое `{confirm: true}` может отправить замороженный IOC; отказ сохраняется. Не автономное разрешение. |
| AI-вмешательство | `POST /api/ai/positions/prepare`; `POST /api/ai/positions/{proposal_id}/decision`; `POST /api/ai/positions/resume` | Подтверждаемые действия с существующей позицией и снятие соответствующего HOLD для возобновления синхронизации. |
| Старое AI review | `POST /api/ai/reviews/{proposal_id}/decision`; `POST /api/ai/resume-copy` | Отдельный исторический исследовательский workflow; не путать с новыми executable proposals. |

Все перечисленные `/api/*` маршруты вызывают `require_user`. Наличие авторизации не устраняет P1-08/P1-10. `AUTO_TRADING=false` ограничивает engine-копирование, но **не является глобальным kill switch**: ручные и новые AI decision-пути имеют отдельные signing clients. Таймаут ответа о сделке не доказывает отсутствие исполнения; повторный POST нельзя считать безопасной проверкой статуса.

## Telegram и события

Главное сообщение/меню, очистка, информационные сообщения и часть исторических callbacks остаются в `desktop/main.py`. Guard `core/profile_mutation.py:30–35` / `desktop/main.py:448–452` перенаправляет старые изменяющие callbacks в Mini App; расположенные ниже старые trading/account UI-ветки не следует автоматически считать доступными. При этом Telegram `analyse():589–608` по-прежнему сохраняет eligibility и корреляции, используемые движком: P1-03.

Существуют разные журналы:

- Telegram message IDs и chat-clear bookkeeping — для доставки/очистки сообщений (`desktop/main.py:277–327`), не журнал подтверждённых исполнений.
- `profile.runtime.journal` — до 200 записей engine; dashboard показывает ограниченный хвост. Это UI-события, не полная exchange history (`core/trading_engine.py:613–616`, `webapp/server.py` dashboard).
- `executions.sqlite3` — журнал операций/ownership; AI proposals и history находятся в своих SQLite. Удаление сообщений не должно означать удаление торговых доказательств.
- История биржевых fills/позиции — отдельный источник сверки. Сам по себе пустой чат не доказывает отсутствие ошибок или ордеров.

## Активный frontend и старые файлы

В `webapp/static/index.html` напрямую загружаются 12 локальных JS: `app.js`, `chart.js`, `manual-trade.js`, `ai-modes.js`, `ai-learning.js`, `ai-user-orders.js`, `ai-position-actions.js`, `ai.js`, `ai-slot.js`, `audio.js`, `matrix.js`, `i18n.js`. `ai.js` лениво подгружает `ai-review.js`: всего **13 активных локальных модулей**. Внешний Telegram Web App script и локальные CSS — отдельные зависимости страницы.

| Группа | Ответственность |
| --- | --- |
| `app.js` | Шесть экранов, dashboard/слоты/анализ/настройки, формы аккаунта, privacy, основные API/reload-связи. |
| `chart.js`, `manual-trade.js` | Свечи, выбор рынка/таймфрейма, текущая цена/позиция, stop-loss и ручное закрытие. |
| `ai.js`, `ai-review.js` | Загрузка и сборка AI-экрана, старые review/research и HOLD-элементы. |
| `ai-modes.js`, `ai-slot.js` | PAPER-трейдер, ассистент позиции, состояние режима и выделение третьего слота. |
| `ai-learning.js` | Read-only карточка отдельной экспериментальной модели, ретроспектива/forward и свежесть коллектора. |
| `ai-user-orders.js`, `ai-position-actions.js` | Раздельные реальные формы с явным подтверждением, замороженными параметрами, expiry, privacy guard и обработкой неоднозначного ответа без retry POST. |
| `i18n.js`, `audio.js`, `matrix.js` | RU/EN, сохранённые настройки звука, декоративная анимация. Переводы также есть внутри модулей; одна таблица i18n не покрывает весь текст сама по себе. |

`stats.js` и `enhance.js` присутствуют на диске, но не подключены текущим `index.html`/активными loader-цепочками. Это legacy frontend, не две дополнительные работающие функции. Не удалялись в фазе 0. CSS: `style.css`, `danger.css`, `chart.css`, `ai.css`; обновление asset query version не является миграцией пользовательских данных.

График использует HTTP polling текущей цены (примерно каждые две секунды) и отдельные запросы свечей. WebSocket/event stream отсутствует. Dashboard/AI обновляются через загрузку страницы/экрана и явные reload после действий; это не постоянная push-доставка всех событий. Telegram notification, UI refresh и исследовательский observer — разные механизмы со своей свежестью.

## Карта backend и границы переиспользования

| Модули `core/` (если не указан иной каталог) | Роль |
| --- | --- |
| `settings`, `storage`, `state_snapshot`, `profile_mutation`, `legacy_metrics` | Конфигурация, зашифрованный JSON, merge/conflict и locks, миграция старых Infinity-метрик. Constructors/migrations могут писать состояние. |
| `hyperliquid`, `capital_snapshot`, `fill_history`, `position_history`, `order_precision`; `integrations/hyperliquid` | Публичное чтение и нормализация, капитал/полная история, точность размеров; signing SDK отделён в integrations. Нельзя считать все reader fallback строгими: P1-04. |
| `trading_engine`, `execution_journal`, `manual_positions` | План копирования, reconciliation/ownership, операция/состояние, ручные закрытия и SL. Общие locks/journal полезны, но не заменяют доказательство конкретного fill: P1-05. |
| `trade_analyzer`, `follower_simulator`, `analysis_metrics` | Аналитика кошелька и модельная симуляция; не обещание фактической доходности следующей копии. |
| `ai_assistant`, `ai_candidates`, `ai_probability`, `ai_outcomes`, `ai_review`, `ai_review_text`, `ai_research`, `ai_policy`, `ai_rescue_policy` | Исторические наблюдения, восемь факторов/сценарии, исследовательские оценки, reviews и политики. |
| `ai_paper_trader`, `ai_modes` | Отдельный виртуальный rules-based трейдер и сохранённая свежесть режимов. |
| `ai_learning`, `ai_learning_worker` | Общая экспериментальная logistic-модель на публичных BTC/ETH свечах, версии и forward-оценка; не доказанная торговая система. |
| `ai_entry_policy`, `ai_user_orders`, `ai_entry_observer` | Независимые новые AI-входы через подтверждение пользователя; уведомления не исполняют их скрыто. Жизненный цикл реальных резервов требует P1-07. |
| `ai_position_actions`, `ai_position_observer` | Предложения REDUCE/AVERAGE существующей позиции при триггере ROE, согласование действий и HOLD после вмешательства. Результат не гарантирован. |

## Тесты и наблюдаемость

В `tests/` есть Python unit/API/virtual-engine сценарии: precision/capital/history, storage/recovery/races, SDK/manual actions, AI learning/paper/confirmed orders/position actions. Node suites: `ui_report_test.cjs`, `ui_ai_review_test.cjs`, `ui_ai_modes_test.cjs`, `ui_ai_learning_test.cjs`, `ui_ai_user_orders_test.cjs`, `ui_ai_position_actions_test.cjs`. Скрипты `scripts/ui_*_visual_audit.cjs` отдельно создают offline-browser отчёты/скриншоты, поэтому не являются строго read-only командами.

Наличие теста или старого PASS-отчёта не доказывает покрытие P1-01…10, установленную версию на сервере или работу реального SDK. В рамках этой документационной задачи тесты/браузер/импорты не запускались; отчёт отдельных разрешённых изолированных тестов фазы 0 — в [PHASE0.md](PHASE0.md). Подготовленный workflow не означает, что Linux CI уже прошёл: локальные Windows-результаты, Linux-запуск, отдельные browser-аудиты и production-проверки надо различать. Не импортировать web/bot против рабочего `data/` при тестировании.

Текущие ограничения наблюдаемости: print/journald вместо единого структурированного журнала; нет общего readiness для шести задач и сильного привязывания всех UI-событий к order/fill ID; raw exception может содержать чувствительный URL (P1-09). Они документированы, а не исправлены. Полный перечень следующей фазы — [PHASE1_FINDINGS.md](PHASE1_FINDINGS.md).
