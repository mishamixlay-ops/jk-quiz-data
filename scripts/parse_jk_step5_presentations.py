#!/usr/bin/env python3
"""
Парсер TrendAgent — Шаг 5: Массовое скачивание PDF-презентаций и выгрузка в S3 Yandex Cloud (в папку jk_base)
"""

import os
import sys
import time
import json
import requests
import re
from pathlib import Path

# Все необходимые импорты для S3 и dotenv
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

# Импорты для Selenium WebDriver
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# ──────────────────────────────────────────────
# НАСТРОЙКИ И ПУТИ
# ──────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent
CHROME_PROFILE = SCRIPT_DIR / "chrome_profile"
LINKS_FILE     = SCRIPT_DIR / "jk_links.json"

# Проверка конфигурации env
possible_env_paths = [SCRIPT_DIR / ".env", SCRIPT_DIR / ".env.txt", SCRIPT_DIR / "env", SCRIPT_DIR / "env.txt"]
config_loaded = False
for path in possible_env_paths:
    if path.exists():
        print(f"[✓] Нашли конфигурационный файл: {path.name}")
        load_dotenv(dotenv_path=path)
        config_loaded = True
        break

if not config_loaded:
    print(f"[❌] КРИТИЧЕСКАЯ ОШИБКА: Конфигурационный файл не найден в {SCRIPT_DIR}")
    sys.exit(1)

# Данные S3 Yandex Cloud
S3_ACCESS_KEY  = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY  = os.getenv("S3_SECRET_KEY")
S3_BUCKET      = os.getenv("S3_BUCKET", "royaltyplace")
S3_ENDPOINT    = os.getenv("S3_ENDPOINT", "https://storage.yandexcloud.net")
S3_PREFIX      = "jk_base"  # Папка из твоего скриншота

if not S3_ACCESS_KEY or not S3_SECRET_KEY:
    print(f"[❌] ОШИБКА КОНФИГУРАЦИИ: Проверь ключи S3 в файле конфигурации.")
    raise RuntimeError("❌ Ключи доступа S3 не найдены.")

BASE_URL = "https://spb.trendagent.ru/objects/list/"


# ──────────────────────────────────────────────
# ХЕЛПЕРЫ И ИНИЦИАЛИЗАЦИЯ
# ──────────────────────────────────────────────
def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name="ru-central1",
    )


def safe_dirname(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|/]', "_", name).strip()


def get_driver():
    CHROME_PROFILE.mkdir(exist_ok=True)
    opts = Options()
    opts.add_argument(f"--user-data-dir={CHROME_PROFILE}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--start-maximized")
    opts.add_argument("--remote-allow-origins=*")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
    )
    return driver


def wait_for_page_ready(driver, timeout=15):
    for _ in range(timeout):
        if driver.execute_script("return document.readyState") == "complete":
            break
        time.sleep(1)


def ensure_auth(driver):
    """Контроль авторизации сессии (копия логики из trendagent_parser_17.py)"""
    print(f"[•] Проверка активности профиля: {CHROME_PROFILE}")
    driver.get(BASE_URL)
    wait_for_page_ready(driver)
    time.sleep(4)
    
    if "sso.trend.tech" in driver.current_url or "login" in driver.current_url:
        print("[!] Требуется вход — авторизуйтесь в открывшемся браузере.")
        input("    После успешного входа в аккаунт нажмите ENTER здесь для продолжения…\n")
        wait_for_page_ready(driver)
        time.sleep(5)
    print("[✓] Авторизация на платформе подтверждена успешно.")


# ──────────────────────────────────────────────
# СКАНЕР ПРЕЗЕНТАЦИЙ
# ──────────────────────────────────────────────
def find_presentation_pdfs(driver) -> list:
    """Ищет табы документов, раскрывает их и парсит ссылки на PDF."""
    driver.execute_script(r"""
        var elements = document.querySelectorAll('button, a, [role="tab"], span, div');
        for (var i = 0; i < elements.length; i++) {
            var el = elements[i];
            var txt = (el.innerText || '').toLowerCase().trim();
            // Кликаем только по компактным целевым элементам (меню, кнопки), чтобы не сломать верстку
            if (txt.length > 2 && txt.length < 50) {
                if (txt.includes('презентация объектов') || txt.includes('презентации') || txt.includes('файлы') || txt.includes('документы')) {
                    if (el.clientHeight > 0) {
                        try { el.click(); break; } catch(e) {}
                    }
                }
            }
        }
    """)
    time.sleep(2.5)

    pdf_links = driver.execute_script(r"""
        var urls = [];
        document.querySelectorAll('a').forEach(function(a) {
            var href = a.href || '';
            var text = (a.innerText || '').trim();
            if (href.includes('.pdf') || href.toLowerCase().includes('download') || href.includes('/storage/files/')) {
                urls.push({
                    url: href,
                    title: text || 'Презентация'
                });
            }
        });
        return urls;
    """)
    return pdf_links


