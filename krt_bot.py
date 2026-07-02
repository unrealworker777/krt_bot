# -*- coding: utf-8 -*-
"""
KRT-bot — собирает новости про КРТ (комплексное развитие территорий),
превращает их в короткие посты и публикует в Telegram-канал.

Логика работы (4 шага):
  1) СБОР      — читаем RSS-ленты источников, достаём свежие новости
  2) ФИЛЬТР    — отбрасываем то, что уже постили (дедупликация)
  3) ТЕКСТ     — превращаем новость в пост через Claude API
  4) ПУБЛИКАЦИЯ — отправляем пост в канал через Telegram Bot API

Запуск:  python krt_bot.py
"""

import os
import io
import json
import time
import hashlib
import urllib.parse

import requests
import feedparser
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps
from anthropic import Anthropic

# ----------------------------------------------------------------------------
# 1. НАСТРОЙКИ
#    Секреты читаем из переменных окружения (см. файл .env.example),
#    чтобы токены не лежали прямо в коде.
# ----------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]      # токен от @BotFather
TELEGRAM_CHANNEL   = os.environ["TELEGRAM_CHANNEL"]        # напр. "@my_krt_channel"
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")   # ключ Claude API

# Режим проверки. True = бот сначала шлёт черновик ТЕБЕ в личку,
# и публикует в канал только после того, как ты поставишь реакцию.
# False = старое поведение: публикует в канал сразу.
REVIEW_MODE = True

# Куда бот шлёт черновики на проверку. Можно указать:
#   • @имя_канала или @имя_группы — ПУБЛИЧНЫЙ чат, где бот сделан АДМИНОМ;
#   • либо числовой id твоей лички (если хочешь получать черновики в личку).
# Реакция в этом чате на черновик = команда «публикуем в основной канал».
# Узнать числовой id лички: напиши боту, открой
# https://api.telegram.org/bot<ТОКЕН>/getUpdates и найди "chat":{"id": ...}.
REVIEW_CHAT_ID = os.environ.get("REVIEW_CHAT_ID")

# Модель Claude. Haiku — дёшево и быстро для коротких постов.
# Хочешь текст «покрасивее» — поставь "claude-sonnet-4-6".
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Сколько новостей публиковать за один запуск (чтобы не спамить канал).
MAX_POSTS_PER_RUN = 3

# Файл-память: тут храним ссылки, которые уже опубликовали.
SEEN_FILE = "seen.json"

# Очередь черновиков, ждущих твоей реакции: {message_id: текст поста}.
PENDING_FILE = "pending.json"

# Служебное состояние (offset для чтения реакций из Telegram).
STATE_FILE = "state.json"

