#!/usr/bin/env python3
"""
Проверка S3: показывает, у каких ЖК в jk_base/ нет ни одного PDF.
Сверяет содержимое бакета со списком всех ЖК из jk_links.json.
"""

import os
import sys
import re
import json
from pathlib import Path
from collections import defaultdict

import boto3
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent
LINKS_FILE = SCRIPT_DIR / "jk_links.json"

# .env
for path in [SCRIPT_DIR / ".env", SCRIPT_DIR / ".env.txt", SCRIPT_DIR / "env", SCRIPT_DIR / "env.txt"]:
    if path.exists():
        load_dotenv(dotenv_path=path)
        break

S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_BUCKET     = os.getenv("S3_BUCKET", "royaltyplace")
S3_ENDPOINT   = os.getenv("S3_ENDPOINT", "https://storage.yandexcloud.net")
S3_PREFIX     = "jk_base"

s3 = boto3.client(
    "s3", endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY, aws_secret_access_key=S3_SECRET_KEY,
    region_name="ru-central1",
)


def safe_dirname(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def slug_to_name(slug: str) -> str:
    return ' '.join(w.capitalize() for w in slug.split('-'))


def list_all_objects():
    """Возвращает все ключи в jk_base/ (с пагинацией)."""
    keys = []
    token = None
    while True:
        kwargs = {"Bucket": S3_BUCKET, "Prefix": f"{S3_PREFIX}/"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            keys.append(obj["Key"])
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return keys


def main():
    print("=" * 60)
    print(f"ПРОВЕРКА S3: {S3_BUCKET}/{S3_PREFIX}/")
    print("=" * 60)

    # 1. Что реально в S3
    keys = list_all_objects()
    print(f"[•] Всего объектов в {S3_PREFIX}/: {len(keys)}")

    # Считаем PDF по папкам (имя папки = первый сегмент после jk_base/)
    pdf_count = defaultdict(int)
    all_folders = set()
    for k in keys:
        parts = k[len(S3_PREFIX) + 1:].split("/", 1)
        if len(parts) < 2:
            continue
        folder = parts[0]
        all_folders.add(folder)
        if k.lower().endswith(".pdf"):
            pdf_count[folder] += 1

    folders_with_pdf = {f for f in all_folders if pdf_count[f] > 0}
    folders_without_pdf = all_folders - folders_with_pdf

    total_pdf = sum(pdf_count.values())
    print(f"[•] Папок (ЖК) в S3: {len(all_folders)}")
    print(f"[•] Из них с PDF:    {len(folders_with_pdf)}")
    print(f"[•] Всего PDF:       {total_pdf}")

    # 2. Сверка со списком всех ЖК (если есть jk_links.json)
    if LINKS_FILE.exists():
        with open(LINKS_FILE, "r", encoding="utf-8") as f:
            targets = json.load(f)

        print("\n" + "=" * 60)
        print(f"СВЕРКА СО СПИСКОМ ВСЕХ ЖК ({len(targets)} шт.)")
        print("=" * 60)

        missing = []  # ЖК, для которых в S3 НЕТ папки или нет PDF
        for t in targets:
            url = t.get("url", "")
            slug = url.rstrip("/").split("/")[-1]
            # Кандидаты на имя папки: name из файла и slug->name
            cand_names = set()
            if t.get("name"):
                cand_names.add(safe_dirname(t["name"]))
            cand_names.add(safe_dirname(slug_to_name(slug)))

            # Ищем совпадение хотя бы с одной папкой, где есть PDF
            found = False
            for cn in cand_names:
                if cn in folders_with_pdf:
                    found = True
                    break
            # Также пробуем нечёткое совпадение (вдруг имя из <title> отличалось)
            if not found:
                for folder in folders_with_pdf:
                    fl = folder.lower()
                    if any(cn.lower() in fl or fl in cn.lower() for cn in cand_names if cn):
                        found = True
                        break

            if not found:
                missing.append((slug, t.get("name", ""), cand_names))

        if missing:
            print(f"\n❌ ЖК БЕЗ PDF в S3 ({len(missing)} шт.):\n")
            for slug, name, cands in missing:
                print(f"  • {slug}   (имя: {name or '—'})")

            # Сохраняем список в файл
            out_path = SCRIPT_DIR / "zhk_bez_pdf.txt"
            with open(out_path, "w", encoding="utf-8") as out:
                out.write(f"ЖК без PDF в S3 ({len(missing)} шт.)\n")
                out.write(f"Бакет: {S3_BUCKET}/{S3_PREFIX}/\n")
                out.write("=" * 50 + "\n\n")
                for slug, name, cands in missing:
                    out.write(f"{name or slug}\n")
                    out.write(f"    слаг: {slug}\n")
                    out.write(f"    url:  https://spb.trendagent.ru/object/{slug}\n\n")
            print(f"\n💾 Список сохранён в файл: {out_path.name}")
        else:
            print("\n✅ Для всех ЖК из списка есть папка с PDF в S3.")
            out_path = SCRIPT_DIR / "zhk_bez_pdf.txt"
            with open(out_path, "w", encoding="utf-8") as out:
                out.write("Для всех ЖК из списка есть папка с PDF в S3.\n")
            print(f"💾 Результат сохранён в файл: {out_path.name}")
    else:
        print(f"\n[!] {LINKS_FILE.name} не найден — пропускаю сверку со списком.")

    # 3. Папки в S3 без PDF (на всякий случай — вдруг создались пустыми)
    if folders_without_pdf:
        print("\n" + "=" * 60)
        print(f"⚠️  Папки в S3, где НЕТ PDF ({len(folders_without_pdf)} шт.):")
        print("=" * 60)
        for f in sorted(folders_without_pdf):
            others = [k.split("/")[-1] for k in keys if k.startswith(f"{S3_PREFIX}/{f}/")]
            print(f"  • {f}  (файлов всего: {len(others)})")

    # 4. Полный список папок с количеством PDF (для справки)
    print("\n" + "=" * 60)
    print("СПИСОК ВСЕХ ПАПОК В S3 И КОЛИЧЕСТВО PDF:")
    print("=" * 60)
    for f in sorted(all_folders, key=lambda x: x.lower()):
        print(f"  {pdf_count[f]:>3} PDF  │  {f}")


if __name__ == "__main__":
    main()
