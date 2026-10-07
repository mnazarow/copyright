# -*- coding: utf-8 -*-
"""Документы Word / LibreOffice → PDF через LibreOffice (soffice --headless)."""
import glob
import os
import shutil
import signal
import subprocess
import tempfile

OFFICE_EXT = (".docx", ".doc", ".odt", ".rtf")
SIGNATURES = {".docx": b"PK\x03\x04", ".odt": b"PK\x03\x04", ".doc": b"\xd0\xcf\x11\xe0", ".rtf": b"{\\rtf"}


def soffice():
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for p in ("/usr/lib/libreoffice/program/soffice", "/opt/libreoffice/program/soffice",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if os.path.exists(p):
            return p
    return None


def is_office(name, data=None):
    ext = os.path.splitext(name.lower())[1]
    if ext not in OFFICE_EXT:
        return False
    return data is None or data[:len(SIGNATURES[ext])] == SIGNATURES[ext]


def find_source(job_dir):
    for p in sorted(glob.glob(os.path.join(job_dir, "source.*"))):
        if p.lower().endswith(OFFICE_EXT):
            return p
    return None


def to_pdf(src, dst, timeout=240):
    return convert(src, dst, "pdf", timeout)


def to_docx(src, dst, timeout=240):
    """DOC, ODT, RTF → DOCX (для отчета в режиме рецензирования)."""
    return convert(src, dst, "docx", timeout)


def convert(src, dst, fmt, timeout=240):
    """Конвертирует src в PDF dst. Отдельный профиль LibreOffice на каждый вызов — вызовы не мешают друг другу."""
    exe = soffice()
    if not exe:
        raise RuntimeError("Для проверки документов Word нужен LibreOffice: установите его "
                           "(sudo apt install libreoffice-writer-nogui) или загрузите PDF")
    tmp = tempfile.mkdtemp(prefix="korrektor-office-")
    try:
        env = dict(os.environ, HOME=tmp)
        p = subprocess.Popen([exe, "--headless", "--norestore", f"-env:UserInstallation=file://{tmp}/profile",
                              "--convert-to", fmt, "--outdir", tmp, src], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
        try:
            out_s, err_s = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)               # soffice — обертка: завершаем всю группу процессов
            p.communicate()
            raise RuntimeError(f"LibreOffice не преобразовал документ за {timeout} с — проверьте файл или загрузите PDF")
        out = glob.glob(os.path.join(tmp, "*." + fmt))
        if p.returncode or not out:
            raise RuntimeError(f"LibreOffice не смог преобразовать документ в {fmt.upper()}: " + (err_s or out_s or "")[-300:])
        shutil.move(out[0], dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dst
