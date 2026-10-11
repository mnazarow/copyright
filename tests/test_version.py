# -*- coding: utf-8 -*-
"""Вывод версии: номер из VERSION, сборка (коммит и дата) из app/BUILD или git, метка файлов для кеша браузера."""
import os
import re
import subprocess

from fastapi.testclient import TestClient

from app import core
from app.main import app

O = {"Origin": "http://testserver"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_version_matches_file():
    assert core.VERSION == open(os.path.join(ROOT, "VERSION")).read().strip()
    assert core.VERSION_FULL.startswith(core.VERSION)


def test_build_label_format():
    assert core.build_label("a382dd4 2026-10-11") == "сборка a382dd4 от 11.10.2026"
    assert core.build_label("a382dd4+ 2026-10-11") == "сборка a382dd4+ от 11.10.2026"
    assert core.build_label("") == ""


def test_health_and_pages_show_version():
    with TestClient(app, base_url="http://testserver") as c:
        h = c.get("/api/health").json()
        assert h["version"] == core.VERSION and h["build"] == core.BUILD
        assert c.post("/login", data={"login": "admin", "password": "тест-пароль"}, headers=O,
                      follow_redirects=False).status_code == 303
        html = c.get("/").text
        assert f"версия {core.VERSION}" in html and f"Версия {core.VERSION}" in html
        if core.BUILD:
            assert core.build_label() in html
        assert re.search(r"мастер-промпт v\d+\.\d+", html)
        v = re.findall(r"/static/css/style\.css\?v=([0-9a-f]+)", html)
        assert v and v[0] != core.VERSION                 # метка файлов, а не номер версии


def test_shell_build_of(tmp_path):
    """build_of из update.sh/install.sh: файл app/BUILD важнее git, без обоих — пусто."""
    funcs = subprocess.run(["sed", "-n", "/^build_of()/,/^}/p;/^ver_full()/,/^}/p", os.path.join(ROOT, "scripts", "update.sh")],
                           capture_output=True, text=True).stdout
    (tmp_path / "b" / "app").mkdir(parents=True)
    (tmp_path / "b" / "app" / "BUILD").write_text("abc1234 2026-10-11\n")
    (tmp_path / "none").mkdir()
    script = funcs + f'''
echo "[$(build_of {tmp_path / "b"})]"
echo "[$(build_of {tmp_path / "none"})]"
echo "[$(ver_full 1.7.0 "$(build_of {tmp_path / "b"})")]"
echo "[$(ver_full 1.7.0 "")]"
'''
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.splitlines()
    assert out == ["[abc1234 2026-10-11]", "[]", "[1.7.0 (сборка abc1234 2026-10-11)]", "[1.7.0]"]
