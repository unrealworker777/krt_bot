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
import base64
import random
import calendar
import hashlib
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
import feedparser
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter
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
MAX_POSTS_PER_RUN = 5

# Брать новости только за последние N дней.
MAX_AGE_DAYS = 30

# --- ГИС Торги (аукционы КРТ) ---
TORGI_API = "https://torgi.gov.ru/new/api/public/lotcards/search"
# Сколько лотов ГИС Торги ставить ВПЕРЁД остальных новостей за один запуск
# (приоритет торгам, но не в ущерб обычным новостям).
TORGI_PRIORITY_PER_RUN = 2

# --- ФИРМЕННЫЙ СТИЛЬ ИПМ ---------------------------------------------------
# Два фирменных цвета из рекомендаций: тёмно-бирюзовый и золото.
# Используются на обложках и в инфографике.
BRAND_TEAL = (0x1B, 0x3A, 0x4B)   # #1B3A4B — основной фон
BRAND_GOLD = (0xC9, 0xA8, 0x4C)   # #C9A84C — акцент

# --- РУБРИКИ ---------------------------------------------------------------
# Кроме новостей канал ведёт собственные рубрики. Ключ — день недели
# (0 = понедельник ... 6 = воскресенье), значение — какую рубрику готовить.
# Рубричный пост ставится в очередь на проверку ПЕРВЫМ, до новостей.
RUBRICS_ENABLED = True
RUBRIC_SCHEDULE = {
    0: "figure",     # понедельник — «Цифра недели»
    1: "faq",        # вторник     — FAQ по КРТ
    2: "case",       # среда       — мини-кейс проекта
    3: "checklist",  # четверг     — чек-лист / инструкция
    4: "digest",     # пятница     — дайджест за неделю
}

# Копилка новостей за неделю — из неё собираются дайджест и «Цифра недели».
WEEK_FILE = "week.json"
WEEK_WINDOW_DAYS = 7

# --- ВОВЛЕЧЕНИЕ ------------------------------------------------------------
# Каждый N-й новостной пост завершается вопросом к аудитории.
# 0 — выключить. 3 — каждый третий пост.
ENGAGEMENT_EVERY_N = 3

# Отправлять ли к рубрикам «Цифра недели» и FAQ настоящее голосование
# (Telegram sendPoll) отдельным сообщением после поста.
SEND_POLLS = True

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

# Показывать ли строку «📎 Подробнее здесь: источник» под постом.
# False → строки нет. True → ссылка на источник под постом (текущий режим).
SHOW_SOURCE = True

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

    # --- Тематические запросы по приоритетным игрокам (пресса пишет о них) ---
    google_news_rss('КРТ Минстрой'),
    google_news_rss('КРТ ФАС торги'),
    google_news_rss('КРТ Москва застройка'),
    google_news_rss('КРТ Московская область'),
    google_news_rss('КРТ застройщики ЕРЗ'),
    google_news_rss('комплексное развитие территорий решение власти'),

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
    # Приоритетные источники — их официальные телеграм-каналы (прямой поток КРТ/ИЖС).
    "erzrf",           # ЕРЗ.РФ
    "minstroyrf",      # Минстрой России
    "minstroymo",      # Минстрой Московской области
    "stroi_mos_ru",    # Стройкомплекс Москвы
    "fasrussia",       # ФАС России
    "government_rus",  # Правительство России
    "duma_gov_ru",     # Государственная Дума
    "mosrutop",        # Правительство Москвы (mos.ru)
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


# --- Память о СМЫСЛЕ уже отправленных новостей (защита от перепечаток) ---
# Хранится в том же seen.json записями вида "h:<число>", чтобы не заводить новый файл.

def known_hashes(seen: set) -> list:
    """Достаёт из памяти отпечатки заголовков, о которых бот уже писал."""
    out = []
    for s in seen:
        if isinstance(s, str) and s.startswith("h:"):
            try:
                out.append(int(s[2:]))
            except ValueError:
                pass
    return out


def remember_hash(seen: set, sh: int) -> None:
    """Запоминает отпечаток заголовка — чтобы та же новость с другого сайта не прошла."""
    seen.add(f"h:{sh}")


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
    """Чистит заголовок от «хвостов», которые дописывает Google News:
    « - Издание», « — подробности события», «| Казахстан» и т.п."""
    t = (title or "").strip()
    # 1) всё после вертикальной черты «|» (обычно там раздел/страна/издание)
    t = t.split("|")[0].strip()
    # 2) мусорные вставки перед источником
    t = re.sub(r"\s*[-–—]\s*подробност\w*\s+событи\w*.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[-–—]\s*(?:читать|подробнее|новости)\b.*$", "", t, flags=re.IGNORECASE)
    # 3) финальный « - Издание» (последний разделитель « - » с пробелами;
    #    внутрисловные дефисы, как в «Контрольно-счетная», не трогаем)
    mm = re.match(r"^(.*\S)\s[-–—]\s(.{1,60})$", t)
    if mm:
        t = mm.group(1)
    return t.strip()

# ----------------------------------------------------------------------------
# 3b. ПРЯМЫЕ ССЫЛКИ НА ИСТОЧНИК
#     Google News отдаёт в RSS не адрес статьи, а свой редирект вида
#     news.google.com/rss/articles/CBMi... — читателю он непрозрачен и
#     подрывает доверие к каналу. Здесь разворачиваем его до настоящего адреса.
# ----------------------------------------------------------------------------

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

_LINK_CACHE = {}          # {ссылка Google News: настоящий адрес} — в пределах запуска


def _decode_gnews_url(link: str) -> str:
    """Быстрый путь: у части ссылок Google News настоящий адрес лежит
    открытым текстом внутри base64-сегмента. Раскодируем и вынем его."""
    m = re.search(r"/(?:rss/)?articles/([A-Za-z0-9_\-]+)", link)
    if not m:
        return ""
    seg = m.group(1)
    seg += "=" * (-len(seg) % 4)              # добиваем до кратности 4
    try:
        raw = base64.urlsafe_b64decode(seg)
    except Exception:
        return ""
    i = raw.find(b"http")
    if i < 1:
        return ""
    # Адрес лежит в protobuf-поле: прямо перед ним записана его длина (varint).
    # Режем строго по длине — иначе к адресу прилипает следующий служебный байт
    # (например 0x30 превращается в лишний «0» в конце ссылки).
    length = 0
    if i >= 2 and (raw[i - 2] & 0x80) and not (raw[i - 1] & 0x80):
        length = (raw[i - 2] & 0x7F) | (raw[i - 1] << 7)      # двухбайтный varint
    elif raw[i - 1] < 0x80:
        length = raw[i - 1]                                    # однобайтный varint

    url = ""
    if 12 <= length <= 600 and i + length <= len(raw):
        url = raw[i:i + length].decode("utf-8", "ignore")
    if not re.fullmatch(r"https?://[\w.\-]+\.[a-z]{2,}(?:[/?#][^\s]*)?", url or "", re.I):
        # длина не сошлась — берём по регулярке, пусть и менее точно
        found = re.search(rb"https?://[^\x00-\x20\"'<>\\]{12,}", raw)
        url = found.group(0).decode("utf-8", "ignore") if found else ""
    return "" if (not url or "news.google.com" in url) else url


def _follow_gnews(link: str) -> str:
    """Медленный путь: идём по ссылке и смотрим, куда нас в итоге привели.
    Google отдаёт JS-страницу, поэтому дополнительно ищем адрес в её HTML."""
    try:
        r = requests.get(link, timeout=20, allow_redirects=True,
                         headers={"User-Agent": UA})
    except Exception:
        return ""
    if "news.google.com" not in r.url:
        return r.url                          # обычный HTTP-редирект сработал
    html = r.text
    for pattern in (
        r'data-n-au="([^"]+)"',                                   # атрибут Google News
        r'<meta[^>]+http-equiv="refresh"[^>]+url=([^"\'>\s]+)',   # meta-refresh
        r'<a[^>]+href="(https?://(?!\S*google\.)[^"]+)"',         # первая внешняя ссылка
    ):
        mm = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if mm:
            return mm.group(1).strip()
    return ""


def resolve_source_url(link: str) -> str:
    """Возвращает ПРЯМОЙ адрес статьи. Не-гугловые ссылки отдаёт как есть."""
    link = (link or "").strip()
    if not link or "news.google.com" not in link:
        return link
    if link in _LINK_CACHE:
        return _LINK_CACHE[link]
    real = _decode_gnews_url(link) or _follow_gnews(link) or link
    real = real.split("?utm_")[0].rstrip("&?")        # чистим рекламные хвосты
    _LINK_CACHE[link] = real
    return real


def domain_of(url: str) -> str:
    """Домен без www — используем как «человеческую» подпись к ссылке."""
    try:
        host = urllib.parse.urlparse(url or "").netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


# Красивые названия для доменов, которые встречаются чаще всего.
SOURCE_NAMES = {
    "erzrf.ru": "ЕРЗ.РФ",
    "minstroyrf.gov.ru": "Минстрой России",
    "torgi.gov.ru": "ГИС Торги",
    "dom.rf": "ДОМ.РФ",
    "xn--80az8a.xn--d1aqf.xn--p1ai": "ДОМ.РФ",
    "duma.gov.ru": "Государственная Дума",
    "government.ru": "Правительство России",
    "mos.ru": "Правительство Москвы",
    "mosreg.ru": "Правительство Московской области",
    "fas.gov.ru": "ФАС России",
    "rbc.ru": "РБК",
    "realty.rbc.ru": "РБК Недвижимость",
    "vedomosti.ru": "Ведомости",
    "kommersant.ru": "Коммерсантъ",
    "interfax.ru": "Интерфакс",
    "tass.ru": "ТАСС",
    "ria.ru": "РИА Новости",
    "iz.ru": "Известия",
    "rg.ru": "Российская газета",
    "cian.ru": "ЦИАН",
    "movp.ru": "Движение.ру",
    "vsn.ru": "VSN",
}


