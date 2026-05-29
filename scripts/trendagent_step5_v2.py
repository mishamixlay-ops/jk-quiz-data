#!/usr/bin/env python3
"""
Парсер TrendAgent — Шаг 5: Массовое скачивание PDF из папок «Презентация объектов» в S3.

Логика (по скриншотам реального UI):
  1. На странице ЖК долистываем до раздела «Файлы объекта» (якорь #files).
  2. Список папок виден сразу. Кликаем папку «Презентация объектов».
  3. В открытой папке у каждого файла есть кнопка скачивания (иконка ⬇).
     PDF-ссылок в DOM нет — URL генерируется JS в момент клика.
  4. Настраиваем Chrome на тихие скачивания в локальную папку,
     кликаем по download-кнопкам, ждём появления .pdf, заливаем в S3.
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

# ──────────────────────────────────────────────
# НАСТРОЙКИ
# ──────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent
CHROME_PROFILE = SCRIPT_DIR / "chrome_profile"
LINKS_FILE     = SCRIPT_DIR / "jk_links.json"
DEBUG_DIR      = SCRIPT_DIR / "_debug"
DOWNLOAD_DIR   = SCRIPT_DIR / "_downloads"

# Для теста: True → обработать только первый ЖК и остановиться
ONLY_FIRST_JK = False

# Сколько секунд ждать завершения скачивания одного файла
DOWNLOAD_TIMEOUT = 60

# Сохранять снапшот папки, если PDF не нашлись
SAVE_DEBUG_ON_EMPTY = True

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
    print(f"[❌] КРИТИЧЕСКАЯ ОШИБКА: Конфигурационный файл не найден в {SCRIPT_DIR}")
    sys.exit(1)

S3_ACCESS_KEY  = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY  = os.getenv("S3_SECRET_KEY")
S3_BUCKET      = os.getenv("S3_BUCKET", "royaltyplace")
S3_ENDPOINT    = os.getenv("S3_ENDPOINT", "https://storage.yandexcloud.net")
S3_PREFIX      = "jk_base"

if not S3_ACCESS_KEY or not S3_SECRET_KEY:
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

    # КЛЮЧЕВОЕ: тихие скачивания в DOWNLOAD_DIR + не открывать PDF в браузере
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
    # На случай, если prefs не подхватились на существующем профиле
    driver.execute_cdp_cmd("Page.setDownloadBehavior", {
        "behavior": "allow",
        "downloadPath": str(DOWNLOAD_DIR.absolute()),
    })
    return driver


def wait_for_page_ready(driver, timeout=15):
    for _ in range(timeout):
        if driver.execute_script("return document.readyState") == "complete":
            break
        time.sleep(1)


def ensure_auth(driver):
    print(f"[•] Проверка профиля: {CHROME_PROFILE}")
    driver.get(BASE_URL)
    wait_for_page_ready(driver)
    time.sleep(4)
    if "sso.trend.tech" in driver.current_url or "login" in driver.current_url:
        print("[!] Требуется вход — авторизуйтесь в открывшемся браузере.")
        input("    После входа нажмите ENTER…\n")
        wait_for_page_ready(driver)
        time.sleep(5)
    print("[✓] Авторизация подтверждена.")


def save_debug_snapshot(driver, step_name: str):
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r'[\\/*?:"<>|]', "_", step_name)[:80]
    (DEBUG_DIR / f"{safe}.html").write_text(driver.page_source, encoding="utf-8")
    inv = driver.execute_script(r"""
        function d(el){
            var ds={}; try{for(var k in el.dataset)ds[k]=el.dataset[k];}catch(e){}
            return {tag:el.tagName, text:((el.innerText||'').trim()).slice(0,120),
                    href:el.href||el.getAttribute('href')||'',
                    title:el.getAttribute('title')||'',
                    aria:el.getAttribute('aria-label')||'',
                    cls:(el.className&&el.className.toString)?el.className.toString().slice(0,150):'',
                    dataset:ds, visible:el.offsetParent!==null};
        }
        var out={url:location.href, anchors:[], buttons:[], svgs:[]};
        document.querySelectorAll('a').forEach(a=>out.anchors.push(d(a)));
        document.querySelectorAll('button').forEach(b=>out.buttons.push(d(b)));
        // SVG-иконки тоже фиксируем — у TrendAgent кнопки часто svg+wrapper
        document.querySelectorAll('svg').forEach(function(s){
            out.svgs.push({cls:(s.getAttribute('class')||''), parent:(s.parentElement && s.parentElement.tagName) || ''});
        });
        return out;
    """)
    (DEBUG_DIR / f"{safe}.json").write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"      📸 Снапшот: _debug/{safe}.html / .json")


# ──────────────────────────────────────────────
# НАВИГАЦИЯ ПО СТРАНИЦЕ ЖК
# ──────────────────────────────────────────────
def goto_files_section(driver) -> bool:
    """Переходит к секции #files и ждёт появления списка папок."""
    driver.execute_script("""
        var navLink = document.querySelector('a[href$="#files"]');
        if (navLink) navLink.click();
        else location.hash = 'files';
    """)
    # Ждём появления любой из папок-маркеров (визуальный признак списка)
    MARKERS = ['презентац', 'планировк', 'разрешит', 'условия прода', 'шаблон договор']
    for _ in range(20):  # до 10 сек
        time.sleep(0.5)
        present = driver.execute_script(r"""
            var markers = arguments[0];
            var text = document.body.innerText.toLowerCase();
            return markers.some(function(m){ return text.includes(m); });
        """, MARKERS)
        if present:
            return True
    return False