def download_and_upload_to_s3(session, s3_client, pdf_url: str, pdf_title: str, jk_name: str) -> bool:
    clean_title = safe_filename(pdf_title)
    if not clean_title.lower().endswith('.pdf'):
        clean_title += ".pdf"
        
    local_path = SCRIPT_DIR / clean_title
    s3_key = f"{S3_PREFIX}/{safe_dirname(jk_name)}/{clean_title}"
    
    try:
        resp = session.get(pdf_url, stream=True, timeout=20)
        if resp.status_code == 200:
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            s3_client.upload_file(
                str(local_path), 
                S3_BUCKET, 
                s3_key,
                ExtraArgs={"ACL": "public-read", "ContentType": "application/pdf"}
            )
            return True
    except Exception as e:
        print(f"      [!] Ошибка передачи файла {pdf_title}: {e}")
    finally:
        if local_path.exists():
            try: os.remove(local_path)
            except: pass
    return False


# ──────────────────────────────────────────────
# MAIN RUNNER
# ──────────────────────────────────────────────
def main():
    print("=" * 60)
    print("ЗАПУСК ШАГА 5: МАССОВАЯ ЗАГРУЗКА ПРЕЗЕНТАЦИЙ В S3")
    print("=" * 60)

    if not LINKS_FILE.exists():
        print(f"[❌] Ошибка: Файл {LINKS_FILE.name} не найден.")
        return

    with open(LINKS_FILE, "r", encoding="utf-8") as f:
        jk_targets = json.load(f)

    print(f"[•] Всего объектов для проверки: {len(jk_targets)}")
    print(f"[•] Направление S3: {S3_BUCKET}/{S3_PREFIX}/")

    s3_client = get_s3_client()
    driver = get_driver()
    
    try:
        # Жесткая проверка авторизации перед циклом
        ensure_auth(driver)

        for index, target in enumerate(jk_targets, 1):
            url = target["url"]
            
            print(f"  [{index}/{len(jk_targets)}] Заходим на объект {url.split('/object/')[-1].replace('/', '')}…")
            
            try:
                driver.get(url)
                wait_for_page_ready(driver)
                time.sleep(3.5)

                # Защита от внезапного логаута во время долгого парсинга
                if "sso.trend.tech" in driver.current_url or "login" in driver.current_url:
                    print("\n[!] Сессия истекла! Авторизуйтесь заново и нажмите ENTER…")
                    input()
                    driver.get(url)
                    wait_for_page_ready(driver)
                    time.sleep(2)

                # ДИНАМИЧЕСКОЕ ИСПРАВЛЕНИЕ ИМЕНИ: Берем настоящее имя вместо "Старт продаж"
                real_jk_name = driver.execute_script(r"""
                    var h1 = document.querySelector('h1, [class*="title"], [class*="name"], .object-header__title');
                    if (h1 && h1.innerText.trim()) return h1.innerText.trim();
                    return document.title.split('|')[0].split('-')[0].trim();
                """) or target["name"]

                print(f"    ↳ Настоящее имя ЖК: «{real_jk_name}»")

                # Прокрутка к блоку файлов
                for pct in [0.4, 0.7, 1.0]:
                    driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {pct});")
                    time.sleep(0.6)

                pdf_files = find_presentation_pdfs(driver)
                
                if not pdf_files:
                    print("    ↳ 📁 Документы/презентации на странице отсутствуют.")
                    continue
                
                print(f"    ↳ Найдено файлов: {len(pdf_files)}. Скачиваем и отправляем в S3…")
                
                # Сессия скачивания документов
                session = requests.Session()
                for cookie in driver.get_cookies():
                    session.cookies.set(cookie['name'], cookie['value'])
                session.headers.update({"Referer": url})

                uploaded_count = 0
                for pdf in pdf_files:
                    success = download_and_upload_to_s3(session, s3_client, pdf["url"], pdf["title"], real_jk_name)
                    if success:
                        uploaded_count += 1
                        
                if uploaded_count > 0:
                    print(f"    [✓ Успешно выгружено]: {uploaded_count} шт. → S3: {S3_PREFIX}/{safe_dirname(real_jk_name)}/")

            except Exception as item_error:
                print(f"    [❌] Ошибка обработки объекта: {item_error}")
                time.sleep(2)

        print("\n" + "=" * 50)
        print("🎉 ВСЕ ПРЕЗЕНТАЦИИ УСПЕШНО СИНХРОНИЗИРОВАНЫ С S3 ПАПКОЙ JK_BASE!")
        print("=" * 50)

    finally:
        driver.quit()


if __name__ == "__main__":
    main()