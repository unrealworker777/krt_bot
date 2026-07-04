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
import re
import json
import time
import calendar
import hashlib
import urllib.parse
from datetime import datetime

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

# Брать новости только за последние N дней (текущая неделя).
MAX_AGE_DAYS = 7

# Файл-память: тут храним ссылки, которые уже опубликовали.
SEEN_FILE = "seen.json"

# Очередь черновиков, ждущих твоей реакции: {message_id: текст поста}.
PENDING_FILE = "pending.json"

# Служебное состояние (offset для чтения реакций из Telegram).
STATE_FILE = "state.json"

# Голос/стиль канала — официально-деловой.
CHANNEL_VOICE = """Ты — редактор Telegram-канала о девелопменте, комплексном развитии территорий (КРТ) и индивидуальном жилищном строительстве (ИЖС) в России. Пиши в ОФИЦИАЛЬНО-ДЕЛОВОМ стиле.

Стиль и требования:
- Деловой регистр: нейтрально, сдержанно, профессионально. Без сленга, без разговорных выражений, без эмоций и восклицаний.
- От третьего лица или безлично. СТРОГО без первого лица: не используй «я», «мне», «мой», «мы», «наш», «на мой взгляд», «считаю», «уверен». Никаких личных историй, «я видел», «мой клиент», риторических вопросов и обращений к читателю на «ты»/«вы».
- Пиши безлично: «закон устанавливает…», «для застройщиков это означает…», «изменения затрагивают…», «предусмотрено…».
- Точность прежде всего: корректные факты и формулировки. Термины (КРТ, ИЖС, ФЗ-494, ФЗ-214, ГПЗУ, ПЗЗ, ЗОУИТ, эскроу, проектное финансирование, ДОМ.РФ) употребляй верно.
- Ясность: официальный тон, но без пустого канцелярита и «воды». Каждое предложение несёт информацию.
- Структура логичная и последовательная.
- Аудитория — застройщики, инвесторы, проектировщики, органы власти и профессиональное сообщество."""

# Футер IPM. Ставить его под каждым постом или нет — переключатель ниже.
# ADD_FOOTER = True  → футер обязателен под каждым постом.
# ADD_FOOTER = False → футера нет (текущий режим).
ADD_FOOTER = False

FOOTER = (
    '<a href="https://t.me/expert_developer">'
    '📌 IPM | LAB — Лаборатория девелопмента. '
    'Разработка и сопровождение сложных девелоперских проектов</a>\n\n'
    '@Porotckii_lab'
)


