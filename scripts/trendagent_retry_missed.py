#!/usr/bin/env python3
"""
ДОБИВАЛЬЩИК: обрабатывает только тех ЖК, у которых не нашлась папка «Презентация объектов»
в основном прогоне. Использует более терпеливые таймауты и расширенный поиск папки.
"""

import os
import sys
import time
import json
import re
import shutil
from pathlib import Path

import boto3
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

SCRIPT_DIR     = Path(__file__).parent
CHROME_PROFILE = SCRIPT_DIR / "chrome_profile"
DEBUG_DIR      = SCRIPT_DIR / "_debug"
DOWNLOAD_DIR   = SCRIPT_DIR / "_downloads"

# ──────────────────────────────────────────────
# СПИСОК ПРОПУЩЕННЫХ ЖК (из первого прогона)
# ──────────────────────────────────────────────
MISSED_JK = [
    "civilizaciya-na-neve",
    "saan-moskovskie-vorota",
    "saan",
    "point-toksovo",
    "neva_haus",
    "defans",
    "bakunina-33",
]

BASE_URL = "https://spb.trendagent.ru"
DOWNLOAD_TIMEOUT = 90  # увеличено

# Загрузка .env
possible_env_paths = [SCRIPT_DIR / ".env", SCRIPT_DIR / ".env.txt", SCRIPT_DIR / "env", SCRIPT_DIR / "env.txt"]
config_loaded = False
for path in possible_env_paths:
    if path.exists():
        print(f"[✓] Нашли конфигурационный файл: {path.name}")
        load_dotenv(dotenv_path=path)
        config_loaded = True
        break
if not config_loaded:
    print(f"[❌] Конфигурационный файл не найден")
    sys.exit(1)

S3_ACCESS_KEY  = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY  = os.getenv("S3_SECRET_KEY")
S3_BUCKET      = os.getenv("S3_BUCKET", "royaltyplace")
S3_ENDPOINT    = os.getenv("S3_ENDPOINT", "https://storage.yandexcloud.net")
S3_PREFIX      = "jk_base"


def get_s3_client():
    return boto3.client(
        "s3", endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY, aws_secret_access_key=S3_SECRET_KEY,
        region_name="ru-central1",
    )


