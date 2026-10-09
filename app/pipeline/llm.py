# -*- coding: utf-8 -*-
"""Вычитка смысла языковой моделью по мастер-промпту (пять проходов, п. 3.5).

Модель получает мастер-промпт целиком и текст нескольких страниц, а возвращает замечания в формате findings.json.
Каждая цитата затем сверяется с текстом PDF: выдуманные цитаты отбрасываются, неверные номера страниц исправляются
(п. 0.13).

Надежность:
  * ответ принимается потоком (OpenAI-совместимые API, Anthropic, Ollama): видно, что модель работает, и можно
    прервать запрос, если она «замолчала»;
  * три предела времени: ожидание начала ответа (загрузка модели и чтение промпта), простой (данные перестали
    приходить) и весь запрос. Прерванный запрос разрывает соединение — сервер модели сразу прекращает работу над ним
    и не задерживает следующий запрос;
  * сетевые ошибки, перегрузка (429, 5xx), тайм-ауты — повтор с нарастающей паузой (учитывается Retry-After);
  * ответ обрезан лимитом токенов или текст не помещается в контекст — часть делится пополам и отправляется снова;
  * неверный ключ, кончились средства, модель не найдена, неверный адрес — остальные части не отправляются:
    ошибка не исчезнет при повторе, а ждать ее на каждой части бессмысленно;
  * сервис не принимает параметр (max_tokens, temperature, поток) — запрос перестраивается и отправляется сразу;
  * Ollama: модель и ее предел контекста проверяются до отправки частей, размер контекста подбирается под
    мастер-промпт один раз на весь документ (смена размера заставила бы Ollama перезагружать модель), запросы идут
    по одному (параллельные ждали бы в очереди Ollama), загрузка модели в память видна в журнале;
  * ответ не в формате JSON — попытка восстановить, иначе повтор;
  * общий лимит времени на весь проход ИИ и остановка задания пользователем;
  * подробный журнал: каждый запрос, ожидание (раз в N секунд), время, объем, токены, причины отказа.
Ошибка ИИ никогда не роняет задание: оно завершается с результатами правил, а в журнале, на странице задания
и в шапке отчета указано, какие страницы модель не проверила и почему.
"""
import json
import logging
import os
import random
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .common import AUTHOR, TYPES, Cancelled, norm, plural, yo

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_DIR = os.path.join(HERE, "prompts")
_log = logging.getLogger("korrektor")

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

BACKOFF = 3.0          # первая пауза перед повтором, с (дальше вдвое больше, не более минуты)

# ошибки, которые не исчезнут при повторе: остальные части документа не отправляются
FATAL = ("auth", "quota", "notfound", "config")
KIND_NAMES = {"network": "нет связи", "timeout": "тайм-аут", "auth": "доступ", "quota": "средства или квота",
              "notfound": "модель или адрес не найдены", "config": "настройки", "ratelimit": "перегрузка",
              "server": "сбой сервиса", "badrequest": "запрос отклонен", "context": "не помещается в контекст",
              "maxtokens": "лимит ответа", "memory": "память сервера модели", "format": "формат ответа",
              "cancelled": "остановлено", "other": "ошибка"}


class AIError(Exception):
    """Ошибка запроса к модели. kind — см. KIND_NAMES; retry — имеет ли смысл повторить; wait — пауза перед повтором
    (Retry-After); limit — допустимый максимум токенов ответа, если сервис его назвал."""

    def __init__(self, message, kind="other", retry=False, wait=None, status=None, limit=None):
        super().__init__(message)
        self.kind, self.retry, self.wait, self.status, self.limit = kind, retry, wait, status, limit


def tokens(text):
    """Грубая оценка числа токенов: для русского текста ≈ 1 токен на 3 знака (точное число знает только модель)."""
    return len(text or "") // 3 + 1


def n_(x):
    return f"{int(x):,}".replace(",", " ")


def s_(sec):
    return f"{float(sec):.1f}".replace(".", ",")


def zn(n):
    """«261 знак», «1 042 знака», «90 099 знаков»."""
    return f"{n_(n)} {plural(n, 'знак', 'знака', 'знаков')}"


def tk(n):
    """«1 токен», «30 122 токена», «32 768 токенов»."""
    return f"{n_(n)} {plural(n, 'токен', 'токена', 'токенов')}"


def sec_(v):
    """Секунды из настройки: «240», «2,5»."""
    v = float(v)
    return str(int(v)) if v == int(v) else s_(v)


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


def pages_body(pages):
    return "\n\n".join(f"=== Страница {p['pdf_page']} ===\n{page_text(p)}" for p in pages
                       if p["lines"] or p.get("head") or p.get("foot"))


USER_HEAD = "Вычитай страницы ниже по мастер-промпту и верни JSON с замечаниями.\n\n"


# ------------------------------------------------------------------ адреса и параметры запроса
def base_url(st):
    prov = st.get("ai_provider")
    b = (st.get("ai_base_url") or "").strip().rstrip("/")
    if prov == "anthropic":
        return re.sub(r"/v1(/messages)?$", "", b) or "https://api.anthropic.com"
    if prov == "openai":
        return re.sub(r"/chat/completions$", "", b) or "https://api.openai.com/v1"
    if prov == "ollama":
        return re.sub(r"/(api(/chat)?|v1)$", "", b) or "http://127.0.0.1:11434"
    return ""


def endpoint(st):
    prov, b = st.get("ai_provider"), base_url(st)
    if not b:
        return ""
    return b + {"anthropic": "/v1/messages", "openai": "/chat/completions", "ollama": "/api/chat"}.get(prov, "")


def context_need(st, system, user):
    """Сколько токенов нужно на запрос: промпт, текст страниц, ответ и запас. Для размера контекста оценка
    осторожнее обычной (2,5 знака на токен): у моделей с небольшим словарем русский текст дробится мельче,
    а нехватка контекста незаметно обрезает мастер-промпт."""
    hi = (len(system or "") + len(user or "")) * 2 // 5 + 2
    return hi + int(st.get("ai_max_tokens") or 8000) + 1024


def pick_context(st, want, model_max=None):
    """Контекст Ollama (num_ctx): из настройки или «авто» — ближайшая степень двойки, в которую помещаются промпт,
    текст страниц и ответ, но не больше предела модели (без этого Ollama молча обрезает длинный мастер-промпт)."""
    fixed = int(st.get("ai_context") or 0)
    if fixed:
        return fixed
    n = 8192
    while n < want and n < 262144:
        n *= 2
    if model_max and n > int(model_max):
        n = int(model_max)
    return n


def context_size(st, system, user, model_max=None):
    want = context_need(st, system, user)
    return pick_context(st, want, model_max), want


def _keep_alive(v):
    v = str(v or "30m").strip()
    return int(v) if re.fullmatch(r"-?\d+", v) else v      # «-1» — число (строку без единиц Ollama не примет)