def ensure_footer(text: str) -> str:
    """Добавляет футер IPM под пост, только если ADD_FOOTER = True.
    Если футер выключен — возвращает текст как есть."""
    text = (text or "").rstrip()
    if not ADD_FOOTER:
        return text                    # футер выключен — ничего не добавляем
    if "t.me/expert_developer" in text and "@Porotckii_lab" in text:
        return text                    # футер уже есть — не дублируем
    return text + "\n\n" + FOOTER

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
    # --- Общая пресса: КРТ ---
    google_news_rss('КРТ "комплексное развитие территорий"'),
    google_news_rss('"комплексное развитие территорий" застройщик'),
    google_news_rss('договор КРТ торги застройка'),

    # --- Общая пресса: ИЖС ---
    google_news_rss('ИЖС "индивидуальное жилищное строительство"'),
    google_news_rss('ИЖС ипотека ДОМ.РФ малоэтажное строительство'),
    google_news_rss('ИЖС закон эскроу подрядчик'),

    # --- Дзен ---
    site_krt("dzen.ru"),

    # --- Федеральные ведомства ---
    site_krt("minstroyrf.gov.ru"),    # Минстрой России
    site_krt("government.ru"),         # Правительство РФ
    site_krt("duma.gov.ru"),          # Государственная Дума
    site_krt("council.gov.ru"),       # Совет Федерации
    site_krt("fas.gov.ru"),           # ФАС (споры по торгам КРТ)

    # --- Институты развития ---
    site_krt("domrf.ru"),             # ДОМ.РФ (дом.рф)
    site_krt("фрт.рф"),               # Фонд развития территорий

    # --- Отраслевые порталы, аналитика, СМИ ---
    site_krt("erzrf.ru"),             # ЕРЗ.РФ — Единый ресурс застройщиков
    site_krt("realty.rbc.ru"),        # РБК Недвижимость
    site_krt("rbc.ru"),               # РБК (весь портал)
    site_krt("forbes.ru"),            # Forbes Россия
    google_news_rss('КРТ девелопмент застройка site:rbc.ru'),
    google_news_rss('КРТ девелопмент недвижимость site:forbes.ru'),
    site_krt("dvizhenie.ru"),         # Движение.ру
    site_krt("congress-krt.ru"),      # Всероссийский Конгресс по КРТ

    # --- Юр-СМИ и разбор практики ---
    site_krt("pravo.ru"),             # Право.ру
    site_krt("advgazeta.ru"),         # Адвокатская газета

    # --- Москва ---
    site_krt("mos.ru"),               # Правительство Москвы (+ районные управы)
    site_krt("krt.mos.ru"),           # Программа КРТ Москвы
    site_krt("stroi.mos.ru"),         # Комплекс градполитики и строительства
    site_krt("apr.moscow"),           # Москомархитектура
    site_krt("investmoscow.ru"),      # Инвестпортал Москвы (КРТ нежилой застройки)

    # --- Московская область ---
    site_krt("mosreg.ru"),            # Правительство Московской области
    site_krt("msk.mosreg.ru"),        # Минстрой Московской области
    site_krt("mosoblarh.mosreg.ru"),  # Мособлархитектура
    site_krt("mosoblduma.ru"),        # Мособлдума

    # --- Санкт-Петербург и Ленинградская область ---
    site_krt("gov.spb.ru"),           # Правительство Санкт-Петербурга
    site_krt("lenobl.ru"),            # Правительство Ленинградской области
    site_krt("ks.lenobl.ru"),         # Комитет по строительству Ленинградской области

    # --- Регионы (примеры; шаблон для остальных) ---
    site_krt("tatarstan.ru"),         # Республика Татарстан
    site_krt("minstroy.tatarstan.ru"),# Минстрой РТ
    site_krt("nobl.ru"),              # Правительство Нижегородской области
    site_krt("ir-no.ru"),             # Институт развития агломерации НО (оператор торгов КРТ)
    site_krt("admkrsk.ru"),           # Администрация Красноярска (раздел КРТ)

    # --- Профобъединения и отраслевые институты ---
    site_krt("nostroy.ru"),           # НОСТРОЙ
    site_krt("noza.ru"),              # НОЗА
    site_krt("rgud.ru"),              # Российская гильдия управляющих и девелоперов
    site_krt("стройкомплекс.рф"),     # ЕИС «Стройкомплекс.РФ» (если индексируется)

    # --- Юр-бюро и разбор практики КРТ ---
    site_krt("landlawfirm.ru"),       # Land Law Firm (справочник по КРТ)
    site_krt("kachkin.ru"),           # Качкин и Партнёры
    site_krt("regionservice.com"),    # Регионсервис
    site_krt("alrf.ru"),              # Ассоциация юристов России
    site_krt("dvitex.ru"),            # Dvitex (изъятие, возмещение при КРТ)

    # --- Прочие ведомства ---
    site_krt("rosreestr.gov.ru"),     # Росреестр (пресс-релизы, разъяснения)

    # ⚠ НЕ добавляем как новостные источники (Google News их не индексирует —
    #    это базы/сервисы/реестры, их нужно мониторить отдельными парсерами):
    #    ГИС Торги (torgi.gov.ru), Росэлторг, НСПД/кадастр (nspd.gov.ru),
    #    publication.pravo.gov.ru, regulation.gov.ru, sozd.duma.gov.ru,
    #    наш.дом.рф, земля.дом.рф, КАД (kad.arbitr.ru), Судакт, ВС РФ (vsrf.ru),
    #    КонсультантПлюс. Тяжёлые за защитой: krt.mos.ru карта, госвеб-муниципалитеты.
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
KEYWORDS = ["крт", "комплексное развитие территор", "ижс", "индивидуальное жилищное строительство"]

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


