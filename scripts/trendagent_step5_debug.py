#!/usr/bin/env python3
"""
Парсер TrendAgent — Шаг 5: Массовое скачивание PDF-презентаций и выгрузка в S3 Yandex Cloud
ФИНАЛЬНАЯ ВЕРСИЯ.
Ключевое отличие от прошлой: секция файлов на странице подгружается ЛЕНИВО.
Чтобы она появилась в DOM, нужно перейти на якорь #files (или кликнуть по нав-ссылке).
"""

import os
import sys
import time
import json
import requests
import re
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# ──────────────────────────────────────────────
# НАСТРОЙКИ
# ──────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent
CHROME_PROFILE = SCRIPT_DIR / "chrome_profile"
LINKS_FILE     = SCRIPT_DIR / "jk_links.json"
DEBUG_DIR      = SCRIPT_DIR / "_debug"

# Если True — для каждого объекта без файлов сохраняем дамп в _debug/
SAVE_DEBUG_ON_EMPTY = True

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

S3_ACCESS_KEY  = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY  = os.getenv("S3_SECRET_KEY")
S3_BUCKET      = os.getenv("S3_BUCKET", "royaltyplace")
S3_ENDPOINT    = os.getenv("S3_ENDPOINT", "https://storage.yandexcloud.net")
S3_PREFIX      = "jk_base"

if not S3_ACCESS_KEY or not S3_SECRET_KEY:
    print(f"[❌] ОШИБКА КОНФИГУРАЦИИ: Проверь ключи S3.")
    raise RuntimeError("❌ Ключи доступа S3 не найдены.")

BASE_URL = "https://spb.trendagent.ru/objects/list/"


# ──────────────────────────────────────────────
# ХЕЛПЕРЫ
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
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()


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
    print(f"[•] Проверка активности профиля: {CHROME_PROFILE}")
    driver.get(BASE_URL)
    wait_for_page_ready(driver)
    time.sleep(4)
    if "sso.trend.tech" in driver.current_url or "login" in driver.current_url:
        print("[!] Требуется вход — авторизуйтесь в открывшемся браузере.")
        input("    После успешного входа нажмите ENTER…\n")
        wait_for_page_ready(driver)
        time.sleep(5)
    print("[✓] Авторизация подтверждена.")


def save_debug_snapshot(driver, jk_name: str):
    """Сохраняет HTML + инвентарь страницы в _debug/ — для разбора, если что-то не работает."""
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r'[\\/*?:"<>|]', "_", jk_name or "page")[:60] or "page"
    (DEBUG_DIR / f"{safe}.html").write_text(driver.page_source, encoding="utf-8")
    inv = driver.execute_script(r"""
        function d(el){
            var ds={}; try{for(var k in el.dataset)ds[k]=el.dataset[k];}catch(e){}
            return {tag:el.tagName, text:((el.innerText||'').trim()).slice(0,120),
                    href:el.href||el.getAttribute('href')||'',
                    download:el.getAttribute('download')||'',
                    onclick:el.getAttribute('onclick')||'',
                    cls:(el.className&&el.className.toString)?el.className.toString().slice(0,150):'',
                    dataset:ds, visible:el.offsetParent!==null};
        }
        var out={url:location.href, anchors:[], buttons:[], iframes:[]};
        document.querySelectorAll('a').forEach(a=>out.anchors.push(d(a)));
        document.querySelectorAll('button').forEach(b=>out.buttons.push(d(b)));
        document.querySelectorAll('iframe').forEach(f=>out.iframes.push({src:f.src||'', id:f.id||''}));
        return out;
    """)
    (DEBUG_DIR / f"{safe}.json").write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"      [debug] Снапшот: _debug/{safe}.html  /  .json")


