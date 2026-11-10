"""
Автономный бот: еврейские новости Украины -> полноценные заметки на иврите -> Telegram-канал.

Как работает:
  1. Собирает свежие новости из RSS: израильские сайты на иврите, украинские еврейские
     организации, поиск Bing News и Google News по ключевым словам.
  2. Отбрасывает явно не относящиеся к теме (по ключевым словам) — экономит лимит ИИ.
  3. Скачивает полный текст статьи.
  4. Gemini (бесплатный ИИ от Google) проверяет, что новость о евреях Украины и не повтор,
     и пишет по ней собственную заметку на иврите.
  5. Публикует в канал: заголовок + текст, без ссылок.

Переменные окружения:
  BOT_TOKEN      — токен бота от @BotFather (обязательно)
  GEMINI_KEY     — ключ Gemini API из aistudio.google.com (обязательно)
  CHANNEL        — @username канала
  ADMIN_CHAT_ID  — ваш личный chat id: бот напишет вам, если что-то сломается (необязательно)
  DRY_RUN=1      — ничего не публиковать, только показать в логе
"""
import html
import json
import os
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, quote_plus, urlparse, parse_qs

import feedparser
import requests
import trafilatura

# ============================ НАСТРОЙКИ ============================

CHANNEL = os.environ.get("CHANNEL", "@Ukraine_Jewish_Kosher_Guide")
SIGNATURE = "@Ukraine_Jewish_Kosher_Guide"  # подпись внизу поста ("" — без подписи)
SHOW_SOURCE = True       # писать в конце "מקור: <название СМИ>" (без ссылки)
USE_PHOTOS = False       # True — прикладывать фото из статьи (фото принадлежат СМИ)
MAX_POSTS_PER_RUN = 3    # не больше N постов за запуск (запуск раз в час)
MAX_AI_CALLS_PER_RUN = 5 # бесплатный лимит Gemini небольшой — не тратим лишнее
MAX_AGE_HOURS = 48       # более старые новости игнорируются
USE_SEARCH_FALLBACK = True  # если текст статьи не скачался — попросить Gemini найти новость в Google

# Слова, хотя бы одно из которых должно быть в заголовке/тексте (иначе новость даже не отправляется в ИИ)
JEWISH_WORDS = [
    "יהוד", "חב\"ד", "חב''ד", "חבד", "רב ", "הרב", "רבני", "קהיל", "בית כנסת", "בית הכנסת",
    "אומן", "ברסלב", "שואה", "עלי", "שליח", "כשר", "תורה", "ישיב", "אנטישמ",
    "евре", "єврей", "иуде", "юде", "юдаї", "раввин", "рабин", "синагог", "хабад", "хасид",
    "умань", "холокост", "голокост", "антисемит", "алия", "алія", "кошер", "мацев", "ваад",
    "jew", "rabbi", "chabad", "synagog", "uman", "holocaust",
]


def gnews(query, lang):
    """RSS поиска Google News за 2 дня."""
    region = {"he": "hl=he&gl=IL&ceid=IL:he",
              "ru": "hl=ru&gl=UA&ceid=UA:ru",
              "uk": "hl=uk&gl=UA&ceid=UA:uk"}[lang]
    return f"https://news.google.com/rss/search?q={quote_plus(query + ' when:2d')}&{region}"


def bing(query, mkt):
    """RSS поиска Bing News (в нём настоящие ссылки на статьи)."""
    return f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss&mkt={mkt}&qft=sortbydate%3d%221%22"


def wp_tag(site, tag):
    """RSS тега на сайте WordPress."""
    return f"{site}/tag/{quote(tag)}/feed/"


FEEDS = [
    # --- Прямые ленты сайтов (полный текст статей) ---
    wp_tag("https://www.jdn.co.il", "אוקראינה"),
    wp_tag("https://www.jdn.co.il", "אומן"),
    wp_tag("https://www.jdn.co.il", "קייב"),
    "https://vaadua.org/rss.xml",
    # --- Bing News ---
    bing("יהודי אוקראינה", "he-IL"),
    bing("הקהילה היהודית באוקראינה", "he-IL"),
    bing('חב"ד אוקראינה', "he-IL"),
    bing("אומן ברסלב", "he-IL"),
    bing("еврейская община Украины", "ru-RU"),
    bing("раввин Украина", "ru-RU"),
    bing("єврейська громада України", "uk-UA"),
    bing("Умань хасиди", "uk-UA"),
    # --- Google News (ссылки раскодируются не всегда — обрабатываются в последнюю очередь) ---
    gnews("יהודי אוקראינה", "he"),
    gnews("הקהילה היהודית באוקראינה", "he"),
    gnews('שליח חב"ד אוקראינה', "he"),
    gnews("הרב הראשי של אוקראינה", "he"),
    gnews("אומן ברסלב", "he"),
    gnews("еврейская община Украины", "ru"),
    gnews("раввин Украины", "ru"),
    gnews("єврейська громада", "uk"),
    gnews("рабин Україна", "uk"),
]