def source_label(item: dict) -> tuple:
    """Возвращает (название источника, домен) для подписи под постом.
    Название берём из справочника по домену, иначе — из самой новости."""
    url = item.get("link") or ""
    dom = domain_of(url)
    name = SOURCE_NAMES.get(dom) or (item.get("source") or "").strip()
    if not name:
        # из домена делаем читаемое имя: erzrf.ru → Erzrf.ru
        name = dom.split(".")[0].capitalize() if dom else "источник"
    return name, dom

# ----------------------------------------------------------------------------
# 4. СБОР НОВОСТЕЙ
# ----------------------------------------------------------------------------

def read_telegram_channel(username: str) -> list:
    """Читает последние посты публичного телеграм-канала через его веб-витрину
    t.me/s/<username>. Никакого доступа/админки не нужно — это открытая страница."""
    items = []
    url = f"https://t.me/s/{username}"
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        html = resp.text
    except Exception as e:
        print(f"  @{username}: не смог прочитать ({e})")
        return items

    soup = BeautifulSoup(html, "html.parser")
    wraps = soup.select(".tgme_widget_message_wrap")
    total = len(wraps)
    matched = 0
    for msg in wraps:
        text_el = msg.select_one(".tgme_widget_message_text")
        link_el = msg.select_one("a.tgme_widget_message_date")
        if not text_el:
            continue
        # только свежее: если у поста есть дата и он старше окна — пропускаем
        time_el = msg.select_one("time[datetime]")
        if time_el and time_el.has_attr("datetime"):
            try:
                ts = datetime.fromisoformat(time_el["datetime"]).timestamp()
                if time.time() - ts > MAX_AGE_DAYS * 86400:
                    continue
            except Exception:
                pass
        text = text_el.get_text("\n", strip=True)
        # Берём только посты, где реально речь про КРТ/ИЖС.
        if not any(k in text.lower() for k in KEYWORDS):
            continue
        matched += 1
        link = link_el["href"] if (link_el and link_el.has_attr("href")) else url
        items.append({
            "title": text.split("\n")[0][:120],   # первая строка как заголовок
            "summary": text[:1500],
            "link": link,
            "source": f"Telegram @{username}",
        })

    note = "  ⚠ витрина пуста — вероятно, выключен веб-предпросмотр или неверное имя" if total == 0 else ""
    print(f"  @{username}: постов на витрине {total}, про КРТ/ИЖС {matched}{note}")
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


# Приоритетные источники: их новости идут ВПЕРЁД остальных (жёсткий приоритет).
# ЕРЗ, Минстрой, Москва/МО, ФАС, Госдума + отраслевые СМИ, юрбюро, регионы.
PRIORITY_KEYWORDS = [
    "ерз", "erzrf", "застройщик",                                   # ЕРЗ.РФ
    "минстрой",                                                     # Минстрой
    "москв", "mos.ru", "mosreg", "подмосков", "мособл",             # Москва / МО
    "фас",                                                          # ФАС
    "госдум", "государственная дума", "депутат", "законопроект",    # Госдума РФ
    "совет федерации", "правительств",                             # федеральный уровень
    "рбк", "ведомост", "коммерсант", "движение", "дом.рф", "domrf", # отраслевые СМИ
    "строительн", "недвижимост",
    "право", "адвокат", "юрист", "качкин", "регионсервис",          # юрбюро
    "татарстан", "нижегород", "красноярск", "область", "край", "республик",  # регионы
]


def source_priority(item: dict) -> int:
    """0 — приоритетный источник (из списка выше), 1 — обычный."""
    blob = ((item.get("source") or "") + " " + (item.get("title") or "")).lower()
    return 0 if any(k in blob for k in PRIORITY_KEYWORDS) else 1


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


def collect_torgi(seen: set) -> list:
    """Тянет свежие лоты КРТ напрямую из API ГИС Торги (первоисточник по аукционам).
    Возвращает список в том же формате, что и новости, с пометкой is_torgi."""
    items = []
    try:
        r = requests.get(TORGI_API, params={
            "text": "комплексное развитие территории",
            "lotStatus": "PUBLISHED,APPLICATIONS_SUBMISSION",
            "page": 0, "size": 20,
            "sort": "firstVersionPublicationDate,desc",
        }, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=30)
        data = r.json()
    except Exception as e:
        print(f"ГИС Торги недоступен ({e}) — пропускаю торги в этот раз")
        return items

    for lot in data.get("content", []):
        lot_id = lot.get("id", "")
        if not lot_id:
            continue
        link = f"https://torgi.gov.ru/new/public/lots/lot/{lot_id}"
        nid = hashlib.sha256(link.encode("utf-8")).hexdigest()[:16]
        if nid in seen:
            continue
        name = (lot.get("lotName") or "Лот КРТ").strip()
        # характеристики (площадь, кадастр)
        chars = {c.get("code"): c.get("characteristicValue")
                 for c in lot.get("characteristics", []) if c.get("code")}
        facts = []
        price = lot.get("priceMin")
        if price:
            facts.append("Стартовая цена: " + f"{int(price):,}".replace(",", " ") + " ₽")
        if chars.get("squareKRT"):
            facts.append(f"Площадь участка: {chars['squareKRT']} м²")
        if chars.get("CadastralNumberKRT"):
            facts.append(f"Кадастровый номер: {chars['CadastralNumberKRT']}")
        end = (lot.get("biddEndTime") or "")[:10]
        if end:
            facts.append(f"Приём заявок до: {end}")

        items.append({
            "id": nid, "title": name[:150], "summary": " • ".join(facts),
            "link": link, "source": "ГИС Торги", "image_url": "", "is_torgi": True,
        })
    print(f"ГИС Торги: новых лотов КРТ: {len(items)}")
    return items


ERZ_NEWS_URL = "https://erzrf.ru/news"


def collect_erz(seen: set) -> list:
    """Парсит ленту новостей ЕРЗ.РФ напрямую (erzrf.ru/news) и берёт материалы
    про КРТ/ИЖС. Опирается на адреса статей /news/<slug>, а не на хрупкие CSS-классы."""
    items = []
    try:
        html = requests.get(ERZ_NEWS_URL, timeout=30,
                            headers={"User-Agent": "Mozilla/5.0"}).text
    except Exception as e:
        print(f"ЕРЗ.РФ недоступен ({e}) — пропускаю")
        return items

    soup = BeautifulSoup(html, "html.parser")
    seen_links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].split("#")[0]
        # приводим абсолютные ссылки к относительным
        href = href.replace("https://erzrf.ru", "").replace("http://erzrf.ru", "")
        # статьи ЕРЗ: /news/<slug> ИЛИ /news/article/<Slug> (новый формат)
        if not re.match(r"^/news/(?:article/)?[A-Za-z0-9_\-]+$", href):
            continue
        if href.endswith("news-archive") or href.endswith("/news/article"):
            continue
        title = a.get_text(" ", strip=True)
        if len(title) < 15:
            continue
        low = title.lower()
        if not any(k in low for k in KEYWORDS):     # только КРТ/ИЖС
            continue
        full = "https://erzrf.ru" + href
        if full in seen_links:
            continue
        seen_links.add(full)
        nid = hashlib.sha256(full.encode("utf-8")).hexdigest()[:16]
        if nid in seen:
            continue
        items.append({
            "id": nid, "title": title[:150], "summary": title,
            "link": full, "source": "ЕРЗ.РФ", "image_url": "",
        })
    print(f"ЕРЗ.РФ (сайт): новых КРТ/ИЖС материалов: {len(items)}")
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
            # Ссылка: сразу пробуем раскодировать редирект Google News.
            # Это «бесплатный» путь без обращения к сети; если не вышло —
            # полное разворачивание сделаем позже, только для нужных новостей.
            raw_link = entry.get("link", "").strip()
            link = _decode_gnews_url(raw_link) if "news.google.com" in raw_link else raw_link
            fresh.append({
                "id": nid,                       # id считаем по ИСХОДНОЙ ссылке —
                                                 # чтобы память seen.json осталась валидной
                "title": clean_title(entry.get("title", "")),
                "summary": entry.get("summary", "").strip(),
                "link": link or raw_link,
                "source": entry.get("source", {}).get("title", ""),
                "image_url": img,
            })

    # --- Телеграм-каналы (веб-витрина t.me/s/...) ---
    print("Читаю телеграм-каналы приоритетных источников:")
    tg_count = 0
    for username in TELEGRAM_SOURCES:
        for tg in read_telegram_channel(username):
            nid = news_id(tg)
            if nid in seen:
                continue
            tg["id"] = nid
            tg["is_tg"] = True
            fresh.append(tg)
            tg_count += 1
    print(f"Собрано новых постов из телеграм-каналов: {tg_count}")

    # --- ЕРЗ.РФ напрямую (парсер сайта) ---
    for it in collect_erz(seen):
        fresh.append(it)

    # Отбор: релевантность + защита от дублей.
    # ВАЖНО: сравниваем не только внутри этого запуска, но и с тем, о чём бот
    # писал РАНЬШЕ (память в seen.json) — иначе та же новость с другого сайта
    # проходит повторно, ведь ссылка у неё другая.
    result = []
    batch_hashes = known_hashes(seen)          # ← отпечатки прошлых запусков
    for item in fresh:
        blob = item["title"] + " " + item.get("summary", "")
        # телеграм-посты уже отобраны по ключевым словам — второй фильтр им не нужен
        if not item.get("is_tg") and not is_relevant(blob):
            continue                                    # мимо темы — отбрасываем
        sh = _simhash(item["title"])
        if any(_hamming(sh, h) <= 10 for h in batch_hashes):
            print(f"  дубль (уже писали об этом): {item['title'][:60]}")
            continue                                    # эта новость уже была — с другого сайта
        batch_hashes.append(sh)
        item["_sh"] = sh                                # запомним отпечаток при отправке
        result.append(item)

    # Приоритет источников: ЕРЗ/Минстрой/Москва-МО/ФАС/СМИ/юрбюро/регионы — вперёд.
    result.sort(key=source_priority)

    # ПРИОРИТЕТ ГИС Торги: несколько лотов КРТ ставим ВПЕРЁД обычных новостей.
    torgi = collect_torgi(seen)
    return torgi[:TORGI_PRIORITY_PER_RUN] + result

