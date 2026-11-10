"""
Автономный бот: еврейские новости Украины -> полноценные заметки на иврите -> Telegram-канал.

Как работает:
  1. Собирает свежие новости из RSS (израильские сайты на иврите + украинские/русские
     еврейские источники + Google News по ключевым словам).
  2. Скачивает текст статьи.
  3. Gemini (бесплатный ИИ от Google) проверяет, что новость действительно о евреях Украины
     и не повторяет уже опубликованную, и пишет по ней собственную заметку на иврите.
  4. Публикует в канал: заголовок + текст, без ссылок.

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
from urllib.parse import quote, quote_plus

import feedparser
import requests
import trafilatura

# ============================ НАСТРОЙКИ ============================

CHANNEL = os.environ.get("CHANNEL", "@Ukraine_Jewish_Kosher_Guide")
SIGNATURE = "@Ukraine_Jewish_Kosher_Guide"  # подпись внизу поста ("" — без подписи)
SHOW_SOURCE = True       # писать в конце "מקור: <название СМИ>" (без ссылки)
USE_PHOTOS = False       # True — прикладывать фото из статьи (фото принадлежат СМИ)
MAX_POSTS_PER_RUN = 3    # не больше N постов за запуск (запуск раз в час)
MAX_AI_CALLS_PER_RUN = 10
MAX_AGE_HOURS = 48       # более старые новости игнорируются
FIRST_RUN_POSTS = 3      # при самом первом запуске — только 3 самые свежие

# Модели Gemini по порядку: если одна недоступна или кончилась квота — берётся следующая
GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.5-flash", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]


def gnews(query, lang):
    """RSS-лента поиска Google News за последние 2 дня."""
    region = {"he": "hl=he&gl=IL&ceid=IL:he",
              "ru": "hl=ru&gl=UA&ceid=UA:ru",
              "uk": "hl=uk&gl=UA&ceid=UA:uk"}[lang]
    return f"https://news.google.com/rss/search?q={quote_plus(query + ' when:2d')}&{region}"


def wp_tag(site, tag):
    """RSS-лента тега на сайте WordPress."""
    return f"{site}/tag/{quote(tag)}/feed/"


FEEDS = [
    # --- Израильские сайты на иврите, прямые ленты ---
    wp_tag("https://www.jdn.co.il", "אוקראינה"),
    wp_tag("https://www.jdn.co.il", "אומן"),
    wp_tag("https://www.jdn.co.il", "קייב"),
    # --- Украинские еврейские организации ---
    "https://vaadua.org/rss.xml",
    # --- Google News на иврите ---
    gnews("יהודי אוקראינה", "he"),
    gnews("הקהילה היהודית באוקראינה", "he"),
    gnews('שליח חב"ד אוקראינה', "he"),
    gnews("הרב הראשי של אוקראינה", "he"),
    gnews("אומן ברסלב", "he"),
    gnews("יהודי קייב OR יהודי אודסה OR יהודי דנייפר OR יהודי חרקוב", "he"),
    # --- Google News на русском / украинском (ИИ перескажет на иврите) ---
    gnews("еврейская община Украины", "ru"),
    gnews("раввин Украины", "ru"),
    gnews("site:fjc.org.ua", "ru"),
    gnews("site:vaadua.org", "uk"),
    gnews("єврейська громада", "uk"),
    gnews("рабин Україна", "uk"),
    gnews("Умань хасиди", "uk"),
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


class QuotaExceeded(Exception):
    pass


# ---------- состояние (что уже опубликовано) ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"seen": {}, "recent_titles": [], "fails": {}, "alerts": {}}


def save_state(state):
    cutoff = (NOW - timedelta(days=30)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}
    state["recent_titles"] = state["recent_titles"][-40:]
    # отметка раз в сутки — чтобы GitHub не отключил расписание из-за "неактивности"
    state["heartbeat"] = NOW.strftime("%Y-%m-%d")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


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


def collect():
    items, working_feeds = [], 0
    cutoff = NOW - timedelta(hours=MAX_AGE_HOURS)
    for url in FEEDS:
        try:
            resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print("Лента недоступна:", url, e)
            continue
        if feed.entries:
            working_feeds += 1
        print(f"{len(feed.entries):3d} записей  {url[:110]}")
        feed_title = clean(feed.feed.get("title", ""))
        for e in feed.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            dt = datetime(*t[:6], tzinfo=timezone.utc) if t else NOW
            if dt < cutoff:
                continue
            is_gnews = "news.google.com" in url
            source = (e.get("source") or {}).get("title", "") if is_gnews else feed_title
            title = clean(e.get("title", ""))
            if is_gnews and source and title.endswith(" - " + source):
                title = title[: -len(" - " + source)]
            full = ""
            if e.get("content"):
                full = clean(e.content[0].get("value", ""))
            items.append({
                "id": e.get("id") or e.get("link"),
                "title": title,
                "link": e.get("link", ""),
                "source": source.split(" - ")[0].strip(),
                "date": dt.isoformat(),
                "rss_text": full or clean(e.get("summary", "")),
                "gnews": is_gnews,
            })
    # убрать одинаковые заголовки, сортировка от старых к новым
    uniq = {}
    for it in items:
        uniq.setdefault(it["title"].lower(), it)
    return sorted(uniq.values(), key=lambda x: x["date"]), working_feeds


def resolve_link(it):
    if not it["gnews"]:
        return it["link"]
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(it["link"], interval=1)
        if r.get("status"):
            return r["decoded_url"]
    except Exception as e:
        print("Не удалось раскодировать ссылку Google News:", e)
    return None


def fetch_article(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        page = r.text
    except Exception as e:
        print("Статья не скачалась:", url, e)
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
1. "relevant": true only if the article is mainly about Jews / Jewish life / Jewish community in Ukraine
   (or Ukrainian Jews / Uman pilgrims). General war news, Israeli politics, or Jews elsewhere -> false.
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


def ask_gemini(prompt):
    quota_hit = False
    for model in GEMINI_MODELS:
        for attempt in range(2):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": GEMINI_KEY},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {"temperature": 0.4,
                                               "responseMimeType": "application/json"}},
                    timeout=180,
                )
            except requests.RequestException as e:
                print(f"Gemini {model}: сеть", e)
                time.sleep(5)
                continue
            if r.status_code == 429:
                quota_hit = True
                print(f"Gemini {model}: лимит, пробую другую модель")
                break
            if r.status_code in (400, 403, 404):
                print(f"Gemini {model}: {r.status_code} {r.text[:300]}")
                if "API_KEY" in r.text or r.status_code == 403:
                    raise RuntimeError("Ключ Gemini не работает: " + r.text[:300])
                break
            if r.status_code >= 500:
                time.sleep(10)
                continue
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                raw = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                raw = re.sub(r"^```(json)?|```$", "", raw.strip()).strip()
                return json.loads(raw)
            except Exception as e:
                print(f"Gemini {model}: непонятный ответ", e, r.text[:300])
                continue
        time.sleep(4)
    if quota_hit:
        raise QuotaExceeded()
    return None


# ---------- главный цикл ----------

def run(state):
    items, working = collect()
    print(f"Работающих лент: {working}/{len(FEEDS)}, свежих новостей: {len(items)}")
    if working == 0:
        alert_admin(state, "no_feeds", "ни одна RSS-лента не ответила. Проверьте лог в GitHub Actions.")
        return

    seen = state["seen"]
    new = [it for it in items if it["id"] not in seen]

    if not seen:  # первый запуск — не заваливаем канал
        for it in new[:-FIRST_RUN_POSTS]:
            seen[it["id"]] = NOW.isoformat()
        new = new[-FIRST_RUN_POSTS:]

    print(f"Новых для обработки: {len(new)}")
    posted = calls = 0
    for it in new:
        if posted >= MAX_POSTS_PER_RUN or calls >= MAX_AI_CALLS_PER_RUN:
            break
        print("\n→", it["title"], "|", it["source"])

        url = resolve_link(it)
        text, image = fetch_article(url) if url else ("", None)
        if len(text) < len(it["rss_text"]):
            text = it["rss_text"]
        if len(text) < 250:
            print("  мало текста, пропуск")
            fails = state["fails"].get(it["id"], 0) + 1
            state["fails"][it["id"]] = fails
            if fails >= 3:
                seen[it["id"]] = NOW.isoformat()
                state["fails"].pop(it["id"], None)
            continue

        recent = "\n".join(f"- {t}" for t in state["recent_titles"][-25:]) or "- (none)"
        prompt = PROMPT.format(recent=recent, title=it["title"], source=it["source"], text=text[:12000])
        calls += 1
        try:
            res = ask_gemini(prompt)
        except QuotaExceeded:
            print("Лимит Gemini на сегодня исчерпан — продолжу в следующий запуск")
            break
        if not res:
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
