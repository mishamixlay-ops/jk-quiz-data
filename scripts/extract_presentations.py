#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Извлечение ТЕКСТА из презентаций/буклетов ЖК (Yandex Object Storage) и обогащение jk_data.json.

ЧТО ДЕЛАЕТ
  1. Подключается к твоему S3 (Yandex), листит папки внутри префикса (по умолчанию jk_base/).
  2. Для каждой папки скачивает все .pdf, тянет текстовый слой через PyMuPDF (fitz).
  3. Берёт ТОЛЬКО то, что реально читается. PDF без текстового слоя (сканы/картинки)
     помечает как «нечитаемый» — их потом можно прогнать через OCR отдельно.
  4. Сопоставляет имя папки с ключом в jk_data.json (умная нормализация:
     гомоглифы латиница/кириллица, слеши, скобки-примечания, регистр) + нечёткий фоллбэк.
  5. Чистит текст (повторы, колонтитулы, голые номера страниц) и дописывает в JSON поле
     "presentation_text" (+ "presentation_sources", "presentation_chars").
  6. Печатает и сохраняет ОТЧЁТ: кого обогатили, у кого текст не вытащился,
     какие папки без пары в данных, какие ЖК в данных без папки, нечёткие матчи на ревью.

ЧЕГО НЕ ДЕЛАЕТ
  - Не трогает существующие поля в jk_data.json (только добавляет новые).
  - Не делает OCR (это отдельный шаг, если решишь, что он нужен).

ЗАПУСК
  pip install boto3 pymupdf python-dotenv
  python extract_presentations.py
  # тест на 3 папках:        python extract_presentations.py --limit 3
  # выгрузить и сырой текст: python extract_presentations.py --dump-text
