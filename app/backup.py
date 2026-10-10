# -*- coding: utf-8 -*-
"""Автоматическое резервное копирование по расписанию: архив папки данных (+ korrektor.env) в заданную папку,
например на сетевой диск. Хранится заданное число последних копий."""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import threading
import time

from . import core

ENV_FILE = "/etc/korrektor/korrektor.env"
STATUS = os.path.join(core.DATA, ".backup_status.json")
_running = threading.Lock()
NAME_RE = re.compile(r"^korrektor-(?:auto|upload)-\d{8}-\d{6}(?:-\d{1,3})?\.tgz(?:\.enc)?$")
ANY_NAME_RE = re.compile(r"^korrektor-[\w-]{1,60}\.tgz(?:\.enc)?$")   # копии из командной строки (korrektor backup) тоже
RESTORE_ITEMS = ("jobs", "batches", "settings.json", "custom_prompt.md", "products.json", ".remind.json")


def _unique(d, kind, ext=".tgz"):
    """Имя новой копии; две копии в одну секунду (страховочная перед восстановлением) не затирают друг друга."""
    base = f"korrektor-{kind}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    name = base + ext
    n = 1
    while os.path.exists(os.path.join(d, name)) or os.path.exists(os.path.join(d, name + ".part")):
        name = f"{base}-{n}{ext}"
        n += 1
    return name, os.path.join(d, name)


def password(st=None):
    """Пароль шифрования копий: из настроек или из переменной KORREKTOR_BACKUP_PASSWORD (командная строка)."""
    return os.environ.get("KORREKTOR_BACKUP_PASSWORD") or (st or core.load_settings()).get("backup_password") or ""


class _Plain:
    """Путь к расшифрованной копии на время работы (для незашифрованной — сам файл); временный файл удаляется."""

    def __init__(self, path, st=None):
        self.path, self.st, self.tmp = path, st, None

    def __enter__(self):
        from . import crypt
        if not crypt.is_encrypted(self.path):
            return self.path
        old = os.umask(0o077)
        try:
            # расшифрованная копия — на локальный диск сервера (папка данных; «.restore-» не попадает в копии),
            # а не рядом с зашифрованной: сетевой диск с копиями не должен видеть открытые данные
            fd, self.tmp = tempfile.mkstemp(prefix=".restore-decrypt-", suffix=".tgz", dir=core.DATA)
            os.close(fd)
            crypt.decrypt_file(self.path, self.tmp, password(self.st))
        except Exception:
            if self.tmp and os.path.exists(self.tmp):
                os.remove(self.tmp)
            raise
        finally:
            os.umask(old)
        return self.tmp

    def __exit__(self, *a):
        if self.tmp and os.path.exists(self.tmp):
            os.remove(self.tmp)


def target_dir(st):
    return (st.get("backup_dir") or "/var/backups/korrektor").rstrip("/")


