"""
Odi askıda yemek takip botu — Hacettepe çevresi.

GitHub Actions bu betiği gün içinde başlatır; betik açık kalıp
11:00–21:00 (Türkiye saati) arasında HER DAKİKA getodi.com'u kontrol eder.
Takip ettiğin restoranlarda "Askıdan al" aktif olunca Telegram'dan haber verir.
"Yok" durumunda mesaj ATMAZ. Yemeği senin yerine ALMAZ.

Telegram komutları (sadece senin chat id'nden gelenlere cevap verir):
  /kontrol  -> o anki durumu hemen yazar
  /yardim   -> komut listesi

Sadece Python standart kütüphanesi kullanır.

Secrets: ODI_EMAIL, ODI_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Variables (isteğe bağlı): RESTAURANTS (";" ile), CITY_ID (Ankara=6),
  START_HOUR / END_HOUR (varsayılan 11 / 21), INTERVAL_SECONDS (varsayılan 60)
"""

import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE = "https://getodi.com"
STATE_FILE = os.environ.get("STATE_FILE", ".state/last.json")
TZ = ZoneInfo("Europe/Istanbul")
DEFAULT_RESTAURANTS = "Sinyor Chef (Hacettepe);Pilavita City;Queen's Burger (Hacettepe BAM)"
UA = "Mozilla/5.0 (askida-takip-botu)"
MAX_RUN_MINUTES = 350  # GitHub bir işi en fazla 6 saat çalıştırır


def env(name, default=""):
    v = os.environ.get(name, "").strip()
    return v if v else default


def now_tr():
    return datetime.now(TZ)


# ---------- Sayfa çözümleme ----------

def parse_menus(page):
    """Ana menü listesindeki her restoran için (isim, askıda_var_mı) döndürür."""
    start = page.find('class="menu-list"')
    if start == -1:
        raise ValueError("Menü listesi bulunamadı (giriş başarısız olmuş olabilir).")
    results = []
    for box in re.split(r'<div class="menu-box"', page[start:])[1:]:
        m = re.search(r'class="menu-title">(.*?)</div>', box, re.S)
        if not m:
            continue
        name = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        available = False
        idx = box.find("Askıdan al")
        if idx != -1:
            ts = box.rfind('<div class="menu-btn-take', 0, idx)
            tag = box[ts: box.find(">", ts) + 1] if ts != -1 else ""
            available = bool(tag) and "disabled" not in tag
        results.append((name, available))
    return results


def remaining_rights(page):
    m = re.search(r"Kalan Odi hakk\S*\s*:?\s*(?:<[^>]+>\s*)*(\d+)", page)
    return int(m.group(1)) if m else None


# ---------- Odi oturumu (sadece bellekte tutulur) ----------

class Odi:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.addheaders = [("User-Agent", UA)]
        self.logged_in = False

    def _open(self, path, data=None):
        with self.opener.open(BASE + path, data=data, timeout=25) as r:
            return r.read().decode("utf-8", errors="replace")

    def login(self):
        email, pw = env("ODI_EMAIL"), env("ODI_PASSWORD")
        if not email or not pw:
            raise RuntimeError("ODI_EMAIL / ODI_PASSWORD ayarlı değil.")
        self._open("/sign-in/", urllib.parse.urlencode({"username": email, "password": pw}).encode())
        self.logged_in = True

    def student_page(self):
        if env("TEST_HTML_FILE"):
            return open(env("TEST_HTML_FILE"), encoding="utf-8").read()
        path = f"/student/?city={env('CITY_ID', '6')}"
        if self.logged_in:
            page = self._open(path)
            if 'class="menu-list"' in page:
                return page
        self.login()  # ilk sefer ya da oturum düştü
        page = self._open(path)
        if 'class="menu-list"' not in page:
            self.logged_in = False
            raise RuntimeError("Giriş yapılamadı — e-posta/şifreyi kontrol et.")
        return page


# ---------- Telegram ----------

def tg(method, params, timeout=20):
    token = env("TELEGRAM_TOKEN")
    if not token:
        return None
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}",
                                data=data, timeout=timeout + 10) as r:
        return json.loads(r.read().decode())


def send(text):
    if env("DRY_RUN") == "1":
        print("[DRY_RUN] Telegram:", text.replace("\n", " | "))
        return
    if not env("TELEGRAM_CHAT_ID"):
        print("UYARI: TELEGRAM_CHAT_ID yok. Mesaj:", text)
        return
    try:
        tg("sendMessage", {"chat_id": env("TELEGRAM_CHAT_ID"), "text": text})
    except Exception as e:
        print("Telegram gönderilemedi:", e)


