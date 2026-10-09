# -*- coding: utf-8 -*-
"""Работа с языковой моделью через поддельный сервер: поток (OpenAI, Anthropic, Ollama), тайм-ауты ожидания
и простоя с разрывом соединения, повторы, деление частей, подстройка параметров, фатальные ошибки, остановка."""
import json
import select
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.pipeline import llm
from app.pipeline.common import Cancelled, PageIndex

QUOTE = "Насосные установки предназначены для водоснабжения зданий."


def finding(page=1):
    return {"page": page, "clause": "текст", "type": "Стилистика", "src": QUOTE, "bad": "предназначены",
            "fix": "Насосные установки служат для водоснабжения зданий.", "good": "служат", "note": "проверка"}


def answer(n=1, page=1):
    return json.dumps({"findings": [finding(page) for _ in range(n)]}, ensure_ascii=False)


class Fake:
    """Поддельный сервер модели. script — очередь поведений для запросов генерации (по одному на запрос)."""

    def __init__(self):
        self.script = deque()
        self.calls = []                 # тела запросов генерации
        self.closed = []                # клиент разорвал соединение, пока сервер «думал»
        self.show = {"details": {"family": "qwen2", "parameter_size": "32.8B", "quantization_level": "Q4_K_M"},
                     "model_info": {"qwen2.context_length": 131072}, "parameters": ""}
        self.ps = [{"name": "qwen2.5:32b", "size": 20 * 2 ** 30, "size_vram": 20 * 2 ** 30}]
        self.loads = []
        self.load_error = None
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code, obj, headers=None):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": "qwen2.5:32b"}, {"name": "llama3.1:8b"}]})
                if self.path == "/api/ps":
                    return self._json(200, {"models": fake.ps})
                self._json(404, {"error": "not found"})

            def _wait_or_close(self, sec):
                """Ждать sec секунд; True — клиент разорвал соединение раньше."""
                end = time.time() + sec
                while time.time() < end:
                    r, _, _ = select.select([self.connection], [], [], 0.05)
                    if r:
                        try:
                            if self.connection.recv(1, socket.MSG_PEEK) == b"":
                                fake.closed.append(time.time())
                                return True
                        except OSError:
                            fake.closed.append(time.time())
                            return True
                return False

            def _stream(self, ctype, lines, gap=0.0):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for ln in lines:
                    data = (ln + "\n").encode()
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                    self.wfile.flush()
                    if gap:
                        time.sleep(gap)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/show":
                    if body.get("model") != "qwen2.5:32b":
                        return self._json(404, {"error": f"model '{body.get('model')}' not found"})
                    return self._json(200, fake.show)
                if self.path == "/api/chat" and body.get("messages") == []:      # загрузка модели в память
                    fake.loads.append(body)
                    if fake.load_error:
                        return self._json(500, {"error": fake.load_error})
                    fake.ps = [{"name": body["model"], "size": 20 * 2 ** 30, "size_vram": 12 * 2 ** 30,
                                "context_length": body["options"]["num_ctx"]}]
                    return self._json(200, {"done": True, "done_reason": "load", "message": {"content": ""}})
                fake.calls.append(body)
                act = fake.script.popleft() if fake.script else ("ok", answer())
                kind = act[0]
                if kind == "status":
                    return self._json(act[1], act[2], act[3] if len(act) > 3 else None)
                if kind == "hang":                       # нет ни заголовков, ни данных
                    self._wait_or_close(act[1])
                    self.close_connection = True
                    return
                if kind == "html":
                    b = b"<html><body>login</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                    return
                text = act[1]
                finish = act[2] if len(act) > 2 else None
                if "/api/chat" in self.path:             # Ollama: NDJSON
                    parts = [json.dumps({"message": {"content": text[i:i + 40]}, "done": False})
                             for i in range(0, len(text), 40)]
                    parts.append(json.dumps({"done": True, "done_reason": finish or "stop",
                                             "prompt_eval_count": act[3] if len(act) > 3 else 1000, "eval_count": 50,
                                             "load_duration": 2e9, "prompt_eval_duration": 3e9}))
                    if kind == "stall":
                        self.send_response(200)
                        self.send_header("Content-Type", "application/x-ndjson")
                        self.send_header("Transfer-Encoding", "chunked")
                        self.end_headers()
                        data = (parts[0] + "\n").encode()
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                        self.wfile.flush()
                        self._wait_or_close(act[2])
                        self.close_connection = True
                        return
                    return self._stream("application/x-ndjson", parts)
                if "/v1/messages" in self.path:          # Anthropic: SSE
                    ev = [f"data: {json.dumps({'type': 'message_start', 'message': {'usage': {'input_tokens': 900, 'cache_read_input_tokens': 800}}})}"]
                    ev += [f"data: {json.dumps({'type': 'content_block_delta', 'delta': {'text': text[i:i + 30]}})}"
                           for i in range(0, len(text), 30)]
                    ev.append(f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': finish or 'end_turn'}, 'usage': {'output_tokens': 77}})}")
                    ev.append("data: " + json.dumps({"type": "message_stop"}))
                    return self._stream("text/event-stream", ev)
                ev = [f"data: {json.dumps({'choices': [{'delta': {'content': text[i:i + 30]}}]})}"
                      for i in range(0, len(text), 30)]
                ev.append(f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': finish or 'stop'}]})}")
                ev.append("data: [DONE]")
                return self._stream("text/event-stream", ev)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()


@pytest.fixture()
def fake(monkeypatch):
    monkeypatch.delenv("KORREKTOR_AI_MOCK", raising=False)
    monkeypatch.setattr(llm, "BACKOFF", 0.05)
    f = Fake()
    yield f
    f.stop()


def dump(pages=4):
    return {"pages_count": pages, "lines": [{"pdf_page": i, "lines": [{"text": QUOTE}, {"text": f"Страница {i} текста."}],
                                             "head": [], "foot": []} for i in range(1, pages + 1)]}


def settings(fake, prov="openai", **kw):
    st = {"ai_provider": prov, "ai_base_url": fake.url + ("/v1" if prov == "openai" else ""), "ai_model":
          "qwen2.5:32b" if prov == "ollama" else "test-model", "ai_api_key": "k", "ai_timeout": 3, "ai_stall_timeout": 1.5,
          "ai_heartbeat": 1, "ai_retries": 1, "ai_parallel": 1, "ai_pages_per_request": 2, "ai_max_tokens": 1000,
          "ai_quote_check": "fix_page"}
    st.update(kw)
    return st


def go(st, d=None, **kw):
    d = d or dump()
    lines, rep = [], {}
    out = llm.run(d, PageIndex(d), st, "", lines.append, report=rep, **kw)
    return out, rep, lines


def test_openai_stream_ok(fake):
    out, rep, lines = go(settings(fake))
    assert len(out) == 2 and out[0]["rule"] == "ai" and out[0]["good"] == "служат"
    assert rep["ok"] == 2 and rep["requests"] == 2 and not rep["failed"]
    assert fake.calls[0]["stream"] is True and fake.calls[0]["max_tokens"] == 1000
    assert any("ИИ ✓ часть 1/2" in ln for ln in lines) and any("итог" in ln for ln in lines)


def test_anthropic_stream_usage(fake):
    out, rep, lines = go(settings(fake, "anthropic", ai_base_url=fake.url))
    assert len(out) == 2 and rep["tokens_in"] == 3400 and rep["tokens_out"] == 154
    assert fake.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert any("из кэша 800" in ln for ln in lines)


def test_ollama_context_once_and_sequential(fake):
    out, rep, lines = go(settings(fake, "ollama", ai_parallel=3, ai_keep_alive="-1"))
    assert len(out) == 2 and rep["parallel"] == 1                       # Ollama — по одному
    ctxs = {c["options"]["num_ctx"] for c in fake.calls}
    assert len(ctxs) == 1 and ctxs.pop() >= 32768                        # один размер на документ, промпт помещается
    assert fake.calls[0]["keep_alive"] == -1 and fake.calls[0]["format"] == "json"
    assert any("контекст Ollama (num_ctx)" in ln for ln in lines)
    assert any("загрузка модели 2,0 с" in ln for ln in lines)


def test_ollama_model_limit_warning(fake):
    fake.show["model_info"] = {"qwen2.context_length": 8192}
    out, rep, lines = go(settings(fake, "ollama"))
    assert all(c["options"]["num_ctx"] == 8192 for c in fake.calls)
    assert any("предел модели 8 192" in ln for ln in lines)


def test_ollama_model_missing_is_fatal_before_requests(fake):
    out, rep, lines = go(settings(fake, "ollama", ai_model="qwen2.5:7b"))
    assert out == [] and fake.calls == [] and rep["failed"]
    assert any("ollama pull qwen2.5:7b" in ln for ln in lines) and any("есть похожие: qwen2.5:32b" in ln for ln in lines)


def test_first_response_timeout_kills_connection_and_retries(fake):
    fake.script.extend([("hang", 10), ("ok", answer())])
    t = time.time()
    out, rep, lines = go(settings(fake, ai_timeout=1.5), dump(2))
    assert len(out) == 1 and rep["retries"] == 1 and rep["ok"] == 1
    assert fake.closed and fake.closed[0] - t < 4                        # сервер увидел разрыв сразу, а не через 10 с
    assert any("не начала отвечать за 1,5 с" in ln for ln in lines)
    assert any("ждем начала ответа" in ln for ln in lines)               # записи о ходе ожидания


def test_stall_timeout(fake):
    fake.script.extend([("stall", answer(), 10), ("ok", answer())])
    out, rep, lines = go(settings(fake, "ollama", ai_stall_timeout=1), dump(2))
    assert len(out) == 1 and rep["retries"] == 1
    assert any("перестала присылать данные" in ln for ln in lines)


def test_rate_limit_retry_after(fake):
    fake.script.extend([("status", 429, {"error": {"message": "slow down"}}, {"Retry-After": "0.1"}), ("ok", answer())])
    out, rep, lines = go(settings(fake), dump(2))
    assert len(out) == 1 and rep["retries"] == 1
    assert any("повтор через 0,1 с" in ln for ln in lines)


def test_auth_error_is_fatal(fake):
    fake.script.append(("status", 401, {"error": {"message": "invalid x-api-key"}}))
    out, rep, lines = go(settings(fake), dump(6))
    assert out == [] and len(fake.calls) == 1                            # остальные части не отправлялись
    assert len(rep["failed"]) == 1 and len(rep["skipped"]) == 2
    assert any("⛔" in ln for ln in lines)
    gap = llm.not_checked(rep)
    assert "стр. 1–2 (доступ)" in gap and "стр. 3–4 (пропущено)" in gap


def test_quota_429_not_retried(fake):
    fake.script.append(("status", 429, {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota"}}))
    out, rep, lines = go(settings(fake), dump(2))
    assert len(fake.calls) == 1 and rep["failed"][0]["kind"] == "quota"


def test_truncated_answer_split(fake):
    fake.script.extend([("ok", '{"findings": [' + json.dumps(finding(), ensure_ascii=False) + ', {"page": 2, "src": "обры', "length"),
                        ("ok", answer(1, 1)), ("ok", answer(1, 2))])
    out, rep, lines = go(settings(fake), dump(2))
    assert rep["splits"] == 1 and len(out) == 2 and len(fake.calls) == 3
    assert any("делю на две части" in ln for ln in lines)


def test_context_error_split(fake):
    fake.script.extend([("status", 400, {"error": {"message": "This model's maximum context length is 8192 tokens"}}),
                        ("ok", answer(1, 1)), ("ok", answer(1, 2))])
    out, rep, lines = go(settings(fake), dump(2))
    assert rep["splits"] == 1 and len(out) == 2


def test_adapt_max_completion_tokens_and_limit(fake):
    fake.script.extend([
        ("status", 400, {"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this model. "
                                              "Use 'max_completion_tokens' instead."}}),
        ("status", 400, {"error": {"message": "max_completion_tokens is too large: 1000. This model supports at most "
                                              "800 completion tokens"}}),
        ("ok", answer())])
    out, rep, lines = go(settings(fake), dump(2))
    assert len(out) == 1 and rep["retries"] == 0
    assert "max_completion_tokens" in fake.calls[1] and fake.calls[2]["max_completion_tokens"] == 800
    assert sum("ИИ ⚙" in ln for ln in lines) == 2


def test_not_json_then_ok(fake):
    fake.script.extend([("ok", "Извините, я не могу"), ("ok", answer())])
    out, rep, lines = go(settings(fake), dump(2))
    assert len(out) == 1 and rep["retries"] == 1 and any("в ответе нет JSON" in ln for ln in lines)


def test_html_page_is_config_error(fake):
    fake.script.append(("html",))
    out, rep, lines = go(settings(fake), dump(4))
    assert out == [] and len(fake.calls) == 1 and rep["failed"][0]["kind"] == "config"


def test_network_down(monkeypatch):
    monkeypatch.delenv("KORREKTOR_AI_MOCK", raising=False)
    monkeypatch.setattr(llm, "BACKOFF", 0.01)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                            # порт свободен — соединение отклоняется
    st = {"ai_provider": "openai", "ai_base_url": f"http://127.0.0.1:{port}/v1", "ai_model": "m", "ai_retries": 1,
          "ai_pages_per_request": 1, "ai_timeout": 3}
    out, rep, lines = go(st, dump(5))
    assert out == [] and any("сервер модели недоступен" in ln for ln in lines)
    assert rep["requests"] <= 4                                          # после трех сетевых ошибок подряд — стоп


def test_cancel_during_request(fake):
    fake.script.append(("hang", 20))
    flag = {"stop": False}
    threading.Timer(1.0, lambda: flag.__setitem__("stop", True)).start()
    t = time.time()
    with pytest.raises(Cancelled):
        go(settings(fake, ai_timeout=30), dump(2), should_stop=lambda: flag["stop"])
    assert time.time() - t < 4
    for _ in range(40):                                                  # сервер замечает разрыв за доли секунды
        if fake.closed:
            break
        time.sleep(0.05)
    assert fake.closed and fake.closed[0] - t < 4


def test_total_deadline_skips_rest(fake, monkeypatch):
    fake.script.extend([("hang", 2.5)] * 3)
    st = settings(fake, ai_timeout=1, ai_retries=0, ai_total_timeout=0.02)   # 1,2 с на весь проход
    out, rep, lines = go(st, dump(6))
    assert rep["skipped"] and any("Общий лимит прохода ИИ" in b["reason"] for b in rep["skipped"])


def test_status_callback_and_heartbeat(fake):
    fake.script.extend([("hang", 2.5), ("ok", answer())])
    seen = []
    out, rep, lines = go(settings(fake, ai_timeout=2), dump(2), status=seen.append)
    assert any("ждем начала ответа" in x for x in seen)


def test_diagnose_quick_and_full(fake, monkeypatch, tmp_path):
    ok, lines = llm.diagnose(settings(fake, "ollama"))
    assert ok and any("модель найдена" in ln for ln in lines) and any("JSON — верно" in ln for ln in lines)
    fake.script.append(("ok", answer(), "stop", 70000))
    ok, lines = llm.diagnose(settings(fake, "ollama"), full=True)
    assert ok and any("Мастер-промпт" in ln for ln in lines)
    ok, lines = llm.diagnose({"ai_provider": "none"})
    assert not ok


def test_classify_errors():
    e = llm._http_error(500, b'{"error":"model requires more system memory (40 GiB) than is available (16 GiB)"}', {}, {"ai_provider": "ollama"})
    assert e.kind == "memory" and e.retry
    e = llm._http_error(404, b'{"error":"model not found"}', {}, {"ai_provider": "openai", "ai_base_url": "http://h:8000"})
    assert e.kind == "notfound" and "/v1" in str(e)
    e = llm._http_error(400, b'{"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too low"}}', {}, {"ai_provider": "anthropic"})
    assert e.kind == "quota"
    e = llm._http_error(400, b'{"error":{"message":"max_tokens: 64000 > 32000, which is the maximum allowed number of output tokens"}}', {}, {"ai_provider": "anthropic"})
    assert e.kind == "maxtokens" and e.limit == 32000
    e = llm._http_error(503, b"<html>Bad gateway</html>", {"retry-after": "7"}, {"ai_provider": "openai"})
    assert e.kind == "server" and e.wait == 7.0
    assert llm.endpoint({"ai_provider": "anthropic", "ai_base_url": "https://api.anthropic.com/v1"}) == "https://api.anthropic.com/v1/messages"
    assert llm.endpoint({"ai_provider": "ollama", "ai_base_url": "http://h:11434/v1/"}) == "http://h:11434/api/chat"
    assert llm._keep_alive("-1") == -1 and llm._keep_alive("30m") == "30m"


def test_ollama_preload_with_same_context(fake):
    fake.ps = []                                                         # модель не загружена
    out, rep, lines = go(settings(fake, "ollama"))
    assert len(fake.loads) == 1 and len(out) == 2
    assert fake.loads[0]["options"]["num_ctx"] == fake.calls[0]["options"]["num_ctx"]
    assert any("модель загружена в память" in ln and "в видеопамяти 60%" in ln for ln in lines)
    assert any("часть модели работает на процессоре" in ln for ln in lines)


def test_ollama_preload_memory_error_is_fatal(fake):
    fake.ps = []
    fake.load_error = "model requires more system memory (40.0 GiB) than is available (15.2 GiB)"
    out, rep, lines = go(settings(fake, "ollama"))
    assert out == [] and fake.calls == [] and "памяти" in rep["failed"][0]["reason"]