def status():
    try:
        return json.load(open(STATUS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_status(**kw):
    s = status()
    s.update(kw)
    core.write_json_atomic(STATUS, s, indent=1)


def run(st=None, reason="расписание", rotate=True):
    """Создает архив. → путь к архиву. Исключение — при ошибке (текст попадает в статус)."""
    st = st or core.load_settings()
    if not _running.acquire(blocking=False):
        raise RuntimeError("резервное копирование уже выполняется")
    try:
        d = target_dir(st)
        os.makedirs(d, exist_ok=True)
        pw = password(st)
        name, path = _unique(d, "auto", ".tgz.enc" if pw else ".tgz")
        tmp = path + ".part"
        pages = bool(st.get("backup_include_pages"))
        data_real = os.path.realpath(core.DATA)
        dest_real = os.path.realpath(d)

        def flt(ti):
            p = ti.name
            if not pages and re.search(r"/pages(/|$)", p):
                return None
            if p.endswith((".tmp", ".part", "korrektor.db", "korrektor.db-journal", "korrektor.db-wal", "korrektor.db-shm",
                           "/.secret", "/.admin_totp.json", "/.state.lock")) or re.search(r"/\.[^/]*\.lock$", p) \
                    or "/__pycache__" in p or "/.restore-" in p:   # ключ подписи сеансов в копию не кладется
                return None                             # базу добавляем отдельно — согласованной копией
            full = os.path.realpath(os.path.join(os.path.dirname(data_real), p))
            if full == dest_real or full.startswith(dest_real + os.sep):
                return None                             # папка копий внутри данных — не копировать саму себя
            return ti
        old = os.umask(0o077)
        dbcopy = tmp + ".db"
        try:
            src = sqlite3.connect(core.DB_FILE, timeout=30)
            dst = sqlite3.connect(dbcopy)
            with dst:
                src.backup(dst)                         # согласованный снимок базы даже во время записи
            src.close()
            dst.close()
            arc = os.path.basename(data_real) or "data"
            with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
                tar.add(core.DATA, arcname=arc, filter=flt)
                tar.add(dbcopy, arcname=f"{arc}/korrektor.db")
                if os.access(ENV_FILE, os.R_OK):
                    _add_env(tar, encrypted=bool(pw))
        finally:
            os.umask(old)
            if os.path.exists(dbcopy):
                os.remove(dbcopy)
        if pw:
            from . import crypt
            old = os.umask(0o077)
            try:
                crypt.encrypt_file(tmp, path + ".encpart", pw)   # AES-256-GCM, ключ из пароля (scrypt)
                os.replace(path + ".encpart", path)
            finally:
                os.umask(old)
                if os.path.exists(path + ".encpart"):
                    os.remove(path + ".encpart")
            os.remove(tmp)
        else:
            os.replace(tmp, path)
        size = os.path.getsize(path)
        digest = sha256(path)
        with open(path + ".sha256", "w") as fh:
            fh.write(f"{digest}  {name}\n")
        os.chmod(path + ".sha256", 0o640)
        keep = int(st.get("backup_keep") or 14)
        files = sorted((f for f in os.listdir(d) if f.startswith("korrektor-auto-") and NAME_RE.match(f)),
                       key=lambda f: os.path.getmtime(os.path.join(d, f)))
        for f in (files[:-keep] if keep > 0 and rotate else []):
            for x in (f, f + ".sha256"):
                try:
                    os.remove(os.path.join(d, x))
                except OSError:
                    pass
        _save_status(last=dt.datetime.now().isoformat(timespec="seconds"), file=path, size=size, error="",
                     reason=reason, day=dt.date.today().isoformat())
        core.log.info("Резервная копия: %s (%s байт)", path, size)
        return path
    except Exception as e:
        _save_status(error=f"{dt.datetime.now():%d.%m %H:%M}: {e}", day=dt.date.today().isoformat())
        core.log.exception("резервное копирование")
        raise
    finally:
        try:
            if "tmp" in locals() and os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        _running.release()


SECRET_ENV = ("KORREKTOR_SECRET", "KORREKTOR_ADMIN_PASSWORD")


def _add_env(tar, encrypted):
    """korrektor.env в копию: в зашифрованную — целиком; в незашифрованную — без ключа подписи сеансов и пароля
    администратора (при восстановлении с --with-env они берутся из текущего файла)."""
    import io
    text = open(ENV_FILE, encoding="utf-8").read()
    if not encrypted:
        text = "\n".join(f"# {ln.split('=', 1)[0]}=… (не хранится в незашифрованной копии)"
                          if ln.split("=", 1)[0].strip() in SECRET_ENV else ln for ln in text.splitlines()) + "\n"
    data = text.encode("utf-8")
    ti = tarfile.TarInfo("etc/korrektor.env")
    ti.size, ti.mode, ti.mtime = len(data), 0o600, int(time.time())
    tar.addfile(ti, io.BytesIO(data))


def listing(st=None):
    st = st or core.load_settings()
    d = target_dir(st)
    try:
        files = sorted((f for f in os.listdir(d) if ANY_NAME_RE.match(f)), key=lambda f: os.path.getmtime(os.path.join(d, f)),
                       reverse=True)
    except OSError:
        return []
    checks = status().get("checks", {})
    return [{"name": f, "size": os.path.getsize(os.path.join(d, f)), "check": checks.get(f),
             "time": dt.datetime.fromtimestamp(os.path.getmtime(os.path.join(d, f))).isoformat(timespec="minutes")}
            for f in files]


def path_of(name, st=None):
    """Полный путь копии по имени из папки копий. None — имя недопустимо или файла нет."""
    if not ANY_NAME_RE.match(name or ""):
        return None
    p = os.path.join(target_dir(st or core.load_settings()), name)
    return p if os.path.isfile(p) else None


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _root(names):
    """Папка данных внутри архива: «korrektor/…» (автокопия) или «var/lib/korrektor/…» (korrektor backup)."""
    roots = sorted({n[:-len("/korrektor.db")] for n in names if n.endswith("/korrektor.db")}, key=len)
    return roots[0] if roots else None


def _unsafe(m):
    n = m.name
    return n.startswith("/") or ".." in n.split("/") or "\\" in n


def _in_scope(m, root):
    """Восстанавливаются только обычные файлы и папки данных; ссылки и устройства из архива пропускаются."""
    if _unsafe(m) or not m.name.startswith(root + "/"):
        return False
    if m.islnk():                                     # жесткая ссылка на файл того же архива (tar хранит так повторы)
        ln = m.linkname
        return ln.startswith(root + "/") and ".." not in ln.split("/")
    return m.isfile() or m.isdir()


def verify(path, st=None):
    """Проверка целостности копии без восстановления (зашифрованная — расшифровывается во временный файл).
    → словарь: ok, errors, сведения."""
    from . import crypt
    res = {"ok": False, "errors": [], "file": os.path.basename(path), "size": os.path.getsize(path),
           "checked": dt.datetime.now().isoformat(timespec="seconds"), "encrypted": crypt.is_encrypted(path)}
    side = path + ".sha256"
    try:
        if os.path.exists(side):
            want = (open(side).read().split() or [""])[0]
            res["sha256"] = "совпадает" if sha256(path) == want else "НЕ совпадает"
            if res["sha256"] != "совпадает":
                res["errors"].append("контрольная сумма не совпадает — файл поврежден или изменен")
        if not res["errors"]:
            with _Plain(path, st) as plain:
                _verify_tar(plain, res)
    except crypt.CryptoError as e:
        res["errors"].append(str(e))
    except OSError as e:
        res["errors"].append(f"файл копии не читается: {e}")
    res["ok"] = not res["errors"]
    try:
        checks = status().get("checks", {})
        checks[res["file"]] = {"ok": res["ok"], "time": res["checked"], "errors": res["errors"][:3]}
        _save_status(checks=dict(list(checks.items())[-50:]))
    except Exception:  # noqa
        pass
    return res


def _verify_tar(path, res):
    try:
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()             # читает весь архив: проверяет сжатие и CRC
            names = [m.name for m in members]
            root = _root(names)
            if not root:
                res["errors"].append("в архиве нет базы korrektor.db — это не резервная копия Корректора")
                return
            res["root"] = root
            bad = [m.name for m in members if _unsafe(m)]
            if bad:
                res["errors"].append(f"в архиве есть недопустимые пути: {', '.join(bad[:3])}")
            res["jobs"] = len({n.split("/")[len(root.split("/")) + 1] for n in names
                               if n.startswith(root + "/jobs/") and n.count("/") > root.count("/") + 2})
            res["pages_included"] = any("/pages/" in n for n in names)
            res["env"] = "etc/korrektor.env" in names
            with tempfile.TemporaryDirectory() as tmp:
                db = tar.extractfile(root + "/korrektor.db")
                if db is None:
                    raise tarfile.TarError("korrektor.db в архиве — не файл")
                dbp = os.path.join(tmp, "k.db")
                with open(dbp, "wb") as fh:
                    shutil.copyfileobj(db, fh)
                con = sqlite3.connect(dbp)
                try:
                    chk = con.execute("PRAGMA integrity_check").fetchone()[0]
                    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    res["db_jobs"] = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] if "jobs" in tables else 0
                    res["db_users"] = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] if "users" in tables else 0
                finally:
                    con.close()
                res["db"] = chk
                if chk != "ok":
                    res["errors"].append(f"база повреждена: {chk[:200]}")
                if "jobs" not in tables:
                    res["errors"].append("в базе нет таблицы заданий")
            if root + "/settings.json" in names:
                try:
                    json.load(tar.extractfile(root + "/settings.json"))
                except ValueError:
                    res["errors"].append("файл настроек поврежден")
    except (tarfile.TarError, OSError, EOFError, sqlite3.Error) as e:
        res["errors"].append(f"архив не читается: {e}")


