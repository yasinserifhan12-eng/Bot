"""
Hacettepe yemekhane günlük menü raporu.

GitHub Actions her sabah çalıştırır; beslenme.hacettepe.edu.tr'den günün
menüsünü okur ve Telegram'dan tek mesaj olarak gönderir.
Giriş gerektirmez, sadece herkese açık sayfayı okur.

Secrets: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (Odi botuyla aynı)
Variables (isteğe bağlı):
  MENU_LOCATION_ID : 1 = Beytepe (varsayılan), 2 = Sıhhiye
  MENU_MEALS       : Gösterilecek öğünler, varsayılan "ogle" (hepsi için "sabah,ogle,aksam,vegan")
"""

import html
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = "https://beslenme.hacettepe.edu.tr"
TZ = ZoneInfo("Europe/Istanbul")
UA = "Mozilla/5.0 (yemekhane-menu-botu)"
DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
MEALS = {
    "sabah": ("☀️", "Kahvaltı"),
    "ogle": ("🍲", "Öğle"),
    "aksam": ("🌙", "Akşam"),
    "vegan": ("🌱", "Vegan"),
}
CATEGORIES = {"ÇORBA": "Çorba", "ANAYEMEK": "Ana yemek", "YARDIMCI YEMEK": "Yardımcı",
              "TATLI": "Tatlı", "İÇECEK": "İçecek", "MEYVE": "Meyve", "SALATA": "Salata"}
LOCATIONS = {"1": "Beytepe", "2": "Sıhhiye"}


def env(name, default=""):
    v = os.environ.get(name, "").strip()
    return v if v else default


def text_of(fragment):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def attr(tag, name):
    m = re.search(rf'\b{name}="([^"]*)"', tag)
    return html.unescape(m.group(1)).strip() if m else ""


def parse_meal(page, meal_id):
    """Bir öğünün günlük görünümünü çözer. Menü yoksa None döner."""
    start = page.find(f'<section id="{meal_id}"')
    if start == -1:
        return None
    end = page.find("weekly-view", start)
    sec = page[start: end if end != -1 else len(page)]
    if "empty-state" in sec:
        return None

    m = re.search(r"Servis Saati</small>\s*<strong[^>]*>(.*?)</strong>", sec, re.S)
    hours = text_of(m.group(1)) if m else ""
    m = re.search(r"<strong>\s*(\d+)\s*</strong>\s*kcal", sec)
    total = int(m.group(1)) if m else 0

    items = []
    # Öğle / akşam / vegan: yemek kartları
    for card in sec.split('class="menu-card-wrapper"')[1:]:
        tag = re.search(r'<div class="menu-card"[^>]*>', card, re.S)
        if not tag:
            continue
        title = attr(tag.group(0), "data-title")
        if not title:
            continue
        g = re.search(r'class="garnitur-inline">.*?<span>(.*?)</span>', card, re.S)
        garnish = text_of(g.group(1)).strip("() ") if g else ""
        items.append({"title": title, "cal": attr(tag.group(0), "data-cal"),
                      "category": attr(tag.group(0), "data-category"), "garnish": garnish})
    # Kahvaltı: düz liste
    if not items:
        ul = re.search(r'class="kahvalti-items">(.*?)</ul>', sec, re.S)
        if ul:
            for li in re.findall(r"<li>(.*?)</li>", ul.group(1), re.S):
                name = text_of(li)
                if name:
                    items.append({"title": name, "cal": "", "category": "", "garnish": ""})
    if not items:
        return None
    return {"hours": hours, "total": total, "items": items}


def format_meal(meal_id, meal, same_as=None):
    icon, label = MEALS[meal_id]
    head = f"{icon} {label}"
    if meal["hours"]:
        head += f" ({meal['hours'].replace(' ', '')})"
    if meal["total"]:
        head += f" — {meal['total']} kcal"
    if same_as:
        return f"{head}\n{same_as} ile aynı"
    if all(not i["category"] for i in meal["items"]):  # kahvaltı
        return head + "\n" + ", ".join(i["title"] for i in meal["items"])
    lines = [head]
    for i in meal["items"]:
        cat = CATEGORIES.get(i["category"].upper(), i["category"].capitalize())
        line = f"• {cat}: {i['title']}" if cat else f"• {i['title']}"
        if i["cal"] and i["cal"] != "0":
            line += f" ({i['cal']} kcal)"
        if i["garnish"]:
            line += f"\n   + {i['garnish']}"
        lines.append(line)
    return "\n".join(lines)


def build_report(page, date, location_id):
    wanted = [m.strip() for m in env("MENU_MEALS", "ogle").split(",")
              if m.strip() in MEALS]
    title = (f"🍴 Hacettepe Yemekhane — {LOCATIONS.get(location_id, 'Yerleşke ' + location_id)}\n"
             f"{date.strftime('%d.%m.%Y')} {DAYS[date.weekday()]}")
    parsed = {m: parse_meal(page, m) for m in wanted}
    blocks = []
    for m in wanted:
        meal = parsed[m]
        if not meal:
            continue
        same = None
        if m == "aksam" and parsed.get("ogle") and \
                [i["title"] for i in meal["items"]] == [i["title"] for i in parsed["ogle"]["items"]]:
            same = "Öğle"
        blocks.append(format_meal(m, meal, same))
    url = f"{BASE}/?date={date.strftime('%Y-%m-%d')}&location_id={location_id}"
    if not blocks:
        if '<section id="ogle"' not in page:
            raise ValueError("Menü sayfası beklenen yapıda değil (site değişmiş olabilir).")
        return f"{title}\n\nBugün için menü yayınlanmamış.\n{url}"
    return title + "\n\n" + "\n\n".join(blocks) + f"\n\n{url}"


def fetch(date, location_id):
    if env("TEST_HTML_FILE"):
        return open(env("TEST_HTML_FILE"), encoding="utf-8").read()
    url = f"{BASE}/?date={date.strftime('%Y-%m-%d')}&location_id={location_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def send(text):
    if env("DRY_RUN") == "1":
        print(text)
        return
    token, chat = env("TELEGRAM_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("UYARI: Telegram ayarlı değil. Mesaj:\n" + text)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)


def wait_until_send_time():
    """Zamanlanmış çalıştırma erken başladıysa MENU_SEND_AT saatine (ör. 09:00) kadar bekler."""
    target = env("MENU_SEND_AT")
    if not target or env("DRY_RUN") == "1":
        return
    now = datetime.now(TZ)
    h, m = (int(x) for x in target.split(":"))
    at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    wait = (at - now).total_seconds()
    if 0 < wait <= 60 * 60:
        print(f"{target} bekleniyor ({int(wait)} sn)...")
        time.sleep(wait)


def main():
    wait_until_send_time()
    date = datetime.now(TZ)
    if env("MENU_DATE"):  # test için: YYYY-MM-DD
        date = datetime.strptime(env("MENU_DATE"), "%Y-%m-%d").replace(tzinfo=TZ)
    location_id = env("MENU_LOCATION_ID", "1")
    try:
        report = build_report(fetch(date, location_id), date, location_id)
    except Exception as e:
        print("Menü alınamadı:", e)
        send(f"⚠️ Yemekhane menüsünü alamadım: {e}\n{BASE}")
        return 1
    send(report)
    print("Gönderildi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
