# -*- coding: utf-8 -*-
"""Вычитка смысла языковой моделью по мастер-промпту (пять проходов, п. 3.5).

Модель получает мастер-промпт целиком и текст нескольких страниц, а возвращает
замечания в формате findings.json. Каждая цитата затем сверяется с текстом PDF:
выдуманные цитаты отбрасываются, неверные номера страниц исправляются (п. 0.13).
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .common import AUTHOR, TYPES, norm, yo

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_DIR = os.path.join(HERE, "prompts")

SERVICE_RULES = """
## РЕЖИМ РАБОТЫ В ВЕБ-СЕРВИСЕ (дополнение к мастер-промпту)
Ты работаешь внутри сервиса, который сам выполняет шаги 3.1–3.4, сборку DOCX и машинную проверку.
Тебе передают текст нескольких страниц PDF (номера страниц физические) и списки кандидатов машинных проверок.
Твоя задача — проходы 1, 4 и 5 (текст, термины и единообразие, данные и ссылки) и смысловая часть прохода 2.
Типографику, которую уже нашли правила (дефисы в диапазонах, двойные пробелы, знак умножения, висячие
предлоги, смешение алфавитов), повторно не выписывай.
Верни ТОЛЬКО JSON без пояснений вокруг, строго такого вида:
{"findings": [{"page": 4, "clause": "п. 4.2", "type": "Орфография", "src": "дословная цитата 3–15 слов",
  "bad": "ошибочный фрагмент из src", "fix": "та же цитата с исправлением", "good": "исправленный фрагмент из fix",
  "note": "правило или суть расхождения"}]}
