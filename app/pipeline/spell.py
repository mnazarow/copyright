# -*- coding: utf-8 -*-
"""Орфография: словарь Hunspell (офлайн) и LanguageTool (по желанию)."""
import re
import shutil
import subprocess
from collections import Counter

from .autofix import hit
from .common import AUTHOR

WORD = re.compile(r"(?<![\w-])([А-ЯЁа-яё][а-яё]{2,}(?:-[а-яё]{2,})?)(?![\w-])")


def hunspell_available():
    return shutil.which("hunspell") is not None


def _candidates(idx):
    """Слова, целиком лежащие в одной строке (без переносов), с адресами."""
    occ = {}
    freq = Counter()
    for page, p in idx.pages.items():
        lines = p["lines"]
        for i, ln in enumerate(lines):
            text = ln["text"]
            prev_hyph = i > 0 and lines[i - 1]["text"].rstrip().endswith("-")
            words = list(WORD.finditer(text))
            for k, m in enumerate(words):
                w = m.group(1)
                if k == 0 and m.start() <= 3 and w[0].islower() and (prev_hyph or i == 0):
                    continue                      # хвост слова после переноса (в т. ч. из другой колонки)
                if m.end() >= len(text.rstrip()) - 1 and text.rstrip().endswith("-"):
                    continue                      # начало слова перед переносом
                if w[0].isupper() and text[:m.start()].strip():
                    continue                      # имя собственное внутри строки (улица, город, марка)
                if text[m.end():m.end() + 1] == "." and len(w) <= 7:
                    continue                      # сокращение: «отопл.», «нерж.»
                freq[w.lower()] += 1
                occ.setdefault(w, []).append((page, text))
    return occ, freq


def hunspell(idx, st, log=print):
    if not st.get("hunspell_enabled") or not hunspell_available():
        if st.get("hunspell_enabled"):
            log("Hunspell не установлен — словарная проверка пропущена")
        return []
    occ, freq = _candidates(idx)
    wl = {w.lower() for w in re.split(r"[\s,;]+", st.get("whitelist") or "") if w}
    stems = [w for w in wl if len(w) >= 5]
    words = [w for w in occ if w.lower() not in wl and w.lower().replace("ё", "е") not in wl
             and not any(w.lower().startswith(s) for s in stems)]
    if not words:
        return []
    d = st.get("hunspell_dict") or "ru_RU"
    try:
        r = subprocess.run(["hunspell", "-i", "utf-8", "-d", d, "-a"], input="\n".join(words), capture_output=True,
                           text=True, encoding="utf-8", timeout=120)
    except Exception as e:  # noqa
        log(f"Hunspell: ошибка запуска: {e}")
        return []
    if r.returncode != 0 or (not r.stdout.strip() and r.stderr.strip()):
        log(f"Hunspell: словарь «{d}» недоступен или ошибка: {(r.stderr or '').strip()[:200]}")
        return []
    bad = {}
    for line in r.stdout.splitlines():
        if line.startswith("&"):
            head, _, sug = line.partition(":")
            w = head.split()[1]
            bad[w] = [s.strip() for s in sug.split(",") if s.strip()][:4]
        elif line.startswith("#"):
            bad[line.split()[1]] = []
    maxf = int(st.get("hunspell_max_freq", 2) or 0)
    H = []
    for w, sug in bad.items():
        if maxf and freq[w.lower()] > maxf:
            continue
        if not sug:
            continue                      # совсем неизвестное слово без вариантов — скорее термин
        for page, text in occ.get(w, [])[:3]:
            if st.get("hunspell_action") == "fix":
                good = sug[0]
                H.append(hit("spell", page, text, w, good, "Орфография",
                             f"Слова «{w}» нет в словаре; исправлено по первому варианту словаря Hunspell "
                             f"(другие варианты: {', '.join(sug[1:]) or 'нет'}).", group=("spell", w)))
            else:
                H.append(hit("spell", page, text, w, None, AUTHOR,
                             f"Слова «{w}» нет в словаре русского языка. Возможные варианты: {', '.join(sug)}. "
                             "Если это термин или марка, его можно добавить в белый список.",
                             group=("spell", w, page)))
    return H


def languagetool(idx, st, log=print, should_stop=None):
    if not st.get("lt_enabled"):
        return []
    import time
    import httpx
    from .common import Cancelled
    url = (st.get("lt_url") or "").rstrip("/") + "/v2/check"
    disabled = ",".join((st.get("lt_disabled_rules") or "").split())
    level = "picky" if st.get("lt_picky") else "default"
    H = []
    total, t0 = len(idx.pages), time.time()
    last = t0
    with httpx.Client(timeout=30) as client:             # одно соединение на весь документ
        for n, (page, p) in enumerate(idx.pages.items(), 1):
            if should_stop and should_stop():
                raise Cancelled()
            if time.time() - last >= 30:
                last = time.time()
                log(f"  … LanguageTool: проверено {n - 1} из {total} страниц, находок {len(H)}, {int(last - t0)} с")
            for ln in p["lines"]:
                text = ln["text"].strip()
                if len(text) < 25 or not re.search(r"[а-яё]{3,}", text):
                    continue
                try:
                    r = client.post(url, data={"text": text, "language": "ru-RU", "disabledRules": disabled,
                                               "level": level})
                    r.raise_for_status()
                    matches = r.json().get("matches", [])
                except Exception as e:  # noqa
                    log(f"⚠ LanguageTool недоступен ({e.__class__.__name__}: {str(e)[:200]}); грамматическая проверка "
                        f"остановлена на стр. {page} — остальные проверки продолжаются")
                    return H
                for m in matches:
                    bad = text[m["offset"]:m["offset"] + m["length"]]
                    rep = [x["value"] for x in m.get("replacements", [])][:3]
                    rid = m.get("rule", {}).get("id", "")
                    cat = m.get("rule", {}).get("category", {}).get("id", "")
                    if not bad.strip() or rid == "MORFOLOGIK_RULE_RU_RU":
                        continue                      # орфографию делает Hunspell
                    type_ = {"PUNCTUATION": "Пунктуация", "GRAMMAR": "Грамматика", "STYLE": "Стилистика",
                             "TYPOGRAPHY": "Типографика", "CASING": "Орфография"}.get(cat, "Грамматика")
                    note = f"{m.get('message', '')} (LanguageTool, правило {rid})."
                    if rep:
                        H.append(hit("lt", page, text, bad, rep[0], type_, note, group=("lt", page, text, bad)))
                    else:
                        H.append(hit("lt", page, text, bad, None, AUTHOR, note, group=("lt", page, text, bad)))
    return H
