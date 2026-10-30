# video-parser

Telegram-бот для автора контента про нейросети:

- **🔥 Подборка** — находит свежие залетающие ролики из TikTok и Instagram Reels по вашим темам
  (хэштеги и поисковые запросы), ранжирует их по скорости набора просмотров, вовлечённости и
  отрыву от числа подписчиков автора. Уже присланные ролики не повторяются. Может приходить сам каждый день.
- **📊 Разбор аккаунта** — присылаете ссылку на профиль TikTok или Instagram. Бот собирает последние
  ролики, считает медиану просмотров, находит залетевшие (×2 к медиане и выше), скачивает их, вырезает
  кадры (хук в первые секунды и дальше по ролику), берёт расшифровку речи и отдаёт всё Claude.
  В ответ — почему каждый ролик зашёл, как устроен хук и структура, что улучшить, чем хиты отличаются
  от слабых роликов, рекомендации и идеи следующих роликов.
- **Разбор одного ролика** — ссылка на конкретное видео или кнопка «🧠 Почему залетело» под роликом из подборки.

## Что нужно

| Что | Где взять |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | создать бота у [@BotFather](https://t.me/BotFather) командой `/newbot` |
| `APIFY_TOKEN` | у вас уже есть — [console.apify.com → Settings → Integrations](https://console.apify.com/settings/integrations) |
| `ANTHROPIC_API_KEY` | [console.anthropic.com → API keys](https://console.anthropic.com/settings/keys), нужен для разбора роликов |
| `ALLOWED_USER_IDS` | ваш Telegram ID — бот сам пришлёт его на `/start`, пока список пуст |
| Сервер | любой VPS или компьютер, где бот будет работать постоянно; нужны Python 3.11+ и ffmpeg (или Docker) |

Бот отвечает только пользователям из `ALLOWED_USER_IDS`, чтобы посторонние не тратили ваши кредиты Apify и Claude.

## Запуск через Docker

```bash
cp .env.example .env    # заполнить токены
docker compose up -d --build
docker compose logs -f
```

## Запуск без Docker

```bash
sudo apt install ffmpeg          # macOS: brew install ffmpeg
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env             # заполнить токены
.venv/bin/python -m bot
```

Первый запуск: оставьте `ALLOWED_USER_IDS` пустым, напишите боту `/start` — он ответит вашим ID.
Впишите ID в `.env` и перезапустите бота.

## Расшифровка речи

- **TikTok** — берутся автоматические субтитры TikTok (если они есть у ролика).
- **Reels** — у Instagram субтитров нет, поэтому для них лучше включить локальное распознавание речи
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper): `pip install -r requirements-whisper.txt`
  (в Docker — `WITH_WHISPER: "1"` в `docker-compose.yml`) и `WHISPER_MODEL=small` в `.env`.
  Работает на процессоре, бесплатно, модель скачается при первом использовании (~500 МБ).

Без расшифровки Claude разбирает ролик по кадрам, подписи и метрикам и прямо говорит, что речи не видел.

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
- `DIGEST_TIME` — время ежедневной подборки (пусто — выключено), `TIMEZONE`;
- `PROFILE_VIDEOS`, `ANALYZE_TOP` — сколько роликов собирать с аккаунта и сколько разбирать подробно;
- `CLAUDE_MODEL`, `CLAUDE_EFFORT` — модель и глубина анализа.

## Что сколько стоит

- **Apify** — акторы `clockworks/tiktok-scraper`, `apify/instagram-reel-scraper`,
  `apify/instagram-hashtag-scraper`, `apify/instagram-scraper` платные за результат (центы за сотню роликов,
  точные цены — на страницах акторов). Подборка по 6 темам × 30 роликов ≈ 180 результатов на площадку.
  Скачивание видео TikTok для разбора — отдельный запуск только для лучших роликов.
  `APIFY_MAX_CHARGE_USD` ограничивает расход на один запуск.
- **Claude** — один разбор аккаунта (5 роликов × 6 кадров + расшифровки) — порядка нескольких десятков
  тысяч входных токенов. Запросы идут с серверным фолбэком (`fallbacks: "default"`): если основная
  модель откажется отвечать, запрос автоматически повторится на запасной модели.

## Как устроено

```
bot/
  __main__.py      запуск, ежедневная подборка
  handlers.py      команды, кнопки, ссылки (aiogram 3)
  service.py       сценарии: подборка, разбор аккаунта, разбор ролика
  apify_source.py  запуски акторов Apify
  models.py        единый формат ролика для TikTok и Instagram
  metrics.py       ранжирование подборки, поиск залетевших роликов
  media.py         скачивание видео, кадры (ffmpeg), субтитры, Whisper
  analyzer.py      разбор через Claude (структурированный JSON-ответ)
  formatting.py    тексты сообщений
  storage.py       SQLite: присланные ролики, кэш, темы
```

Если какой-то актор Apify поменяет формат входа, поля можно дополнить без правки кода через
`APIFY_*_EXTRA_INPUT` (JSON), а сам актор заменить через `APIFY_*_ACTOR`.

## Тесты

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```