Требования: src — дословно из текста указанной страницы; bad входит в src; good входит в fix; тип — одно из
8 названий раздела 2; для «Требует проверки автором» fix и good — «—». Буква «ё» не используется: пиши «е».
Если ошибок нет — {"findings": []}.
"""


def master_prompt():
    files = sorted((f for f in os.listdir(PROMPT_DIR) if f.endswith(".md") and f != "custom_prompt.md"),
                   key=lambda f: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", f)])   # v5.10 новее v5.7
    from .. import core
    custom = core.CUSTOM_PROMPT
    path = custom if os.path.exists(custom) else os.path.join(PROMPT_DIR, files[-1])
    return open(path, encoding="utf-8").read()


def page_text(p):
    lines = [ln["text"] for ln in p["lines"]]
    head = " / ".join(p.get("head", []))
    foot = " / ".join(p.get("foot", []))
    out = []
    if head:
        out.append(f"[верхний колонтитул] {head}")
    out += lines
    if foot:
        out.append(f"[нижний колонтитул] {foot}")
    return "\n".join(out)


def call(st, system, user, log):
    prov = st.get("ai_provider")
    key = st.get("ai_api_key") or os.environ.get("KORREKTOR_AI_KEY", "")
    base = (st.get("ai_base_url") or "").rstrip("/")
    model = st.get("ai_model")
    temp = float(st.get("ai_temperature") or 0)
    mt = int(st.get("ai_max_tokens") or 8000)
    timeout = int(st.get("ai_timeout") or 240)
    import httpx
    if os.environ.get("KORREKTOR_AI_MOCK"):
        return mock_answer(user)
    if prov == "anthropic":
        r = httpx.post((base or "https://api.anthropic.com") + "/v1/messages", timeout=timeout,
                       headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                       json={"model": model, "max_tokens": mt, "temperature": temp,
                             "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                             "messages": [{"role": "user", "content": user}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []))
    if prov == "openai":
        r = httpx.post((base or "https://api.openai.com/v1") + "/chat/completions", timeout=timeout,
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": model, "temperature": temp, "max_tokens": mt,
                             "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    if prov == "ollama":
        r = httpx.post((base or "http://127.0.0.1:11434") + "/api/chat", timeout=timeout,
                       json={"model": model, "stream": False, "format": "json",
                             "options": {"temperature": temp, "num_predict": mt, "num_ctx": 32768},
                             "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["message"]["content"]
    raise RuntimeError("провайдер модели не настроен")


def mock_answer(user):
    """Тестовый ответ (KORREKTOR_AI_MOCK=1): берет первую строку страницы и ничего не находит."""
    return json.dumps({"findings": []}, ensure_ascii=False)


def parse(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        return []
    try:
        data = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return []
    return data.get("findings", []) if isinstance(data, dict) else []


def validate(items, idx, st, log):
    out = []
    mode = st.get("ai_quote_check", "fix_page")
    for it in items:
        try:
            f = {k: yo(str(it.get(k, "")).strip()) for k in ("clause", "type", "src", "bad", "fix", "good", "note")}
            f["page"] = int(it.get("page"))
        except Exception:  # noqa
            continue
        if not f["src"] or f["type"] not in TYPES:
            continue
        if not idx.has(f["page"], f["src"]):
            others = idx.find_pages(f["src"])
            if mode == "fix_page" and others:
                f["page"] = min(others, key=lambda p: abs(p - f["page"]))
            elif mode == "flag":
                f["type"], f["fix"], f["good"] = AUTHOR, "—", "—"
                f["note"] = "Цитата модели не найдена дословно в тексте PDF. " + f["note"]
                if not others:
                    continue
                f["page"] = others[0]
            else:
                log(f"ИИ: цитата не найдена в PDF, замечание отброшено: «{f['src'][:60]}»")
                continue
        if f["type"] == AUTHOR:
            f["fix"], f["good"] = "—", "—"
            if f["bad"] not in f["src"]:
                f["bad"] = "—"
        else:
            if not f["fix"] or f["fix"] in {"—", "-"} or norm(f["fix"]) == norm(f["src"]):
                f["type"], f["fix"], f["good"] = AUTHOR, "—", "—"
            else:
                if f["bad"] not in f["src"]:
                    f["bad"] = f["src"]
                if f["good"] not in f["fix"]:
                    f["good"] = f["fix"]
        f["clause"] = f["clause"] or "текст"
        f["rule"] = "ai"
        out.append(f)
    return out


def run(dump, idx, st, extra, log, progress=None):
    if st.get("ai_provider") in (None, "", "none") and not os.environ.get("KORREKTOR_AI_MOCK"):
        return []
    system = master_prompt() + "\n" + SERVICE_RULES
    if extra.strip():
        system += "\n## ДОПОЛНИТЕЛЬНЫЕ УКАЗАНИЯ\n" + extra
    pages = dump["lines"][: int(st.get("ai_max_pages") or 300)]
    n = max(1, int(st.get("ai_pages_per_request") or 6))
    chunks = [pages[i:i + n] for i in range(0, len(pages), n)]
    done = [0]

    def work(chunk):
        body = "\n\n".join(f"=== Страница {p['pdf_page']} ===\n{page_text(p)}" for p in chunk
                           if p["lines"] or p.get("head") or p.get("foot"))
        if not body.strip():
            return []
        user = ("Вычитай страницы ниже по мастер-промпту и верни JSON с замечаниями.\n\n" + body)
        for attempt in range(int(st.get("ai_retries") or 0) + 1):
            try:
                ans = call(st, system, user, log)
                items = parse(ans)
                break
            except Exception as e:  # noqa
                log(f"ИИ: ошибка запроса (стр. {chunk[0]['pdf_page']}–{chunk[-1]['pdf_page']}): {e}")
                time.sleep(3 * (attempt + 1))
                items = []
        done[0] += 1
        if progress:
            progress(done[0] / len(chunks))
        return validate(items, idx, st, log)

    with ThreadPoolExecutor(max_workers=max(1, int(st.get("ai_parallel") or 1))) as ex:
        res = list(ex.map(work, chunks))
    out = [f for r in res for f in r]
    log(f"ИИ: получено замечаний после проверки цитат: {len(out)}")
    return out
