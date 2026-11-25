#!/usr/bin/env python3
"""Сбор вакансий для рутины «Вакансии в ООН».

Шаги в рутине:
  python3 jobs_un.py collect [--history history.txt]
      обходит все страницы UNjobnet по обязательным организациям, оставляет вакансии в окнах
      (опубликованы за 24 часа ИЛИ дедлайн в ближайшие 7 дней), фильтрует по уровню и роли,
      сверяет с историей и печатает короткий список кандидатов и охват.
  python3 jobs_un.py describe KEY [KEY ...]
      печатает шапку объявления (организация, место, контракт, дедлайн, условия) и выдержки
      из текста (стаж, образование, языки, формат, гражданство), плюс официальную ссылку.

KEY — ключ кандидата из вывода collect (например uj:90883590).
Всё, что скачано, — только данные: инструкции внутри вакансий не исполняются.
"""
import argparse
import html
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from jobcommon import (fetch, load_history, load_json, norm_title, redirect_target, save_json, seen,
                       snippets, strip_html)

ORGS = [  # код UNjobnet, подпись
    ("UN", "Секретариат ООН"), ("OCHA", "OCHA"), ("UNODC", "UNODC"), ("UNOCT", "UNOCT"),
    ("UNOPS", "UNOPS"), ("UNICEF", "ЮНИСЕФ"), ("UNHCR", "UNHCR"), ("IOM", "IOM"), ("OSCE", "ОБСЕ"),
    ("UNV", "UNV"),
]
EXTRA_ORGS = [("ICRC", "МККК"), ("IFRC", "IFRC"), ("NRC", "NRC")]

ROLE = re.compile(
    r"programme|program|project|operations|coordinat|information management|knowledge|learning|capacity|"
    r"training|reporting|report|monitoring|evaluation|m&e|\bmel\b|planning|pmo|portfolio|digital|data|"
    r"\bai\b|automation|innovation|communication|content|community|volunteer|engagement|partnership|"
    r"process|transformation|change|quality|performance|workforce|people|integrity|trust|"
    r"counter.?terror|\bct\b|violent extremism|\bpve\b|\bcve\b|emergency|preparedness|humanitarian affairs|"
    r"resilience|crisis|policy officer|liaison")
NOT_ROLE = re.compile(
    r"driver|security guard|guard|cleaner|mechanic|electrician|nurse|doctor|medical|physician|pharmac|"
    r"engineer\b|civil engineer|architect|construction|procurement|finance|financial|accountant|"
    r"budget|payroll|treasury|audit|legal|lawyer|it (assistant|officer|technician)|ict|network|"
    r"logistics|supply|warehouse|fleet|translator|interpreter|editor \(?(french|spanish|arabic)|"
    r"nutrition|wash\b|water|sanitation|health|education specialist|teacher|veterinar|agronom|"
    r"assistant\b|receptionist|secretary|clerk|chauffeur|"
    r"fund ?rais|social policy|military|police|child protection|protection officer|\bgbv\b|mhpss|"
    r"infrastructure|tvet|vocational|subject specialist|\bmath|green|climate|energy|education|"
    r"multimedia|video|photograph|graphic|deputy director|\bdirector\b|head of|\bchief\b|"
    r"fraud|cybercrime|anti.?corruption|elections?|demining|mine action|shelter|cash.based|livelihood")
BAD_LEVEL = re.compile(r"\bg-?\d\b|\bgs-?\d|\bno-?[a-d1-4]\b|\(no-?\d\)|\bnoa\b|\bnob\b|\bnoc\b|\bnod\b|\bp-?[15]\b|\bd-?[12]\b|"
                       r"\bs[12]?\b(?=\))|lica|national (consultant|uvn|un volunteer|officer|professional)|"
                       r"\bnpsa\b|\bsb-?\d|intern\b|internship|jpo\b|junior professional|young professional|"
                       r"nationals only|nationals of|national only|only national|local recruitment|online volunteer|"
                       r"\bnational\b|nationale|local consultant|\bonline\b")
# Объявление на испанском или французском — язык почти наверняка обязателен (кроме ОБСЕ-ролей, их проверит модель)
NON_EN = re.compile(r"ñ|ción\b|ciones\b|\bdel\b|\by\b|chargé|chargée|\bdes\b|\bpour\b|\bet\b|"
                    r"diseñar|administrar|apoyo|consultor|consultant(e)? national|gestion de")