# ──────────────────────────────────────────────
# КЛЮЧЕВАЯ ФУНКЦИЯ: открывает секцию #files и собирает ссылки
# ──────────────────────────────────────────────
def open_files_section(driver) -> bool:
    """
    Активирует ленивый блок файлов на странице.
    Возвращает True, если в DOM появились элементы, похожие на ссылки/кнопки скачивания.
    """
    # 1. Переходим на якорь — это триггерит lazy-load в TrendAgent
    driver.execute_script("""
        // Сначала жмём якорь в нав-меню (если есть), иначе ставим hash вручную
        var navLink = document.querySelector('a[href$="#files"]');
        if (navLink) { navLink.click(); }
        else { location.hash = 'files'; }
    """)

    # 2. Ждём появления секции в DOM (до 10 секунд)
    for _ in range(20):
        time.sleep(0.5)
        ready = driver.execute_script(r"""
            // Секция с id="files" или контейнер, где появились ссылки на pdf/файлы
            var section = document.getElementById('files') ||
                          document.querySelector('[id*="files"], [class*="files"], [class*="documents"]');
            if (!section) return false;
            // Проверяем, что внутри что-то реальное появилось
            var hasLinks = section.querySelectorAll('a[href*=".pdf"], a[download], a[href*="/storage"], a[href*="/file"], a[href*="/download"], button').length > 0;
            return hasLinks;
        """)
        if ready:
            return True

    # 3. Финальная прокрутка — на всякий случай добиваем триггер lazy-load
    driver.execute_script("""
        var s = document.getElementById('files');
        if (s) s.scrollIntoView({behavior:'instant', block:'start'});
        window.scrollBy(0, 300);
    """)
    time.sleep(2.5)

    # Проверяем ещё раз
    return driver.execute_script(r"""
        var section = document.getElementById('files') ||
                      document.querySelector('[id*="files"], [class*="files"], [class*="documents"]');
        if (!section) return false;
        return section.querySelectorAll('a, button').length > 0;
    """) or False


def expand_documents_view(driver) -> bool:
    """
    Кликает по элементу «Все документы» (React-кнопка-обёртка) — она разворачивает
    список папок документов. Возвращает True, если клик удался.
    """
    clicked = driver.execute_script(r"""
        var TARGETS = ['все документы', 'документы', 'все файлы'];
        var nodes = document.querySelectorAll('a, button, [role="button"], div, span, li');
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            if (el.offsetParent === null) continue;
            // Свой текст без вложенных простыней
            var own = '';
            for (var j = 0; j < el.childNodes.length; j++) {
                if (el.childNodes[j].nodeType === 3) own += el.childNodes[j].nodeValue;
            }
            own = own.trim().toLowerCase();
            // Точное совпадение по своему тексту (только короткие элементы)
            if (own && own.length < 40 && TARGETS.indexOf(own) !== -1) {
                try {
                    el.scrollIntoView({block:'center'});
                    el.click();
                    return own;
                } catch(e) {}
            }
        }
        return null;
    """)

    if not clicked:
        return False

    print(f"      [•] Кликнул на: «{clicked}»")

    # Ждём появления признаков раскрытия — либо текст «презентация», либо новые ссылки/кнопки
    for _ in range(20):  # до 10 сек
        time.sleep(0.5)
        ready = driver.execute_script(r"""
            // Появилась ли где-то «презентация» в тексте?
            var bodyText = document.body.innerText.toLowerCase();
            if (bodyText.includes('презентац')) return true;
            // Или ссылка на pdf где-то?
            if (document.querySelector('a[href*=".pdf"], a[href*=".PDF"]')) return true;
            return false;
        """)
        if ready:
            return True
    return True  # клик прошёл, но контента так и не дождались — пусть следующий шаг разбирается


def open_presentation_folder(driver) -> str:
    """
    После открытия секции #files ищет папку «Презентация объектов» и кликает по ней.
    Возвращает строку-статус: 'opened' | 'not_found' | 'no_pdfs_after_open'.
    """
    # 1. Ищем и кликаем по элементу с текстом «Презентация объектов»
    clicked = driver.execute_script(r"""
        var TARGET = 'презентация объектов';
        // Берём только относительно компактные элементы (не родительские контейнеры)
        var nodes = document.querySelectorAll(
            'a, button, [role="button"], [role="tab"], div, span, li, h2, h3, h4, p'
        );
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            if (el.offsetParent === null) continue;
            // Свой текст элемента, без вложенных простыней
            var own = '';
            for (var j = 0; j < el.childNodes.length; j++) {
                if (el.childNodes[j].nodeType === 3) own += el.childNodes[j].nodeValue;
            }
            own = own.toLowerCase().trim();
            // Если своего текста нет — пробуем innerText, но только для коротких элементов
            var txt = own || ((el.innerText || '').toLowerCase().trim());
            if (!txt || txt.length > 80) continue;
            if (txt.includes(TARGET)) {
                try {
                    el.scrollIntoView({block:'center'});
                    el.click();
                    return txt;
                } catch(e) {}
            }
        }
        return null;
    """)

    if not clicked:
        return 'not_found'

    print(f"      [•] Открыл папку: «{clicked}»")

    # 2. Ждём появления PDF внутри (до 8 секунд)
    for _ in range(16):
        time.sleep(0.5)
        has_pdfs = driver.execute_script(r"""
            var section = document.getElementById('files') ||
                          document.querySelector('[id*="files"], [class*="files"]');
            var scope = section || document;
            return scope.querySelectorAll(
                'a[href*=".pdf"], a[href*=".PDF"], a[download], ' +
                'a[href*="/storage"], a[href*="/files/"], a[href*="/media/"], ' +
                '[data-href*=".pdf"], [data-url*=".pdf"]'
            ).length > 0;
        """)
        if has_pdfs:
            return 'opened'

    return 'no_pdfs_after_open'