def _payload(st, system, user, ctx=None):
    ctx = ctx if ctx is not None else {}
    compat = ctx.setdefault("compat", set())
    prov = st.get("ai_provider")
    key = (st.get("ai_api_key") or os.environ.get("KORREKTOR_AI_KEY", "")).strip()
    model = (st.get("ai_model") or "").strip()
    temp = float(st.get("ai_temperature") or 0)
    mt = int(st.get("ai_max_tokens") or 8000)
    if ctx.get("max_tokens"):
        mt = min(mt, int(ctx["max_tokens"]))
    stream = "no_stream" not in compat
    if prov == "anthropic":
        if not key:
            raise AIError("не задан «Ключ API» Anthropic (Настройки → «Искусственный интеллект»)", "config")
        sysblock = {"type": "text", "text": system}
        if "no_cache" not in compat:
            sysblock["cache_control"] = {"type": "ephemeral"}
        body = {"model": model, "max_tokens": mt, "stream": stream, "system": [sysblock],
                "messages": [{"role": "user", "content": user}]}
        if "no_temperature" not in compat:
            body["temperature"] = temp
        return {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, body
    if prov == "openai":
        body = {"model": model, "stream": stream,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if "no_temperature" not in compat:
            body["temperature"] = temp
        body["max_completion_tokens" if "max_completion_tokens" in compat else "max_tokens"] = mt
        return ({"Authorization": f"Bearer {key}"} if key else {}), body
    if prov == "ollama":
        opts = {"num_predict": mt, "num_ctx": int(ctx.get("num_ctx") or context_size(st, system, user)[0])}
        if "no_temperature" not in compat:
            opts["temperature"] = temp
        return {}, {"model": model, "stream": stream, "format": "json", "keep_alive": _keep_alive(st.get("ai_keep_alive")),
                    "options": opts,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    raise AIError("провайдер модели не выбран (Настройки → «Искусственный интеллект» → «Провайдер языковой модели»)",
                  "config")


# ------------------------------------------------------------------ разбор ошибок
def _classify(text):
    """Вид ошибки по тексту ответа сервиса (одинаково для HTTP-ответа и ошибки внутри потока)."""
    low = (text or "").lower()
    if re.search(r"insufficient_quota|credit balance|billing|exceeded your (current )?quota|payment required|"
                 r"недостаточно средств|баланс", low):
        return "quota"
    if re.search(r"max_tokens.{0,40}(too large|at most|maximum|>)|max_completion_tokens.{0,40}(too large|at most)|"
                 r"supports at most \d+ (completion|output) tokens|maximum allowed number of output tokens", low):
        return "maxtokens"
    if re.search(r"requires more system memory|out of memory|cuda error|failed to allocate|not enough memory|"
                 r"insufficient memory|cudamalloc|недостаточно памяти", low):
        return "memory"
    if re.search(r"context length|context window|context_length_exceeded|maximum context|too many tokens|"
                 r"prompt is too long|input is too long|too long|request too large|reduce the length|"
                 r"exceeds? the (model|maximum|context)|слишком длин", low):
        return "context"
    return None


def _limit(text):
    """Допустимый максимум токенов ответа из текста ошибки («at most 4096», «> 32000, which is the maximum»)."""
    for rx in (r"at most (\d{3,6})", r"> ?(\d{3,6}), which is the maximum", r"maximum[^\d]{0,40}(\d{3,6})"):
        m = re.search(rx, text or "", re.I)
        if m:
            return int(m.group(1))
    return None


def _http_error(status, body, headers, st):
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    try:
        j = json.loads(text)
        err = j.get("error") if isinstance(j, dict) else None
        detail = (err.get("message") if isinstance(err, dict) else err) or (j.get("message") if isinstance(j, dict) else "") \
            or (j.get("detail") if isinstance(j, dict) else "") or text
    except ValueError:
        detail = re.sub(r"<[^>]+>", " ", text) if "<html" in text.lower() else text
    detail = re.sub(r"\s+", " ", str(detail)).strip()[:400] or "пустой ответ"
    wait = None
    ra = (headers or {}).get("retry-after")
    if ra:
        try:
            wait = min(120.0, max(0.0, float(ra)))
        except ValueError:
            pass
    prov, model, url = st.get("ai_provider"), st.get("ai_model"), endpoint(st)
    kind = _classify(detail)
    if status in (401, 403) and kind != "quota":
        return AIError(f"модель отказала в доступе (HTTP {status}): {detail}. Проверьте «Ключ API» и права ключа",
                       "auth", status=status)
    if status == 402 or kind == "quota":
        return AIError(f"у провайдера закончились средства или квота (HTTP {status}): {detail}. Пополните баланс "
                       "или увеличьте лимит в личном кабинете провайдера", "quota", status=status)
    if kind == "maxtokens":
        return AIError(f"сервис не принимает такой лимит ответа (HTTP {status}): {detail}. Уменьшите «Максимум "
                       "токенов ответа»", "maxtokens", status=status, limit=_limit(detail))
    if kind == "context":
        return AIError(f"текст не помещается в контекст модели (HTTP {status}): {detail}", "context", status=status)
    if kind == "memory":
        return AIError(f"серверу модели не хватает памяти (HTTP {status}): {detail}. Уменьшите «Страниц в одном "
                       "запросе» или «Размер контекста модели», выберите модель меньше или освободите видеокарту",
                       "memory", retry=True, wait=wait, status=status)
    if status == 404:
        hint = ""
        if prov == "ollama":
            hint = f". Загрузите модель на сервере Ollama: ollama pull {model}"
        elif prov == "openai" and not base_url(st).rstrip("/").endswith("/v1"):
            hint = ". Адрес OpenAI-совместимого API обычно заканчивается на /v1"
        return AIError(f"HTTP 404: модель «{model}» не найдена или неверный «Адрес API» ({url}): {detail}{hint}",
                       "notfound", status=status)
    if status == 405:
        return AIError(f"HTTP 405: по адресу {url} нет API модели — проверьте «Адрес API» и «Провайдер»", "config",
                       status=status)
    if status in (408, 409, 425, 429):
        return AIError(f"сервис модели перегружен или превышен лимит запросов (HTTP {status}): {detail}", "ratelimit",
                       retry=True, wait=wait, status=status)
    if status >= 500:
        return AIError(f"ошибка на стороне сервиса модели (HTTP {status}): {detail}", "server", retry=True, wait=wait,
                       status=status)
    return AIError(f"запрос отклонен (HTTP {status}): {detail}", "badrequest", status=status)


def _stream_error(msg, etype=""):
    kind = _classify(msg) or ("server" if re.search(r"overload|api_error|internal|unavailable|timeout", etype + " " + msg,
                                                  re.I) else "other")
    if kind == "other" and "not found" in msg.lower():
        kind = "notfound"
    retry = kind in ("server", "memory", "other") or "rate" in etype
    return AIError(f"сервис модели прервал ответ: {msg}", kind, retry=retry)


# ------------------------------------------------------------------ чтение потокового ответа
def _read_stream(st, resp, acc):
    """Разбор потокового ответа провайдера: текст копится в acc["parts"], время последних данных — в acc["last"]."""
    prov = st.get("ai_provider")
    ctype = (resp.headers.get("content-type") or "").lower()

    def got(text):
        if text:
            if acc["first"] is None:
                acc["first"] = time.time()
            acc["parts"].append(text)
            acc["chars"] += len(text)
        acc["last"] = time.time()

    if "text/html" in ctype:
        raise AIError(f"по адресу {endpoint(st)} отвечает веб-страница, а не API модели — проверьте «Адрес API» и "
                      "«Провайдер»", "config")
    if "json" in ctype and "ndjson" not in ctype and "event-stream" not in ctype and prov != "ollama":
        raw = resp.read() or b"{}"                              # сервер не поддерживает поток — обычный ответ
        try:
            data = json.loads(raw)
        except ValueError:
            raise AIError(f"сервер модели вернул не JSON: «{raw[:160].decode('utf-8', 'replace')}»", "format", retry=True)
        if isinstance(data, dict) and data.get("error"):
            e = data["error"]
            raise _stream_error(e.get("message") if isinstance(e, dict) else str(e),
                                (e.get("type") if isinstance(e, dict) else "") or "")
        if prov == "anthropic":
            got("".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict)))
            acc["finish"] = data.get("stop_reason")
            acc["usage"] = data.get("usage") or {}
        else:
            ch = (data.get("choices") or [{}])[0]
            got((ch.get("message") or {}).get("content") or ch.get("text") or "")
            acc["finish"] = ch.get("finish_reason")
            u = data.get("usage") or {}
            acc["usage"] = {"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens")}
        return
    for line in resp.iter_lines():
        acc["last"] = time.time()
        line = line.strip()
        if not line:
            continue
        if prov == "ollama":
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("error"):
                raise _stream_error(f"Ollama: {obj['error']}")
            got((obj.get("message") or {}).get("content") or obj.get("response") or "")
            if obj.get("done"):
                acc["finish"] = obj.get("done_reason") or "stop"
                acc["usage"] = {"input_tokens": obj.get("prompt_eval_count"), "output_tokens": obj.get("eval_count"),
                                "load_seconds": round((obj.get("load_duration") or 0) / 1e9, 1),
                                "prompt_seconds": round((obj.get("prompt_eval_duration") or 0) / 1e9, 1),
                                "eval_seconds": round((obj.get("eval_duration") or 0) / 1e9, 1)}
                break
            continue
        if not line.startswith("data:"):
            continue                                             # «event: …», комментарии SSE
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("error") or obj.get("type") == "error":
            e = obj.get("error") or {}
            raise _stream_error(e.get("message") if isinstance(e, dict) else str(e),
                                (e.get("type") if isinstance(e, dict) else "") or "")
        if prov == "anthropic":
            t = obj.get("type")
            if t == "content_block_delta":
                got((obj.get("delta") or {}).get("text") or "")
            elif t == "message_start":
                u = (obj.get("message") or {}).get("usage") or {}
                acc["usage"]["input_tokens"] = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + \
                    (u.get("cache_creation_input_tokens") or 0)
                acc["usage"]["cached_tokens"] = u.get("cache_read_input_tokens") or 0
            elif t == "message_delta":
                acc["finish"] = (obj.get("delta") or {}).get("stop_reason") or acc["finish"]
                acc["usage"]["output_tokens"] = (obj.get("usage") or {}).get("output_tokens")
            elif t == "message_stop":
                break
            continue
        for ch in obj.get("choices") or []:
            got((ch.get("delta") or {}).get("content") or (ch.get("message") or {}).get("content") or ch.get("text") or "")
            if ch.get("finish_reason"):
                acc["finish"] = ch["finish_reason"]
        if obj.get("usage"):
            acc["usage"] = {"input_tokens": obj["usage"].get("prompt_tokens"),
                            "output_tokens": obj["usage"].get("completion_tokens")}