# ============================ КОД ============================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
STATE_FILE = "state.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
NOW = datetime.now(timezone.utc)
API = "https://generativelanguage.googleapis.com/v1beta"
KIND_ORDER = {"direct": 0, "bing": 1, "gnews": 2}


class QuotaExceeded(Exception):
    pass


class SearchUnavailable(Exception):
    pass


# ---------- состояние ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    else:
        st = {}
    for k, v in {"seen": {}, "recent_titles": [], "fails": {}, "alerts": {}}.items():
        st.setdefault(k, v)
    return st


def save_state(state):
    cutoff = (NOW - timedelta(days=30)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
    state["recent_titles"] = state["recent_titles"][-40:]
    state["heartbeat"] = NOW.strftime("%Y-%m-%d")  # чтобы GitHub не счёл репозиторий заброшенным
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def mark_fail(state, item_id):
    fails = state["fails"].get(item_id, 0) + 1
    state["fails"][item_id] = fails
    if fails >= 3:
        state["seen"][item_id] = NOW.isoformat()
        state["fails"].pop(item_id, None)


# ---------- Telegram ----------

def tg(method, **payload):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=60)
    if not r.ok:
        print(f"Telegram {method} error:", r.text)
    return r.ok


def alert_admin(state, key, text):
    """Сообщение владельцу, не чаще раза в сутки по одной проблеме."""
    print("ALERT:", text)
    if not ADMIN_CHAT_ID or DRY_RUN:
        return
    last = state["alerts"].get(key, "")
    if last and last > (NOW - timedelta(hours=24)).isoformat():
        return
    if tg("sendMessage", chat_id=ADMIN_CHAT_ID, text=f"⚠️ Новостной бот: {text}"[:4000]):
        state["alerts"][key] = NOW.isoformat()


def publish(title, body, source, image):
    parts = [f"<b>{html.escape(title)}</b>", html.escape(body)]
    footer = []
    if SHOW_SOURCE and source:
        footer.append(f"מקור: {html.escape(source)}")
    if SIGNATURE:
        footer.append(html.escape(SIGNATURE))
    if footer:
        parts.append("\n".join(footer))
    text = "\n\n".join(parts)

    if DRY_RUN:
        print("\n========== ПОСТ ==========\n" + text + "\n==========================\n")
        return True
    if USE_PHOTOS and image and len(text) <= 1024:
        if tg("sendPhoto", chat_id=CHANNEL, photo=image, caption=text, parse_mode="HTML"):
            return True
    return tg("sendMessage", chat_id=CHANNEL, text=text[:4096], parse_mode="HTML",
              link_preview_options={"is_disabled": True})


# ---------- сбор новостей ----------