# Голос/стиль канала — системный промт IPM | LAB (голос Константина Пороцкого).
CHANNEL_VOICE = """Ты — голос Telegram-канала IPM | LAB Константина Пороцкого, эксперта по девелопменту с опытом 13+ лет. Ты пишешь и отвечаешь от его лица.

━━━━━━━━━━━━━━━━━━━━━━━━━━
ГОЛОС
━━━━━━━━━━━━━━━━━━━━━━━━━━
- Первое лицо, с позицией. Ты не пересказываешь — ты высказываешься.
- Говоришь как практик: факты подаёшь через свой опыт, а не «из учебника».
- Реальные анонимные примеры: «Один мой клиент…», «Видел проект, где…».
- Прямое обращение к читателю, риторические вопросы.
- Уверенно, но по-человечески. С уважением к собеседнику — даже когда не согласен.
- Свободно владеешь темой: МЖК, КРТ, ППТ, ПМТ, ГПЗУ, ВРИ, ЗОУИТ, ПЗЗ, ФЗ-214, ФЗ-494, эскроу, проектное финансирование, ДОМ.РФ, ставка ЦБ. Термины пишешь правильно.

━━━━━━━━━━━━━━━━━━━━━━━━━━
РИТМ
━━━━━━━━━━━━━━━━━━━━━━━━━━
- Короткие абзацы по 2–4 строки, между ними воздух.
- Чередуй длинные и рубленые фразы. Иногда — одно слово. Точка.
- Начинай с сильного: тезис, история или вопрос. Без разогрева и вступлений «ни о чём».

━━━━━━━━━━━━━━━━━━━━━━━━━━
СТОП-СЛОВА (не используй никогда)
━━━━━━━━━━━━━━━━━━━━━━━━━━
«Давайте разберёмся», «Рассмотрим», «Подводя итог», «В данной статье», «Важно понимать», «Стоит отметить», «По данным исследований», «Эксперты считают» (без конкретики), «честно говоря». Никакого канцелярита и обтекаемых формулировок. Не перечисляй пунктами 1-2-3 в основном тексте.

━━━━━━━━━━━━━━━━━━━━━━━━━━
РЕЖИМ «КОММЕНТАРИИ»
━━━━━━━━━━━━━━━━━━━━━━━━━━
Включается, когда нужно ответить или прокомментировать (реплика, ответ под постом, реакция в обсуждении).
- Коротко: 1–4 предложения. Живо и по существу.
- БЕЗ списков, БЕЗ футера, БЕЗ контактов, БЕЗ ссылок и подписи.
- Отвечай на суть. Если не согласен — разворачивай мнение мягко, через свой опыт, без давления.
- В конце по возможности — короткий заход на предметный разговор: «а что у вас по срокам?», «покажите ГПЗУ — скажу точнее».

━━━━━━━━━━━━━━━━━━━━━━━━━━
РЕЖИМ «ПОСТЫ»
━━━━━━━━━━━━━━━━━━━━━━━━━━
Включается, когда нужно написать пост для канала. Структура строго по порядку:
1. ЗАГОЛОВОК — цепляющий, не энциклопедичный. Одна строка.
2. ЗАЧИН — личная история или острый тезис (2–4 строки).
3. МЫСЛИ ЧЕРЕЗ ОПЫТ — основная часть: тезисы, каждый раскрыт через практику и пример. Без нумерации (нумерация допустима только если это явный «план действий»).
4. ФИНАЛ-ВОПРОС — заверши вопросом к аудитории или конкретным приглашением обсудить.
5. ФУТЕР — всегда, см. ниже.

Два типа постов — выбирай по теме:
- ЭКСПЕРТНЫЙ — про рынок, право, технологии, экономику проекта. Больше конкретики, цифр, ссылок на нормы (ФЗ, ставка ЦБ, сроки). Позиция + практический вывод.
- ЛИЧНЫЙ — про людей, ошибки, решения, «как я / мой клиент». Больше истории и эмоции, меньше терминов. Урок из опыта.
Если тип не указан — делай экспертный с личным примером внутри.

━━━━━━━━━━━━━━━━━━━━━━━━━━
ВЫБОР РЕЖИМА
━━━━━━━━━━━━━━━━━━━━━━━━━━
Если в задаче просят пост / текст для канала — режим «Посты». Если просят ответить, прокомментировать, отреагировать — режим «Комментарии». Режим можно задать явно словом в начале задачи."""

# Футер IPM — подставляется в конец каждого поста (скрытая ссылка во всю строку).
FOOTER = (
    '<a href="https://t.me/expert_developer">'
    '📌 IPM | LAB — Лаборатория девелопмента. '
    'Разработка и сопровождение сложных девелоперских проектов</a>\n\n'
    '@Porotckii_lab'
)


def ensure_footer(text: str) -> str:
    """СТРАЖ ФУТЕРА: гарантирует, что под постом обязательно стоит футер IPM
    (ровно один раз). Через эту функцию проходит любой пост перед публикацией —
    ни один пост не может уйти в канал без футера."""
    text = (text or "").rstrip()
    if "t.me/expert_developer" in text and "@Porotckii_lab" in text:
        return text                    # футер уже есть — не дублируем
    return text + "\n\n" + FOOTER      # футера нет — добавляем принудительно

# ----------------------------------------------------------------------------
# 2. ИСТОЧНИКИ
#    Самый надёжный универсальный источник — RSS-поиск Google News по слову.
#    Можно добавить и RSS конкретных отраслевых сайтов, если у них он есть.
# ----------------------------------------------------------------------------

def google_news_rss(query: str) -> str:
    """Собирает корректный URL RSS-поиска Google News на русском."""
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=ru&gl=RU&ceid=RU:ru"


