#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разведка бакета: показывает префиксы (папки) в корне и их содержимое,
чтобы понять, какое значение поставить в S3_PREFIX.
Читает ключи и из S3_*, и из AWS_* (как основной скрипт).
"""
import os, sys
try:
    from dotenv import load_dotenv
    # пробуем оба имени файла: .env и env
    load_dotenv(".env"); load_dotenv("env")
except ImportError:
    pass
import boto3
from botocore.config import Config as BotoConfig

ENDPOINT = os.getenv("S3_ENDPOINT", "https://storage.yandexcloud.net")
BUCKET   = os.getenv("S3_BUCKET", "royaltyplace")
REGION   = os.getenv("S3_REGION", "ru-central1")
AK = os.getenv("S3_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID")
SK = os.getenv("S3_SECRET_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")

print(f"[•] endpoint={ENDPOINT}  bucket={BUCKET}  region={REGION}")
print(f"[•] ключ найден: {'да' if AK else 'НЕТ'};  секрет найден: {'да' if SK else 'НЕТ'}")
if not AK or not SK:
    sys.exit("Ключи не нашлись ни в S3_*, ни в AWS_*. Покажи имена переменных из env (без значений).")

s = boto3.client("s3", endpoint_url=ENDPOINT, region_name=REGION,
                 aws_access_key_id=AK, aws_secret_access_key=SK,
                 config=BotoConfig(signature_version="s3v4"))

def show(prefix=""):
    r = s.list_objects_v2(Bucket=BUCKET, Prefix=prefix, Delimiter="/")
    cps = [p["Prefix"] for p in r.get("CommonPrefixes", [])]
    files = [o["Key"] for o in r.get("Contents", []) if o["Key"] != prefix]
    print(f"\n=== Внутри '{prefix or '(корень)'}' ===")
    print(f"  Папок: {len(cps)}, файлов: {len(files)}")
    for p in cps[:60]:
        print("   📁", p)
    for fkey in files[:10]:
        print("   📄", fkey)
    return cps

# 1) корень
roots = show("")

# 2) заглянуть на один уровень внутрь каждой папки корня — где лежат ЖК
print("\n──────── Заглядываю внутрь папок корня (по 1 уровню) ────────")
for p in roots[:20]:
    sub = s.list_objects_v2(Bucket=BUCKET, Prefix=p, Delimiter="/")
    subcps = [c["Prefix"] for c in sub.get("CommonPrefixes", [])]
    print(f"\n  '{p}' → подпапок: {len(subcps)}")
    for sc in subcps[:8]:
        print("       ", sc)
    if len(subcps) > 8:
        print(f"        … ещё {len(subcps)-8}")

print("\n[ПОДСКАЗКА] Ищи папку, внутри которой подпапки с названиями ЖК")
print("            (Meltzer Hall, Вилла Марина и т.п.). Её путь и впиши в S3_PREFIX.")