def _socks(obj, depth=0, seen=None):
    """Сокеты внутри пула соединений httpx (служебные поля httpcore; в другой версии библиотеки — пустой список)."""
    seen = seen if seen is not None else set()
    if obj is None or depth > 9 or id(obj) in seen:
        return []
    seen.add(id(obj))
    if isinstance(obj, socket.socket):
        return [obj]
    out = []
    for name in ("_transport", "_mounts", "_pool", "_connections", "_connection", "_network_stream", "_stream", "_sock"):
        v = getattr(obj, name, None)
        if v is None or isinstance(v, (str, bytes, int, float)):
            continue
        items = list(v.values()) if isinstance(v, dict) else list(v) if isinstance(v, (list, tuple)) else [v]
        for it in items:
            out += _socks(it, depth + 1, seen)
    return out


def _kill(client):
    """Разрыв соединений прерванного запроса: сервер модели увидит обрыв и прекратит генерацию (иначе Ollama
    продолжает обрабатывать брошенный запрос, а следующий ждет его в очереди)."""
    try:
        for s in _socks(client):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    except Exception:  # noqa — разрыв соединения необязателен: поток запроса завершится по своему тайм-ауту
        pass


# ------------------------------------------------------------------ состояние Ollama
def ollama_root(st):
    return base_url(dict(st, ai_provider="ollama"))


def ollama_models(st, timeout=10):
    import httpx
    try:
        r = httpx.get(ollama_root(st) + "/api/tags", timeout=timeout)
        return [m.get("name") or m.get("model") for m in (r.json().get("models") or [])] if r.status_code == 200 else []
    except Exception:  # noqa
        return []


def _same_model(a, b):
    a, b = (a or "").strip(), (b or "").strip()
    return a == b or a == b + ":latest" or b == a + ":latest"


def ollama_info(st, timeout=20):
    """Модель на сервере Ollama: семейство, размер, квантование, предел контекста. AIError — если модели нет
    или сервер недоступен."""
    import httpx
    root, model = ollama_root(st), (st.get("ai_model") or "").strip()
    if not model:
        raise AIError("не указана «Модель» (например, qwen2.5:32b)", "config")
    try:
        r = httpx.post(root + "/api/show", json={"model": model, "name": model}, timeout=timeout)
    except httpx.HTTPError as e:
        raise AIError(f"нет связи с Ollama {root}: {e.__class__.__name__}: {e}. Проверьте «Адрес API», что Ollama запущена "
                      "(ollama serve) и доступна с сервера Корректора (OLLAMA_HOST=0.0.0.0 для доступа по сети)",
                      "network", retry=True)
    if r.status_code == 404:
        names = ollama_models(st)
        near = [n for n in names if n.split(":")[0] == model.split(":")[0]]
        hint = ("; есть похожие: " + ", ".join(near[:6])) if near else ("; загружены: " + ", ".join(names[:10])) if names else ""
        raise AIError(f"модель «{model}» не найдена на сервере Ollama {root}{hint}. Загрузите ее: ollama pull {model}",
                      "notfound", status=404)
    if r.status_code != 200:
        raise _http_error(r.status_code, r.content[:4000], r.headers, st)
    try:
        j = r.json()
    except ValueError:
        raise AIError(f"по адресу {root} отвечает не Ollama — проверьте «Адрес API»", "config")
    det, mi = j.get("details") or {}, j.get("model_info") or {}
    ctx = next((v for k, v in mi.items() if k.endswith(".context_length") and isinstance(v, int)), None)
    m = re.search(r"num_ctx\s+(\d+)", j.get("parameters") or "")
    return {"name": model, "family": det.get("family"), "params": det.get("parameter_size"),
            "quant": det.get("quantization_level"), "context": ctx, "modelfile_ctx": int(m.group(1)) if m else None}


def ollama_ps(st, timeout=3):
    """Загружена ли модель в память и какая доля — в видеопамяти. → словарь или None (модель не загружена)."""
    import httpx
    r = httpx.get(ollama_root(st) + "/api/ps", timeout=timeout)
    r.raise_for_status()
    for m in r.json().get("models") or []:
        if _same_model(m.get("name") or m.get("model"), st.get("ai_model")):
            size, vram = m.get("size") or 0, m.get("size_vram") or 0
            return {"size": size, "vram": vram, "share": (vram / size) if size else None, "ctx": m.get("context_length")}
    return None


def _vram_text(ps, ctx=None):
    """« (19,8 ГБ, в видеопамяти 100%)» и предупреждение, если часть модели работает на процессоре."""
    if not ps:
        return ""
    gb = s_(ps["size"] / 2 ** 30)
    if ps.get("share") is None:
        return f" ({gb} ГБ)"
    pct = int(round(ps["share"] * 100))
    msg = f" ({gb} ГБ, в видеопамяти {pct}%)"
    if pct < 95 and not (ctx or {}).get("vram_warned"):
        if ctx is not None:
            ctx["vram_warned"] = True
        msg += " — часть модели работает на процессоре, ответы будут заметно медленнее"
    return msg