def _wait_idle(timeout):
    end = time.time() + timeout
    while time.time() < end:
        with core.db() as c:
            fresh = (dt.datetime.now() - dt.timedelta(minutes=10)).isoformat(timespec="seconds")
            running = c.execute("SELECT COUNT(*) FROM jobs WHERE status='running' OR (status='uploading' AND created>?)",
                                (fresh,)).fetchone()[0]
        if not running and not core.BUSY:
            return True
        time.sleep(1)
    return False


_restoring = threading.Lock()


def _db_copy(src_path, dst_path):
    src = sqlite3.connect(src_path, timeout=30)
    dst = sqlite3.connect(dst_path, timeout=30)
    try:
        with dst:
            src.backup(dst)                         # замена «на лету»: открытые соединения видят новые данные
    finally:
        src.close()
        dst.close()


def restore(path, live=True, wait=120, safety_copy=True):
    """Восстановление данных из копии: база, задания, настройки, мастер-промпт, справочники.
    Перед восстановлением — проверка целостности и (live) страховочная копия текущего состояния.
    При любой ошибке на середине все возвращается как было. Ключ подписи сеансов (.secret),
    второй фактор встроенного администратора и состояние копий не трогаются. → словарь с итогом."""
    from . import crypt
    if not _restoring.acquire(blocking=False):
        raise RuntimeError("восстановление уже выполняется")
    try:
        side = path + ".sha256"
        if os.path.exists(side) and sha256(path) != (open(side).read().split() or [""])[0]:
            raise ValueError("копия не прошла проверку: контрольная сумма не совпадает — файл поврежден или изменен")
        try:
            with _Plain(path) as plain:             # зашифрованная копия расшифровывается один раз
                res = _restore(plain, live, wait, safety_copy)
        except crypt.CryptoError as e:
            raise ValueError(f"копия не расшифрована: {e}")
        res["file"] = os.path.basename(path)
        return res
    finally:
        _restoring.release()