# ----------------------------------------------------------------------------
# 5. ТЕКСТ ПОСТА через Claude
# ----------------------------------------------------------------------------

# Лимит Telegram на подпись к фото и на обычное сообщение.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
# Предельный объём поста, который вообще собираем (дайджест длиннее новости).
MAX_POST_CHARS = 3500


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


def _clean_emoji(text: str, extra: str = "") -> str:
    """Оставляет в тексте поста только 📍 и 🇷🇺 — остальные эмодзи (🔹 🔷 ✅ и пр.) убирает.
    Маркеры тезисов «—» и обычная пунктуация не трогаются.
    extra — дополнительные разрешённые символы (например «❓» для вопроса к аудитории)."""
    keep = {"📍", "🇷", "🇺"} | set(extra)   # 🇷🇺 состоит из двух символов-индикаторов
    out = []
    for ch in (text or ""):
        code = ord(ch)
        is_emoji = (
            0x1F300 <= code <= 0x1FAFF or   # пиктограммы, символы, эмодзи
            0x2600  <= code <= 0x27BF  or   # разное + дингбаты (✅ ✔ ➡ и т.п.)
            0x2B00  <= code <= 0x2BFF  or   # стрелки/звёзды
            code in (0xFE0F, 0x20E3)        # модификаторы эмодзи
        )
        if is_emoji and ch not in keep:
            continue
        out.append(ch)
    # подчищаем пробелы, оставшиеся на месте удалённых эмодзи
    txt = re.sub(r"[ \t]{2,}", " ", "".join(out))
    txt = "\n".join(line.strip() for line in txt.split("\n"))
    return txt.strip()


def fetch_article_text(url: str, limit: int = 4000) -> str:
    """Загружает статью по ссылке и достаёт основной текст (абзацы).
    Нужно, чтобы бот разбирал СОДЕРЖАНИЕ статьи, а не только заголовок."""
    if not url:
        return ""
    try:
        r = requests.get(url, timeout=25, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        html = r.text
    except Exception as e:
        print(f"  не смог загрузить текст статьи ({str(e)[:60]})")
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = "\n".join(p for p in paras if len(p) > 40)   # выкидываем меню/подписи
        return text[:limit]
    except Exception:
        return ""


# --- ФОРМАТЫ ПОСТА ---------------------------------------------------------
# Пост всегда состоит из двух частей и НЕ разбивается на озаглавленные блоки:
#   1) сжатая выжимка новости — только то, без чего смысл теряется;
#   2) аналитика — что из этого следует для девелопера, вплетённая в текст.
# Форматы отличаются подачей и объёмом, чтобы лента не выглядела под копирку.

POST_FORMATS = [
    {
        "id": "brief",
        "limit": 480,
        "task": (
            "СПЛОШНЫМ ТЕКСТОМ, без списков. Первый абзац — 1–2 предложения: что "
            "произошло, с конкретикой (кто, что, когда, сколько). Второй абзац — "
            "2–3 предложения о том, что это меняет на практике: сроки, деньги, "
            "документы, структура сделки. Второй абзац идёт обычным текстом, без "
            "заголовка и без ярлыка перед ним."
        ),
        "shape": (
            "<b>Срок рассмотрения документации КРТ сокращён вдвое</b>\n\n"
            "Правительство установило предельный срок рассмотрения документации по "
            "проектам КРТ — 15 рабочих дней вместо 30. Норма действует для решений, "
            "принятых после 1 сентября.\n\n"
            "Предпроектная стадия сокращается примерно на месяц, поэтому графики "
            "финансирования, привязанные к дате согласования, придётся пересчитать. "
            "Преимущество получают те, у кого документация готова заранее."
        ),
    },
    {
        "id": "theses",
        "limit": 700,
        "task": (
            "Сразу после заголовка — одно предложение с сутью новости. Затем 3–4 "
            "факта по существу, каждый отдельной строкой с «— »: цифры, сроки, "
            "требования, решения. Заверши 2–3 предложениями о практических "
            "последствиях — обычным текстом, без списка, без заголовка и без ярлыка."
        ),
        "shape": (
            "<b>Регионы получили право сокращать согласование проектов КРТ</b>\n\n"
            "🇷🇺 Правительство утвердило поправки к порядку работы с территориями "
            "комплексного развития.\n\n"
            "— Срок рассмотрения документации: 15 рабочих дней вместо 30.\n"
            "— Решение о КРТ для промзон принимается без публичных слушаний.\n"
            "— Требования к социнфраструктуре закреплены на этапе договора.\n\n"
            "Вход в проекты по промзонам ускоряется на 3–4 недели, но требования к "
            "социальной части фиксируются раньше. Заложить их в экономику придётся "
            "до подписания договора, а не по ходу проектирования."
        ),
    },
    {
        "id": "figure_lead",
        "limit": 560,
        "task": (
            "Начни основной текст с самой значимой цифры из статьи — она стоит в "
            "первом предложении и выделена <b>жирным</b>. Одно-два предложения: "
            "откуда цифра и что за ней стоит. Затем 2 предложения о последствиях "
            "для рынка — обычным текстом, без списков, без заголовка и без ярлыка."
        ),
        "shape": (
            "<b>Под КРТ в регионе отведено 340 гектаров</b>\n\n"
            "<b>340 га</b> — площадь территорий, включённых в перечень комплексного "
            "развития после пересмотра границ. Основной объём приходится на три "
            "бывшие промышленные зоны.\n\n"
            "Пул площадок с готовым градостроительным статусом расширяется. Но "
            "промзоны означают затраты на вывод производств и рекультивацию — их "
            "стоит просчитать до заявки на торги."
        ),
    },
]

# Сквозной счётчик постов: по нему чередуются форматы и расставляются
# вопросы к аудитории. Значение переносится между запусками через state.json.
_POST_SEQ = 0


def _next_seq() -> int:
    """Следующий номер поста в сквозной нумерации."""
    global _POST_SEQ
    _POST_SEQ += 1
    return _POST_SEQ


def pick_format(seq: int) -> dict:
    """Формат поста по его номеру — форматы идут по кругу, не повторяясь подряд."""
    return POST_FORMATS[seq % len(POST_FORMATS)]


def make_post(item: dict) -> str:
    """Превращает сырую новость в пост в голосе IPM | LAB.
    Футер и ссылку на источник добавляет сам код — модель их не пишет."""
    # Разворачиваем редирект Google News до настоящего адреса статьи.
    # Делаем это здесь, а не при сборе, — чтобы не ходить в сеть за новостями,
    # которые в итоге не попадут в канал.
    if item.get("link"):
        item["link"] = resolve_source_url(item["link"])

    source_line = ""
    if SHOW_SOURCE:
        link = item.get("link")
        if not link:
            # у новости нет прямой ссылки — ведём на поиск по заголовку
            link = "https://www.google.com/search?q=" + urllib.parse.quote(item.get("title", ""))
        # экранируем адрес (в ссылках бывает &) и название источника
        url = link.replace("&", "&amp;").replace('"', "%22")
        name, dom = source_label(item)
        name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # Название источника — обычным текстом, а не только гиперссылкой:
        # читатель видит, кто автор материала, ещё до перехода по ссылке.
        source_line = f'\n\n📎 Источник: <b>{name}</b>'
        if dom:
            source_line += f' ({dom})'
        source_line += f' — <a href="{url}">читать оригинал</a>'
    tail = source_line + ("\n\n" + FOOTER if ADD_FOOTER else "")  # футер — только если включён

    # Лоты ГИС Торги — собираем пост из точных данных лота (без ИИ, чтобы не переврать цифры).
    if item.get("is_torgi"):
        lines = ["🏛 <b>Аукцион по КРТ</b>", "", f"<b>{item['title']}</b>"]
        if item.get("summary"):
            lines.append("")
            for f in item["summary"].split(" • "):
                emo = ("💰" if f.startswith("Стартовая") else
                       "📐" if f.startswith("Площадь") else
                       "🗺" if f.startswith("Кадастровый") else
                       "🗓" if f.startswith("Приём") else "—")
                lines.append(f"{emo} {f}")
        return "\n".join(lines) + tail

    # Без ключа Claude — простая заглушка, но уже с футером и источником.
    if not ANTHROPIC_API_KEY:
        return f"📍 <b>{item['title']}</b>{tail}"

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Читаем САМУ статью (у телеграм-постов текст уже есть в summary).
    article = item.get("summary", "") or ""
    if not item.get("is_tg") and item.get("link"):
        full = fetch_article_text(item["link"])
        if len(full) > len(article):
            article = full

    # Формат поста чередуется от поста к посту — лента перестаёт быть однотипной.
    seq = _next_seq()
    fmt = pick_format(seq)
    item["_format"] = fmt["id"]
    ask_question = bool(ENGAGEMENT_EVERY_N) and seq % ENGAGEMENT_EVERY_N == 0

    # Блок с вопросом к аудитории — добавляем не в каждый пост, чтобы
    # приём не приедался и не выглядел искусственным.
    question_task = ""
    if ask_question:
        question_task = (
            "\n\nЗАДАЧА 4 — ВОПРОС К АУДИТОРИИ:\n"
            "Последней строкой задай один короткий деловой вопрос по теме поста, "
            "на который профессионалу есть что ответить в комментариях. "
            "Вопрос конкретный и по делу, не риторический и не «а что думаете вы?». "
            "Начни строку с «❓ ». Это ЕДИНСТВЕННОЕ место, где допустимо обращение "
            "к читателю."
        )

    prompt = f"""Разбери эту статью и напиши по ней пост для канала в ОФИЦИАЛЬНО-ДЕЛОВОМ стиле.

ОРИГИНАЛЬНЫЙ ЗАГОЛОВОК (его НЕЛЬЗЯ копировать): {item['title']}

ТЕКСТ СТАТЬИ:
{article[:4000]}

ЗАДАЧА 1 — СВОЙ ЗАГОЛОВОК:
Придумай СОБСТВЕННЫЙ заголовок. Он должен по смыслу отражать суть, но быть сформулирован ИНАЧЕ, другими словами, чем оригинальный. Не перефразируй оригинал слово в слово, зайди с другой стороны: через суть решения, последствие или главную цифру. Одна строка, жирным (<b>).

ЗАДАЧА 2 — ФОРМАТ ЭТОГО ПОСТА:
{fmt['task']}
Строго держись именно этого формата — он задан для этого поста специально.

ЗАДАЧА 3 — АНАЛИТИКА:
Заверши пост выводом о том, что новость означает для девелопера: на что повлияет в сроках, деньгах, документах или структуре сделки. Вывод — часть текста, а НЕ отдельный озаглавленный блок. ЗАПРЕЩЕНЫ любые служебные строки и ярлыки: «Взгляд ИПМ», «Что это значит», «Для кого это», «Кого касается», «Что делать», «Вывод», «Контекст», «Итого» — и любые другие подзаголовки внутри поста. Пиши конкретно, без общих фраз. Если фактуры в статье мало — напиши, на что стоит обратить внимание при отслеживании темы, но без выдуманных деталей.{question_task}

ТРЕБОВАНИЯ К СТИЛЮ:
- Официально-деловой тон: нейтрально, точно, профессионально. Без восклицаний.
- СТРОГО БЕЗ ПЕРВОГО ЛИЦА: не используй «я», «мне», «мой», «мы», «наш», «считаю». Пиши безлично и от третьего лица.
- Без вводных фраз-воды («Как известно», «Стоит отметить», «В последнее время»).
- ЭМОДЗИ: только 🇷🇺 — и только если речь о федеральном уровне (Минстрой, Госдума, Правительство РФ, ДОМ.РФ, федеральные законы). НИКАКИХ других эмодзи в тексте.
- ЧЕСТНОСТЬ ПО ФАКТАМ: бери факты ТОЛЬКО из текста статьи выше. Ничего не выдумывай. Если текст статьи пуст или неинформативен — опирайся на оригинальный заголовок и напиши коротко, без придуманных деталей.
- Объём — до {fmt['limit']} знаков на весь пост. Выжимка новости — максимально сжато, 1–2 предложения; остальное — аналитика. Ни одного предложения без информации: убери «как известно», «стоит отметить», «в целом», «важно понимать».
- Форматирование — только теги <b> и <i>. Без хэштегов.
- НЕ добавляй подпись и ссылку на источник — их подставит система.

ПРИМЕР ФОРМЫ (ориентир по структуре и тону, НЕ по содержанию):
{fmt['shape']}

Верни ТОЛЬКО текст поста, без пояснений."""

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1400,
        system=CHANNEL_VOICE,
        messages=[{"role": "user", "content": prompt}],
    )
    body = msg.content[0].text.strip()
    body = _clean_emoji(body, extra="❓")   # оставляем 📍, 🇷🇺 и знак вопроса-эмодзи
    # Общий предохранитель по объёму: длинные посты уедут отдельным сообщением
    # (см. send_post), поэтому жёсткий лимит подписи здесь больше не нужен.
    body = _fit_body(body, tail, prefix_reserve=60, limit=MAX_POST_CHARS)
    if not body.lstrip().startswith("📍"):
        body = "📍 " + body          # маркер-заголовок в начале поста
    return f"{body}{tail}"