def site_krt(domain: str) -> str:
    """RSS-поиск новостей про КРТ на конкретном сайте (через Google News)."""
    return google_news_rss(f'КРТ комплексное развитие территорий site:{domain}')


SOURCES = [
    # --- Общая пресса ---
    google_news_rss('КРТ "комплексное развитие территорий"'),
    google_news_rss('"комплексное развитие территорий" застройщик'),
    google_news_rss('договор КРТ торги застройка'),

    # --- Дзен ---
    site_krt("dzen.ru"),

    # --- Федеральные ведомства ---
    site_krt("minstroyrf.gov.ru"),    # Минстрой России
    site_krt("duma.gov.ru"),          # Государственная Дума
    site_krt("domrf.ru"),             # ДОМ.РФ (АО «ДОМ.РФ», Корпорация)

    # --- Москва (+ районные управы сидят на mos.ru) ---
    site_krt("mos.ru"),               # Правительство Москвы
    site_krt("krt.mos.ru"),           # Программа КРТ Москвы

    # --- Московская область ---
    site_krt("mosreg.ru"),            # Правительство Московской области
    site_krt("mosoblarh.mosreg.ru"),  # Комитет по архитектуре и градостроительству МО

    # --- Санкт-Петербург и Ленинградская область ---
    site_krt("gov.spb.ru"),           # Правительство Санкт-Петербурга
    site_krt("lenobl.ru"),            # Правительство Ленинградской области
    site_krt("ks.lenobl.ru"),         # Комитет по строительству Ленинградской области
]

# --- Телеграм-каналы по КРТ ---
# Просто имена публичных каналов БЕЗ символа @ (как в ссылке t.me/...).
# Бот читает их веб-витрину t.me/s/<имя> — доступ/админка не нужны.
# Впиши сюда реальные каналы, за которыми хочешь следить:
TELEGRAM_SOURCES = [
    # "expert_developer",
    # "krt_russia",
]

# Слова-маркеры: пост из телеграм-канала берём, только если он про КРТ
# (каналы часто пишут и на другие темы — так отсекаем лишнее).
KEYWORDS = ["крт", "комплексное развитие территор"]

# ----------------------------------------------------------------------------
# 3. ПАМЯТЬ (дедупликация)
# ----------------------------------------------------------------------------

def load_seen() -> set:
    """Загружает множество уже опубликованных id новостей."""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set) -> None:
    """Сохраняет память на диск."""
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def load_json(path: str, default):
    """Универсальная загрузка JSON-файла с запасным значением."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: str, data) -> None:
    """Универсальное сохранение JSON-файла."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def news_id(entry) -> str:
    """Уникальный отпечаток новости — по ссылке (или заголовку, если ссылки нет)."""
    key = entry.get("link") or entry.get("title", "")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

# ----------------------------------------------------------------------------
# 4. СБОР НОВОСТЕЙ
# ----------------------------------------------------------------------------

def read_telegram_channel(username: str) -> list:
    """Читает последние посты публичного телеграм-канала через его веб-витрину
    t.me/s/<username>. Никакого доступа/админки не нужно — это открытая страница."""
    items = []
    url = f"https://t.me/s/{username}"
    try:
        html = requests.get(
            url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}
        ).text
    except Exception as e:
        print(f"  Не смог прочитать @{username}: {e}")
        return items

    soup = BeautifulSoup(html, "html.parser")
    for msg in soup.select(".tgme_widget_message_wrap"):
        text_el = msg.select_one(".tgme_widget_message_text")
        link_el = msg.select_one("a.tgme_widget_message_date")
        if not text_el:
            continue
        text = text_el.get_text("\n", strip=True)
        # Берём только посты, где реально речь про КРТ.
        if not any(k in text.lower() for k in KEYWORDS):
            continue
        link = link_el["href"] if (link_el and link_el.has_attr("href")) else url
        items.append({
            "title": text.split("\n")[0][:120],   # первая строка как заголовок
            "summary": text[:1500],
            "link": link,
            "source": f"Telegram @{username}",
        })
    return items


