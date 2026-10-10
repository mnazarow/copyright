# -*- coding: utf-8 -*-
"""Процесс обработки одного задания или сводки пакета (запускается веб-сервером, см. core._process):

    python -m app.worker job <номер>
    python -m app.worker batch <пакет>

Разбор PDF, картинки страниц, LibreOffice и проход ИИ выполняются здесь, а не в процессе веб-сервера:
аварийное завершение библиотеки на «трудном» PDF или нехватка памяти завершают только этот процесс.

Сигналы: SIGUSR1 — «Остановить» (задание завершает этап и записывает итог); SIGTERM — остановка службы
(процесс и его дочерние процессы завершаются сразу с кодом 75, задание возвращается в очередь и продолжится
после запуска службы). Если веб-сервер завершился аварийно, процесс задания завершается сам."""
import faulthandler
import os
import signal
import sys
import threading
import time


EXIT_INTERRUPTED = 75                                # см. core.EXIT_INTERRUPTED


def _interrupted(*_):
    """Остановка службы или аварийное завершение веб-сервера: завершить свои дочерние процессы (extract.py,
    hunspell, LibreOffice) и выйти с кодом «прервано» — веб-сервер вернет задание в очередь."""
    try:
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        except ValueError:                           # вызов не из главного потока (наблюдение за родителем)
            pass
        from .pipeline import office
        office.kill_running()
        if os.getpgid(0) == os.getpid():             # процесс — лидер своей группы (так запускает core._spawn)
            os.killpg(os.getpid(), signal.SIGTERM)
    except Exception:  # noqa
        pass
    finally:
        os._exit(EXIT_INTERRUPTED)


def _watch_parent():
    """Веб-сервер завершился (аварийно или его убили) — задание не должно продолжаться без присмотра: после
    перезапуска служба сама снова поставит его в очередь."""
    try:
        parent = int(os.environ.get("KORREKTOR_PARENT_PID") or 0)
    except ValueError:
        parent = 0
    if not parent:
        return
    try:                                             # Linux: сигнал при завершении родителя
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except Exception:  # noqa
        pass

    def loop():
        while True:
            if os.getppid() != parent:
                _interrupted()
            time.sleep(2)
    if os.getppid() != parent:
        _interrupted()
    threading.Thread(target=loop, daemon=True, name="parent-watch").start()


def main(argv):
    if len(argv) != 2 or argv[0] not in ("job", "batch"):
        print(__doc__, file=sys.stderr)
        return 2
    faulthandler.enable()                            # при аварии библиотеки — стек всех потоков в журнал службы
    try:                                             # при нехватке памяти ядро завершит процесс задания, а не веб-сервер
        with open("/proc/self/oom_score_adj", "w") as fh:          # (наследуют и extract.py, LibreOffice)
            fh.write("500")
    except OSError:
        pass
    signal.signal(signal.SIGTERM, _interrupted)
    _watch_parent()
    from . import core
    core.apply_log_level()
    if argv[0] == "job":
        jid = int(argv[1])

        def stop(*_):
            core.CANCEL.add(jid)                     # обработчик задания проверяет CANCEL на каждом этапе
        signal.signal(signal.SIGUSR1, stop)
        core._run_job(jid)
        return 0
    import fcntl
    from . import batch
    with open(os.path.join(core.BATCHES, ".summary.lock"), "a") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)      # два последних задания пакета завершились одновременно
        batch.maybe_finish(argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