def find_presentation_pdfs(driver) -> list:
    """Собирает ссылки на PDF в открытой папке «Презентация объектов»."""
    pdfs = driver.execute_script(r"""
        var section = document.getElementById('files') ||
                      document.querySelector('[id*="files"], [class*="files"], [class*="documents"]');
        var scope = section || document;

        var results = [];
        var seen = new Set();
        function push(url, title) {
            if (!url || seen.has(url)) return;
            seen.add(url);
            results.push({url: url, title: (title||'').trim() || 'Презентация'});
        }

        // a) Прямые <a> на .pdf или с атрибутом download
        scope.querySelectorAll('a').forEach(function(a) {
            var href = a.href || '';
            var h = href.toLowerCase();
            // Берём только .pdf или явные download — фильтры на /storage/ /files/ /media/ только если в URL .pdf
            if (h.includes('.pdf') || a.hasAttribute('download')) {
                push(href, a.innerText || a.getAttribute('download') || a.title);
            }
        });

        // b) data-href / data-url / data-file с .pdf
        scope.querySelectorAll('[data-href],[data-url],[data-file]').forEach(function(el) {
            var u = el.dataset.href || el.dataset.url || el.dataset.file;
            if (!u) return;
            if (u.toLowerCase().includes('.pdf')) {
                var full = u.startsWith('http') ? u : (location.origin + u);
                push(full, el.innerText);
            }
        });

        // c) onclick с URL .pdf внутри
        scope.querySelectorAll('button, [role="button"]').forEach(function(b) {
            var oc = b.getAttribute('onclick') || '';
            var m = oc.match(/['"]((?:https?:\/\/|\/)[^'"\s]+\.(?:pdf|PDF))['"]/);
            if (m) push(m[1].startsWith('http') ? m[1] : location.origin + m[1], b.innerText);
        });

        return results;
    """)
    return pdfs