def _ollama_state(st, ctx):
    if not ctx.get("ps", True):
        return ""
    try:
        ps = ollama_ps(st)
    except Exception:  # noqa — старые версии Ollama без /api/ps
        ctx["ps"] = False
        return ""
    if ps is None:
        return "; модель загружается в память сервера Ollama"
    return f"; модель в памяти{_vram_text(ps, ctx)}, читает промпт и страницы"


def _ollama_load(st, ctx, log, status=None, should_stop=None):
    """Загрузка модели в память до отправки частей (запрос без сообщений, с тем же контекстом): время загрузки
    не съедает тайм-аут ожидания ответа первой части. → None или AIError — вычитка невозможна."""
    import httpx
    try:
        ps = ollama_ps(st)
    except Exception:  # noqa
        ps = None
    num = int(ctx.get("num_ctx") or 8192)
    if ps and (not ps.get("ctx") or int(ps["ctx"]) == num):
        log("ИИ: модель уже в памяти Ollama" + _vram_text(ps, ctx))
        return None
    url = ollama_root(st) + "/api/chat"
    body = {"model": (st.get("ai_model") or "").strip(), "messages": [], "stream": False,
            "keep_alive": _keep_alive(st.get("ai_keep_alive")), "options": {"num_ctx": num}}
    limit = max(600.0, 3 * float(st.get("ai_timeout") or 240))
    beat = max(1.0, float(st.get("ai_heartbeat") or 15))
    acc = {"done": threading.Event(), "error": None, "client": None, "resp": None}

    def worker():
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=20.0, read=limit + 30, write=60.0, pool=30.0)) as client:
                acc["client"] = client
                r = client.post(url, json=body)
                acc["resp"] = (r.status_code, r.content[:4000], r.headers)
        except BaseException as e:  # noqa
            acc["error"] = e
        finally:
            acc["done"].set()

    log(f"ИИ: загрузка модели в память Ollama (контекст {tk(num)}), ждем до {int(limit // 60)} мин")
    t0 = time.time()
    next_beat = t0 + beat
    threading.Thread(target=worker, daemon=True, name="ai-load").start()
    while not acc["done"].wait(1.0):
        now = time.time()
        if should_stop and should_stop():
            _kill(acc["client"])
            raise Cancelled()
        if now - t0 > limit:
            _kill(acc["client"])
            log(f"ИИ ⚠ модель не загрузилась за {int(limit // 60)} мин — части будут отправлены, но первая может "
                "не уложиться в «Тайм-аут ожидания ответа»")
            return None
        if now >= next_beat:
            next_beat = now + beat
            msg = f"загрузка модели в память Ollama: {int(now - t0)} с"
            log("ИИ … " + msg)
            if status:
                try:
                    status("ИИ: " + msg)
                except Exception:  # noqa
                    pass
    e = acc["error"]
    if e is not None:
        if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
            return AIError(f"нет связи с Ollama {url}: {e}", "network")
        log(f"ИИ ⚠ предварительная загрузка модели не удалась ({e.__class__.__name__}: {e}) — части отправляются как обычно")
        return None
    code, content, headers = acc["resp"]
    if code != 200:
        err = _http_error(code, content, headers, st)
        if err.kind in FATAL or err.kind == "memory":
            return err
        log(f"ИИ ⚠ предварительная загрузка модели: {err}")
        return None
    try:
        ps = ollama_ps(st)
    except Exception:  # noqa
        ps = None
    log(f"ИИ: модель загружена в память за {s_(time.time() - t0)} с" + _vram_text(ps, ctx))
    return None


# ------------------------------------------------------------------ один запрос к модели
def _adapt(e, st, ctx):
    """Сервис не принял параметр запроса → перестроить запрос. → описание изменения или None."""
    compat = ctx.setdefault("compat", set())
    low, prov = str(e).lower(), st.get("ai_provider")
    if e.kind == "maxtokens" and e.limit and e.limit < int(ctx.get("max_tokens") or 10 ** 9):
        ctx["max_tokens"] = e.limit
        return f"сервис ограничивает ответ {n_(e.limit)} токенами — лимит ответа уменьшен на время проверки"
    if e.kind != "badrequest":
        return None
    if prov == "openai" and "max_completion_tokens" in low and "max_completion_tokens" not in compat:
        compat.add("max_completion_tokens")
        return "модель требует параметр max_completion_tokens вместо max_tokens"
    if "temperature" in low and "no_temperature" not in compat:
        compat.add("no_temperature")
        return "модель не принимает настройку «Температура» — используется значение модели по умолчанию"
    if "cache_control" in low and prov == "anthropic" and "no_cache" not in compat:
        compat.add("no_cache")
        return "сервис не поддерживает кэширование промпта — промпт отправляется без кэша"
    if "stream" in low and "no_stream" not in compat:
        compat.add("no_stream")
        return "сервис не поддерживает потоковый ответ — ответ принимается целиком (ход генерации не виден)"
    return None


def request(st, system, user, log=None, status=None, should_stop=None, label="запрос", ctx=None):
    """Один запрос к модели с потоковым ответом и контролем времени. ctx — общее состояние прохода (подстройка
    параметров под сервис, контекст Ollama). → {"text", "finish", "usage", "first", "seconds", "chars"};
    AIError — при ошибке."""
    log = log or (lambda m: None)
    ctx = ctx if ctx is not None else {}
    if os.environ.get("KORREKTOR_AI_MOCK"):
        return {"text": mock_answer(user), "finish": "stop", "usage": {}, "first": 0.0, "seconds": 0.0, "chars": 20}
    for _ in range(4):
        try:
            return _request_once(st, system, user, log, status, should_stop, label, ctx)
        except AIError as e:
            what = _adapt(e, st, ctx)
            if not what:
                raise
            log(f"ИИ ⚙ {label}: {what} — запрос отправлен заново")
    raise AIError("сервис отклоняет запрос и после подстройки параметров", "badrequest")