def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def site_name(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def bing_real_url(link):
    q = parse_qs(urlparse(link).query)
    return q["url"][0] if q.get("url") else link


def looks_relevant(text):
    t = text.lower()
    return any(w in t for w in JEWISH_WORDS)


def collect():
    items, working = [], 0
    cutoff = NOW - timedelta(hours=MAX_AGE_HOURS)
    for url in FEEDS:
        kind = "gnews" if "news.google.com" in url else "bing" if "bing.com" in url else "direct"
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print("Лента недоступна:", url[:90], e)
            continue
        if feed.entries:
            working += 1
        print(f"{len(feed.entries):3d} записей  [{kind}] {url[:100]}")
        feed_title = clean(feed.feed.get("title", ""))
        for e in feed.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            dt = datetime(*t[:6], tzinfo=timezone.utc) if t else NOW
            if dt < cutoff:
                continue
            link = e.get("link", "")
            title = clean(e.get("title", ""))
            if kind == "gnews":
                source = (e.get("source") or {}).get("title", "")
                if source and title.endswith(" - " + source):
                    title = title[: -len(" - " + source)]
            elif kind == "bing":
                link = bing_real_url(link)
                source = clean(e.get("news_source", "")) or site_name(link)
            else:
                source = feed_title.split(" - ")[0].split(" | ")[0].strip() or site_name(link)
            full = clean(e.content[0].get("value", "")) if e.get("content") else ""
            items.append({
                "id": e.get("id") or link,
                "title": title,
                "link": link,
                "source": source,
                "date": dt.isoformat(),
                "rss_text": full or clean(e.get("summary", "")),
                "kind": kind,
            })
    uniq = {}
    for it in sorted(items, key=lambda x: KIND_ORDER[x["kind"]]):
        uniq.setdefault(re.sub(r"\W+", "", it["title"].lower()), it)
    return list(uniq.values()), working


def resolve_link(it):
    if it["kind"] != "gnews":
        return it["link"]
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(it["link"], interval=1)
        if r.get("status"):
            return r["decoded_url"]
        print("  ссылка Google News не раскодировалась")
    except Exception as e:
        print("  ошибка раскодирования Google News:", e)
    return None


def fetch_article(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        page = r.text
    except Exception as e:
        print("  статья не скачалась:", str(e)[:150])
        return "", None
    text = trafilatura.extract(page, include_comments=False, include_tables=False) or ""
    image = None
    try:
        meta = trafilatura.extract_metadata(page)
        image = meta.image if meta else None
    except Exception:
        pass
    return text, image


# ---------- Gemini ----------

PROMPT = """You are the editor of a Hebrew-language Telegram news channel about Jewish life in Ukraine
(Jewish communities, rabbis and Chabad emissaries, synagogues, Jewish holidays in Ukraine, Uman pilgrims,
antisemitism in Ukraine, Holocaust memory in Ukraine, aid to Ukrainian Jews, aliyah from Ukraine,
Israel–Ukraine relations as they affect Jews).

Below is a news article (it may be in Hebrew, Russian, Ukrainian or English).

Tasks:
1. "relevant": true only if the article is a NEWS report mainly about Jews / Jewish life / Jewish community
   in Ukraine (or Ukrainian Jews / Uman pilgrims). General war news, Israeli politics, Jews elsewhere,
   opinion columns, archive pages or ads -> false.
2. "duplicate": true if it reports the same event as one of these already published headlines:
{recent}
3. If relevant and not duplicate, write an ORIGINAL news item in modern Hebrew, in your own words
   (do not copy sentences from the source, do not translate word for word):
   - "title": a short newsy headline (up to 12 words)
   - "body": 2–5 short paragraphs, 600–1400 characters total, neutral news style,
     only facts that appear in the article; keep names, numbers, places, dates accurate;
     no links, no hashtags, no emojis, no mention of "the article" or "the source".
   Separate paragraphs with a blank line.

Answer ONLY with JSON: {{"relevant": bool, "duplicate": bool, "title": "...", "body": "..."}}

Original headline: {title}
Published by: {source}
Article text:
{text}
"""

SEARCH_PROMPT = """Use Google Search to find this news story and read the details from reliable sources.
Headline: {title}
Published by: {source}
Date: {date}
Short snippet: {snippet}

If you cannot find reliable details about this exact story, answer {{"relevant": false}}.
Otherwise, use the facts you found as the "article text" for the task below.

"""

_models_cache = None


def version_key(name):
    nums = re.findall(r"\d+(?:\.\d+)?", name)
    return float(nums[0]) if nums else 0.0


def gemini_models():
    """Список доступных моделей Flash — узнаём у Google, чтобы не зависеть от устаревших имён."""
    global _models_cache
    if _models_cache is not None:
        return _models_cache
    found = []
    try:
        r = requests.get(f"{API}/models?pageSize=200", headers={"x-goog-api-key": GEMINI_KEY}, timeout=30)
        if r.status_code in (400, 401, 403):
            raise RuntimeError("Ключ Gemini не работает: " + r.text[:300])
        for m in r.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in name or any(b in name for b in
                                          ("image", "tts", "audio", "live", "thinking", "exp", "embed", "robotics", "computer")):
                continue
            found.append(name)
    except RuntimeError:
        raise
    except Exception as e:
        print("Не удалось получить список моделей:", e)
    # стабильные новые flash -> flash-lite -> preview
    found.sort(key=lambda n: ("preview" in n, "lite" in n, -version_key(n), len(n)))
    models = ["gemini-flash-latest"] + found + ["gemini-flash-lite-latest"]
    _models_cache = list(dict.fromkeys(models))
    print("Модели Gemini:", ", ".join(_models_cache[:8]))
    return _models_cache


def parse_json(raw):
    raw = re.sub(r"^```(json)?|```$", "", raw.strip()).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None


def ask_gemini(prompt, search=False):
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4}}
    if search:
        body["tools"] = [{"google_search": {}}]
    else:
        body["generationConfig"]["responseMimeType"] = "application/json"

    limited = 0
    models = gemini_models()
    for model in models:
        for attempt in range(2):
            try:
                r = requests.post(f"{API}/models/{model}:generateContent",
                                  headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=180)
            except requests.RequestException as e:
                print(f"  Gemini {model}: сеть", e)
                time.sleep(5)
                continue
            if r.status_code == 429:
                limited += 1
                print(f"  Gemini {model}: лимит")
                break
            if r.status_code in (401, 403) or "API_KEY_INVALID" in r.text:
                raise RuntimeError("Ключ Gemini не работает: " + r.text[:300])
            if r.status_code in (400, 404):
                print(f"  Gemini {model}: {r.status_code} недоступна")
                break
            if r.status_code >= 500:
                print(f"  Gemini {model}: сервер занят ({r.status_code}), повтор")
                time.sleep(8)
                continue
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                raw = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                res = parse_json(raw)
                if res is not None:
                    return res
                print(f"  Gemini {model}: ответ без JSON")
            except Exception as e:
                print(f"  Gemini {model}: непонятный ответ", e, r.text[:200])
            break
        if limited >= 3:
            break
    if limited:
        raise SearchUnavailable() if search else QuotaExceeded()
    return None