def clean_title(title: str) -> str:
    """Google News дописывает в конец заголовка « - Издание» (напр. « - Дзен»).
    Убираем этот хвост, чтобы источник не попадал на обложку и в текст."""
    return re.sub(r"\s+[-–—]\s+[^-–—]{1,45}$", "", (title or "")).strip()

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
        # только свежее: если у поста есть дата и он старше недели — пропускаем
        time_el = msg.select_one("time[datetime]")
        if time_el and time_el.has_attr("datetime"):
            try:
                ts = datetime.fromisoformat(time_el["datetime"]).timestamp()
                if time.time() - ts > MAX_AGE_DAYS * 86400:
                    continue
            except Exception:
                pass
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


# ── Точность мониторинга (идеи из референс-реализации) ──
# Сильный триггер → релевантно сразу. Голое «КРТ»/«ИЖС» → только если рядом
# есть отраслевой якорь (иначе это может быть другая аббревиатура/тема).
_STRONG = [
    r"комплексн\w+\s+развити\w+\s+территори",
    r"494-ФЗ",
    r"договор\w*\s+о\s+КРТ",
    r"торг\w+.{0,40}комплексн",
    r"изъяти\w+.{0,40}(?:КРТ|комплексн)",
    r"индивидуальн\w+\s+жилищн\w+\s+строительств",   # ИЖС
]
_STRONG_RE = re.compile("|".join(_STRONG), re.IGNORECASE)
_BARE = re.compile(r"\b(?:КРТ|ИЖС)\b")
_ANCHORS = ["застройк", "градостроит", "земельн", "девелоп", "аукцион",
            "правообладател", "реновац", "редевелоп", "территори",
            "жиль", "жилищн", "малоэтаж", "загородн", "ипотек", "эскроу"]


def is_relevant(text: str) -> bool:
    """Отсекает мимо-тематические совпадения (напр. «КРТ» как другая аббревиатура)."""
    text = text or ""
    if _STRONG_RE.search(text):
        return True
    if _BARE.search(text):
        low = text.lower()
        return any(a in low for a in _ANCHORS)
    return False