def collect_news(seen: set) -> list:
    """Читает все источники и возвращает список НОВЫХ новостей."""
    fresh = []
    for url in SOURCES:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            nid = news_id(entry)
            if nid in seen:
                continue  # уже постили — пропускаем
            # пытаемся достать картинку из новости (для обложки-фото)
            img = ""
            if entry.get("media_content"):
                img = entry["media_content"][0].get("url", "")
            if not img and entry.get("media_thumbnail"):
                img = entry["media_thumbnail"][0].get("url", "")
            fresh.append({
                "id": nid,
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "").strip(),
                "link": entry.get("link", "").strip(),
                "source": entry.get("source", {}).get("title", ""),
                "image_url": img,
            })

    # --- Телеграм-каналы (веб-витрина t.me/s/...) ---
    for username in TELEGRAM_SOURCES:
        for tg in read_telegram_channel(username):
            nid = news_id(tg)
            if nid in seen:
                continue
            tg["id"] = nid
            fresh.append(tg)

    # Чем меньше дублей по заголовку, тем лучше — оставим уникальные заголовки
    unique = {}
    for item in fresh:
        unique.setdefault(item["title"], item)
    return list(unique.values())

# ----------------------------------------------------------------------------
# 5. ТЕКСТ ПОСТА через Claude
# ----------------------------------------------------------------------------

def _fit_body(body: str, reserve: int) -> str:
    """Страховка по длине: пост уходит ОТДЕЛЬНЫМ сообщением (лимит Telegram 4096).
    Если вдруг длиннее — режем по границе предложения, футер сохраняем."""
    budget = 4096 - reserve
    if len(body) <= budget:
        return body
    cut = body[:budget].rstrip()
    for sep in (". ", "! ", "? ", ".\n", "\n"):
        i = cut.rfind(sep)
        if i > budget * 0.5:
            return cut[:i + 1].rstrip()
    i = cut.rfind(" ")
    return (cut[:i] if i > 0 else cut).rstrip() + "…"


def make_post(item: dict) -> str:
    """Превращает сырую новость в пост в голосе IPM | LAB.
    Футер и ссылку на источник добавляет сам код — модель их не пишет."""
    source_line = ""
    if item.get("link"):
        label = item.get("source") or "Источник"
        source_line = f'\n\n<a href="{item["link"]}">🔗 {label}</a>'
    tail = source_line + "\n\n" + FOOTER   # источник + футер IPM в самом низу

    # Без ключа Claude — простая заглушка, но уже с футером и источником.
    if not ANTHROPIC_API_KEY:
        return f"<b>{item['title']}</b>{tail}"

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""Режим «Посты». Напиши РАЗВЁРНУТЫЙ пост для канала IPM | LAB по этой новости — от лица Константина Пороцкого, с его личным экспертным комментарием.

Заголовок новости: {item['title']}
Краткое описание: {item['summary']}

Как писать:
- Тип поста — ЭКСПЕРТНЫЙ, с личным авторским комментарием Константина. Это не пересказ новости, а его взгляд на неё.
- Структура: цепляющий заголовок → зачин (почему это вообще важно) → 3–4 развёрнутые мысли через опыт девелопера, что это значит для застройщика/инвестора в контексте КРТ → финал-вопрос к аудитории.
- Это должен быть именно КОММЕНТАРИЙ эксперта: оценка, что хорошо/плохо, на что обратить внимание, к чему это ведёт на рынке.
- ЧЕСТНОСТЬ ПО ФАКТАМ: мнение, оценка и общий опыт («за годы работы я вижу, что…») — можно и нужно. Но НЕ выдумывай конкретные факты, цифры, имена и «истории клиента» именно про это событие, если их нет в описании выше.
- Объём — развёрнутый, примерно 1500–2800 знаков. Живой язык, короткие абзацы, воздух между ними.
- Форматирование — только теги <b> и <i>. Без хэштегов.
- НЕ добавляй футер, подпись, контакты и ссылку на источник — их подставит система сама.