def click_presentation_folder(driver) -> bool:
    """Кликает по папке «Презентация объектов» в списке папок раздела «Файлы»."""
    clicked = driver.execute_script(r"""
        var TARGET = 'презентация объектов';
        // Ищем самые компактные элементы, содержащие точный текст папки
        var all = document.querySelectorAll('div, span, li, a, button, p, h2, h3, h4');
        var best = null;
        for (var i = 0; i < all.length; i++) {
            var el = all[i];
            if (el.offsetParent === null) continue;
            // Свой текст без потомков
            var own = '';
            for (var j = 0; j < el.childNodes.length; j++) {
                if (el.childNodes[j].nodeType === 3) own += el.childNodes[j].nodeValue;
            }
            own = own.trim().toLowerCase();
            if (own === TARGET) {
                // нашли точное вхождение
                best = el;
                break;
            }
        }
        if (!best) return null;
        // Клик может быть нужен не на самом span, а на ближайшем «строчном» родителе
        var clickable = best;
        // Поднимаемся по родителям, пока не найдём div/li/a/button с pointer/role/onclick
        for (var k = 0; k < 5 && clickable.parentElement; k++) {
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
    """)
    if not clicked:
        return False
    print(f"      [•] Кликнул по: {clicked}")
    # Ждём, пока вид сменится на содержимое папки (появится «← Презентация объектов» или хлебная крошка)
    for _ in range(20):
        time.sleep(0.5)
        opened = driver.execute_script(r"""
            // Признак открытой папки: либо хлебная крошка с этим именем, либо появилась кнопка «назад» к Все документы
            var text = document.body.innerText.toLowerCase();
            // В корне «Все документы» — это просто текст; в открытой папке — он становится кликабельной хлебной крошкой со /
            return text.includes('все документы /') || text.includes('все документы/') || text.includes('← презентация объектов');
        """)
        if opened:
            return True
    return True  # клик прошёл, дальше проверим по содержимому


# ──────────────────────────────────────────────
# СКАЧИВАНИЕ ИЗ ОТКРЫТОЙ ПАПКИ
# ──────────────────────────────────────────────
def mark_download_buttons(driver) -> int:
    """
    Помечает кнопки «скачать» внутри открытой папки уникальным атрибутом data-tp-dl.
    Возвращает количество помеченных кнопок.

    Логика обнаружения: ищет в каждой «строке файла» кнопку с иконкой стрелки вниз
    или с aria-label/title="Скачать"/"Download", либо <a download>.
    """
    count = driver.execute_script(r"""
        // Очищаем прошлые пометки
        document.querySelectorAll('[data-tp-dl]').forEach(function(el){ el.removeAttribute('data-tp-dl'); });

        // Ищем «строки файлов» — обычно это li/div со списком атрибутов файла (имя, размер, дата)
        // Признак строки: содержит и имя файла, и кнопку скачивания рядом.
        // Идём от обратного: находим ВСЕ потенциальные download-кнопки на странице.
        var candidates = [];

        // 1. <a download> или <a href*=".pdf">
        document.querySelectorAll('a[download], a[href*=".pdf"], a[href*=".PDF"]').forEach(function(a){
            if (a.offsetParent !== null) candidates.push(a);
        });

        // 2. Кнопки с aria-label/title содержащим "скач" или "download"
        document.querySelectorAll('button, a').forEach(function(el){
            if (el.offsetParent === null) return;
            var labels = ((el.getAttribute('aria-label')||'') + ' ' + (el.getAttribute('title')||'')).toLowerCase();
            if (labels.includes('скач') || labels.includes('download') || labels.includes('загруз')) {
                candidates.push(el);
            }
        });

        // 3. Кнопки, внутри которых есть svg с классом, содержащим "download" или "arrow-down"
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

        // Уникализуем
        var seen = new Set();
        var unique = candidates.filter(function(el){
            if (seen.has(el)) return false;
            seen.add(el);
            return true;
        });

        // Помечаем
        unique.forEach(function(el, i){ el.setAttribute('data-tp-dl', 'btn_' + i); });
        return unique.length;
    """)
    return count


