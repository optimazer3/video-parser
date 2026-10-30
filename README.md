# video-parser

Telegram-бот для автора контента про нейросети:

- **🔥 Подборка** — находит свежие залетающие ролики из TikTok и Instagram Reels по вашим темам
  (хэштеги и поисковые запросы), ранжирует их по скорости набора просмотров, вовлечённости и
  отрыву от числа подписчиков автора. Уже присланные ролики не повторяются. Может приходить сама каждый день.
- **📊 Разбор аккаунта** — присылаете ссылку на профиль TikTok или Instagram. Бот собирает последние
  ролики, считает медиану просмотров, находит залетевшие (×2 к медиане и выше), скачивает их, вырезает
  кадры (хук в первые секунды и дальше по ролику), берёт расшифровку речи и отдаёт всё нейросети.
  В ответ — почему каждый ролик зашёл, как устроен хук и структура, что улучшить, чем хиты отличаются
  от слабых роликов, рекомендации и идеи следующих роликов.
- **Разбор одного ролика** — ссылка на конкретное видео или кнопка «🧠 Почему залетело» под роликом из подборки.

Данные собирает [Apify](https://apify.com), разбирает и расшифровывает речь нейросеть через
[Polza.ai](https://polza.ai) (по умолчанию Claude Sonnet 5.5).

## Что нужно

| Что | Где взять |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | создать бота у [@BotFather](https://t.me/BotFather) командой `/newbot` |
| `APIFY_TOKEN` | [console.apify.com → Settings → Integrations](https://console.apify.com/settings/integrations) |
| `POLZA_API_KEY` | [polza.ai → API-ключи](https://polza.ai/dashboard/api-keys), на балансе должны быть деньги |
| `ALLOWED_USER_IDS` | ваш Telegram ID — бот сам пришлёт его на `/start`, пока список пуст |
| Python 3.11+ и ffmpeg | см. ниже |

Бот отвечает только пользователям из `ALLOWED_USER_IDS`, чтобы посторонние не тратили ваши деньги на Apify и Polza.

## Запуск на своём компьютере

Бот работает, пока запущен на компьютере. Сервер не нужен: бот сам опрашивает Telegram.

### 1. Скачать код

[Скачать ZIP](https://github.com/optimazer3/video-parser/archive/refs/heads/claude/amazing-allen-odgb9c.zip)
и распаковать, или через git:

```bash
git clone -b claude/amazing-allen-odgb9c https://github.com/optimazer3/video-parser.git
```

### 2. Установить Python и ffmpeg

**Windows** — поставить Python 3.12 с [python.org](https://www.python.org/downloads/) (в установщике
отметить «Add python.exe to PATH»), затем в PowerShell:

```powershell
winget install --id Gyan.FFmpeg -e
```

После установки ffmpeg закройте и снова откройте PowerShell.

**macOS**:

```bash
brew install python ffmpeg
```

### 3. Установить зависимости и заполнить настройки

Открыть терминал в папке проекта.

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
notepad .env
```

macOS / Linux:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
open -e .env
```

В `.env` вписать `TELEGRAM_BOT_TOKEN`, `APIFY_TOKEN`, `POLZA_API_KEY`. `ALLOWED_USER_IDS` пока оставить пустым.

### 4. Запустить

Windows:

```powershell
.venv\Scripts\python -m bot
```

macOS / Linux:

```bash
.venv/bin/python -m bot
```

Напишите боту `/start` — он ответит вашим Telegram ID. Впишите его в `ALLOWED_USER_IDS` в `.env`,
остановите бота (Ctrl+C) и запустите снова.

### Если Instagram заблокирован

Видео Reels бот скачивает со своего компьютера (кадры и речь нужны для разбора). Если Instagram у вас не
открывается, включите VPN на время работы бота или пропишите прокси в `MEDIA_PROXY`. TikTok, Apify и Polza
работают без этого.

## Команды

- `/feed` или кнопка «🔥 Подборка» — подборка роликов
- `/topics` — показать темы; `/topics #нейросети, #chatgpt, нейросети для работы` — задать свои
  (с `#` — хэштег, без — поисковый запрос); `/topics reset` — по умолчанию
- ссылка на профиль (`tiktok.com/@name`, `instagram.com/name`) — разбор аккаунта
- ссылка на ролик (`tiktok.com/@name/video/…`, `vm.tiktok.com/…`, `instagram.com/reel/…`) — разбор ролика
- `@name` — бот спросит, TikTok это или Instagram

## Настройки

Все настройки — в `.env`, описание в [.env.example](.env.example). Главные:

- `TOPICS`, `FEED_DAYS`, `FEED_SIZE`, `FEED_MIN_VIEWS` — что и как искать в подборке;
- `DIGEST_TIME` — время ежедневной подборки (пусто — выключено; приходит, только если бот в это время запущен);
- `PROFILE_VIDEOS`, `ANALYZE_TOP` — сколько роликов собирать с аккаунта и сколько разбирать подробно;
- `LLM_MODEL`, `LLM_REASONING_EFFORT` — модель и глубина разбора; модель должна понимать картинки;
- `TRANSCRIBE_MODEL` — модель расшифровки речи.

## Расшифровка речи

- **TikTok** — берутся автоматические субтитры TikTok (бесплатно), если они есть у ролика.
- **Reels и ролики TikTok без субтитров** — бот вырезает звук через ffmpeg и отправляет его в Polza на
  распознавание (`TRANSCRIBE_MODEL`, по умолчанию `openai/gpt-4o-mini-transcribe`, ~0,35 ₽ за минуту).

## Что сколько стоит

- **Apify** — акторы `clockworks/tiktok-scraper`, `apify/instagram-reel-scraper`,
  `apify/instagram-hashtag-scraper`, `apify/instagram-scraper` платные за результат (центы за сотню роликов,
  точные цены — на страницах акторов). Подборка по 6 темам × 30 роликов ≈ 180 результатов на площадку.
  Скачивание видео TikTok для разбора — отдельный запуск только для лучших роликов.
  `APIFY_MAX_CHARGE_USD` ограничивает расход на один запуск.
- **Polza** — разбор аккаунта (5 роликов × 6 кадров + расшифровки) на Claude Sonnet 5.5 по текущим ценам
  Polza выходит примерно в 10–20 ₽, на Claude Opus 5.5 — примерно вдвое дороже. Разбор одного ролика —
  несколько рублей. Расшифровка речи — около рубля на разбор.

## Как устроено

```
bot/
  __main__.py      запуск, ежедневная подборка
  handlers.py      команды, кнопки, ссылки (aiogram 3)
  service.py       сценарии: подборка, разбор аккаунта, разбор ролика
  apify_source.py  запуски акторов Apify
  models.py        единый формат ролика для TikTok и Instagram
  metrics.py       ранжирование подборки, поиск залетевших роликов
  media.py         скачивание видео, кадры и звук (ffmpeg), субтитры
  llm.py           клиент Polza.ai (OpenAI-совместимый API): чат и распознавание речи
  analyzer.py      разбор роликов нейросетью (ответ в JSON)
  formatting.py    тексты сообщений
  storage.py       SQLite: присланные ролики, кэш, темы
```

Если какой-то актор Apify поменяет формат входа, поля можно дополнить без правки кода через
`APIFY_*_EXTRA_INPUT` (JSON), а сам актор заменить через `APIFY_*_ACTOR`.

## Docker (для сервера, позже)

```bash
docker compose up -d --build
```

## Тесты

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```