def _restore(path, live, wait, safety_copy):
    chk = verify(path)
    if not chk["ok"]:
        raise ValueError("копия не прошла проверку: " + "; ".join(chk["errors"]))
    root = chk["root"]
    safety = None
    if live:
        with core._lock:
            core.PAUSED.set()                       # очередь стоит, изменяющие запросы отклоняются (main.guard)
    try:
        if live and not _wait_idle(wait):
            raise RuntimeError("идет проверка или загрузка документа — дождитесь окончания и повторите")
        if safety_copy:
            safety = run(reason="перед восстановлением", rotate=False)   # не удалить восстанавливаемую копию
        stage = tempfile.mkdtemp(prefix=".restore-", dir=core.DATA)
        old = os.path.join(stage, "_old")
        moved, db_replaced = [], False
        os.makedirs(old)
        try:
            with tarfile.open(path, "r:gz") as tar:
                members = [m for m in tar.getmembers() if _in_scope(m, root)]
                for m in members:
                    m.mode = (m.mode | 0o600) & 0o770 if m.isfile() else 0o770
                    m.uid = m.gid = os.getuid() if hasattr(os, "getuid") else 0
                kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
                tar.extractall(os.path.join(stage, "new"), members=members, **kw)
            src = os.path.join(stage, "new", root)
            if os.path.exists(core.DB_FILE):
                _db_copy(core.DB_FILE, os.path.join(old, "korrektor.db"))   # для отката
            for item in RESTORE_ITEMS:              # сначала файлы…
                cur, new = os.path.join(core.DATA, item), os.path.join(src, item)
                had = os.path.exists(cur)
                if had:
                    os.replace(cur, os.path.join(old, item))
                moved.append((item, had))
                if os.path.exists(new):
                    os.replace(new, cur)
            _db_copy(os.path.join(src, "korrektor.db"), core.DB_FILE)   # …затем база
            db_replaced = True
            os.makedirs(core.JOBS, exist_ok=True)
            os.makedirs(core.BATCHES, exist_ok=True)
        except BaseException:
            core.log.exception("восстановление прервано — возвращаю прежние данные")
            for item, had in reversed(moved):       # откат: прежние файлы на место
                cur = os.path.join(core.DATA, item)
                try:
                    if os.path.isdir(cur) and not os.path.islink(cur):
                        shutil.rmtree(cur)
                    elif os.path.exists(cur):
                        os.remove(cur)
                    if had:
                        os.replace(os.path.join(old, item), cur)
                except OSError:
                    core.log.exception("откат %s", item)
            if db_replaced and os.path.exists(os.path.join(old, "korrektor.db")):
                _db_copy(os.path.join(old, "korrektor.db"), core.DB_FILE)
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        core.init_db()                              # миграции, если копия от старой версии
        core.load_settings()                        # миграция настроек старой версии
        try:
            from . import analytics
            analytics.decisions_changed()
        except Exception:  # noqa
            pass
        with core.db() as c:
            c.execute("UPDATE jobs SET status='queued', progress=0 WHERE status='running'")
            c.execute("UPDATE jobs SET status='failed', error='загрузка прервана восстановлением из копии' "
                      "WHERE status='uploading'")
        core.log.warning("Данные восстановлены из %s", path)
        return {"file": os.path.basename(path), "jobs": chk.get("db_jobs"), "users": chk.get("db_users"),
                "safety": os.path.basename(safety) if safety else "", "pages": chk.get("pages_included")}
    finally:
        if live:
            core.PAUSED.clear()
            core.kick()