# ---------- главный цикл ----------

def run(state):
    items, working = collect()
    print(f"Работающих лент: {working}/{len(FEEDS)}, свежих новостей: {len(items)}")
    if working == 0:
        alert_admin(state, "no_feeds", "ни одна RSS-лента не ответила. Проверьте лог в GitHub Actions.")
        return

    seen = state["seen"]
    new = []
    for it in items:
        if it["id"] in seen:
            continue
        if not looks_relevant(it["title"] + " " + it["rss_text"]):
            seen[it["id"]] = NOW.isoformat()  # явно не по теме — не тратим ИИ
            continue
        new.append(it)
    # прямые сайты -> Bing -> Google News; внутри — свежие первыми
    new.sort(key=lambda x: (KIND_ORDER[x["kind"]], -datetime.fromisoformat(x["date"]).timestamp()))
    print(f"Новых по теме для обработки: {len(new)}")

    posted = calls = 0
    search_ok = USE_SEARCH_FALLBACK
    for it in new:
        if posted >= MAX_POSTS_PER_RUN or calls >= MAX_AI_CALLS_PER_RUN:
            break
        print(f"\n→ [{it['kind']}] {it['title']} | {it['source']}")

        url = resolve_link(it)
        text, image = fetch_article(url) if url else ("", None)
        if len(text) < len(it["rss_text"]):
            text = it["rss_text"]

        recent = "\n".join(f"- {t}" for t in state["recent_titles"][-25:]) or "- (none)"
        search = len(text) < 400
        if search:
            if not search_ok:
                print("  текста нет, поиск недоступен — попробую в другой раз")
                mark_fail(state, it["id"])
                continue
            print("  текста нет — ищу новость через Gemini + Google Search")
            prompt = SEARCH_PROMPT.format(title=it["title"], source=it["source"],
                                          date=it["date"][:10], snippet=text[:500])
            prompt += PROMPT.format(recent=recent, title=it["title"], source=it["source"],
                                    text="(use the facts you found via search)")
        else:
            prompt = PROMPT.format(recent=recent, title=it["title"], source=it["source"], text=text[:12000])

        calls += 1
        try:
            res = ask_gemini(prompt, search=search)
        except SearchUnavailable:
            print("  поиск Google в Gemini недоступен на бесплатном тарифе — дальше только статьи с текстом")
            search_ok = False
            mark_fail(state, it["id"])
            continue
        except QuotaExceeded:
            print("Лимит Gemini исчерпан — продолжу в следующий запуск")
            break
        if not res:
            mark_fail(state, it["id"])
            continue

        seen[it["id"]] = NOW.isoformat()
        state["fails"].pop(it["id"], None)
        if not res.get("relevant"):
            print("  не по теме")
            continue
        if res.get("duplicate"):
            print("  дубль уже опубликованной новости")
            continue
        title, body = (res.get("title") or "").strip(), (res.get("body") or "").strip()
        if not title or len(body) < 200:
            print("  ИИ вернул пустой текст")
            continue
        if publish(title, body, it["source"], image):
            print("  ✅ опубликовано")
            state["recent_titles"].append(title)
            posted += 1
            time.sleep(5)

    print(f"\nОпубликовано: {posted}")


def main():
    if not BOT_TOKEN or not GEMINI_KEY:
        raise SystemExit("Не заданы BOT_TOKEN и/или GEMINI_KEY")
    state = load_state()
    try:
        run(state)
    except Exception as e:
        traceback.print_exc()
        alert_admin(state, "crash", f"ошибка: {e}")
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