def _request_once(st, system, user, log, status, should_stop, label, ctx):
    import httpx
    url = endpoint(st)
    if not url:
        raise AIError("провайдер модели не выбран (Настройки → «Искусственный интеллект» → «Провайдер языковой модели»)",
                      "config")
    headers, payload = _payload(st, system, user, ctx)
    prov = st.get("ai_provider")
    first_to = float(st.get("ai_timeout") or 240)
    stall = float(st.get("ai_stall_timeout") or 120)
    hard = max(first_to * 3, 900.0)
    if not payload.get("stream", True):
        first_to = hard                                   # без потока первый знак приходит вместе со всем ответом
    beat = max(1.0, float(st.get("ai_heartbeat") or 15))
    acc = {"parts": [], "chars": 0, "first": None, "last": None, "finish": None, "usage": {}, "error": None,
           "client": None, "status": None, "done": threading.Event()}
    t0 = time.time()

    def worker():
        try:
            timeout = httpx.Timeout(connect=20.0, read=max(first_to, stall) + 30, write=120.0, pool=30.0)
            with httpx.Client(timeout=timeout) as client:
                acc["client"] = client
                with client.stream("POST", url, headers=headers, json=payload) as resp:
                    acc["status"] = resp.status_code
                    acc["last"] = time.time()
                    if resp.status_code != 200:
                        raise _http_error(resp.status_code, resp.read()[:4000], resp.headers, st)
                    _read_stream(st, resp, acc)
        except BaseException as e:  # noqa — разбирается в потоке задания
            acc["error"] = e
        finally:
            acc["done"].set()

    th = threading.Thread(target=worker, daemon=True, name="ai-request")
    th.start()
    next_beat = t0 + beat
    while not acc["done"].wait(1.0):
        now = time.time()
        el = now - t0
        abort = None
        if should_stop and should_stop():
            abort = AIError("остановлено пользователем", "cancelled")
        elif acc["first"] is None and el > first_to:
            msg = f"модель не начала отвечать за {sec_(first_to)} с (настройка «Тайм-аут ожидания ответа»)"
            if prov == "ollama":
                msg += (f". Локальной модели нужно время, чтобы загрузиться в память и прочитать мастер-промпт с текстом "
                        f"страниц (~{tk(tokens(system) + tokens(user))}): увеличьте тайм-аут до 600–900 с, "
                        "уменьшите «Страниц в одном запросе» или используйте сервер с видеокартой")
            else:
                msg += ". Сервис перегружен или запрос слишком велик: уменьшите «Страниц в одном запросе» или повторите позже"
            abort = AIError(msg, "timeout", retry=True)
        elif acc["first"] is not None and acc["last"] and now - acc["last"] > stall:
            abort = AIError(f"модель перестала присылать данные: {int(now - acc['last'])} с без ответа "
                            "(настройка «Тайм-аут простоя»)", "timeout", retry=True)
        elif el > hard:
            abort = AIError(f"запрос длится дольше {int(hard // 60)} мин — прерван (предел: три «Тайм-аута ожидания "
                            "ответа», но не меньше 15 минут)", "timeout", retry=True)
        if abort:
            _kill(acc["client"])
            raise abort
        if now >= next_beat:
            next_beat = now + beat
            if acc["first"] is None:
                if prov == "ollama":
                    phase = _ollama_state(st, ctx) if el >= 10 else ""
                    phase = phase or "; Ollama читает промпт и страницы"
                elif acc["status"] is None:
                    phase = "; сервер еще не ответил"
                else:
                    phase = "; сервер принял запрос, модель читает промпт и страницы"
                msg = f"{label}: ждем начала ответа {int(el)} с из {sec_(first_to)}{phase}"
            else:
                rate = acc["chars"] / 3 / max(0.5, now - acc["first"])
                msg = (f"{label}: модель пишет ответ — {zn(acc['chars'])} (~{tk(acc['chars'] // 3)}, "
                       f"{s_(rate)} ток./с), прошло {int(el)} с, последние данные {int(now - (acc['last'] or now))} с назад")
            log("ИИ … " + msg)
            if status:
                try:
                    status("ИИ: " + msg)
                except Exception:  # noqa — строка состояния необязательна
                    pass
    e = acc["error"]
    if e is not None:
        if isinstance(e, AIError):
            raise e
        name = e.__class__.__name__
        if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
            raise AIError(f"нет связи с сервером модели {url}: {e}. Проверьте «Адрес API», что сервер запущен и доступен "
                          "с сервера Корректора (сеть, прокси, межсетевой экран)", "network", retry=True)
        if isinstance(e, httpx.TimeoutException):
            raise AIError(f"тайм-аут сети при обращении к {url} ({name})", "timeout", retry=True)
        if isinstance(e, (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.NetworkError)):
            raise AIError(f"соединение с сервером модели оборвалось ({name}: {e})", "network", retry=True)
        if isinstance(e, httpx.UnsupportedProtocol) or isinstance(e, httpx.InvalidURL):
            raise AIError(f"неверный «Адрес API» {url}: {e}", "config")
        if isinstance(e, ValueError):
            raise AIError(f"сервер модели вернул непонятный ответ ({name}: {str(e)[:200]})", "format", retry=True)
        if isinstance(e, MemoryError):
            raise AIError("не хватило памяти сервера Корректора при чтении ответа модели", "other", retry=True)
        raise AIError(f"ошибка запроса к модели ({name}: {str(e)[:300]})", "other", retry=True)
    return {"text": "".join(acc["parts"]), "finish": acc["finish"], "usage": acc["usage"],
            "first": (acc["first"] - t0) if acc["first"] else None, "seconds": time.time() - t0, "chars": acc["chars"]}


def call(st, system, user, log=None):
    """Совместимость: текст ответа одного запроса."""
    return request(st, system, user, log)["text"]


def mock_answer(user):
    """Тестовый ответ (KORREKTOR_AI_MOCK=1): ничего не находит."""
    return json.dumps({"findings": []}, ensure_ascii=False)


# ------------------------------------------------------------------ разбор ответа
def _close_truncated(s):
    """Обрезанный JSON → текст до последнего целого замечания + закрывающие скобки."""
    depth, ins, esc, last_obj = 0, False, False, -1
    for i, ch in enumerate(s):
        if ins:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                ins = False
            continue
        if ch == '"':
            ins = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if ch == "}" and depth == 2:
                last_obj = i                          # закрылось замечание внутри «findings»: {"findings": [ {...}
    if last_obj < 0:
        return None
    return s[:last_obj + 1] + "]}"