def wait_for_commands(state, seconds):
    """`seconds` boyunca Telegram'ı dinler (long polling) — bekleme süresi de budur.
    /kontrol gelirse hemen True döner."""
    deadline = time.time() + seconds
    if env("DRY_RUN") == "1" or not env("TELEGRAM_TOKEN"):
        time.sleep(max(0, seconds))
        return False
    wanted = False
    while time.time() < deadline and not wanted:
        t = int(max(1, min(50, deadline - time.time())))
        try:
            res = tg("getUpdates", {"offset": state.get("tg_offset", 0), "timeout": t,
                                    "allowed_updates": '["message"]'}, timeout=t)
        except Exception as e:
            print("Telegram dinlenemedi:", e)
            time.sleep(min(10, max(0, deadline - time.time())))
            continue
        for u in (res or {}).get("result", []):
            state["tg_offset"] = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != env("TELEGRAM_CHAT_ID"):
                continue  # başkalarından gelen mesajları yok say
            text = (msg.get("text") or "").strip().lower()
            if text.startswith(("/kontrol", "/durum")):
                wanted = True
            elif text.startswith(("/yardim", "/yardım", "/start", "/help")):
                send("Komutlar:\n/kontrol — şu anki askıda durumunu yazar\n"
                     "Askıya yemek gelince ben otomatik haber veririm.")
    return wanted


# ---------- Durum ----------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    # Sadece hassas olmayan bilgiler saklanır (restoran durumları, Telegram sırası).
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    keep = {k: state.get(k) for k in ("available", "tg_offset")}
    with open(STATE_FILE, "w") as f:
        json.dump(keep, f)


def window_end():
    n = now_tr()
    start_h, end_h = int(env("START_HOUR", "11")), int(env("END_HOUR", "21"))
    if env("FORCE") == "1":
        return n + timedelta(minutes=int(env("FORCE_MINUTES", "10")))
    if not (start_h <= n.hour < end_h):
        return None
    end = n.replace(hour=end_h, minute=0, second=0, microsecond=0)
    return min(end, n + timedelta(minutes=MAX_RUN_MINUTES))


def check_once(odi, targets, state, reply=False):
    page = odi.student_page()
    menus = parse_menus(page)
    rights = remaining_rights(page)
    prev = state.get("available") or {}
    current, newly, lines = {}, [], []
    for target in targets:
        matches = [(n, a) for n, a in menus if target.lower() in n.lower()]
        if not matches:
            lines.append(f"❔ {target}: listede bulunamadı")
            continue
        avail = any(a for _, a in matches)
        current[target] = avail
        lines.append(f"{'✅' if avail else '❌'} {matches[0][0]}")
        if avail and not prev.get(target):
            newly.append(matches[0][0])
    state["available"] = current
    stamp = now_tr().strftime("%H:%M")
    print(f"{stamp}  " + "  ".join(lines))

    link = f"{BASE}/student/?city={env('CITY_ID', '6')}"
    hak = f"\nKalan Odi hakkın: {rights}" if rights is not None else ""
    if newly:
        send("🍽️ Askıda yemek var!\n" + "\n".join(f"• {n}" for n in newly)
             + f"\nSaat {stamp}{hak}\n{link}")
    elif reply:
        send(f"Durum ({stamp}):\n" + "\n".join(lines) + hak)


def main():
    end = window_end()
    if end is None:
        print("Çalışma saatleri dışında, çıkılıyor.")
        return 0

    targets = [t.strip() for t in env("RESTAURANTS", DEFAULT_RESTAURANTS).split(";") if t.strip()]
    interval = int(env("INTERVAL_SECONDS", "60"))
    max_loops = int(env("MAX_LOOPS", "0"))  # test için
    state = load_state()
    odi = Odi()
    error_notified = False
    reply, loops = False, 0
    print(f"Başladı, {end.strftime('%H:%M')}'e kadar her {interval} sn kontrol edecek.")

    while now_tr() < end:
        started = time.time()
        try:
            check_once(odi, targets, state, reply=reply)
            if error_notified:
                send("✅ Odi botu tekrar çalışıyor.")
            error_notified = False
        except Exception as e:
            print("Kontrol başarısız:", e)
            odi.logged_in = False
            if reply:
                send(f"⚠️ Şu an kontrol edemedim: {e}")
            elif not error_notified:
                send(f"⚠️ Odi botu kontrol yapamadı: {e}\nHer dakika tekrar deneyeceğim.")
                error_notified = True
        save_state(state)
        loops += 1
        if max_loops and loops >= max_loops:
            break
        remaining = min(interval - (time.time() - started), (end - now_tr()).total_seconds())
        reply = wait_for_commands(state, max(0, remaining))

    save_state(state)
    print("Bitti.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