def download_and_upload_to_s3(session, s3_client, pdf_url: str, pdf_title: str, jk_name: str) -> bool:
    clean_title = safe_filename(pdf_title)
    if not clean_title.lower().endswith('.pdf'):
        clean_title += ".pdf"
    local_path = SCRIPT_DIR / clean_title
    s3_key = f"{S3_PREFIX}/{safe_dirname(jk_name)}/{clean_title}"
    try:
        resp = session.get(pdf_url, stream=True, timeout=30)
        if resp.status_code == 200:
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            s3_client.upload_file(
                str(local_path), S3_BUCKET, s3_key,
                ExtraArgs={"ACL": "public-read", "ContentType": "application/pdf"}
            )
            return True
        else:
            print(f"      [!] HTTP {resp.status_code} для {pdf_url}")
    except Exception as e:
        print(f"      [!] Ошибка передачи {pdf_title}: {e}")
    finally:
        if local_path.exists():
            try: os.remove(local_path)
            except: pass
    return False


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    print("=" * 60)
    print("ЗАПУСК ШАГА 5: МАССОВАЯ ЗАГРУЗКА ПРЕЗЕНТАЦИЙ В S3")
    print("=" * 60)

    if not LINKS_FILE.exists():
        print(f"[❌] Файл {LINKS_FILE.name} не найден.")
        return

    with open(LINKS_FILE, "r", encoding="utf-8") as f:
        jk_targets = json.load(f)

    print(f"[•] Всего объектов: {len(jk_targets)}")
    print(f"[•] Направление S3: {S3_BUCKET}/{S3_PREFIX}/")

    s3_client = get_s3_client()
    driver = get_driver()

    stats = {"with_files": 0, "without_files": 0, "uploaded_total": 0, "errors": 0}

    try:
        ensure_auth(driver)
        user_agent = driver.execute_script("return navigator.userAgent")

        for index, target in enumerate(jk_targets, 1):
            url = target["url"]
            short_id = url.rstrip('/').split('/')[-1]
            print(f"\n  [{index}/{len(jk_targets)}] {short_id}")

            try:
                driver.get(url)
                wait_for_page_ready(driver)
                time.sleep(3)

                if "sso.trend.tech" in driver.current_url or "login" in driver.current_url:
                    print("[!] Сессия истекла! Авторизуйтесь и нажмите ENTER…")
                    input()
                    driver.get(url)
                    wait_for_page_ready(driver)
                    time.sleep(2)

                # Имя ЖК со страницы
                real_jk_name = driver.execute_script(r"""
                    var h1 = document.querySelector('h1, [class*="title"], [class*="name"], .object-header__title');
                    if (h1 && h1.innerText.trim()) return h1.innerText.trim();
                    return document.title.split('|')[0].split('-')[0].trim();
                """) or target.get("name", short_id)
                print(f"    ↳ ЖК: «{real_jk_name}»")

                # ШАГ 1 — активируем секцию #files на странице
                opened = open_files_section(driver)
                if not opened:
                    print(f"    ↳ 📁 Секция файлов не активировалась (у ЖК нет документов?)")
                    stats["without_files"] += 1
                    if SAVE_DEBUG_ON_EMPTY:
                        save_debug_snapshot(driver, real_jk_name)
                    continue

                # ШАГ 2 — разворачиваем «Все документы» (показывает список папок)
                expand_documents_view(driver)

                # ШАГ 3 — открываем папку «Презентация объектов»
                folder_status = open_presentation_folder(driver)
                if folder_status == 'not_found':
                    print(f"    ↳ 📁 У этого ЖК нет папки «Презентация объектов».")
                    stats["without_files"] += 1
                    if SAVE_DEBUG_ON_EMPTY:
                        save_debug_snapshot(driver, real_jk_name)
                    continue
                if folder_status == 'no_pdfs_after_open':
                    print(f"    ↳ 📁 Папка открыта, но PDF в ней не появились.")
                    stats["without_files"] += 1
                    if SAVE_DEBUG_ON_EMPTY:
                        save_debug_snapshot(driver, real_jk_name)
                    continue

                # ШАГ 4 — собираем PDF
                pdf_files = find_presentation_pdfs(driver)
                if not pdf_files:
                    print(f"    ↳ 📁 Папка открылась, но скрипт не нашёл ссылок на PDF.")
                    stats["without_files"] += 1
                    if SAVE_DEBUG_ON_EMPTY:
                        save_debug_snapshot(driver, real_jk_name)
                    continue

                print(f"    ↳ ✓ Найдено PDF: {len(pdf_files)}. Скачиваем в S3…")
                stats["with_files"] += 1

                # HTTP-сессия с куками и юзер-агентом из Chrome
                session = requests.Session()
                for cookie in driver.get_cookies():
                    session.cookies.set(cookie['name'], cookie['value'])
                session.headers.update({
                    "Referer": url,
                    "User-Agent": user_agent,
                    "Accept": "application/pdf,application/octet-stream,*/*",
                })

                uploaded_count = 0
                for pdf in pdf_files:
                    if download_and_upload_to_s3(session, s3_client, pdf["url"], pdf["title"], real_jk_name):
                        uploaded_count += 1
                stats["uploaded_total"] += uploaded_count

                if uploaded_count > 0:
                    print(f"    [✓] Выгружено: {uploaded_count} шт. → {S3_PREFIX}/{safe_dirname(real_jk_name)}/")

            except Exception as e:
                print(f"    [❌] Ошибка: {e}")
                stats["errors"] += 1
                time.sleep(2)

        print("\n" + "=" * 60)
        print("🎉 ГОТОВО")
        print(f"  С файлами:    {stats['with_files']}")
        print(f"  Без файлов:   {stats['without_files']}")
        print(f"  Всего в S3:   {stats['uploaded_total']} PDF")
        print(f"  Ошибки:       {stats['errors']}")
        print("=" * 60)

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