def safe_dirname(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def get_driver():
    CHROME_PROFILE.mkdir(exist_ok=True)
    DOWNLOAD_DIR.mkdir(exist_ok=True)
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
    prefs = {
        "download.default_directory": str(DOWNLOAD_DIR.absolute()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        "safebrowsing.enabled": True,
    }
    opts.add_experimental_option("prefs", prefs)
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
    )
    driver.execute_cdp_cmd("Page.setDownloadBehavior", {
        "behavior": "allow", "downloadPath": str(DOWNLOAD_DIR.absolute())
    })
    return driver


def wait_for_page_ready(driver, timeout=20):
    for _ in range(timeout):
        if driver.execute_script("return document.readyState") == "complete":
            break
        time.sleep(1)


def ensure_auth(driver):
    driver.get(f"{BASE_URL}/objects/list/")
    wait_for_page_ready(driver)
    time.sleep(4)
    if "sso.trend.tech" in driver.current_url or "login" in driver.current_url:
        print("[!] Требуется вход — авторизуйтесь и нажмите ENTER…")
        input()
        wait_for_page_ready(driver)
        time.sleep(5)
    print("[✓] Авторизация подтверждена.")


def goto_files_section_thorough(driver) -> bool:
    """Терпеливый переход к секции файлов: до 25 сек ожидания, повторные попытки."""
    # Сначала несколько раз скроллим по странице, чтобы прогрузить ленивые компоненты
    for _ in range(3):
        driver.execute_script("window.scrollBy(0, window.innerHeight);")
        time.sleep(0.5)

    # Затем переходим к якорю
    driver.execute_script("""
        var navLink = document.querySelector('a[href$="#files"]');
        if (navLink) navLink.click();
        else location.hash = 'files';
    """)
    MARKERS = ['презентац', 'планировк', 'разрешит', 'условия прода', 'шаблон договор',
               'регламент', 'буклет', 'все документы']
    for _ in range(50):  # до 25 сек
        time.sleep(0.5)
        present = driver.execute_script(r"""
            var markers = arguments[0];
            var text = document.body.innerText.toLowerCase();
            return markers.some(function(m){ return text.includes(m); });
        """, MARKERS)
        if present:
            time.sleep(1)
            return True
    return False


def click_presentation_folder_robust(driver) -> bool:
    """
    Более устойчивый клик. Перед поиском дополнительно скроллит секцию #files,
    пробует несколько вариантов клика (на сам текст, на родителя строки папки).
    """
    # Скроллим внутри секции файлов на всякий случай
    driver.execute_script("""
        var section = document.getElementById('files');
        if (section) section.scrollIntoView({block:'start'});
    """)
    time.sleep(1)

    # Логируем, какие папки видны
    folders = driver.execute_script(r"""
        var section = document.getElementById('files') || document;
        var titles = [];
        section.querySelectorAll('h3, [class*="object-files__item_name"], [class*="folder"], div, span').forEach(function(el){
            var own = '';
            for (var j = 0; j < el.childNodes.length; j++) {
                if (el.childNodes[j].nodeType === 3) own += el.childNodes[j].nodeValue;
            }
            own = own.trim();
            if (own && own.length > 2 && own.length < 60 && el.offsetParent !== null) {
                titles.push(own);
            }
        });
        // уникализуем и берём только осмысленные
        var seen = new Set();
        return titles.filter(function(t){
            if (seen.has(t)) return false;
            seen.add(t);
            var tl = t.toLowerCase();
            // отсекаем явный мусор (даты, размеры, кнопки)
            if (/^\d+[.,]?\d*\s*(kb|mb|гб|мб|кб)$/i.test(tl)) return false;
            if (/^\d{2}\.\d{2}\.\d{4}$/.test(tl)) return false;
            return true;
        }).slice(0, 30);
    """)
    print(f"      [•] Видимые элементы в секции файлов:")
    for f in folders:
        marker = " ⭐" if "презентац" in f.lower() else ""
        print(f"          {marker} {f}")

    clicked = driver.execute_script(r"""
        var TARGET = 'презентация объектов';
        var all = document.querySelectorAll('div, span, li, a, button, p, h2, h3, h4');
        for (var i = 0; i < all.length; i++) {
            var el = all[i];
            if (el.offsetParent === null) continue;
            var own = '';
            for (var j = 0; j < el.childNodes.length; j++) {
                if (el.childNodes[j].nodeType === 3) own += el.childNodes[j].nodeValue;
            }
            own = own.trim().toLowerCase();
            if (own === TARGET) {
                var clickable = el;
                for (var k = 0; k < 6 && clickable.parentElement; k++) {
                    var cs = window.getComputedStyle(clickable);
                    if (cs.cursor === 'pointer') break;
                    if (clickable.tagName === 'A' || clickable.tagName === 'BUTTON') break;
                    if (clickable.getAttribute('role')) break;
                    clickable = clickable.parentElement;
                }
                try {
                    clickable.scrollIntoView({block:'center'});
                    clickable.click();
                    return clickable.tagName + ':' + (clickable.className||'').slice(0,80);
                } catch(e) { return 'CLICK_FAIL: ' + e.message; }
            }
        }
        return null;
    """)
    if not clicked:
        return False
    print(f"      [•] Кликнул по: {clicked}")
    # Ждём дольше — до 15 секунд
    for _ in range(30):
        time.sleep(0.5)
        opened = driver.execute_script(r"""
            var text = document.body.innerText.toLowerCase();
            return text.includes('все документы /') || text.includes('все документы/') ||
                   text.includes('← презентация объектов') || text.includes('презентация объектов\n');
        """)
        if opened: return True
    return True


def mark_download_buttons(driver) -> int:
    return driver.execute_script(r"""
        document.querySelectorAll('[data-tp-dl]').forEach(function(el){ el.removeAttribute('data-tp-dl'); });
        var candidates = [];
        document.querySelectorAll('a[download], a[href*=".pdf"], a[href*=".PDF"]').forEach(function(a){
            if (a.offsetParent !== null) candidates.push(a);
        });
        document.querySelectorAll('button, a').forEach(function(el){
            if (el.offsetParent === null) return;
            var labels = ((el.getAttribute('aria-label')||'') + ' ' + (el.getAttribute('title')||'')).toLowerCase();
            if (labels.includes('скач') || labels.includes('download') || labels.includes('загруз')) {
                candidates.push(el);
            }
        });
        document.querySelectorAll('button, a').forEach(function(el){
            if (el.offsetParent === null) return;
            var svgs = el.querySelectorAll('svg');
            for (var i = 0; i < svgs.length; i++) {
                var cls = (svgs[i].getAttribute('class') || '').toLowerCase();
                var use = svgs[i].querySelector('use');
                var useHref = use ? (use.getAttribute('href') || use.getAttribute('xlink:href') || '').toLowerCase() : '';
                if (cls.includes('download') || cls.includes('arrow-down') ||
                    useHref.includes('download') || useHref.includes('arrow-down')) {
                    candidates.push(el);
                    break;
                }
            }
        });
        var seen = new Set();
        var unique = candidates.filter(function(el){
            if (seen.has(el)) return false;
            seen.add(el);
            return true;
        });
        unique.forEach(function(el, i){ el.setAttribute('data-tp-dl', 'btn_' + i); });
        return unique.length;
    """)


def get_file_names_in_dir(d: Path) -> set:
    return set(p.name for p in d.iterdir() if p.is_file())


def wait_for_new_download(before_files: set, timeout: int = DOWNLOAD_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.6)
        current = {p.name for p in DOWNLOAD_DIR.iterdir() if p.is_file() and not p.name.endswith('.crdownload')}
        new_files = current - before_files
        if new_files:
            still = any(p.name.endswith('.crdownload') for p in DOWNLOAD_DIR.iterdir())
            if not still:
                name = next(iter(new_files))
                return DOWNLOAD_DIR / name
    return None


def clean_download_dir():
    if DOWNLOAD_DIR.exists():
        for p in DOWNLOAD_DIR.iterdir():
            try:
                if p.is_file(): p.unlink()
                elif p.is_dir(): shutil.rmtree(p)
            except Exception: pass
    DOWNLOAD_DIR.mkdir(exist_ok=True)


def download_all_pdfs_from_folder(driver, s3_client, jk_name: str) -> int:
    clean_download_dir()
    n_buttons = mark_download_buttons(driver)
    print(f"      [•] Найдено download-кнопок: {n_buttons}")
    if n_buttons == 0:
        return 0
    buttons = driver.find_elements(By.CSS_SELECTOR, '[data-tp-dl]')
    uploaded = 0
    for i, btn in enumerate(buttons, 1):
        try:
            before = get_file_names_in_dir(DOWNLOAD_DIR)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.3)
            btn.click()
            new_file = wait_for_new_download(before)
            if not new_file:
                print(f"      [!] Файл {i}/{n_buttons}: не скачался за {DOWNLOAD_TIMEOUT}с")
                continue
            if new_file.suffix.lower() != ".pdf":
                print(f"      [-] {new_file.name} — не PDF")
                try: new_file.unlink()
                except: pass
                continue
            s3_key = f"{S3_PREFIX}/{safe_dirname(jk_name)}/{new_file.name}"
            try:
                s3_client.upload_file(
                    str(new_file), S3_BUCKET, s3_key,
                    ExtraArgs={"ACL": "public-read", "ContentType": "application/pdf"}
                )
                print(f"      [✓] {new_file.name}")
                uploaded += 1
            except Exception as e:
                print(f"      [!] Ошибка S3: {e}")
            finally:
                try: new_file.unlink()
                except: pass
        except Exception as e:
            print(f"      [!] Ошибка на кнопке {i}: {e}")
    return uploaded