"""

import os
import re
import io
import sys
import json
import argparse
import tempfile
import unicodedata
from difflib import SequenceMatcher

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:
    sys.exit("Нет boto3. Установи: pip install boto3")

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("Нет PyMuPDF. Установи: pip install pymupdf")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env можно и через окружение

# ──────────────────────────────────────────────────────────────────────────────
# КОНФИГ. Значения берутся из .env / окружения; ниже — дефолты под наш случай.
# Требуемые переменные .env:
#   S3_ENDPOINT       (по умолч. https://storage.yandexcloud.net)
#   S3_BUCKET         (по умолч. royaltyplace)
#   S3_PREFIX         (по умолч. jk_base/)
#   S3_ACCESS_KEY     (твой ключ)
#   S3_SECRET_KEY     (твой секрет)
#   S3_REGION         (по умолч. ru-central1)
# ──────────────────────────────────────────────────────────────────────────────
ENDPOINT   = os.getenv("S3_ENDPOINT", "https://storage.yandexcloud.net")
BUCKET     = os.getenv("S3_BUCKET", "royaltyplace")
PREFIX     = os.getenv("S3_PREFIX", "jk_base/")
REGION     = os.getenv("S3_REGION", "ru-central1")
ACCESS_KEY = os.getenv("S3_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID")
SECRET_KEY = os.getenv("S3_SECRET_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")

# Файл с данными ЖК (dict, ключи = названия ЖК). Лежит рядом со скриптом.
JK_DATA_PATH      = os.getenv("JK_DATA_PATH", "jk_data.json")
OUT_DATA_PATH     = "jk_data_enriched.json"
OUT_REPORT_JSON   = "extraction_report.json"
OUT_REPORT_TXT    = "extraction_report.txt"
DUMP_TEXT_DIR     = "extracted_text"   # если запущено с --dump-text

# Папки, которые НЕ являются ЖК — пропускаем.
SKIP_FOLDERS = {"trendagent", "trend agent"}

# Порог «читаемости»: если средне символов на страницу меньше — считаем PDF
# картиночным/сканом (текстового слоя нет).
MIN_CHARS_PER_PAGE = 25
MIN_CHARS_TOTAL    = 120
# Порог нечёткого матча папка↔ключ данных.
FUZZY_THRESHOLD    = 0.86

# ──────────────────────────────────────────────────────────────────────────────
# НОРМАЛИЗАЦИЯ ИМЁН (главная защита от потери ЖК)
# ──────────────────────────────────────────────────────────────────────────────
# Кириллические буквы, выглядящие как латинские → приводим к латинице,
# чтобы 'АМО' (кир.) и 'AMO' (лат.) стали одинаковыми.
_HOMO = {
    'а':'a','в':'b','е':'e','к':'k','м':'m','н':'h','о':'o','р':'p',
    'с':'c','т':'t','у':'y','х':'x','і':'i','ё':'e',
}

def canon(name: str) -> str:
    """Каноническая форма имени для сравнения: нижний регистр, без скобок,
    гомоглифы→латиница, только буквы и цифры."""
    s = name.lower().strip()
    s = re.sub(r'\(.*?\)', ' ', s)              # выкинуть (бывш. …) и т.п.
    s = unicodedata.normalize('NFKC', s)
    s = ''.join(_HOMO.get(ch, ch) for ch in s)  # гомоглифы
    s = re.sub(r'[^0-9a-zа-я]+', '', s)         # оставить только буквы/цифры
    return s

def fuzzy(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()

# ──────────────────────────────────────────────────────────────────────────────
# ЧИСТКА ТЕКСТА
# ──────────────────────────────────────────────────────────────────────────────
def clean_text(pages):
    """pages: список строк (по странице). Убираем колонтитулы-повторы,
    голые номера страниц, схлопываем пробелы."""
    # Найти строки, повторяющиеся почти на каждой странице (колонтитулы).
    from collections import Counter
    line_counter = Counter()
    per_page_lines = []
    for p in pages:
        lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
        per_page_lines.append(lines)
        for ln in set(lines):
            line_counter[ln] += 1
    n_pages = max(1, len(pages))
    boiler = {ln for ln, c in line_counter.items()
              if c >= max(3, n_pages * 0.6) and len(ln) < 80}

    out = []
    for lines in per_page_lines:
        for ln in lines:
            if ln in boiler:                      # колонтитул
                continue
            if re.fullmatch(r'[\d\s/–—.-]{1,6}', ln):  # голый номер страницы
                continue
            out.append(ln)
    text = '\n'.join(out)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

# ──────────────────────────────────────────────────────────────────────────────
# S3
# ──────────────────────────────────────────────────────────────────────────────
def s3_client():
    if not ACCESS_KEY or not SECRET_KEY:
        sys.exit("Нет ключей S3. Заполни S3_ACCESS_KEY / S3_SECRET_KEY в .env")
    return boto3.client(
        "s3", endpoint_url=ENDPOINT, region_name=REGION,
        aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY,
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
    )

def list_folders(s3):
    """Имена папок первого уровня внутри PREFIX."""
    folders = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=PREFIX, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            name = cp["Prefix"][len(PREFIX):].rstrip("/")
            if name:
                folders.append(name)
    return sorted(folders)

def list_pdfs(s3, folder):
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    pre = f"{PREFIX}{folder}/"
    for page in paginator.paginate(Bucket=BUCKET, Prefix=pre):
        for obj in page.get("Contents", []):
            if obj["Key"].lower().endswith(".pdf"):
                keys.append(obj["Key"])
    return keys

def download_bytes(s3, key):
    buf = io.BytesIO()
    s3.download_fileobj(BUCKET, key, buf)
    return buf.getvalue()

# ──────────────────────────────────────────────────────────────────────────────
# PDF → текст
# ──────────────────────────────────────────────────────────────────────────────
def extract_pdf_text(pdf_bytes):
    """Возвращает (pages:list[str], n_pages, total_chars, readable:bool)."""
    pages = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        return [], 0, 0, False, f"open_error:{type(e).__name__}"
    try:
        for pg in doc:
            pages.append(pg.get_text("text") or "")
    finally:
        doc.close()
    total = sum(len(p) for p in pages)
    n = max(1, len(pages))
    readable = (total >= MIN_CHARS_TOTAL) and (total / n >= MIN_CHARS_PER_PAGE)
    return pages, len(pages), total, readable, "ok"

# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="обработать только N папок (тест)")
    ap.add_argument("--dump-text", action="store_true", help="сохранить сырой текст по ЖК в файлы")
    args = ap.parse_args()

    if not os.path.exists(JK_DATA_PATH):
        sys.exit(f"Нет файла данных: {JK_DATA_PATH}")
    with open(JK_DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)

    # карта канон-имя → реальный ключ
    canon_map = {canon(k): k for k in data.keys()}

    s3 = s3_client()
    print(f"[•] Бакет: {BUCKET}/{PREFIX}  (endpoint {ENDPOINT})")
    folders = list_folders(s3)
    print(f"[•] Папок найдено: {len(folders)}")
    if args.limit:
        folders = folders[:args.limit]
        print(f"[•] Тестовый режим: только первые {len(folders)}")

    if args.dump_text:
        os.makedirs(DUMP_TEXT_DIR, exist_ok=True)

    report = {
        "enriched": [],          # {jk, folder, pdfs, chars}
        "matched_no_text": [],   # папка сматчилась, но текст не вытащился (сканы)
        "folder_no_match": [],   # папка есть, ЖК в данных нет
        "fuzzy_matches": [],     # {folder, matched_jk, score} — проверить руками
        "skipped": [],           # служебные папки
        "pdf_unreadable": [],    # {jk/folder, pdf, reason}
        "errors": [],
    }
    matched_keys = set()

    for i, folder in enumerate(folders, 1):
        if canon(folder) in {canon(x) for x in SKIP_FOLDERS}:
            report["skipped"].append(folder)
            print(f"  [{i}/{len(folders)}] {folder}  → служебная, пропуск")
            continue

        print(f"  [{i}/{len(folders)}] {folder}")
        # матчинг
        cf = canon(folder)
        jk_key, score, how = None, 1.0, "exact"
        if cf in canon_map:
            jk_key = canon_map[cf]
        else:
            best, best_s = None, 0.0
            for ck, real in canon_map.items():
                s = fuzzy(cf, ck)
                if s > best_s:
                    best, best_s = real, s
            if best_s >= FUZZY_THRESHOLD:
                jk_key, score, how = best, best_s, "fuzzy"
                report["fuzzy_matches"].append(
                    {"folder": folder, "matched_jk": best, "score": round(best_s, 3)})
                print(f"      ~ нечёткий матч → '{best}' ({best_s:.2f}) — проверь")
            else:
                report["folder_no_match"].append(folder)
                print(f"      ✗ нет пары в jk_data.json")

        # скачиваем и читаем PDF
        try:
            pdf_keys = list_pdfs(s3, folder)
        except Exception as e:
            report["errors"].append({"folder": folder, "error": str(e)})
            print(f"      [!] ошибка листинга: {e}")
            continue

        if not pdf_keys:
            print(f"      (PDF нет)")
            if jk_key:
                report["matched_no_text"].append({"jk": jk_key, "folder": folder, "reason": "no_pdf"})
            continue

        all_pages, sources, readable_any = [], [], False
        for key in pdf_keys:
            fname = key.split("/")[-1]
            try:
                blob = download_bytes(s3, key)
                pages, npg, total, readable, status = extract_pdf_text(blob)
            except Exception as e:
                report["pdf_unreadable"].append(
                    {"folder": folder, "pdf": fname, "reason": f"download/parse:{e}"})
                print(f"      [!] {fname}: ошибка ({e})")
                continue
            if status != "ok":
                report["pdf_unreadable"].append({"folder": folder, "pdf": fname, "reason": status})
                print(f"      [-] {fname}: {status}")
                continue
            if readable:
                all_pages.extend(pages)
                sources.append(fname)
                readable_any = True
                print(f"      [✓] {fname}: {total} симв., {npg} стр.")
            else:
                report["pdf_unreadable"].append(
                    {"folder": folder, "pdf": fname,
                     "reason": f"low_text ({total} симв / {npg} стр)"})
                print(f"      [-] {fname}: текста почти нет ({total} симв) — вероятно скан")

        if readable_any:
            text = clean_text(all_pages)
            if jk_key:
                data[jk_key]["presentation_text"] = text
                data[jk_key]["presentation_sources"] = sources
                data[jk_key]["presentation_chars"] = len(text)
                matched_keys.add(jk_key)
                report["enriched"].append(
                    {"jk": jk_key, "folder": folder, "pdfs": sources, "chars": len(text)})
            if args.dump_text:
                safe = re.sub(r'[^0-9A-Za-zА-Яа-я _-]', '_', folder)
                with open(os.path.join(DUMP_TEXT_DIR, f"{safe}.txt"), "w", encoding="utf-8") as f:
                    f.write(text)
        else:
            if jk_key:
                report["matched_no_text"].append(
                    {"jk": jk_key, "folder": folder, "reason": "all_pdfs_unreadable"})
            print(f"      ✗ читаемого текста не нашлось ни в одном PDF")

    # ЖК из данных, для которых вообще не было папки
    report["data_no_folder"] = sorted(
        k for k in data.keys() if k not in matched_keys
        and not any(d.get("jk") == k for d in report["matched_no_text"])
    ) if not args.limit else []

    # сохранить обогащённые данные и отчёты
    with open(OUT_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    with open(OUT_REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # текстовый отчёт
    lines = []
    lines.append("=" * 64)
    lines.append("ОТЧЁТ ПО ИЗВЛЕЧЕНИЮ ТЕКСТА ПРЕЗЕНТАЦИЙ")
    lines.append("=" * 64)
    lines.append(f"Обогащено ЖК (есть текст):      {len(report['enriched'])}")
    lines.append(f"Сматчились, но текста нет:       {len(report['matched_no_text'])}")
    lines.append(f"Папки без пары в данных:         {len(report['folder_no_match'])}")
    lines.append(f"Нечёткие матчи (на ревью):       {len(report['fuzzy_matches'])}")
    lines.append(f"Нечитаемых PDF (сканы и пр.):     {len(report['pdf_unreadable'])}")
    lines.append(f"Служебные папки (пропущены):      {len(report['skipped'])}")
    lines.append(f"Ошибки:                          {len(report['errors'])}")
    if not args.limit:
        lines.append(f"ЖК в данных без папки в S3:       {len(report.get('data_no_folder', []))}")
    lines.append("")

    def block(title, items, render):
        lines.append(f"── {title} ({len(items)})")
        for it in items:
            lines.append("   " + render(it))
        lines.append("")

    block("НЕ СОБРАЛИ ТЕКСТ (сматчились, но пусто)", report["matched_no_text"],
          lambda x: f"{x['jk']}  [{x.get('reason','')}]  (папка: {x['folder']})")
    block("ПАПКИ БЕЗ ПАРЫ В jk_data.json", report["folder_no_match"], lambda x: x)
    block("НЕЧЁТКИЕ МАТЧИ — ПРОВЕРИТЬ ВРУЧНУЮ", report["fuzzy_matches"],
          lambda x: f"{x['folder']}  →  {x['matched_jk']}  ({x['score']})")
    block("НЕЧИТАЕМЫЕ PDF", report["pdf_unreadable"],
          lambda x: f"{x['folder']} / {x['pdf']}  [{x['reason']}]")
    if not args.limit:
        block("ЖК В ДАННЫХ БЕЗ ПАПКИ В S3", report.get("data_no_folder", []), lambda x: x)

    report_txt = "\n".join(lines)
    with open(OUT_REPORT_TXT, "w", encoding="utf-8") as f:
        f.write(report_txt)

    print("\n" + report_txt)
    print(f"[✓] Данные:  {OUT_DATA_PATH}")
    print(f"[✓] Отчёт:   {OUT_REPORT_TXT}  /  {OUT_REPORT_JSON}")
    if args.dump_text:
        print(f"[✓] Текст:   {DUMP_TEXT_DIR}/")


if __name__ == "__main__":
    main()