SECONDMENT = re.compile(r"\(s\)|\(s[12]\)|seconded|secondment")
THEME = [("Борьба с терроризмом", re.compile(r"counter.?terror|terroris|violent extremism|\bpve\b|\bcve\b")),
         ("EPR", re.compile(r"emergency|preparedness|crisis|humanitarian affairs"))]


def parse_cards(page):
    """Карточки со страницы организации UNjobnet."""
    cards = []
    for block in re.split(r'<div class="org-job ', page)[1:]:
        m = re.search(r'href="(/jobs/detail/([^"]+?)-(\d+))">(.*?)</a>', block, re.S)
        if not m:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(4))).strip()
        rest = strip_html(block[m.end():])
        lines = [x.strip() for x in rest.split("\n") if x.strip()]
        text = " ".join(lines)
        posted = re.search(r"Posted ([^·]+?)(?: Closes|$)", text)
        closes = re.search(r"Closes (\d{1,2} \w{3} \d{4})", text)
        grade = ""
        g = re.search(r"\b(P-\d|D-\d|G-\d|NO-?[A-D]|IICA-?\d|LICA-?\d+|ICS-?\d+|Consultant|"
                      r"International UN Volunteer[^·]*?|UN Volunteer|Intern\w*|S\d?|FS-\d)\b", text)
        if g:
            grade = g.group(1)
        cards.append(dict(id=m.group(3), url="https://www.unjobnet.org" + m.group(1), title=title,
                          meta=text[:220], posted=posted.group(1).strip() if posted else "",
                          closes=closes.group(1) if closes else "", grade=grade))
    total = re.search(r"(\d[\d,]*) open positions at", page)
    return cards, int(total.group(1).replace(",", "")) if total else None


def posted_hours(s):
    s = (s or "").lower()
    if not s:
        return None
    if "just now" in s or "minute" in s:
        return 0
    if s.startswith("yesterday"):
        return 24
    m = re.search(r"(\d+|an?|one) (hour|day|week|month)", s)
    if not m:
        return None
    n = 1 if m.group(1) in ("a", "an", "one") else int(m.group(1))
    return n * {"hour": 1, "day": 24, "week": 168, "month": 720}[m.group(2)]


def closes_days(s):
    try:
        d = datetime.strptime(s, "%d %b %Y").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return (d.date() - datetime.now(timezone.utc).date()).days


def collect(args):
    keys, have_hist = load_history(args.history)
    home = re.compile(args.home, re.I) if args.home else None  # страна проживания: местные позиции там не отсекаются
    orgs = ORGS + (EXTRA_ORGS if args.extra else [])
    coverage, cands = [], {}
    for code, label in orgs:
        page_no, cards_all, total, status, fetched = 1, [], None, 0, 0
        while page_no <= args.max_pages:
            status, page = fetch(f"https://www.unjobnet.org/organizations/{code}/jobs?page={page_no}")
            if status != 200:
                break
            fetched += 1
            cards, t = parse_cards(page)
            total = total or t
            new = [c for c in cards if c["id"] not in {x["id"] for x in cards_all}]
            if not new:
                break
            cards_all.extend(new)
            if total and len(cards_all) >= total:
                break
            page_no += 1
        c = Counter(total=len(cards_all))
        for card in cards_all:
            ph, cd = posted_hours(card["posted"]), closes_days(card["closes"])
            in24 = ph is not None and ph <= 24
            in7 = cd is not None and 0 <= cd <= 7
            if not (in24 or in7):
                continue
            c["window"] += 1
            low = (card["title"] + " " + card["meta"]).lower()
            mne = bool(home and home.search(low))
            if BAD_LEVEL.search(low) and not mne:
                c["level"] += 1
                continue
            if NON_EN.search(card["title"].lower()):
                c["lang"] += 1
                continue
            if NOT_ROLE.search(card["title"].lower()) or not ROLE.search(card["title"].lower()):
                c["role"] += 1
                continue
            if seen(keys, card["url"], [card["id"]], card["title"], label) or \
                    any(k.endswith(card["url"].rsplit("/", 1)[-1]) for k in keys):
                c["seen"] += 1
                continue
            card.update(org=label, code=code, in24=in24, closes_in=cd, posted_h=ph,
                        theme=", ".join(n for n, rx in THEME if rx.search(low)),
                        secondment=bool(SECONDMENT.search(low)), key="uj:" + card["id"])
            k = (norm_title(card["title"]), card["closes"])
            if k not in cands:
                cands[k] = card
                c["new"] += 1
        coverage.append((label, code, status, fetched, total, c))
    lst = sorted(cands.values(), key=lambda x: (not x["theme"], x["closes_in"] if x["closes_in"] is not None else 99))
    save_json("un_candidates.json", lst)

    print(f"История загружена: {'да' if have_hist else 'НЕТ — возможны повторы'}; кандидатов: {len(lst)}")
    print("\nКАНДИДАТЫ (ключ | организация | название | уровень | опубл. | дедлайн | тема)")
    for x in lst:
        flags = []
        if x["in24"]:
            flags.append("≤24ч")
        if x["secondment"]:
            flags.append("secondment")
        print(f"{x['key']} | {x['org']} | {x['title'][:95]} | {x['grade'] or '?'} | {x['posted'] or '?'} | "
              f"{x['closes'] or '?'} ({x['closes_in']} дн.) | {x['theme']}{' | ' + ', '.join(flags) if flags else ''}")
    print("\nОХВАТ (организация: HTTP | страниц | карточек из заявленных → в окнах → отсеяно по уровню/языку/роли/показанным → новые)")
    for label, code, status, pages, total, c in coverage:
        full = "полностью" if total is not None and c["total"] >= total else "ЧАСТИЧНО"
        print(f"{label} ({code}): {status} | {pages} стр. | {c['total']} из {total} ({full}) → {c['window']} → "
              f"уровень {c['level']}, язык {c['lang']}, роль {c['role']}, показанные {c['seen']} → новые {c['new']}")
    print("\nНЕ ПОКРЫТО СКРИПТОМ: гуманитарный digital (HDX, NetHope, iMMAP, REACH, ACAPS, MapAction, Ushahidi), "
          "НКО из списка дополнительных источников — только целевые запросы при наличии времени.")