def _simhash(text: str, bits: int = 64) -> int:
    """SimHash заголовка — для отлова near-дублей (одна новость в разных источниках)."""
    vec = [0] * bits
    for tok in re.findall(r"\w+", (text or "").lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        for i in range(bits):
            vec[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if vec[i] > 0:
            out |= (1 << i)
    return out


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def collect_news(seen: set) -> list:
    """Читает все источники и возвращает список НОВЫХ новостей."""
    fresh = []
    for url in SOURCES:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            nid = news_id(entry)
            if nid in seen:
                continue  # уже постили — пропускаем
            # только свежее: пропускаем новости старше MAX_AGE_DAYS
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub and (time.time() - calendar.timegm(pub)) > MAX_AGE_DAYS * 86400:
                continue
            # пытаемся достать картинку из новости (для обложки-фото)
            img = ""
            if entry.get("media_content"):
                img = entry["media_content"][0].get("url", "")
            if not img and entry.get("media_thumbnail"):
                img = entry["media_thumbnail"][0].get("url", "")
            fresh.append({
                "id": nid,
                "title": clean_title(entry.get("title", "")),
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

    # Отбор: релевантность (точность) + near-дедуп (похожие перепечатки одной новости).
    result = []
    batch_hashes = []
    for item in fresh:
        blob = item["title"] + " " + item.get("summary", "")
        if not is_relevant(blob):
            continue                                    # мимо темы — отбрасываем
        sh = _simhash(item["title"])
        if any(_hamming(sh, h) <= 10 for h in batch_hashes):
            continue                                    # почти дубль другой заметки
        batch_hashes.append(sh)
        result.append(item)
    return result

# ----------------------------------------------------------------------------
# 5. ТЕКСТ ПОСТА через Claude
# ----------------------------------------------------------------------------

def _visible_len(s: str) -> int:
    """Длина ВИДИМОГО текста: Telegram не считает HTML-теги и URL в лимит подписи."""
    return len(re.sub(r"<[^>]+>", "", s))


def _fit_body(body: str, tail: str, prefix_reserve: int = 60, limit: int = 1024) -> str:
    """Страховка по длине подписи к фото (лимит 1024 по видимому тексту).
    Считаем без HTML-тегов и без длинных URL. Режем по границе предложения,
    футер сохраняем."""
    budget = limit - _visible_len(tail) - prefix_reserve
    if _visible_len(body) <= budget:
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
        label = item.get("source") or "источник"
        source_line = f'\n\nПодробнее здесь: <a href="{item["link"]}">{label}</a>'
    tail = source_line + ("\n\n" + FOOTER if ADD_FOOTER else "")  # футер — только если включён

    # Без ключа Claude — простая заглушка, но уже с футером и источником.
    if not ANTHROPIC_API_KEY:
        return f"<b>{item['title']}</b>{tail}"

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""Напиши пост для канала в ОФИЦИАЛЬНО-ДЕЛОВОМ стиле по этой новости.

Заголовок новости: {item['title']}
Краткое описание: {item['summary']}

Как писать:
- Официально-деловой тон: нейтрально, точно, профессионально. Без эмоций, восклицаний и риторических вопросов.
- СТРОГО БЕЗ ПЕРВОГО ЛИЦА: не используй слова «я», «мне», «мой», «мы», «наш», «на мой взгляд», «считаю». Никакого личного авторства. Пиши безлично и от третьего лица. Без обращений к читателю на «ты»/«вы».
- Начинай сразу с сути. Без вводных фраз-воды («Как известно», «В последнее время», «Стоит отметить»).
- Структура: заголовок (жирным) → фактическая суть новости (что произошло) → деловой разбор значения для застройщиков/инвесторов в контексте КРТ или ИЖС (последствия, требования, сроки, риски) → сдержанный итог.
- ЧЕСТНОСТЬ ПО ФАКТАМ: опирайся ТОЛЬКО на заголовок и описание выше. Не выдумывай цифры, имена и детали, которых нет в исходнике. Общий профессиональный контекст рынка — допустим.
- Объём — до 800 знаков. Плотно, по существу.
- Форматирование — только теги <b> и <i>. Без хэштегов.
- НЕ добавляй подпись, контакты и ссылку на источник — их подставит система сама.

Пример нужного СТИЛЯ (ориентир по тону и форме, не по содержанию):
<b>Москва включила в программу КРТ три площадки бывших промзон</b>

Правительство Москвы расширило программу комплексного развития территорий, включив в неё три участка общей площадью около 40 га. Решение закрепляет за территориями статус, необходимый для последующей застройки.

Для застройщиков это означает уточнение градостроительных параметров и сроков освоения. Появление новых площадок в проработанных локациях снижает неопределённость на этапе входа в проект.

Реализация будет вестись в соответствии с утверждёнными параметрами и графиком.

Верни ТОЛЬКО текст поста, без пояснений."""

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=900,
        system=CHANNEL_VOICE,
        messages=[{"role": "user", "content": prompt}],
    )
    body = msg.content[0].text.strip()
    # тело + источник + футер должны влезть в подпись к фото (по видимому тексту)
    body = _fit_body(body, tail, prefix_reserve=60, limit=1024)
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


def _draw_footer(img, source=None):
    """Снизу-слева — белый бренд «IPM | LAB». Источник на обложке не пишем."""
    draw = ImageDraw.Draw(img)
    brand_font = _font(30)
    draw.text((44, COVER_H - 62), "IPM | LAB", font=brand_font, fill=(255, 255, 255))


def _topic_of(title: str) -> str:
    """Определяет тему поста по заголовку — для тематического силуэта на обложке."""
    t = (title or "").lower()
    if "ижс" in t or "индивидуальное жилищное" in t or "малоэтаж" in t or "загородн" in t:
        return "izhs"
    return "krt"


def _draw_theme(img: Image.Image, topic: str) -> Image.Image:
    """Рисует снизу еле заметный тематический силуэт: домики (ИЖС) или здания (КРТ).
    Полупрозрачный, чтобы не мешать заголовку."""
    overlay = Image.new("RGBA", (COVER_W, COVER_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    color = (255, 255, 255, 38)            # белый силуэт, очень прозрачный
    base = COVER_H - 16                     # линия «земли» у нижнего края

    if topic == "izhs":
        # ряд домиков с двускатными крышами
        x = 20
        import random as _r
        _r.seed(len(img.tobytes()) % 1000)  # стабильно, но с вариацией
        while x < COVER_W:
            w = _r.choice([90, 110, 130])
            h = _r.choice([70, 90, 110])
            roof = int(w * 0.45)
            d.rectangle([x, base - h, x + w, base], fill=color)              # тело дома
            d.polygon([(x - 8, base - h), (x + w + 8, base - h),
                       (x + w / 2, base - h - roof)], fill=color)            # крыша
            x += w + 34
    else:
        # силуэт городской застройки — прямоугольные здания разной высоты
        x = 10
        import random as _r
        _r.seed(len(img.tobytes()) % 1000)
        while x < COVER_W:
            w = _r.choice([70, 90, 110])
            h = _r.choice([110, 150, 190, 230])
            d.rectangle([x, base - h, x + w, base], fill=color)
            x += w + 22

    out = Image.alpha_composite(img.convert("RGBA"), overlay)
    return out.convert("RGB")


def make_cover(title: str, source: str, variant: str, image_url: str = None) -> bytes:
    """Возвращает PNG-обложку (bytes) для указанного варианта."""
    if variant == "photo":
        img = _photo_background(image_url) if image_url else None
        if img is None:                    # нет фото — запасной дизайн другого оттенка
            img = _gradient((44, 48, 56), (18, 20, 26))
    else:                                  # design
        img = _gradient(BG_TOP, BG_DARK)
        img = _draw_theme(img, _topic_of(title))                     # тематический силуэт
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
            # публикуем одним сообщением: обложка + текст под ней
            cover = make_cover(
                data["title"], data["source"], "design",
                data.get("image_url") or None,
            )
            send_photo(TELEGRAM_CHANNEL, cover, ensure_footer(data["text"]))

            # убираем черновик этого материала из очереди
            grp = data.get("group")
            for k in [k for k, v in pending.items() if v.get("group") == grp]:
                pending.pop(k, None)
            pending.pop(str(mid), None)

            published += 1
            print("✓ Одобрено и опубликовано в канал")
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

            # одна обложка + текст под ней, одним сообщением. Реакция = публикуем.
            cover = make_cover(item["title"], item["source"], "design", item.get("image_url"))
            caption = "🔎 ЧЕРНОВИК — поставь реакцию, чтобы опубликовать в канал\n\n" + post_text
            resp = send_photo(REVIEW_CHAT_ID, cover, caption)
            mid = resp["result"]["message_id"]
            pending[str(mid)] = {
                "text": post_text,
                "title": item["title"],
                "source": item["source"],
                "image_url": item.get("image_url", ""),
                "group": item["id"],
            }

            seen.add(item["id"])
            sent += 1
            print(f"→ Черновик отправлен: {item['title'][:55]}")
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
                send_photo(TELEGRAM_CHANNEL, cover, ensure_footer(make_post(item)))
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
