# Корректор

Веб-сервис корректорской вычитки PDF-документов компании «ВОДОКОМФОРТ» по мастер-промпту корректора v5.7.
Загрузите PDF — получите отчет DOCX (альбомная таблица «как в документе → как правильно»), PDF с пометками,
XLSX и JSON. Отчет собирается и проверяется кодом: 25 машинных требований оформления и полноты.

Возможности: прием PDF и Word; правки прямо в исходном DOCX (рецензирование); аналитика и тренды с целями
качества; сравнение версий; стандарт оформления (шрифты, кегли, цвета, поля) и сверка со справочником продукции 1С,
включая таблицы характеристик; контроль разбора и напоминания; задачи в Битрикс24; отчет для руководства в PDF;
роли, LDAP, единый вход (OIDC), двухфакторный вход, разделение по отделам, ограничение по сетям, журнал действий;
уведомления (почта, Telegram, вебхук); резервные копии с проверкой и восстановлением; метрики Prometheus; Docker.

## Установка одной командой

```bash
sudo bash korrektor-1.5.0-installer.sh            # самораспаковывающийся установщик
# или из распакованной папки:
sudo bash scripts/install.sh
```

Через 2–5 минут на экране появятся адрес и пароль. Ключи: `--with-nginx`, `--with-ocr`, `--with-languagetool`,
`--port N`, `--open-firewall` (полный список — `--help`).

- Обновление: `sudo bash korrektor-<версия>-installer.sh --update` или `sudo korrektor update --source …`
- Удаление: `sudo korrektor uninstall` (данные сохраняются) или `sudo korrektor uninstall --purge`
- Управление: `korrektor help`

## Документация

Встроена в сервис (раздел «Документация») и лежит в папке `docs/`:

| Файл | Содержание |
|---|---|
| [docs/index.md](docs/index.md) | обзор и быстрый старт |
| [docs/methodology.md](docs/methodology.md) | методология вычитки |
| [docs/usage.md](docs/usage.md) | работа с сервисом |
| [docs/analytics.md](docs/analytics.md) | аналитика отчетов |
| [docs/trends.md](docs/trends.md) | тренды |
| [docs/users.md](docs/users.md) | пользователи, роли, вход, второй фактор, отделы, сети, журнал |
| [docs/integrations.md](docs/integrations.md) | уведомления, напоминания, отчет для руководства, Битрикс24, метрики |
| [docs/standards.md](docs/standards.md) | стандарт оформления и справочник продукции |
| [docs/settings.md](docs/settings.md) | справочник всех 172 настроек |
| [docs/installation.md](docs/installation.md) | установка, обновление, удаление |
| [docs/administration.md](docs/administration.md) | администрирование и безопасность |
| [docs/api.md](docs/api.md) | REST API |
| [docs/faq.md](docs/faq.md) | вопросы и неполадки |
| docs/Корректор_документация.pdf, .docx | вся документация одним файлом PDF и Word |

## Состав

```
app/            веб-сервис (FastAPI): main.py, admin.py, core.py, users.py, totp.py, oidc.py, notify.py,
                analytics.py, compare.py, control.py, backup.py, report_pdf.py, batch.py, settings_schema.py
app/pipeline/   конвейер проверки: правила, словарь, ИИ, технический контроль, отчеты
app/pipeline/scripts/   extract.py, build.py, verify.py — дословно из мастер-промпта v5.7
app/prompts/    мастер-промпт
app/templates/, app/static/   интерфейс в стиле vodokomfort.ru
tests/          автоматические тесты (python3 -m pytest -q tests/)
docs/           документация, схемы (diagrams/*.mmd), снимки экрана (images/)
scripts/        install.sh, update.sh, uninstall.sh, make_bundle.sh, gen_settings_doc.py
```

## Разработка

```bash
python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt
KORREKTOR_DATA=./data KORREKTOR_ADMIN_PASSWORD=dev uvicorn app.main:app --reload --port 8080
bash scripts/make_bundle.sh      # dist/korrektor-<версия>.tar.gz и установщик
```

Сторонние компоненты: шрифты Roboto и Roboto Condensed (Apache License 2.0), Mermaid (MIT), Chart.js (MIT), PyMuPDF (AGPL-3.0),
python-docx, FastAPI, Hunspell, LanguageTool (LGPL, по желанию), Tesseract (Apache 2.0, по желанию).