# ----------------------------------------------------------------------------
# 5b. ОБЛОЖКИ К ПОСТУ (две штуки на выбор)
#     Вариант "design" — оформленный фон + заголовок по центру.
#     Вариант "photo"  — фото из новости, затемнённое, + заголовок по центру.
#     Размер 1200×630 — стандарт для превью-картинок.
# ----------------------------------------------------------------------------

COVER_W, COVER_H = 1200, 630

# Палитра обложки — фирменные цвета ИПМ (см. блок «ФИРМЕННЫЙ СТИЛЬ» выше).
BG_TOP      = BRAND_TEAL                 # #1B3A4B — тёмно-бирюзовый
BG_DARK     = (0x0A, 0x1A, 0x23)         # затемнённый тон того же бирюзового
ACCENT      = BRAND_GOLD                 # #C9A84C — золото
TITLE_COLOR = (244, 247, 250)            # мягкий белый
MUTED_COLOR = (168, 190, 200)            # приглушённый текст (подписи)


# Основной шрифт обложек. Положи файл шрифта в папку fonts/ рядом с krt_bot.py.
# Годится любой .ttf/.otf; если файла нет — бот откатится на системный DejaVu.
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


# --- МАТЕРИАЛ ДЛЯ УНИКАЛЬНОЙ ОБЛОЖКИ ----------------------------------------
# Чтобы под каждым постом стояла своя картинка, а не один и тот же фон,
# собираем три источника: фото из статьи, ключевую цифру и регион.