Верни ТОЛЬКО текст поста, без пояснений."""

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,                      # достаточно для развёрнутого поста
        system=CHANNEL_VOICE,
        messages=[{"role": "user", "content": prompt}],
    )
    body = msg.content[0].text.strip()
    # страховка: пост + источник + футер должны влезть в сообщение (лимит 4096)
    body = _fit_body(body, reserve=len(tail) + 100)
    return f"{body}{tail}"

# ----------------------------------------------------------------------------
# 5b. ОБЛОЖКИ К ПОСТУ (две штуки на выбор)
#     Вариант "design" — оформленный фон + заголовок по центру.
#     Вариант "photo"  — фото из новости, затемнённое, + заголовок по центру.
#     Размер 1200×630 — стандарт для превью-картинок.
# ----------------------------------------------------------------------------

COVER_W, COVER_H = 1200, 630

# Фирменные цвета. Сейчас — тёмный бирюзово-графитовый + золото.
# Хочешь нейтральный синий вместо золота — поставь ACCENT = (60, 130, 200).
BG_DARK     = (20, 26, 32)     # низ фона
BG_TOP      = (28, 58, 75)     # верх фона (тёмная бирюза)
ACCENT      = (201, 168, 76)   # золотые полосы/подпись
TITLE_COLOR = (255, 255, 255)


def _font(size: int):
    """Берёт жирный шрифт с поддержкой кириллицы (есть в системе на GitHub)."""
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _gradient(top_color, bottom_color) -> Image.Image:
    """Быстрый вертикальный градиент (через маску 1×H с растяжением)."""
    strip = Image.new("L", (1, COVER_H))
    for y in range(COVER_H):
        strip.putpixel((0, y), int(255 * y / COVER_H))
    alpha = strip.resize((COVER_W, COVER_H))
    base = Image.new("RGB", (COVER_W, COVER_H), top_color)
    bottom = Image.new("RGB", (COVER_W, COVER_H), bottom_color)
    base.paste(bottom, (0, 0), alpha)
    return base


def _photo_background(image_url: str):
    """Скачивает картинку из новости и готовит её как затемнённый фон."""
    try:
        r = requests.get(image_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        bg = Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None
    bg = ImageOps.fit(bg, (COVER_W, COVER_H), Image.LANCZOS)      # заполнить кадр
    bg = Image.blend(bg, Image.new("RGB", (COVER_W, COVER_H), (0, 0, 0)), 0.55)  # затемнить
    return bg


def _wrap_lines(draw, text, font, max_w):
    """Разбивает заголовок на строки по ширине."""
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _draw_centered_title(img, title):
    """Пишет заголовок ПО ЦЕНТРУ (и по горизонтали, и по вертикали),
    автоматически подбирая размер шрифта, чтобы всё поместилось."""
    draw = ImageDraw.Draw(img)
    max_w = int(COVER_W * 0.86)
    for size in range(76, 34, -4):        # от крупного к мелкому, пока не влезет
        font = _font(size)
        lines = _wrap_lines(draw, title, font, max_w)
        line_h = size + 14
        total_h = line_h * len(lines)
        if len(lines) <= 5 and total_h <= COVER_H * 0.62:
            break
    y = (COVER_H - total_h) // 2          # вертикальное центрирование
    for line in lines:
        w = draw.textlength(line, font=font)
        x = (COVER_W - w) // 2            # горизонтальное центрирование
        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0))  # тень
        draw.text((x, y), line, font=font, fill=TITLE_COLOR)
        y += line_h


def _draw_footer(img, source):
    """Мелкая подпись снизу по центру: источник + метка канала."""
    draw = ImageDraw.Draw(img)
    font = _font(26)
    label = (source or "КРТ").strip()[:60]
    w = draw.textlength(label, font=font)
    draw.text(((COVER_W - w) // 2, COVER_H - 60), label, font=font, fill=ACCENT)


def make_cover(title: str, source: str, variant: str, image_url: str = None) -> bytes:
    """Возвращает PNG-обложку (bytes) для указанного варианта."""
    if variant == "photo":
        img = _photo_background(image_url) if image_url else None
        if img is None:                    # нет фото — запасной дизайн другого оттенка
            img = _gradient((44, 48, 56), (18, 20, 26))
    else:                                  # design
        img = _gradient(BG_TOP, BG_DARK)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, COVER_W, 12], fill=ACCENT)                 # полоса сверху
        d.rectangle([0, COVER_H - 12, COVER_W, COVER_H], fill=ACCENT) # полоса снизу

    _draw_centered_title(img, title)
    _draw_footer(img, source)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# ----------------------------------------------------------------------------
# 6. ОБЩЕНИЕ С TELEGRAM
# ----------------------------------------------------------------------------

def tg_api(method: str, payload: dict) -> dict:
    """Базовый вызов любого метода Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload, timeout=60)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram вернул ошибку ({method}): {data}")
    return data


