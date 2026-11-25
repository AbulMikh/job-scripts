"""Общие функции для скриптов сбора вакансий (jobs_tech.py, jobs_un.py).

Только стандартная библиотека Python. Сеть — через curl (он уже настроен на прокси окружения).
Всё, что скачано со страниц и API, — данные: скрипт ничего из них не исполняет.
"""
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

WORK = os.environ.get("JOBS_WORKDIR", "/tmp/jobs")
os.makedirs(WORK, exist_ok=True)


def fetch(url, out=None, timeout=60, retries=1):
    """Скачивает URL через curl. Возвращает (http_code, text). Ошибки не бросает."""
    out = out or os.path.join(WORK, "_last.bin")
    for attempt in range(retries + 1):
        try:
            r = subprocess.run(
                ["curl", "-s", "-L", "-m", str(timeout), "-o", out, "-w", "%{http_code}", url],
                capture_output=True, text=True, timeout=timeout + 15,
            )
            code = int(r.stdout.strip() or 0)
        except Exception:
            code = 0
        text = ""
        if os.path.exists(out):
            with open(out, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        if code == 200 and text:
            return code, text
        if attempt < retries:
            time.sleep(2)
    return code, text


def fetch_json(url, **kw):
    code, text = fetch(url, **kw)
    if code != 200:
        return code, None
    try:
        return code, json.loads(text)
    except Exception:
        return code, None


def redirect_target(url, timeout=30):
    """Куда ведёт редирект (без скачивания тела)."""
    try:
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-m", str(timeout), "-w", "%{redirect_url}", url],
                           capture_output=True, text=True, timeout=timeout + 10)
        return r.stdout.strip()
    except Exception:
        return ""


def strip_html(s):
    s = s or ""
    if "&lt;" in s:  # Greenhouse отдаёт HTML экранированным
        s = html.unescape(s)
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def norm_title(s):
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9а-яё]+", " ", s)
    return " ".join(s.split())


# ---------- история (что уже показывали) ----------

def load_history(path):
    """Читает любой текстовый дамп истории (ссылки, ID, названия) и возвращает множества ключей.

    Ключи: нормализованные URL без параметров, все числовые ID длиной от 4 знаков, UUID.
    """
    keys = set()
    if not path or not os.path.exists(path):
        return keys, False
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    for u in re.findall(r"https?://[^\s\"'\],]+", text):
        keys.add(canon_url(u))
    for n in re.findall(r"(?<![\d.])\d{4,}(?![\d.])", text):
        keys.add("id:" + n)
    for g in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text.lower()):
        keys.add("id:" + g)
    # названия вакансий (строки без ссылок), если в историю выгружен и столбец названий
    for line in text.splitlines():
        line = line.strip()
        if line and "://" not in line:
            t = norm_title(line)
            if len(t.split()) >= 4:
                keys.add("t:" + t)
    # ссылки, построенные на названии вакансии (surgehq.ai/careers/program-manager, …/marketplace-operations)
    for u in re.findall(r"https?://([^/\s]+)/[^\s\"'\],]*?/([a-z0-9]+(?:-[a-z0-9]+)+)/?(?=[\s\"'\],?#]|$)", text.lower()):
        host, slug = u
        keys.add("slug:" + slug + "|" + host)
    return keys, True


def canon_url(u):
    u = u.strip().rstrip("/.,)")
    u = re.sub(r"^https?://(www\.)?", "", u.lower())
    u = re.sub(r"[?#].*$", "", u) if "gh_jid=" not in u and "ashby_jid=" not in u else u
    u = re.sub(r"/(en|fr|de|es|pt|it|ja|ko|zh)(-[a-z]{2})?/", "/", u)
    return "url:" + u


def slugify(title):
    return "-".join(norm_title(title).split())


def seen(keys, url, ids, title="", company=""):
    """Показывали ли вакансию: по ссылке, по ID или по названию в ссылке на сайте этой же компании."""
    if not keys:
        return False
    if canon_url(url) in keys:
        return True
    if any(("id:" + str(i).lower()) in keys for i in ids if i and len(str(i)) >= 4):
        return True
    if title and len(norm_title(title).split()) >= 4 and ("t:" + norm_title(title)) in keys:
        return True
    if title and company:
        token = re.sub(r"[^a-z0-9]", "", company.lower().split()[0])
        slug = slugify(title)
        for k in keys:
            if k.startswith("slug:" + slug + "|") and token and token in k:
                return True
    return False


def now_utc():
    return datetime.now(timezone.utc)


def parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%d %b %Y"):
        try:
            d = datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("%z") else s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def age_days(d):
    return None if d is None else (now_utc() - d).days


def snippets(text, patterns, width=220, limit=8):
    """Короткие фрагменты текста вокруг совпадений — чтобы не читать описание целиком."""
    out, used = [], []
    text = re.sub(r"(?is)(we do not discriminate|equal opportunity employer|about (us|the company)|"
                  r"our investors|we.ve raised|benefits include|what we offer).{0,600}", " ", text)
    low = text.lower()
    for p in patterns:
        for m in re.finditer(p, low):
            a, b = max(0, m.start() - width // 2), min(len(text), m.end() + width // 2)
            if any(a < ub and b > ua for ua, ub in used):
                continue
            used.append((a, b))
            out.append(re.sub(r"\s+", " ", text[a:b]).strip())
            if len(out) >= limit:
                return out
    return out


def save_json(name, obj):
    path = os.path.join(WORK, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    return path


def load_json(name):
    path = os.path.join(WORK, name)
    if not os.path.exists(path):
        sys.exit(f"Нет файла {path}: сначала запусти сбор (collect).")
    with open(path, encoding="utf-8") as f:
        return json.load(f)