def get_file_names_in_dir(d: Path) -> set:
    return set(p.name for p in d.iterdir() if p.is_file())


def wait_for_new_download(before_files: set, timeout: int = DOWNLOAD_TIMEOUT) -> Path | None:
    """Ждёт появления нового файла в DOWNLOAD_DIR. Возвращает Path к нему или None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.6)
        # Игнорируем .crdownload (временные)
        current = {p.name for p in DOWNLOAD_DIR.iterdir() if p.is_file() and not p.name.endswith('.crdownload')}
        new_files = current - before_files
        if new_files:
            # Дополнительная проверка, что нет «братских» .crdownload
            still_downloading = any(p.name.endswith('.crdownload') for p in DOWNLOAD_DIR.iterdir())
            if not still_downloading:
                name = next(iter(new_files))
                return DOWNLOAD_DIR / name
    return None


def clean_download_dir():
    """Удаляет всё из DOWNLOAD_DIR (включая забытые .crdownload)."""
    if DOWNLOAD_DIR.exists():
        for p in DOWNLOAD_DIR.iterdir():
            try:
                if p.is_file(): p.unlink()
                elif p.is_dir(): shutil.rmtree(p)
            except Exception:
                pass
    DOWNLOAD_DIR.mkdir(exist_ok=True)


def download_all_pdfs_from_folder(driver, s3_client, jk_name: str) -> int:
    """
    Кликает по всем помеченным download-кнопкам, скачивает PDF, заливает в S3.
    Возвращает количество успешно загруженных PDF.
    """
    clean_download_dir()

    n_buttons = mark_download_buttons(driver)
    print(f"      [•] Найдено download-кнопок в DOM: {n_buttons}")
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
            new_file = wait_for_new_download(before, timeout=DOWNLOAD_TIMEOUT)
            if not new_file:
                print(f"      [!] Файл {i}/{n_buttons}: ничего не скачалось за {DOWNLOAD_TIMEOUT}с")
                continue

            # Фильтруем — нам нужны только PDF
            if not new_file.suffix.lower() == ".pdf":
                print(f"      [-] {new_file.name} — не PDF, пропускаем")
                try: new_file.unlink()
                except: pass
                continue

            # Заливаем в S3
            s3_key = f"{S3_PREFIX}/{safe_dirname(jk_name)}/{new_file.name}"
            try:
                s3_client.upload_file(
                    str(new_file), S3_BUCKET, s3_key,
                    ExtraArgs={"ACL": "public-read", "ContentType": "application/pdf"}
                )
                print(f"      [✓] {new_file.name} → s3://{S3_BUCKET}/{s3_key}")
                uploaded += 1
            except Exception as e:
                print(f"      [!] Ошибка S3 для {new_file.name}: {e}")
            finally:
                try: new_file.unlink()
                except: pass
        except Exception as e:
            print(f"      [!] Ошибка на кнопке {i}: {e}")
    return uploaded


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    print("=" * 60)
    print("ШАГ 5: СКАЧИВАНИЕ PDF ИЗ «ПРЕЗЕНТАЦИЯ ОБЪЕКТОВ» В S3")
    print(f"  Режим теста (ONLY_FIRST_JK): {ONLY_FIRST_JK}")
    print("=" * 60)

    if not LINKS_FILE.exists():
        print(f"[❌] Файл {LINKS_FILE.name} не найден.")
        return

    with open(LINKS_FILE, "r", encoding="utf-8") as f:
        jk_targets = json.load(f)

    if ONLY_FIRST_JK:
        jk_targets = jk_targets[:1]

    print(f"[•] Объектов к обработке: {len(jk_targets)}")
    print(f"[•] Направление S3: {S3_BUCKET}/{S3_PREFIX}/")
    print(f"[•] Папка локальных загрузок: {DOWNLOAD_DIR}")

    s3_client = get_s3_client()
    driver = get_driver()
    stats = {"with_pdfs": 0, "no_pdfs": 0, "total_pdfs": 0, "errors": 0}

    try:
        ensure_auth(driver)

        for index, target in enumerate(jk_targets, 1):
            url = target["url"]
            short_id = url.rstrip('/').split('/')[-1]
            print(f"\n  [{index}/{len(jk_targets)}] {short_id}")

            try:
                driver.get(url)
                wait_for_page_ready(driver)
                time.sleep(3)

                if "sso.trend.tech" in driver.current_url or "login" in driver.current_url:
                    print("[!] Сессия истекла. Авторизуйтесь и нажмите ENTER…")
                    input()
                    driver.get(url)
                    wait_for_page_ready(driver)
                    time.sleep(2)

                # Имя ЖК — берём из <title> страницы. Формат: «Meltzer Hall - TrendAgent».
                # Это самый стабильный источник: не зависит от React, не путается с файлами/статусами.
                def slug_to_name(slug: str) -> str:
                    # meltzer-hall → Meltzer Hall, villa-marina → Villa Marina
                    return ' '.join(w.capitalize() for w in slug.split('-'))

                jk_name = ""
                page_title = (driver.title or "").strip()
                if page_title:
                    # Отрезаем хвост " - TrendAgent" / " | TrendAgent" и подобное
                    cleaned = re.split(r'\s*[-|·–—]\s*TrendAgent', page_title, flags=re.IGNORECASE)[0].strip()
                    # Если разделителя не было — пробуем срезать просто " - " или " | "
                    if cleaned == page_title:
                        cleaned = re.split(r'\s*[|·]\s*', page_title)[0].strip()
                    if 3 <= len(cleaned) <= 80:
                        jk_name = cleaned

                # Фолбэк — поле name из jk_links.json (если осмысленное)
                if not jk_name:
                    candidate = (target.get("name") or "").strip()
                    BAD = {"старт продаж", "продажи открыты", "продажи закрыты", "скоро в продаже"}
                    if candidate and candidate.lower() not in BAD and len(candidate) >= 3:
                        jk_name = candidate

                # Последний фолбэк — нормализованный слаг из URL
                if not jk_name:
                    jk_name = slug_to_name(short_id)

                print(f"    ↳ ЖК: «{jk_name}»")

                # ШАГ 1: переход к разделу #files
                if not goto_files_section(driver):
                    print(f"    ↳ 📁 Раздел файлов не появился — пропускаем.")
                    stats["no_pdfs"] += 1
                    if SAVE_DEBUG_ON_EMPTY:
                        save_debug_snapshot(driver, f"{short_id}_no_files_section")
                    continue

                # ШАГ 2: клик по папке «Презентация объектов»
                if not click_presentation_folder(driver):
                    print(f"    ↳ 📁 Папки «Презентация объектов» нет.")
                    stats["no_pdfs"] += 1
                    if SAVE_DEBUG_ON_EMPTY:
                        save_debug_snapshot(driver, f"{short_id}_no_presentation_folder")
                    continue

                # Дать React дорендерить список файлов в папке
                time.sleep(2)

                # ШАГ 3: скачать все PDF
                uploaded = download_all_pdfs_from_folder(driver, s3_client, jk_name)
                if uploaded > 0:
                    stats["with_pdfs"] += 1
                    stats["total_pdfs"] += uploaded
                    print(f"    ↳ ✓ Загружено в S3: {uploaded} PDF")
                else:
                    stats["no_pdfs"] += 1
                    if SAVE_DEBUG_ON_EMPTY:
                        save_debug_snapshot(driver, f"{short_id}_folder_open_no_pdfs")

            except Exception as e:
                print(f"    [❌] Ошибка ЖК: {e}")
                stats["errors"] += 1
                if SAVE_DEBUG_ON_EMPTY:
                    try: save_debug_snapshot(driver, f"{short_id}_error")
                    except: pass

        print("\n" + "=" * 60)
        print("🎉 ГОТОВО")
        print(f"  С PDF:      {stats['with_pdfs']} ЖК")
        print(f"  Без PDF:    {stats['no_pdfs']} ЖК")
        print(f"  Всего в S3: {stats['total_pdfs']} PDF")
        print(f"  Ошибки:     {stats['errors']}")
        print("=" * 60)

    finally:
        driver.quit()
        clean_download_dir()


if __name__ == "__main__":
    main()