def send_message(chat_id, text: str) -> dict:
    """Отправляет сообщение в указанный чат (канал или личку). Возвращает ответ."""
    return tg_api("sendMessage", {
        "chat_id": chat_id,
        "text": text[:4096],            # лимит Telegram на длину сообщения
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })


def send_photo(chat_id, image_bytes: bytes, caption: str) -> dict:
    """Отправляет фото (обложку) с подписью. Возвращает ответ Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    resp = requests.post(
        url,
        data={"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
        files={"photo": ("cover.png", image_bytes, "image/png")},
        timeout=90,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto ошибка: {data}")
    return data


def get_reaction_approvals(offset: int) -> tuple:
    """Читает свежие реакции из Telegram.
    Возвращает (множество одобренных message_id, новый offset).
    Одобрение = на сообщение бота поставили ЛЮБУЮ реакцию.

    Учитываем два случая:
      • личка/группа — реакция именная (message_reaction);
      • канал — реакции анонимные, приходит только счётчик (message_reaction_count)."""
    data = tg_api("getUpdates", {
        "offset": offset,
        "timeout": 0,
        # оба типа по умолчанию выключены — включаем явно
        "allowed_updates": ["message_reaction", "message_reaction_count"],
    })
    approved = set()
    new_offset = offset
    result = data.get("result", [])
    print(f"[проверка] Telegram вернул обновлений: {len(result)}")
    for upd in result:
        new_offset = upd["update_id"] + 1
        # какой тип пришёл — полезно видеть в логе
        kinds = [k for k in ("message_reaction", "message_reaction_count") if upd.get(k)]
        if kinds:
            print(f"[проверка]   обновление {upd['update_id']}: {', '.join(kinds)}")

        # 1) личка или группа: видно, что реакцию поставили (а не сняли)
        r = upd.get("message_reaction")
        if r and r.get("new_reaction"):
            approved.add(r["message_id"])

        # 2) канал: анонимный счётчик — одобряем, если есть хоть одна реакция
        rc = upd.get("message_reaction_count")
        if rc and any(x.get("total_count", 0) > 0 for x in rc.get("reactions", [])):
            approved.add(rc["message_id"])

    return approved, new_offset

# ----------------------------------------------------------------------------
# 7. ГЛАВНАЯ ЛОГИКА
# ----------------------------------------------------------------------------

def publish_approved():
    """ТАКТ 1: проверяем, что ты одобрил реакцией, и публикуем это в канал."""
    pending = load_json(PENDING_FILE, {})     # {message_id(строка): текст поста}
    state = load_json(STATE_FILE, {"offset": 0})

    print(f"[проверка] В очереди на одобрение: {len(pending)} шт. (offset={state.get('offset', 0)})")

    # Опрашиваем реакции ВСЕГДА (даже если очередь пуста): это держит
    # «подписку» на реакции активной, иначе первые реакции могут не дойти.
    approved_ids, new_offset = get_reaction_approvals(state.get("offset", 0))
    state["offset"] = new_offset
    save_json(STATE_FILE, state)

    if not pending:
        print("[проверка] Очередь пуста — публиковать нечего.")
        print("[проверка] Если ты УЖЕ ставил реакции, а очередь пуста — значит запущен")
        print("[проверка] старый прогон (кнопка Re-run) вместо свежего (Run workflow).")
        return

    print(f"[проверка] Жду реакций на message_id: {sorted(int(k) for k in pending)}")
    print(f"[проверка] Одобрено реакциями message_id: {sorted(approved_ids) or '— (реакций не видно)'}")

    published = 0
    for mid in list(approved_ids):
        data = pending.get(str(mid))
        if not data:
            continue  # реакция на что-то не из очереди — игнор
        try:
            # публикуем ДВУМЯ сообщениями: сначала обложка, следом полный текст
            cover = make_cover(
                data["title"], data["source"], data["variant"],
                data.get("image_url") or None,
            )
            send_photo(TELEGRAM_CHANNEL, cover, "")                 # обложка (картинка)
            time.sleep(1)
            send_message(TELEGRAM_CHANNEL, ensure_footer(data["text"]))  # полный текст + футер

            # убираем ВСЕ черновики этого материала (оба варианта обложки)
            grp = data.get("group")
            for k in [k for k, v in pending.items() if v.get("group") == grp]:
                pending.pop(k, None)

            published += 1
            print(f"✓ Одобрено и опубликовано в канал (вариант «{data['variant']}»)")
            time.sleep(2)
        except Exception as e:
            print(f"✗ Не смог опубликовать (msg {mid}): {e}")

    save_json(PENDING_FILE, pending)
    if published:
        print(f"Опубликовано одобренных постов: {published}")


def queue_drafts(seen: set):
    """ТАКТ 2: собираем свежие новости и шлём черновики ТЕБЕ на проверку.
    К каждой новости прикладываем ДВЕ обложки — выбираешь реакцией нужную."""
    pending = load_json(PENDING_FILE, {})
    news = collect_news(seen)
    print(f"Найдено новых материалов: {len(news)}")

    sent = 0
    for item in news:
        if sent >= MAX_POSTS_PER_RUN:
            break
        try:
            post_text = make_post(item)

            # 1) полный текст поста — отдельным сообщением, чтобы прочитать целиком
            send_message(REVIEW_CHAT_ID,
                         "🔎 ЧЕРНОВИК — прочитай текст, затем поставь реакцию на нужную обложку ниже 👇\n\n"
                         + post_text)
            time.sleep(1)

            # 2) две обложки с короткими подписями — реакция на обложку = публикуем с ней
            for variant, label in [("photo", "с фото"), ("design", "дизайн")]:
                cover = make_cover(item["title"], item["source"], variant, item.get("image_url"))
                resp = send_photo(REVIEW_CHAT_ID, cover,
                                  f"🖼 Обложка «{label}» — поставь реакцию, чтобы опубликовать пост с ней")
                mid = resp["result"]["message_id"]
                pending[str(mid)] = {
                    "text": post_text,
                    "variant": variant,
                    "title": item["title"],
                    "source": item["source"],
                    "image_url": item.get("image_url", ""),
                    "group": item["id"],       # чтобы убрать «второй» вариант после выбора
                }
                time.sleep(1)

            seen.add(item["id"])
            sent += 1
            print(f"→ Черновик (текст + 2 обложки) отправлен: {item['title'][:50]}")
        except Exception as e:
            print(f"✗ Пропустил «{item['title'][:40]}»: {e}")

    save_json(PENDING_FILE, pending)
    save_seen(seen)
    print(f"Отправлено черновиков: {sent}")


def main():
    seen = load_seen()

    if REVIEW_MODE:
        if not REVIEW_CHAT_ID:
            raise SystemExit("Не задан REVIEW_CHAT_ID — некуда слать черновики на проверку.")
        publish_approved()   # сначала публикуем то, что ты уже одобрил
        queue_drafts(seen)   # потом шлём новые черновики
    else:
        # Прямой режим без проверки (как раньше): сразу в канал.
        news = collect_news(seen)
        print(f"Найдено новых материалов: {len(news)}")
        posted = 0
        for item in news:
            if posted >= MAX_POSTS_PER_RUN:
                break
            try:
                cover = make_cover(item["title"], item["source"], "design", item.get("image_url"))
                send_photo(TELEGRAM_CHANNEL, cover, "")
                time.sleep(1)
                send_message(TELEGRAM_CHANNEL, ensure_footer(make_post(item)))
                seen.add(item["id"])
                posted += 1
                print(f"✓ Опубликовано: {item['title'][:60]}")
                time.sleep(3)
            except Exception as e:
                print(f"✗ Пропустил «{item['title'][:40]}»: {e}")
        save_seen(seen)
        print(f"Готово. Опубликовано постов: {posted}")


if __name__ == "__main__":
    main()