def save_debug(driver, name):
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r'[\\/*?:"<>|]', "_", name)[:80]
    (DEBUG_DIR / f"retry_{safe}.html").write_text(driver.page_source, encoding="utf-8")
    print(f"      📸 Сохранил снапшот: _debug/retry_{safe}.html")


def main():
    print("=" * 60)
    print("ДОБИВАЛЬЩИК: повторная обработка пропущенных ЖК")
    print(f"  В списке: {len(MISSED_JK)} ЖК")
    print("=" * 60)

    s3_client = get_s3_client()
    driver = get_driver()
    stats = {"with_pdfs": 0, "no_pdfs": 0, "total": 0}

    try:
        ensure_auth(driver)

        for index, slug in enumerate(MISSED_JK, 1):
            url = f"{BASE_URL}/object/{slug}"
            print(f"\n  [{index}/{len(MISSED_JK)}] {slug}")
            try:
                driver.get(url)
                wait_for_page_ready(driver)
                time.sleep(5)  # больше времени на первичный рендер

                if "sso.trend.tech" in driver.current_url or "login" in driver.current_url:
                    print("[!] Сессия истекла. Авторизуйтесь и нажмите ENTER…")
                    input()
                    driver.get(url)
                    wait_for_page_ready(driver)
                    time.sleep(5)

                # Имя ЖК из <title>
                page_title = (driver.title or "").strip()
                jk_name = ""
                if page_title:
                    cleaned = re.split(r'\s*[-|·–—]\s*TrendAgent', page_title, flags=re.IGNORECASE)[0].strip()
                    if cleaned == page_title:
                        cleaned = re.split(r'\s*[|·]\s*', page_title)[0].strip()
                    if 3 <= len(cleaned) <= 80:
                        jk_name = cleaned
                if not jk_name:
                    jk_name = ' '.join(w.capitalize() for w in slug.split('-'))
                print(f"    ↳ ЖК: «{jk_name}»")

                # ШАГ 1: дотошный переход к секции файлов
                if not goto_files_section_thorough(driver):
                    print(f"    ↳ 📁 Раздел файлов так и не появился")
                    save_debug(driver, f"{slug}_no_files")
                    stats["no_pdfs"] += 1
                    continue

                # ШАГ 2: устойчивый клик с диагностикой видимых папок
                if not click_presentation_folder_robust(driver):
                    print(f"    ↳ 📁 Папки «Презентация объектов» нет")
                    save_debug(driver, f"{slug}_no_folder")
                    stats["no_pdfs"] += 1
                    continue

                time.sleep(3)  # больше времени на рендер содержимого папки

                # ШАГ 3: скачивание
                uploaded = download_all_pdfs_from_folder(driver, s3_client, jk_name)
                if uploaded > 0:
                    stats["with_pdfs"] += 1
                    stats["total"] += uploaded
                    print(f"    ↳ ✓ Загружено: {uploaded} PDF")
                else:
                    stats["no_pdfs"] += 1
                    save_debug(driver, f"{slug}_folder_empty")

            except Exception as e:
                print(f"    [❌] Ошибка: {e}")
                stats["no_pdfs"] += 1
                try: save_debug(driver, f"{slug}_error")
                except: pass

        print("\n" + "=" * 60)
        print("ИТОГ ДОБИВАЛЬЩИКА")
        print(f"  С PDF:      {stats['with_pdfs']} ЖК")
        print(f"  Без PDF:    {stats['no_pdfs']} ЖК")
        print(f"  Всего в S3: {stats['total']} PDF")
        print("=" * 60)

    finally:
        driver.quit()
        clean_download_dir()


if __name__ == "__main__":
    main()