def fetch_og_image(url: str) -> str:
    """Достаёт из статьи главное изображение (og:image / twitter:image).
    Это даёт настоящее фото объекта или стройки вместо абстрактного фона."""
    if not url:
        return ""
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": UA})
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return ""
    for attrs in ({"property": "og:image"}, {"name": "og:image"},
                  {"name": "twitter:image"}, {"property": "twitter:image"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            img = tag["content"].strip()
            if img.startswith("//"):
                img = "https:" + img
            elif img.startswith("/"):
                img = urllib.parse.urljoin(url, img)
            if img.startswith("http"):
                return img
    return ""


# Ключевая цифра для инфографики: число + единица измерения.
_FIGURE_RE = re.compile(
    r"(?<![\w,.])(\d{1,3}(?:[  \u00a0]\d{3})+|\d+(?:[.,]\d+)?)"
    r"\s*(млрд|млн|тыс\.?)?\s*"
    r"(га|гектар\w*|м²|кв\.?\s?м|₽|руб\w*|%|процент\w*|"
    r"рабочих дн\w+|дн\w+|лет|года?|месяц\w*|квартир\w*|дом\w+|семей|человек)",
    re.IGNORECASE)

# Насколько «говорящая» единица измерения. Площади, деньги, доли и объёмы
# смотрятся на обложке лучше всего; сроки — тоже неплохо; всё прочее слабо.
_UNIT_WEIGHT = {
    3: ("га", "гектар", "м²", "кв", "₽", "руб", "%", "процент",
        "квартир", "дом", "семей", "человек"),
    2: ("рабочих дн", "дн", "лет", "год", "месяц"),
}


def extract_key_figure(text: str) -> str:
    """Находит в тексте самую выразительную цифру — для обложки-инфографики.
    Возвращает короткую строку вида «340 га», «1,2 млрд ₽», «15 дней»
    или пустую строку, если ничего подходящего нет."""
    plain = re.sub(r"<[^>]+>", " ", text or "")
    best, best_score = "", 0
    for num, scale, unit in _FIGURE_RE.findall(plain):
        u = unit.lower()
        # годы («2025 год») цифрой на обложке бесполезны
        if u.startswith(("год", "года")) and re.fullmatch(r"(19|20)\d{2}", num):
            continue
        score = 1
        for weight, units in _UNIT_WEIGHT.items():
            if any(u.startswith(s) for s in units):
                score = max(score, weight)
        if scale:
            score += 2                      # «млн», «млрд» — крупный масштаб
        if len(num) >= 4:
            score += 1                      # четырёхзначные числа заметнее
        if score > best_score:
            # нормализуем запись единицы к короткой форме
            u_out = unit
            for k, v in (("гектар", "га"), ("кв", "м²"), ("процент", "%"),
                         ("руб", "₽"), ("рабочих дн", "дней")):
                if u.startswith(k):
                    u_out = v
                    break
            parts = [num.replace("\u00a0", " ").strip()]
            if scale:
                parts.append("тыс." if scale.lower().startswith("тыс") else scale)
            parts.append(u_out)
            best, best_score = " ".join(parts), score
    # слабые совпадения («5 объектов») на обложку не выносим
    return best if best_score >= 2 else ""


# Регионы: по упоминанию в заголовке подбираем подпись и оттенок обложки,
# чтобы посты по разным территориям визуально отличались.
REGIONS = [
    ("москв", "Москва"), ("подмосков", "Московская область"),
    ("мособл", "Московская область"), ("петербург", "Санкт-Петербург"),
    ("ленинградск", "Ленинградская область"), ("татарстан", "Республика Татарстан"),
    ("казан", "Казань"), ("башкорт", "Республика Башкортостан"),
    ("уф", "Уфа"), ("екатеринбург", "Екатеринбург"),
    ("свердловск", "Свердловская область"), ("новосибирск", "Новосибирская область"),
    ("красноярск", "Красноярский край"), ("краснодар", "Краснодарский край"),
    ("сочи", "Сочи"), ("ростов", "Ростовская область"),
    ("нижегородск", "Нижегородская область"), ("нижнем новгород", "Нижний Новгород"),
    ("самар", "Самарская область"), ("челябинск", "Челябинская область"),
    ("тюмен", "Тюменская область"), ("пермск", "Пермский край"),
    ("воронеж", "Воронежская область"), ("волгоград", "Волгоградская область"),
    ("омск", "Омская область"), ("иркутск", "Иркутская область"),
    ("хабаровск", "Хабаровский край"), ("приморск", "Приморский край"),
    ("владивосток", "Владивосток"), ("калининград", "Калининградская область"),
    ("крым", "Республика Крым"), ("севастопол", "Севастополь"),
    ("якут", "Республика Саха (Якутия)"), ("тул", "Тульская область"),
    ("ярославск", "Ярославская область"), ("саратов", "Саратовская область"),
]


def detect_region(text: str) -> str:
    """Название региона из текста — для подписи на обложке-карте."""
    low = (text or "").lower()
    for key, name in REGIONS:
        if key in low:
            return name
    return ""


def _seed_of(text: str) -> int:
    """Стабильный сид по тексту: у каждого поста своя, но воспроизводимая
    геометрия обложки. Раньше сид считался от размера картинки — и был
    одинаковым для всех постов, отчего обложки выглядели под копирку."""
    return int(hashlib.md5((text or "").encode("utf-8")).hexdigest()[:8], 16)


def _custom_font_path():
    """Возвращает путь к пользовательскому шрифту из папки fonts/ (любой .ttf/.otf)."""
    exact = os.path.join(FONT_DIR, "Bildungswirkung.ttf")
    if os.path.exists(exact):
        return exact
    if os.path.isdir(FONT_DIR):
        for f in sorted(os.listdir(FONT_DIR)):
            if f.lower().endswith((".ttf", ".otf")):
                return os.path.join(FONT_DIR, f)
    return None


def _font(size: int):
    """Берёт основной шрифт из fonts/, иначе — системный DejaVu (кириллица)."""
    candidates = []
    custom = _custom_font_path()
    if custom:
        candidates.append(custom)
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
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
    # Лёгкий бирюзовый тон, чтобы фото попало в палитру ИПМ, но осталось видно.
    # Было 0.62 — фотография превращалась в ровное бирюзовое пятно.
    bg = Image.blend(bg, Image.new("RGB", (COVER_W, COVER_H), BRAND_TEAL), 0.28)
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


# --- ТИПОГРАФИКА И СЛОИ ОБЛОЖКИ ---------------------------------------------
# Макет один для всех вариантов: фон → тёмная подложка снизу → надзаголовок →
# заголовок слева над футером. Читается как обложка новостного материала и
# одинаково работает и на фото, и на фирменном фоне.

PAD_X = 64            # левое и правое поле
TITLE_BOTTOM = 104    # отступ от низа до последней строки заголовка


def _scrim(img: Image.Image, top_frac: float = 0.38, strength: int = 232) -> Image.Image:
    """Линейная тёмная подложка снизу — заголовок читается на любом фоне.
    Раньше вместо неё было размытое пятно-эллипс по центру: на фото оно оставляло
    светлые углы, и белый текст местами пропадал."""
    y0 = int(COVER_H * top_frac)
    strip = Image.new("L", (1, COVER_H), 0)
    for y in range(y0, COVER_H):
        t = (y - y0) / max(1, COVER_H - y0)
        strip.putpixel((0, y), int(strength * (t ** 1.5)))
    alpha = strip.resize((COVER_W, COVER_H))
    out = img.copy()
    out.paste(Image.new("RGB", (COVER_W, COVER_H), BG_DARK), (0, 0), alpha)
    return out


def _vignette(img: Image.Image, strength: int = 96) -> Image.Image:
    """Мягкое затемнение по краям — вместо бирюзового «свечения» по центру."""
    mask = Image.new("L", (COVER_W, COVER_H), 0)
    ImageDraw.Draw(mask).ellipse(
        [-COVER_W * 0.22, -COVER_H * 0.30, COVER_W * 1.22, COVER_H * 1.30], fill=255)
    mask = ImageOps.invert(mask).filter(ImageFilter.GaussianBlur(110))
    mask = mask.point(lambda v: int(v * strength / 255))
    out = img.copy()
    out.paste(Image.new("RGB", (COVER_W, COVER_H), (0, 0, 0)), (0, 0), mask)
    return out


def _draw_marks(img: Image.Image, seed: int) -> Image.Image:
    """Сдержанная фирменная графика: несколько тонких золотых линий в правом
    верхнем углу. Заменяет прежние процедурные «дома» и «сетку кварталов» —
    они выглядели как программная заглушка."""
    _r = random.Random(seed)
    overlay = Image.new("RGBA", (COVER_W, COVER_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    x0 = COVER_W - _r.choice([210, 260, 310])
    for i in range(_r.choice([3, 4, 5])):
        off = i * _r.choice([18, 24, 30])
        d.line([(x0 + off, 0), (x0 + off - 150, 150)],
               fill=(ACCENT[0], ACCENT[1], ACCENT[2], 34), width=2)
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _draw_title(img: Image.Image, title: str, max_size: int = 60,
                min_size: int = 32, max_lines: int = 4) -> int:
    """Заголовок по левому краю, прижат к низу. Возвращает Y верхней строки —
    по нему ставится надзаголовок. Тень не рисуем: за читаемость отвечает scrim,
    а двойная отрисовка со сдвигом на 2 px выглядела дешёво."""
    draw = ImageDraw.Draw(img)
    max_w = COVER_W - PAD_X * 2
    size, lines = min_size, [title]
    for size in range(max_size, min_size - 1, -3):
        font = _font(size)
        lines = _wrap_lines(draw, title, font, max_w)
        if len(lines) <= max_lines:
            break
    font = _font(size)
    line_h = int(size * 1.24)
    top = COVER_H - TITLE_BOTTOM - line_h * len(lines)
    y = top
    for line in lines:
        draw.text((PAD_X, y), line, font=font, fill=TITLE_COLOR)
        y += line_h
    return top


def _draw_kicker(img: Image.Image, text: str, y: int) -> None:
    """Надзаголовок: короткая золотая черта и подпись капсом (регион, тема)."""
    if not text:
        return
    d = ImageDraw.Draw(img)
    d.rectangle([PAD_X, y + 9, PAD_X + 30, y + 12], fill=ACCENT)
    d.text((PAD_X + 44, y), text.upper(), font=_font(21), fill=ACCENT)


def _draw_footer(img, source=None):
    """Снизу-слева — золотой маркер и белый бренд «IPM | LAB». Источник не пишем."""
    d = ImageDraw.Draw(img)
    d.rectangle([PAD_X, COVER_H - 52, PAD_X + 5, COVER_H - 36], fill=ACCENT)
    d.text((PAD_X + 18, COVER_H - 55), "IPM | LAB", font=_font(24),
           fill=(255, 255, 255))


def _draw_figure(img: Image.Image, figure: str) -> Image.Image:
    """Ключевая цифра — крупно, золотом, по левому краю в верхней половине."""
    d = ImageDraw.Draw(img)
    f = _font(120)
    for size in range(120, 52, -6):
        f = _font(size)
        if d.textlength(figure, font=f) <= COVER_W - PAD_X * 2:
            break
    d.text((PAD_X, int(COVER_H * 0.14)), figure, font=f, fill=ACCENT)
    return img


def choose_layout(item: dict, post_text: str = "", recent: list = None) -> tuple:
    """Решает, какая обложка подойдёт этому посту, и готовит для неё материал.
    Возвращает (макет, доп. данные).

    Сначала собираем ВСЕ подходящие варианты по убыванию уместности:
      photo   — есть настоящее фото объекта из статьи;
      figure  — в тексте есть выразительная ключевая цифра;
      map     — материал привязан к территории (лот, регион в заголовке);
      skyline — базовый вариант с силуэтом застройки.
    Затем берём первый, который не совпадает с недавно использованными.
    Без этого шага почти всё уходило бы в figure (цифра есть в большинстве
    новостей), и лента опять выглядела бы под копирку."""
    recent = recent or []
    title = item.get("title", "")
    options = []

    # 1) фото из статьи: сначала то, что дал RSS, иначе og:image со страницы
    photo = item.get("image_url") or ""
    if not photo and item.get("link") and not item.get("is_torgi"):
        photo = fetch_og_image(item["link"])
        item["image_url"] = photo          # запомним — пригодится при публикации
    if photo:
        options.append(("photo", {"image_url": photo}))

    # 2) ключевая цифра — из готового поста, иначе из заголовка и краткого текста
    figure = extract_key_figure(post_text) or extract_key_figure(
        title + " " + item.get("summary", ""))
    if figure:
        options.append(("figure", {"figure": figure}))

    # 3) территория: лоты ГИС Торги и материалы с явным регионом
    region = detect_region(title)
    if item.get("is_torgi") or region:
        options.append(("map", {"region": region}))

    options.append(("skyline", {}))        # всегда доступен как запасной

    for layout, extra in options:
        if layout not in recent:
            return layout, extra
    return options[0]                      # все варианты недавно были — берём лучший


def make_cover(title: str, source: str = "", variant: str = "auto",
               image_url: str = None, extra: dict = None) -> bytes:
    """Возвращает PNG-обложку (bytes).

    variant: photo | figure | map | skyline (старое «design» = skyline).
    extra:   данные для макета — {"figure": "340 га", "region": "Казань"}.

    Макет одинаковый во всех вариантах — меняются фон и надзаголовок. Так лента
    выглядит как единая серия, а не как набор разнородных картинок."""
    extra = extra or {}
    seed = _seed_of(title)
    if variant in ("design", "auto"):
        variant = "skyline"

    img = None
    if variant == "photo":
        img = _photo_background(image_url) if image_url else None
        if img is None:
            variant = "skyline"           # фото не скачалось — фирменный фон

    if img is None:
        img = _gradient(BG_TOP, BG_DARK)
        img = _vignette(img)
        img = _draw_marks(img, seed)

    img = _scrim(img)
    ImageDraw.Draw(img).rectangle([0, 0, COVER_W, 3], fill=ACCENT)  # золотая линия сверху

    kicker = ""
    if variant == "figure" and extra.get("figure"):
        img = _draw_figure(img, extra["figure"])
    elif variant == "map":
        kicker = extra.get("region", "") or "Территория"

    top = _draw_title(img, title)
    if kicker:
        _draw_kicker(img, kicker, y=top - 42)
    _draw_footer(img, source)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


# ----------------------------------------------------------------------------
# 5c. СОБСТВЕННЫЕ РУБРИКИ
#     Канал не должен состоять из одних пересказов чужих новостей. Здесь
#     собираются авторские форматы: «Цифра недели», еженедельный дайджест,
#     мини-кейсы, FAQ по КРТ и чек-листы. Расписание — в RUBRIC_SCHEDULE.
# ----------------------------------------------------------------------------

MSK = timezone(timedelta(hours=3))


def now_msk() -> datetime:
    """Текущее время по Москве. На сервере GitHub Actions часы стоят по UTC,
    а расписание рубрик и «один пост в день» должны считаться по местному дню."""
    return datetime.now(MSK)


def _state_get(key: str, default=None):
    """Читает поле из служебного состояния (state.json)."""
    return load_json(STATE_FILE, {}).get(key, default)


def _state_set(**pairs) -> None:
    """Пишет поля в служебное состояние, не затирая остальные."""
    st = load_json(STATE_FILE, {})
    st.update(pairs)
    save_json(STATE_FILE, st)


# --- копилка новостей за неделю ---------------------------------------------

def remember_for_week(item: dict) -> None:
    """Складывает материал в недельную копилку — из неё потом собираются
    дайджест и «Цифра недели»."""
    week = load_json(WEEK_FILE, [])
    week.append({
        "title": item.get("title", ""),
        "summary": (item.get("summary", "") or "")[:600],
        "link": item.get("link", ""),
        "source": (source_label(item))[0],
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    # чистим всё, что старше окна
    edge = datetime.now(timezone.utc) - timedelta(days=WEEK_WINDOW_DAYS)
    week = [w for w in week if (w.get("date") or "") >= edge.isoformat(timespec="seconds")]
    save_json(WEEK_FILE, week[-80:])          # верхняя граница на всякий случай


def week_entries() -> list:
    """Материалы за последние WEEK_WINDOW_DAYS дней, свежие — первыми."""
    edge = datetime.now(timezone.utc) - timedelta(days=WEEK_WINDOW_DAYS)
    week = [w for w in load_json(WEEK_FILE, [])
            if (w.get("date") or "") >= edge.isoformat(timespec="seconds")]
    return list(reversed(week))


# --- обращение к модели ------------------------------------------------------

def _ask_claude(prompt: str, max_tokens: int = 1200) -> str:
    """Один вызов модели в голосе канала. Пустая строка — если ключа нет."""
    if not ANTHROPIC_API_KEY:
        return ""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=max_tokens,
        system=CHANNEL_VOICE, messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _ask_json(prompt: str, max_tokens: int = 1400):
    """То же, но ответ ожидается в JSON. Возвращает объект или None."""
    raw = _ask_claude(prompt + "\n\nВерни ТОЛЬКО JSON, без пояснений и без ```.",
                      max_tokens)
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except Exception:
        mm = re.search(r"[\{\[].*[\}\]]", raw, re.DOTALL)
        try:
            return json.loads(mm.group(0)) if mm else None
        except Exception:
            return None


# --- банк вопросов и тем -----------------------------------------------------
# Рубрики FAQ и «Чек-лист» идут по кругу: индекс последней использованной темы
# хранится в state.json, поэтому вопросы не повторяются от недели к неделе.

FAQ_QUESTIONS = [
    "Чем комплексное развитие территории отличается от реновации?",
    "Какие виды КРТ предусмотрены Градостроительным кодексом?",
    "Кто может выступить инициатором проекта КРТ?",
    "Как участок попадает в границы КРТ и можно ли это отследить заранее?",
    "Какие права сохраняются у собственника недвижимости в границах КРТ?",
    "Как проходят торги на право заключения договора о КРТ?",
    "Что входит в обязательства застройщика по договору о КРТ?",
    "Как определяется размер возмещения при изъятии недвижимости для КРТ?",
    "В каком порядке можно оспорить решение о КРТ?",
    "Какие гарантии предусмотрены для нанимателей жилья при КРТ?",
    "Как соотносятся проект планировки территории и договор о КРТ?",
    "Какие сроки установлены для этапов реализации проекта КРТ?",
    "Что такое КРТ незастроенной территории и чем оно особенно?",
    "Как учитываются объекты культурного наследия в границах КРТ?",
    "Что происходит с договором аренды земли при включении участка в КРТ?",
    "Какие требования к социальной инфраструктуре предъявляются в проектах КРТ?",
]

CHECKLIST_TOPICS = [
    "Что делать, если участок попал в границы КРТ",
    "Как подготовиться к торгам на право заключения договора о КРТ",
    "Что проверить на площадке до входа в проект КРТ",
    "Что проверить в проекте договора о КРТ до подписания",
    "Как оценить риск изъятия недвижимости в границах КРТ",
    "Какие документы собрать правообладателю в границах КРТ",
    "Как выстроить график проекта КРТ от решения до ввода",
    "Как проверить обременения и ЗОУИТ на площадке КРТ",
    "Что учесть в финансовой модели проекта КРТ",
    "Как пройти согласование документации по планировке в рамках КРТ",
    "Что проверить в участке под ИЖС перед покупкой",
    "Как подготовить площадку КРТ к проектному финансированию",
]


def _rotate(bank: list, key: str) -> str:
    """Берёт следующую тему из банка по кругу и запоминает позицию."""
    i = int(_state_get(key, -1)) + 1
    _state_set(**{key: i % len(bank)})
    return bank[i % len(bank)]


# --- сами рубрики ------------------------------------------------------------

def _rubric_figure() -> dict:
    """«Цифра недели» — одна ключевая цифра рынка КРТ с пояснением.
    Цифру берём ТОЛЬКО из реальных материалов недели и проверяем, что она
    действительно встречается в источнике, — иначе рубрику пропускаем."""
    entries = week_entries()
    if len(entries) < 3:
        print("Цифра недели: мало материалов за неделю — пропускаю")
        return None

    material = "\n\n".join(
        f"[{i}] {e['title']}\n{e.get('summary', '')[:300]}"
        for i, e in enumerate(entries[:15]))

    data = _ask_json(f"""Ниже — заголовки и краткие описания материалов о КРТ и ИЖС за неделю.

{material}

Выбери ОДНУ самую значимую для рынка цифру, которая ЯВНО присутствует в тексте выше. Не придумывай и не пересчитывай цифры — бери ровно так, как написано.

Верни JSON:
{{
  "figure": "короткая запись цифры для обложки, например «340 га» или «1,2 млрд ₽»",
  "source_index": номер материала в квадратных скобках,
  "text": "текст поста в официально-деловом стиле, 450–700 знаков: что это за цифра, откуда она, что означает для застройщиков и инвесторов. Форматирование — только <b> и <i>. Без заголовка и без ссылок.",
  "poll_question": "короткий вопрос для голосования по теме, до 100 знаков",
  "poll_options": ["вариант 1", "вариант 2", "вариант 3"]
}}""")

    if not data or not data.get("figure") or not data.get("text"):
        return None

    # проверка: цифра должна реально встречаться в материалах недели
    digits = re.sub(r"\D", "", str(data["figure"]))[:4]
    haystack = re.sub(r"\s", "", material)
    if digits and digits not in re.sub(r"\D", "", haystack):
        print(f"Цифра недели: «{data['figure']}» не найдена в источниках — пропускаю")
        return None

    try:
        src = entries[int(data.get("source_index", 0))]
    except Exception:
        src = entries[0]

    text = f"📊 <b>ЦИФРА НЕДЕЛИ</b>\n\n{_clean_emoji(data['text'])}"
    if src.get("link"):
        url = src["link"].replace("&", "&amp;").replace('"', "%22")
        text += f'\n\n📎 Источник: <b>{src.get("source", "источник")}</b>'
        if domain_of(src["link"]):
            text += f' ({domain_of(src["link"])})'
        text += f' — <a href="{url}">читать оригинал</a>'

    return {
        "id": "rubric_figure_" + now_msk().strftime("%Y%W"),
        "title": str(data["figure"]),
        "text": text,
        "layout": "figure",
        "extra": {"figure": str(data["figure"])},
        "source": "ИПМ",
        "poll": _poll_from(data),
    }


def _rubric_digest() -> dict:
    """Еженедельный дайджест — 5–7 новостей недели одним постом.
    Список и ссылки собирает код, модель пишет только пояснения к пунктам."""
    entries = [e for e in week_entries() if e.get("link")][:7]
    if len(entries) < 4:
        print("Дайджест: мало материалов за неделю — пропускаю")
        return None

    listing = "\n".join(f"[{i}] {e['title']}" for i, e in enumerate(entries))
    data = _ask_json(f"""Ниже — материалы о КРТ и ИЖС за прошедшую неделю.

{listing}

Для КАЖДОГО пункта напиши одну строку по существу: что произошло и почему это важно рынку. Максимум 140 знаков на строку, официально-деловой стиль, без первого лица.

Верни JSON: {{"items": [{{"index": 0, "line": "..."}}, ...], "intro": "одно предложение-подводка ко всему дайджесту"}}""")

    if not data or not data.get("items"):
        return None

    lines = ["🗂 <b>ДАЙДЖЕСТ НЕДЕЛИ</b>", ""]
    if data.get("intro"):
        lines += [_clean_emoji(str(data["intro"])), ""]

    used = 0
    for it in data["items"]:
        try:
            e = entries[int(it["index"])]
        except Exception:
            continue
        url = e["link"].replace("&", "&amp;").replace('"', "%22")
        used += 1
        lines.append(f'{used}. <b>{_esc(e["title"][:110])}</b>')
        lines.append(f'{_clean_emoji(str(it.get("line", "")))[:160]}')
        lines.append(f'<a href="{url}">{e.get("source") or domain_of(e["link"])}</a>')
        lines.append("")
    if used < 4:
        return None

    lines.append("❓ Какая из тем недели заметнее всего повлияет на ваши проекты?")
    return {
        "id": "rubric_digest_" + now_msk().strftime("%Y%W"),
        "title": "Дайджест недели: КРТ и ИЖС",
        "text": "\n".join(lines).strip(),
        "layout": "skyline",
        "extra": {},
        "source": "ИПМ",
        "poll": None,
    }


def _rubric_faq() -> dict:
    """FAQ по КРТ — один вопрос за пост, темы идут по кругу без повторов."""
    question = _rotate(FAQ_QUESTIONS, "faq_i")
    body = _ask_claude(f"""Напиши пост-ответ для рубрики «FAQ по КРТ».

ВОПРОС: {question}

Требования:
- Ответ по существу российского законодательства о комплексном развитии территорий: Градостроительный кодекс, ФЗ-494 и связанные нормы.
- Структура: 2–3 предложения основного ответа, затем 2–4 уточняющих тезиса строками с «— ».
- Заверши 1–2 предложениями практического вывода для девелопера — обычным текстом, без заголовка и без ярлыка вида «Взгляд ИПМ».
- Официально-деловой стиль, без первого лица, без обращений к читателю.
- Если по вопросу есть разночтения в практике — прямо это укажи, не выдавай спорное за однозначное.
- Не приводи номера статей и точные даты, если не уверен в них полностью. Лучше без ссылки на норму, чем с неверной.
- Объём до 900 знаков. Форматирование — только <b> и <i>. Без заголовка — его добавит система.

Верни ТОЛЬКО текст ответа.""", max_tokens=1200)
    if not body:
        return None

    text = (f"❓ <b>FAQ ПО КРТ</b>\n\n<b>{_esc(question)}</b>\n\n"
            f"{_clean_emoji(body)}")
    return {
        "id": "rubric_faq_" + hashlib.md5(question.encode()).hexdigest()[:8],
        "title": question,
        "text": text,
        "layout": "skyline",
        "extra": {},
        "source": "ИПМ",
        "poll": {
            "question": "Сталкивались с этим вопросом на практике?",
            "options": ["Да, в текущем проекте", "Да, раньше", "Пока нет"],
        },
    }


def _rubric_checklist() -> dict:
    """Чек-листы и инструкции — практический разбор одной ситуации."""
    topic = _rotate(CHECKLIST_TOPICS, "checklist_i")
    body = _ask_claude(f"""Напиши пост-чек-лист для канала о девелопменте и КРТ.

ТЕМА: {topic}

Требования:
- 5–7 пунктов, каждый с новой строки в формате «— <b>Действие.</b> Пояснение в одном предложении.»
- Пункты идут в логическом порядке выполнения, от первого шага к последнему.
- Только практические действия и проверки, никакой общей теории.
- Заверши 1–2 предложениями о том, где на этом пути чаще всего теряют время или деньги — обычным текстом, без заголовка и без ярлыка.
- Официально-деловой стиль, без первого лица.
- Не указывай конкретные номера статей и сроки, если не уверен в них полностью.
- Объём до 1000 знаков. Форматирование — только <b> и <i>. Без заголовка.

Верни ТОЛЬКО текст чек-листа.""", max_tokens=1300)
    if not body:
        return None

    text = f"✅ <b>ЧЕК-ЛИСТ</b>\n\n<b>{_esc(topic)}</b>\n\n{_clean_emoji(body)}"
    return {
        "id": "rubric_checklist_" + hashlib.md5(topic.encode()).hexdigest()[:8],
        "title": topic,
        "text": text,
        "layout": "map",
        "extra": {"region": ""},
        "source": "ИПМ",
        "poll": None,
    }


def _rubric_case(seen: set) -> dict:
    """Мини-кейс: разбор конкретного проекта или лота КРТ по реальным данным."""
    # приоритет — свежий лот ГИС Торги: там точные, проверяемые цифры
    lots = collect_torgi(set())
    base, article = None, ""
    if lots:
        base = lots[0]
        article = base.get("summary", "")
    else:
        for e in week_entries():
            if e.get("link") and detect_region(e["title"]):
                base = {"title": e["title"], "link": e["link"],
                        "source": e.get("source", ""), "summary": e.get("summary", "")}
                article = fetch_article_text(e["link"]) or e.get("summary", "")
                break
    if not base or len(article) < 80:
        print("Мини-кейс: нет подходящего материала — пропускаю")
        return None

    body = _ask_claude(f"""Напиши мини-разбор проекта для рубрики «Мини-кейс КРТ».

НАЗВАНИЕ: {base['title']}

ИСХОДНЫЕ ДАННЫЕ:
{article[:3000]}

Требования:
- Структура из трёх блоков с подзаголовками в <b>жирном</b>: «Площадка», «Условия», «На что смотреть».
- Под каждым подзаголовком — 1–2 предложения или 2 тезиса с «— ».
- Все цифры бери ТОЛЬКО из исходных данных выше. Ничего не досчитывай и не додумывай.
- Заверши 1–2 предложениями: при каких вводных площадка интересна, при каких — нет. Обычным текстом, без заголовка и без ярлыка.
- Официально-деловой стиль, без первого лица. До 1000 знаков.
- Форматирование — только <b> и <i>. Без заголовка и без ссылок.

Верни ТОЛЬКО текст разбора.""", max_tokens=1300)
    if not body:
        return None

    text = f"🏗 <b>МИНИ-КЕЙС</b>\n\n<b>{_esc(base['title'][:120])}</b>\n\n{_clean_emoji(body)}"
    if base.get("link"):
        url = base["link"].replace("&", "&amp;").replace('"', "%22")
        name, dom = source_label(base)
        text += f'\n\n📎 Источник: <b>{_esc(name)}</b>'
        if dom:
            text += f' ({dom})'
        text += f' — <a href="{url}">читать оригинал</a>'

    return {
        "id": "rubric_case_" + hashlib.md5(base["title"].encode()).hexdigest()[:8],
        "title": base["title"][:120],
        "text": text,
        "layout": "map",
        "extra": {"region": detect_region(base["title"])},
        "source": base.get("source", "ИПМ"),
        "poll": None,
    }


def _poll_from(data: dict):
    """Достаёт голосование из ответа модели, если оно корректное."""
    q = str(data.get("poll_question") or "").strip()
    opts = [str(o).strip()[:100] for o in (data.get("poll_options") or []) if str(o).strip()]
    if not q or len(opts) < 2:
        return None
    return {"question": q[:300], "options": opts[:4]}


def _esc(text: str) -> str:
    """Экранирует текст, который вставляем в HTML-разметку поста."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


RUBRIC_BUILDERS = {
    "figure":    lambda seen: _rubric_figure(),
    "digest":    lambda seen: _rubric_digest(),
    "faq":       lambda seen: _rubric_faq(),
    "checklist": lambda seen: _rubric_checklist(),
    "case":      _rubric_case,
}


def build_today_rubric(seen: set) -> dict:
    """Готовит рубричный пост на сегодня по расписанию.
    None — если рубрика на сегодня не назначена, уже выходила или не собралась."""
    if not RUBRICS_ENABLED:
        return None
    kind = RUBRIC_SCHEDULE.get(now_msk().weekday())
    if not kind:
        return None

    # одна рубрика в день: бот запускается несколько раз в сутки
    today = now_msk().strftime("%Y-%m-%d")
    if _state_get("rubric_done") == f"{today}:{kind}":
        return None

    print(f"Рубрика на сегодня: {kind}")
    try:
        post = RUBRIC_BUILDERS[kind](seen)
    except Exception as e:
        print(f"Рубрика «{kind}» не собралась: {e}")
        return None
    if not post:
        return None

    post["kind"] = kind
    post["text"] = ensure_footer(_fit_body(post["text"], "", 0, MAX_POST_CHARS))
    _state_set(rubric_done=f"{today}:{kind}")
    return post


# ----------------------------------------------------------------------------
# 6. ОБЩЕНИЕ С TELEGRAM
# ----------------------------------------------------------------------------

def tg_api(method: str, payload: dict, soft: bool = False) -> dict:
    """Базовый вызов любого метода Telegram Bot API.
    soft=True — вернуть ответ даже при ошибке (не бросать исключение)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, json=payload, timeout=60)
    data = resp.json()
    if not data.get("ok") and not soft:
        raise RuntimeError(f"Telegram вернул ошибку ({method}): {data}")
    return data


def strip_tags(text: str) -> str:
    """Убирает все HTML-теги — для отправки обычным текстом, если разметка битая."""
    text = re.sub(r"<[^>]*>", "", text or "")
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def sanitize_html(text: str) -> str:
    """Делает разметку безопасной для Telegram: экранирует случайные < > &,
    сохраняя валидные теги <b> <i> <a href>, и закрывает незакрытые теги."""
    text = text or ""
    # 1) выкинуть заведомо ОБРЕЗАННЫЙ тег в конце (после обрезки по длине),
    #    но только если это реальное начало тега (< + буква/слэш), а не «<5 га»
    text = re.sub(r"<\/?[a-zA-Z][^>]*$", "", text)

    # 2) защитить валидные теги, экранировать остальные < > &, вернуть теги
    saved = []
    def _protect(mm):
        saved.append(mm.group(0))
        return f"\x00{len(saved) - 1}\x00"
    text = re.sub(r'</?(?:b|i)>|<a\s+href="[^"]*">|</a>', _protect, text, flags=re.I)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for i, tag in enumerate(saved):
        text = text.replace(f"\x00{i}\x00", tag)

    # 3) закрыть незакрытые b/i/a
    for tag in ("b", "i"):
        o = len(re.findall(rf"<{tag}>", text, re.I))
        c = len(re.findall(rf"</{tag}>", text, re.I))
        if o > c:
            text += f"</{tag}>" * (o - c)
    ao = len(re.findall(r"<a\b[^>]*>", text, re.I))
    ac = len(re.findall(r"</a>", text, re.I))
    if ao > ac:
        text += "</a>" * (ao - ac)
    return text


def send_message(chat_id, text: str) -> dict:
    """Отправляет сообщение. Если HTML битый — шлёт обычным текстом (не теряя пост)."""
    resp = tg_api("sendMessage", {
        "chat_id": chat_id, "text": sanitize_html(text)[:4096],
        "parse_mode": "HTML", "disable_web_page_preview": False,
    }, soft=True)
    if not resp.get("ok") and "parse entities" in str(resp.get("description", "")):
        resp = tg_api("sendMessage", {
            "chat_id": chat_id, "text": strip_tags(text)[:4096],
            "disable_web_page_preview": False,
        })
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram sendMessage ошибка: {resp}")
    return resp


def send_photo(chat_id, image_bytes: bytes, caption: str) -> dict:
    """Отправляет фото с подписью. Если HTML в подписи битый — шлёт подпись без разметки."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

    def _post(cap, html=True):
        # НЕ режем по сырым символам: лимит Telegram (1024) — по ВИДИМОМУ тексту,
        # а длинный адрес ссылки его не занимает. Обрезка тут ломала ссылку в конце.
        data = {"chat_id": chat_id, "caption": cap}
        if html:
            data["parse_mode"] = "HTML"
        return requests.post(
            url, data=data,
            files={"photo": ("cover.png", image_bytes, "image/png")}, timeout=90,
        ).json()

    data = _post(sanitize_html(caption), html=True)
    if not data.get("ok") and "parse entities" in str(data.get("description", "")):
        print("⚠ HTML в подписи не разобрался — отправляю без разметки (ссылка станет текстом)")
        data = _post(strip_tags(caption), html=False)     # запасной путь: без разметки
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto ошибка: {data}")
    return data


def _split_for_caption(text: str) -> tuple:
    """Делит пост на подпись к фото и «хвост» отдельным сообщением.
    Рубричные посты (дайджест, чек-лист) длиннее лимита подписи в 1024 знака."""
    if _visible_len(text) <= CAPTION_LIMIT:
        return text, ""
    head, rest, used = [], [], 0
    for para in text.split("\n\n"):
        plen = _visible_len(para) + 2
        if not rest and used + plen <= CAPTION_LIMIT - 40:
            head.append(para)
            used += plen
        else:
            rest.append(para)
    if not head:                       # первый же абзац длиннее лимита
        head, rest = [text[:CAPTION_LIMIT]], [text[CAPTION_LIMIT:]]
    return "\n\n".join(head), "\n\n".join(rest)


def send_post(chat_id, cover: bytes, text: str, prefix: str = "") -> int:
    """Публикует пост: обложка + текст. Если текст не влезает в подпись —
    остаток уходит следующим сообщением. Возвращает message_id ФОТО
    (именно на него ставится реакция-одобрение)."""
    caption, rest = _split_for_caption(text)
    resp = send_photo(chat_id, cover, (prefix + caption) if prefix else caption)
    mid = resp["result"]["message_id"]
    if rest:
        time.sleep(1)
        send_message(chat_id, rest)
    return mid


def send_poll(chat_id, question: str, options: list) -> None:
    """Отправляет голосование — рубрики с опросом заметно поднимают вовлечённость.
    Ошибку не роняем наверх: пост важнее опроса."""
    if not SEND_POLLS or not question or len(options or []) < 2:
        return
    resp = tg_api("sendPoll", {
        "chat_id": chat_id,
        "question": question[:300],
        "options": [o[:100] for o in options[:10]],
        "is_anonymous": True,
        "allows_multiple_answers": False,
    }, soft=True)
    if not resp.get("ok"):
        print(f"⚠ Голосование не отправилось: {resp.get('description')}")


def check_comments_enabled() -> None:
    """Проверяет, подключено ли к каналу обсуждение (комментарии под постами).
    Включаются они не кодом, а в настройках канала — поэтому просто напоминаем."""
    resp = tg_api("getChat", {"chat_id": TELEGRAM_CHANNEL}, soft=True)
    if not resp.get("ok"):
        return
    if not resp.get("result", {}).get("linked_chat_id"):
        print("⚠ К каналу не подключена группа обсуждения — комментарии под постами")
        print("  недоступны. Включается вручную: настройки канала → Обсуждение.")


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
        # совместимость: раньше в очереди мог лежать просто текст (строка)
        if isinstance(data, str):
            data = {"text": data, "title": "", "source": "", "image_url": "", "group": None}
        try:
            title = data.get("title") or (data.get("text", "")[:80]) or "КРТ"
            # обложку пересобираем тем же макетом, что был у черновика
            cover = make_cover(title, data.get("source", ""),
                               data.get("layout", "design"),
                               data.get("image_url") or None,
                               data.get("extra") or {})
            send_post(TELEGRAM_CHANNEL, cover, ensure_footer(data.get("text", "")))

            # голосование к рубрике — отдельным сообщением следом за постом
            poll = data.get("poll")
            if poll:
                time.sleep(1)
                send_poll(TELEGRAM_CHANNEL, poll.get("question", ""),
                          poll.get("options", []))

            # убираем черновик этого материала из очереди
            grp = data.get("group")
            if grp is not None:
                # чистим только словарные записи с тем же group (строки пропускаем)
                for k in [k for k, v in pending.items()
                          if isinstance(v, dict) and v.get("group") == grp]:
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


DRAFT_PREFIX = "🔎 ЧЕРНОВИК — поставь реакцию, чтобы опубликовать в канал\n\n"


def _queue_rubric(pending: dict) -> int:
    """Ставит в очередь рубричный пост на сегодня. Возвращает 1, если поставил."""
    post = build_today_rubric(set())
    if not post:
        return 0
    try:
        cover = make_cover(post["title"], post.get("source", ""),
                           post.get("layout", "skyline"), None, post.get("extra"))
        mid = send_post(REVIEW_CHAT_ID, cover, post["text"], prefix=DRAFT_PREFIX)
        pending[str(mid)] = {
            "text": post["text"], "title": post["title"],
            "source": post.get("source", ""), "image_url": "",
            "layout": post.get("layout", "skyline"), "extra": post.get("extra") or {},
            "poll": post.get("poll"), "group": post["id"],
        }
        print(f"→ Черновик рубрики «{post['kind']}» отправлен")
        return 1
    except Exception as e:
        print(f"✗ Рубрику не отправил: {e}")
        return 0


def queue_drafts(seen: set):
    """ТАКТ 2: собираем свежие новости и шлём черновики ТЕБЕ на проверку.
    Сначала — рубрика дня (если сегодня она по расписанию), затем новости."""
    pending = load_json(PENDING_FILE, {})

    # 1) авторская рубрика на сегодня — идёт первой, вне лимита на новости
    sent = _queue_rubric(pending)
    save_json(PENDING_FILE, pending)

    # 2) новости
    news = collect_news(seen)
    print(f"Найдено новых материалов: {len(news)}")

    # какие макеты обложек уже использованы — чтобы не ставить два одинаковых подряд
    recent = list(_state_get("recent_layouts", []))
    posted = 0
    for item in news:
        if posted >= MAX_POSTS_PER_RUN:
            break
        try:
            post_text = make_post(item)

            # обложка подбирается под материал: фото объекта, крупная цифра,
            # схема территории или силуэт застройки — см. choose_layout
            layout, extra = choose_layout(item, post_text, recent)
            recent = (recent + [layout])[-2:]      # помним два последних макета
            cover = make_cover(item["title"], item["source"], layout,
                               item.get("image_url"), extra)

            mid = send_post(REVIEW_CHAT_ID, cover, post_text, prefix=DRAFT_PREFIX)
            pending[str(mid)] = {
                "text": post_text,
                "title": item["title"],
                "source": item["source"],
                "image_url": item.get("image_url", ""),
                "layout": layout,
                "extra": extra,
                "poll": None,
                "group": item["id"],
            }

            seen.add(item["id"])
            if item.get("_sh") is not None:
                remember_hash(seen, item["_sh"])   # помним СМЫСЛ — против перепечаток
            remember_for_week(item)                # копилка для дайджеста и «Цифры недели»
            posted += 1
            sent += 1
            print(f"→ Черновик отправлен [{layout}/{item.get('_format', '—')}]: "
                  f"{item['title'][:50]}")
        except Exception as e:
            print(f"✗ Пропустил «{item['title'][:40]}»: {e}")

    save_json(PENDING_FILE, pending)
    save_seen(seen)
    # чередование форматов и макетов продолжится в следующем запуске
    _state_set(post_seq=_POST_SEQ, recent_layouts=recent)
    print(f"Отправлено черновиков: {sent}")


def main():
    global _POST_SEQ
    seen = load_seen()
    # продолжаем сквозную нумерацию постов — от неё зависит чередование форматов
    _POST_SEQ = int(_state_get("post_seq", 0))
    check_comments_enabled()      # напоминание, если обсуждение не подключено

    if REVIEW_MODE:
        if not REVIEW_CHAT_ID:
            raise SystemExit("Не задан REVIEW_CHAT_ID — некуда слать черновики на проверку.")
        publish_approved()   # сначала публикуем то, что ты уже одобрил
        queue_drafts(seen)   # потом шлём новые черновики
    else:
        # Прямой режим без проверки (как раньше): сразу в канал.
        posted = 0

        # рубрика дня — первой
        rubric = build_today_rubric(seen)
        if rubric:
            try:
                cover = make_cover(rubric["title"], rubric.get("source", ""),
                                   rubric.get("layout", "skyline"), None,
                                   rubric.get("extra"))
                send_post(TELEGRAM_CHANNEL, cover, rubric["text"])
                if rubric.get("poll"):
                    send_poll(TELEGRAM_CHANNEL, rubric["poll"]["question"],
                              rubric["poll"]["options"])
                posted += 1
                print(f"✓ Опубликована рубрика «{rubric['kind']}»")
                time.sleep(3)
            except Exception as e:
                print(f"✗ Рубрику не опубликовал: {e}")

        news = collect_news(seen)
        print(f"Найдено новых материалов: {len(news)}")
        recent = list(_state_get("recent_layouts", []))
        news_posted = 0
        for item in news:
            if news_posted >= MAX_POSTS_PER_RUN:
                break
            try:
                text = ensure_footer(make_post(item))
                layout, extra = choose_layout(item, text, recent)
                recent = (recent + [layout])[-2:]
                cover = make_cover(item["title"], item["source"], layout,
                                   item.get("image_url"), extra)
                send_post(TELEGRAM_CHANNEL, cover, text)
                seen.add(item["id"])
                if item.get("_sh") is not None:
                    remember_hash(seen, item["_sh"])
                remember_for_week(item)
                news_posted += 1
                posted += 1
                print(f"✓ Опубликовано [{layout}]: {item['title'][:55]}")
                time.sleep(3)
            except Exception as e:
                print(f"✗ Пропустил «{item['title'][:40]}»: {e}")
        save_seen(seen)
        _state_set(post_seq=_POST_SEQ, recent_layouts=recent)
        print(f"Готово. Опубликовано постов: {posted}")


if __name__ == "__main__":
    main()