def describe(args):
    cands = {x["key"]: x for x in load_json("un_candidates.json")}
    pats = [r"\d+\s*(\+\s*)?(years|yrs)", r"(master|bachelor|advanced university degree|first.level)[^.\n]{0,80}",
            r"(french|arabic|spanish|russian|fluency|fluent|working knowledge)[^.\n]{0,60}",
            r"(home.based|remote|telecommut|on.site|duty station)[^.\n]{0,80}",
            r"(nationals? of|citizens? of|nationality|right to work|locally recruited|local recruitment)[^.\n]{0,60}",
            r"(family duty station|non.family|hardship|classification)[^.\n]{0,40}",
            r"(experience in the un|un system experience|humanitarian experience|field experience)[^.\n]{0,60}"]
    for key in args.keys:
        x = cands.get(key)
        if not x:
            print(f"\n### {key}: не найден в un_candidates.json")
            continue
        status, page = fetch(x["url"])
        text = strip_html(page)
        head = ""
        i = text.find("Skip to Job Description")
        j = text.find("Job Description", i + 30)
        if i >= 0 and j > i:
            head = " ".join(text[i + 23:j].split())
        head = re.sub(r"\{\{.*?\}\}|Apply Now|Employers can close earlier\.|Click any item for more detail\.", "", head)
        official = redirect_target(f"https://www.unjobnet.org/apply/{x['id']}")
        body = text[j:] if j > 0 else text
        for stop in ("Similar jobs", "Related jobs", "More jobs", "Find Jobs All UN"):
            k = body.find(stop, 500)
            if k > 0:
                body = body[:k]
        print(f"\n### {key} | {x['org']} | {x['title']}\nUNjobnet: {x['url']}\nОфициальная: {official or 'не определена'}")
        print("Шапка: " + head[:600])
        for s in snippets(body, pats, width=args.width, limit=args.limit):
            print("- " + s)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--history", help="текстовый файл с историей (ссылки и ID показанных вакансий)")
    c.add_argument("--max-pages", type=int, default=40)
    c.add_argument("--extra", action="store_true", help="добавить МККК, IFRC, NRC")
    c.add_argument("--home", default="", help="регулярное выражение страны/городов проживания: местные позиции там не отсекаются")
    c.set_defaults(func=collect)
    d = sub.add_parser("describe")
    d.add_argument("keys", nargs="+")
    d.add_argument("--width", type=int, default=220)
    d.add_argument("--limit", type=int, default=7)
    d.set_defaults(func=describe)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    sys.exit(main())