def parse_json(text):
    """→ (замечания, None) или ([], описание ошибки). Восстанавливает ответ в ```-блоке, с лишними запятыми
    и обрезанный посередине."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.M).strip()
    if not t:
        return [], "пустой ответ"
    a = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    if a < 0:
        return [], "в ответе нет JSON"
    body = t[a:]
    for cand in (body[:body.rfind("}") + 1] if "}" in body else body, body):
        for fix in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            try:
                data = json.loads(fix)
            except ValueError:
                continue
            if isinstance(data, list):
                return data, None
            if isinstance(data, dict):
                items = data.get("findings")
                if items is None:
                    return [], "в JSON нет поля «findings»"
                return items if isinstance(items, list) else [], None
    closed = _close_truncated(body)
    if closed:
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", closed))
            return list(data.get("findings") or []), "ответ обрезан — взяты целые замечания до места обрыва"
        except ValueError:
            pass
    return [], "ответ не удалось разобрать как JSON"


def parse(text):
    """Совместимость: только список замечаний."""
    return parse_json(text)[0]


def validate(items, idx, st, log, drops=None):
    out = []
    mode = st.get("ai_quote_check", "fix_page")
    drops = drops if drops is not None else []
    for it in items:
        if not isinstance(it, dict):
            drops.append("элемент ответа — не объект")
            continue
        try:
            f = {k: yo(str(it.get(k, "")).strip()) for k in ("clause", "type", "src", "bad", "fix", "good", "note")}
            f["page"] = int(it.get("page"))
        except Exception:  # noqa
            drops.append(f"нет номера страницы: «{str(it.get('src', ''))[:50]}»")
            continue
        if not f["src"]:
            drops.append("пустая цитата")
            continue
        if f["type"] not in TYPES:
            drops.append(f"неизвестный тип «{f['type'][:30]}»: «{f['src'][:50]}»")
            continue
        if not idx.has(f["page"], f["src"]):
            others = idx.find_pages(f["src"])
            if mode == "fix_page" and others:
                f["page"] = min(others, key=lambda p: abs(p - f["page"]))
            elif mode == "flag":
                f["type"], f["fix"], f["good"] = AUTHOR, "—", "—"
                f["note"] = "Цитата модели не найдена дословно в тексте PDF. " + f["note"]
                if not others:
                    drops.append(f"цитата не найдена в PDF: «{f['src'][:60]}»")
                    continue
                f["page"] = others[0]
            else:
                drops.append(f"цитата не найдена в PDF: «{f['src'][:60]}»")
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
        f["note"] = f["note"] or "Замечание языковой модели."
        f["clause"] = f["clause"] or "текст"
        f["rule"] = "ai"
        out.append(f)
    return out


# ------------------------------------------------------------------ проход по документу
class _Run:
    def __init__(self, st, log, status, should_stop, total_chunks, ctx):
        self.st, self.log, self.status, self.should_stop, self.ctx = st, log, status, should_stop, ctx
        self.lock = threading.Lock()
        self.total = total_chunks
        self.done = 0
        self.fatal = None
        self.fatal_kind = None
        self.net_fail = 0
        self.last_err = None
        mins = float(st.get("ai_total_timeout") or 0)
        self.deadline = time.time() + mins * 60 if mins > 0 else None
        self.report = {"chunks": total_chunks, "ok": 0, "failed": [], "skipped": [], "requests": 0, "retries": 0,
                       "splits": 0, "tokens_in": 0, "tokens_out": 0, "dropped": 0, "seconds": 0.0, "accepted": 0}

    def stop_reason(self):
        if self.should_stop and self.should_stop():
            raise Cancelled()
        if self.fatal is not None:
            return f"пропущено: {KIND_NAMES.get(self.fatal_kind) or self.fatal} — см. ошибку выше"
        if self.deadline and time.time() > self.deadline:
            return f"пропущено: истек «Общий лимит прохода ИИ» ({self.st.get('ai_total_timeout')} мин)"
        return None

    def set_fatal(self, why, kind=None):
        with self.lock:
            if self.fatal is None:
                self.fatal, self.fatal_kind = why, kind
                return True
        return False


def _rng(pages):
    a, b = pages[0]["pdf_page"], pages[-1]["pdf_page"]
    return f"стр. {a}" if a == b else f"стр. {a}–{b}"


def _sleep(r, sec):
    end = time.time() + sec
    while time.time() < end:
        if r.should_stop and r.should_stop():
            raise Cancelled()
        time.sleep(max(0.0, min(1.0, end - time.time())))


def _chunk(r, system, pages, idx, label, depth=0):
    st, log = r.st, r.log
    body = pages_body(pages)
    rng = _rng(pages)
    if not body.strip():
        log(f"ИИ · {label} ({rng}): на страницах нет текста — запрос не нужен")
        with r.lock:
            r.report["ok"] += 1
        return []
    user = USER_HEAD + body
    retries = int(st.get("ai_retries") if st.get("ai_retries") is not None else 2)
    for attempt in range(retries + 1):
        why = r.stop_reason()
        if why:
            with r.lock:
                r.report["skipped"].append({"pages": rng, "reason": why})
            log(f"ИИ ⏭ {label} ({rng}): {why}")
            return []
        tag = f"{label} ({rng})" + (f", попытка {attempt + 1} из {retries + 1}" if attempt else "")
        log(f"ИИ ▶ {tag}: {zn(len(body))} текста (~{tk(tokens(user))}) + мастер-промпт, запрос к модели")
        with r.lock:
            r.report["requests"] += 1
        try:
            res = request(st, system, user, log, r.status, r.should_stop, label=f"{label} ({rng})", ctx=r.ctx)
        except AIError as e:
            if e.kind == "cancelled":
                raise Cancelled()
            log(f"ИИ ✗ {tag}: {KIND_NAMES.get(e.kind, 'ошибка')} — {e}")
            key = (e.kind, str(e)[:160])
            with r.lock:
                repeat = r.last_err == key
                r.last_err = key
            if e.kind in FATAL or (e.kind in ("badrequest", "memory", "maxtokens") and repeat):
                with r.lock:
                    r.report["failed"].append({"pages": rng, "reason": str(e), "kind": e.kind})
                if r.set_fatal(str(e), e.kind):
                    log("ИИ ⛔ остальные части не отправляются: " + ("ошибка повторилась" if e.kind not in FATAL else
                        "ошибка не исчезнет при повторе") + ". Исправьте причину и проверьте документ заново "
                        "(кнопка «Проверить заново»)")
                return []
            if e.kind == "context" and len(pages) > 1 and depth < 5:
                return _split(r, system, pages, idx, label, depth, "текст не поместился в контекст модели")
            if e.kind == "network":
                with r.lock:
                    r.net_fail += 1
                    nf = r.net_fail
                if nf >= 3:
                    if r.set_fatal("сервер модели недоступен (три сетевые ошибки подряд)", "network"):
                        log("ИИ ⛔ три сетевые ошибки подряд — сервер модели недоступен, остальные части не отправляются")
                    with r.lock:
                        r.report["failed"].append({"pages": rng, "reason": str(e), "kind": e.kind})
                    return []
            if not e.retry or attempt == retries:
                with r.lock:
                    r.report["failed"].append({"pages": rng, "reason": str(e), "kind": e.kind})
                log(f"ИИ ✗ {label} ({rng}): часть не проверена моделью после {attempt + 1} "
                    f"{plural(attempt + 1, 'попытки', 'попыток', 'попыток')}")
                return []
            wait = e.wait if e.wait is not None else min(60.0, BACKOFF * (2 ** attempt)) + random.uniform(0, BACKOFF * 0.6)
            with r.lock:
                r.report["retries"] += 1
            log(f"ИИ ↻ {label} ({rng}): повтор через {s_(wait)} с")
            _sleep(r, wait)
            continue
        u = res.get("usage") or {}
        with r.lock:
            r.net_fail = 0
            r.last_err = None
            r.report["tokens_in"] += int(u.get("input_tokens") or 0)
            r.report["tokens_out"] += int(u.get("output_tokens") or 0)
            r.report["seconds"] += res["seconds"]
        items, perr = parse_json(res["text"])
        uinfo = ""
        if u.get("input_tokens") or u.get("output_tokens"):
            uinfo = f", токены: запрос {n_(u.get('input_tokens') or 0)}, ответ {n_(u.get('output_tokens') or 0)}"
            if u.get("cached_tokens"):
                uinfo += f" (из кэша {n_(u['cached_tokens'])})"
        if u.get("load_seconds") or u.get("prompt_seconds"):
            uinfo += (f", загрузка модели {s_(u.get('load_seconds') or 0)} с, чтение промпта "
                      f"{s_(u.get('prompt_seconds') or 0)} с, генерация {s_(u.get('eval_seconds') or 0)} с")
        first = f", первый знак через {s_(res['first'])} с" if res.get("first") is not None else ""
        log(f"ИИ ✓ {tag}: ответ {zn(res['chars'])} за {s_(res['seconds'])} с{first}{uinfo}; "
            f"причина остановки: {res.get('finish') or '—'}")
        num = r.ctx.get("num_ctx")
        if num and u.get("input_tokens") and int(u["input_tokens"]) >= 0.97 * num and not r.ctx.get("trunc_warned"):
            r.ctx["trunc_warned"] = True
            log(f"ИИ ⚠ Ollama прочитала {tk(u['input_tokens'])} при контексте {n_(num)} — запрос, вероятно, обрезан "
                "(потеряна часть мастер-промпта). Уменьшите «Страниц в одном запросе» или выберите модель с длинным "
                "контекстом")
        first_to = float(st.get("ai_timeout") or 240)
        if res.get("first") and res["first"] > 0.75 * first_to and not r.ctx.get("slow_warned"):
            r.ctx["slow_warned"] = True
            log(f"ИИ ⚠ модель начала отвечать через {s_(res['first'])} с при «Тайм-ауте ожидания ответа» {int(first_to)} с "
                "— запас мал, следующие части могут не уложиться. Увеличьте тайм-аут")
        if res.get("finish") in ("length", "max_tokens"):
            log(f"ИИ ⚠ {label} ({rng}): ответ обрезан лимитом «Максимум токенов ответа» "
                f"({r.ctx.get('max_tokens') or st.get('ai_max_tokens')})")
            if len(pages) > 1 and depth < 5:
                return _split(r, system, pages, idx, label, depth, "ответ модели обрезан лимитом токенов")
        if perr and not items:
            preview = re.sub(r"\s+", " ", res["text"])[:240]
            log(f"ИИ ✗ {tag}: {perr}. Начало ответа: «{preview}»")
            if attempt < retries:
                with r.lock:
                    r.report["retries"] += 1
                log(f"ИИ ↻ {label} ({rng}): повтор — модель должна вернуть только JSON")
                continue
            with r.lock:
                r.report["failed"].append({"pages": rng, "reason": f"ответ модели не в формате JSON ({perr})",
                                           "kind": "format"})
            return []
        if perr:
            log(f"ИИ ⚠ {label} ({rng}): {perr}")
        drops = []
        out = validate(items, idx, st, log, drops)
        with r.lock:
            r.report["dropped"] += len(drops)
            r.report["accepted"] += len(out)
            r.report["ok"] += 1
        log(f"ИИ ✓ {label} ({rng}): замечаний в ответе {len(items)}, принято после проверки цитат {len(out)}"
            + (f", отброшено {len(drops)}" if drops else ""))
        for d in drops[:8]:
            log(f"ИИ · отброшено: {d}")
        if len(drops) > 8:
            log(f"ИИ · … и еще {len(drops) - 8}")
        return out
    return []


def _split(r, system, pages, idx, label, depth, why):
    mid = len(pages) // 2
    with r.lock:
        r.report["splits"] += 1
    r.log(f"ИИ ✂ {label} ({_rng(pages)}): {why} — делю на две части: {_rng(pages[:mid])} и {_rng(pages[mid:])}")
    return (_chunk(r, system, pages[:mid], idx, label + ".1", depth + 1) +
            _chunk(r, system, pages[mid:], idx, label + ".2", depth + 1))


def _ollama_prepare(st, system, chunks, ctx, log, report):
    """Проверка модели на сервере Ollama и выбор контекста на весь документ. → None или AIError (вычитка невозможна)."""
    try:
        info = ollama_info(st)
    except AIError as e:
        if e.kind in FATAL or e.kind == "network":
            return e
        log(f"ИИ ⚠ не удалось узнать параметры модели Ollama: {e}")
        info = {}
    if info:
        log(f"ИИ: модель Ollama «{info['name']}» — семейство {info.get('family') or '?'}, {info.get('params') or '?'} "
            f"параметров, квантование {info.get('quant') or '?'}, предел контекста модели "
            f"{tk(info['context']) if info.get('context') else 'неизвестен'}")
    want = max(context_need(st, system, USER_HEAD + pages_body(ch)) for ch in chunks)
    mmax, fixed = info.get("context"), int(st.get("ai_context") or 0)
    num = pick_context(st, want, mmax)
    ctx["num_ctx"] = num
    report["num_ctx"] = num
    keep = st.get("ai_keep_alive") or "30m"
    if want > num:
        if mmax and num >= mmax and not fixed:
            advice = "Выберите модель с длинным контекстом (128K) или уменьшите «Страниц в одном запросе»"
            why = f"предел модели {n_(mmax)}"
        else:
            advice = "Поставьте «Размер контекста модели» = 0 (подбирается сам) или уменьшите «Страниц в одном запросе»"
            why = "настройка «Размер контекста модели»"
        log(f"ИИ ⚠ запрос (~{tk(want)} вместе с ответом) больше контекста {tk(num)} ({why}): Ollama "
            f"обрежет начало мастер-промпта, и вычитка потеряет часть правил. {advice}")
    else:
        log(f"ИИ: контекст Ollama (num_ctx) {tk(num)}" + (" — подобран автоматически" if not fixed else "")
            + f", нужно до ~{n_(want)}; один размер на весь документ (смена размера перезагружает модель); "
              f"модель держится в памяти {keep} между запросами")
    if fixed and mmax and fixed > mmax:
        log(f"ИИ ⚠ «Размер контекста модели» {n_(fixed)} больше предела модели {n_(mmax)} — Ollama уменьшит его до предела")
    return None


def run(dump, idx, st, extra, log, progress=None, status=None, should_stop=None, report=None):
    """Проход ИИ по документу. → список замечаний; report (словарь) заполняется сводкой: сколько частей
    проверено, какие страницы и почему не проверены, токены и время."""
    report = report if report is not None else {}
    prov = st.get("ai_provider")
    if prov in (None, "", "none") and not os.environ.get("KORREKTOR_AI_MOCK"):
        return []
    system = master_prompt() + "\n" + SERVICE_RULES
    if extra.strip():
        system += "\n## ДОПОЛНИТЕЛЬНЫЕ УКАЗАНИЯ\n" + extra
    limit = int(st.get("ai_max_pages") or 300)
    pages = dump["lines"][:limit]
    if len(dump["lines"]) > limit:
        log(f"ИИ ⚠ документ длиннее лимита — модель проверит первые {limit} из {len(dump['lines'])} страниц "
            "(настройка «Лимит страниц для ИИ»)")
    n = max(1, int(st.get("ai_pages_per_request") or 6))
    chunks = [pages[i:i + n] for i in range(0, len(pages), n)]
    par = max(1, int(st.get("ai_parallel") or 1))
    ctx = {"compat": set()}
    mock = bool(os.environ.get("KORREKTOR_AI_MOCK"))
    if prov == "ollama" and par > 1 and not st.get("ai_ollama_parallel"):
        log(f"ИИ: Ollama — части отправляются по одному (параллельные запросы ждали бы в очереди Ollama и упирались "
            f"в тайм-аут). «Параллельных запросов» = {par} действует для облачных API; для Ollama включите "
            "«Параллельные запросы к Ollama», если сервер настроен на OLLAMA_NUM_PARALLEL > 1")
        par = 1
    log(f"ИИ: провайдер {prov if prov not in (None, '', 'none') else 'тестовый режим'}, модель {st.get('ai_model') or '—'}, адрес {endpoint(st) or '—'}")
    log(f"ИИ: {len(chunks)} {plural(len(chunks), 'часть', 'части', 'частей')} по {n} стр., параллельно {par}; "
        f"ожидание начала ответа {sec_(st.get('ai_timeout') or 240)} с, простой {sec_(st.get('ai_stall_timeout') or 120)} с"
        + (f", весь проход ИИ {st.get('ai_total_timeout')} мин" if st.get("ai_total_timeout") else "")
        + f"; повторов {st.get('ai_retries') if st.get('ai_retries') is not None else 2}; "
          f"максимум токенов ответа {st.get('ai_max_tokens') or 8000}; запись о ходе ожидания — раз в "
          f"{sec_(max(1.0, float(st.get('ai_heartbeat') or 15)))} с")
    log(f"ИИ: мастер-промпт с дополнениями — {zn(len(system))} (~{tk(tokens(system))}); он отправляется "
        "с каждым запросом" + (" (у Anthropic кэшируется)" if prov == "anthropic" else ""))
    report.update(provider=prov or "mock", model=st.get("ai_model") or "", parallel=par, pages_per_request=n,
                  chunks=len(chunks))
    if not chunks:
        report.update(ok=0, failed=[], skipped=[], requests=0, retries=0, splits=0, tokens_in=0, tokens_out=0,
                      dropped=0, accepted=0, seconds=0.0, seconds_total=0.0)
        return []
    if prov == "ollama" and not mock:
        why = _ollama_prepare(st, system, chunks, ctx, log, report) or _ollama_load(st, ctx, log, status, should_stop)
        if why:
            log(f"ИИ ⛔ {why}")
            log("ИИ ⛔ вычитка моделью не выполнена — задание завершается с результатами правил")
            report.update(ok=0, failed=[{"pages": f"{_rng(pages)}", "reason": str(why), "kind": why.kind}], skipped=[],
                          requests=0, retries=0, splits=0, tokens_in=0, tokens_out=0, dropped=0, accepted=0,
                          seconds=0.0, seconds_total=0.0)
            return []
    r = _Run(st, log, status, should_stop, len(chunks), ctx)
    t0 = time.time()

    def work(i_chunk):
        i, chunk = i_chunk
        try:
            return _chunk(r, system, chunk, idx, f"часть {i + 1}/{len(chunks)}")
        except Cancelled:
            raise
        except Exception as e:  # noqa — ошибка в одной части не прерывает проход
            _log.exception("ИИ: часть %s", i + 1)
            with r.lock:
                r.report["failed"].append({"pages": _rng(chunk), "reason": f"внутренняя ошибка: {e!r}", "kind": "other"})
            log(f"ИИ ✗ часть {i + 1}/{len(chunks)} ({_rng(chunk)}): внутренняя ошибка {e!r} — часть пропущена")
            return []
        finally:
            with r.lock:
                r.done += 1
                d = r.done
            if progress:
                try:
                    progress(d / len(chunks))
                except Cancelled:
                    raise
                except Exception:  # noqa
                    pass

    res = []
    ex = ThreadPoolExecutor(max_workers=par, thread_name_prefix="ai")
    futs = [ex.submit(work, (i, c)) for i, c in enumerate(chunks)]
    try:
        for f in futs:
            res.append(f.result())
    except Cancelled:
        r.fatal = "остановлено пользователем"
        for f in futs:
            f.cancel()
        log("ИИ ■ проход остановлен пользователем")
        raise
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
    out = [f for x in res for f in x]
    rep = r.report
    rep["seconds_total"] = round(time.time() - t0, 1)
    rep["seconds"] = round(rep["seconds"], 1)
    rep["compat"] = sorted(ctx.get("compat") or [])
    report.update(rep)
    bad = rep["failed"] + rep["skipped"]
    log(f"ИИ: итог — проверено частей {rep['ok']} из {rep['chunks']}"
        + (f" (делений {rep['splits']})" if rep["splits"] else "")
        + f", запросов {rep['requests']}, повторов {rep['retries']}, время {s_(rep['seconds_total'])} с"
        + (f", токены: запрос {n_(rep['tokens_in'])}, ответ {n_(rep['tokens_out'])}" if rep["tokens_in"] or rep["tokens_out"] else "")
        + f"; замечаний принято {len(out)}" + (f", отброшено {rep['dropped']}" if rep["dropped"] else ""))
    if bad:
        groups = {}
        for b in bad:
            groups.setdefault(b["reason"], []).append(b["pages"])
        log("ИИ ⚠ не проверены моделью: " + "; ".join(f"{', '.join(pg[:15])}{' …' if len(pg) > 15 else ''} — {why}"
                                                     for why, pg in list(groups.items())[:8]))
    return out


def not_checked(report):
    """Строка «какие страницы модель не проверила» для шапки отчета и страницы задания (или пустая строка)."""
    bad = (report or {}).get("failed", []) + (report or {}).get("skipped", [])
    if not bad:
        return ""
    kinds = []
    for b in bad:
        k = KIND_NAMES.get(b.get("kind") or ("timeout" if "тайм-аут" in b.get("reason", "") else "other"), "ошибка")
        if b.get("reason", "").startswith("пропущено"):
            k = "пропущено"
        kinds.append(f"{b['pages']} ({k})")
    return "Вычитка языковой моделью выполнена не полностью, не проверены: " + "; ".join(kinds[:20]) + \
        (" …" if len(kinds) > 20 else "")


# ------------------------------------------------------------------ проверка связи (страница настроек)
def diagnose(st, full=False, log=None):
    """Проверка связи с моделью. full — с настоящим мастер-промптом (показывает время чтения промпта и обрезку
    контекста). → (успех, [строки отчета])."""
    lines = []
    prov = st.get("ai_provider")
    if prov in (None, "", "none"):
        return False, ["Провайдер не выбран: «Провайдер языковой модели» — «Не подключен». Выберите провайдера и "
                       "сохраните настройки"]
    lines.append(f"Провайдер: {prov}, модель: {st.get('ai_model') or '—'}, адрес: {endpoint(st)}")
    ctx = {"compat": set()}
    info = {}
    try:
        if prov == "ollama":
            info = ollama_info(st)
            lines.append(f"Ollama: модель найдена — {info.get('family') or '?'}, {info.get('params') or '?'}, "
                         f"квантование {info.get('quant') or '?'}, предел контекста "
                         f"{tk(info['context']) if info.get('context') else 'неизвестен'}")
        if full:
            system = master_prompt() + "\n" + SERVICE_RULES
            user = (USER_HEAD + "=== Страница 1 ===\nНасосная установка предназначена для водоснабжения зданий.Диапазон "
                    "давления 10-20 бар, мощность 5,5кВт.")
            st2 = dict(st, ai_max_tokens=min(int(st.get("ai_max_tokens") or 8000), 2000))
            if prov == "ollama":
                num, want = context_size(st, system, user + " " * 3 * 6000, info.get("context"))
                ctx["num_ctx"] = num
                lines.append(f"Контекст Ollama на запрос: {tk(num)} (с запасом на 6 страниц текста нужно ~{n_(want)})")
            lines.append(f"Мастер-промпт: {zn(len(system))}, ~{tk(tokens(system))}")
        else:
            system = "Ты проверяешь связь. Ответь строго JSON {\"findings\": []}."
            user = "Проверка связи"
            st2 = dict(st, ai_max_tokens=500)
        res = request(st2, system, user, log, label="проверка", ctx=ctx)
        items, perr = parse_json(res["text"])
        u = res.get("usage") or {}
        lines.append(f"Ответ за {s_(res['seconds'])} с" + (f", первый знак через {s_(res['first'])} с" if res.get("first") is not None else "")
                     + f", {zn(res['chars'])}, причина остановки: {res.get('finish') or '—'}")
        if u.get("input_tokens") or u.get("output_tokens"):
            lines.append(f"Токены: запрос {n_(u.get('input_tokens') or 0)}, ответ {n_(u.get('output_tokens') or 0)}"
                         + (f" (из кэша {n_(u['cached_tokens'])})" if u.get("cached_tokens") else "")
                         + (f"; загрузка модели {s_(u.get('load_seconds') or 0)} с, чтение промпта {s_(u.get('prompt_seconds') or 0)} с"
                            if u.get("load_seconds") or u.get("prompt_seconds") else ""))
        if ctx.get("num_ctx") and u.get("input_tokens") and int(u["input_tokens"]) >= 0.97 * ctx["num_ctx"]:
            lines.append("⚠ Ollama, вероятно, обрезала промпт: прочитано почти столько токенов, сколько вмещает контекст")
        if full and res.get("first") is not None:
            first_to = float(st.get("ai_timeout") or 240)
            if res["first"] > 0.6 * first_to:
                lines.append(f"⚠ Начало ответа через {s_(res['first'])} с — близко к «Тайм-ауту ожидания ответа» ({int(first_to)} с): "
                             "увеличьте тайм-аут, иначе части документа будут прерываться")
        preview = re.sub(r"\s+", " ", res["text"])[:120]
        lines.append("Формат ответа: " + ("JSON — верно" if not perr else f"не JSON ({perr}); начало: «{preview}»"))
        if ctx["compat"] or ctx.get("max_tokens"):
            lines.append("Подстройка под сервис: " + ", ".join(sorted(ctx["compat"]) + (
                [f"ответ не длиннее {tk(ctx['max_tokens'])}"] if ctx.get("max_tokens") else [])))
        return (not perr or bool(items)), lines
    except AIError as e:
        lines.append(f"Ошибка ({KIND_NAMES.get(e.kind, e.kind)}): {e}")
        return False, lines
    except Exception as e:  # noqa
        lines.append(f"Ошибка: {e.__class__.__name__}: {e}")
        return False, lines