def save_upload(src):
    """Загруженный администратором архив кладется в папку копий под именем korrektor-upload-…tgz."""
    d = target_dir(core.load_settings())
    os.makedirs(d, exist_ok=True)
    head = src.read(len(b"KORREKTOR-ENC1\n"))
    src.seek(0)
    name, path = _unique(d, "upload", ".tgz.enc" if head == b"KORREKTOR-ENC1\n" else ".tgz")
    old = os.umask(0o077)
    try:
        total = 0
        with open(path + ".part", "wb") as fh:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                total += len(chunk)
                if total > MAX_UPLOAD:
                    raise ValueError("архив больше 20 ГБ")
                fh.write(chunk)
        os.replace(path + ".part", path)
    except BaseException:
        if os.path.exists(path + ".part"):
            os.remove(path + ".part")
        raise
    finally:
        os.umask(old)
    return path


MAX_UPLOAD = 20 * 1024 ** 3


def _cli():
    """python -m app.backup verify|restore ФАЙЛ — для korrektor restore (сервис остановлен)."""
    if len(sys.argv) == 2 and sys.argv[1] == "backup":
        print("Резервная копия:", run(reason="командная строка"))
        return
    if len(sys.argv) == 4 and sys.argv[1] == "decrypt":           # для korrektor restore --with-env
        from . import crypt
        try:
            crypt.decrypt_file(sys.argv[2], sys.argv[3], password())
        except crypt.CryptoError as e:
            print("Ошибка:", e)
            sys.exit(1)
        return
    if len(sys.argv) < 3 or sys.argv[1] not in ("verify", "restore"):
        print("использование: python -m app.backup backup | verify ФАЙЛ | restore ФАЙЛ | decrypt ФАЙЛ.enc ФАЙЛ.tgz")
        sys.exit(2)
    path = os.path.abspath(sys.argv[2])
    if sys.argv[1] == "verify":
        r = verify(path)
        for k, v in r.items():
            print(f"{k}: {v}")
        sys.exit(0 if r["ok"] else 1)
    try:
        r = restore(path, live=False, safety_copy="--no-safety" not in sys.argv)
    except Exception as e:  # noqa
        print("Ошибка:", e)
        sys.exit(1)
    print(f"Восстановлено: заданий {r['jobs']}, пользователей {r['users']}"
          + (f"; страховочная копия текущих данных: {r['safety']}" if r["safety"] else ""))



def loop():
    while True:
        try:
            st = core.load_settings()
            if st.get("backup_enabled"):
                now = dt.datetime.now()
                if now.hour >= int(st.get("backup_hour", 2) or 0) and status().get("day") != now.date().isoformat():
                    try:
                        run(st)
                    except Exception:  # noqa — статус и журнал уже записаны
                        pass
        except Exception:  # noqa
            core.log.exception("расписание резервного копирования")
        time.sleep(300)


if __name__ == "__main__":
    _cli()
