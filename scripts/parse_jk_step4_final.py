#!/usr/bin/env python3
"""
Парсер TrendAgent — Шаг 4: Массовый парсер ЖК с интеграцией базовых цен (от...)
"""

import os
import sys
import time
import json
import requests
import re
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# ──────────────────────────────────────────────
# НАСТРОЙКИ И ПУТИ
# ──────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent
CHROME_PROFILE = SCRIPT_DIR / "chrome_profile"
LINKS_FILE     = SCRIPT_DIR / "jk_links.json"
FINAL_DB_FILE  = SCRIPT_DIR / "jk_data.json"

# Ссылка на список для прогрева B2B-контекста перед заходом на карточки
LIST_URL = "https://spb.trendagent.ru/objects/list?apartments-level_type%5B0%5D=65b8bfec34b5a527f81042ae&apartments-level_type%5B1%5D=65b8bfec34b5a527f81042af"


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


def parse_single_jk(driver, base_info: dict) -> dict:
    """Глубокий парсинг страницы одного ЖК: параметры + цены от + API финансов."""
    # Плавный скроллинг карточки для вызова ленивой отрисовки DOM и триггера API
    for pct in [0.3, 0.7, 1.0]:
        driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {pct});")
        time.sleep(0.8)
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.5)

    # 1. Извлекаем чистое название ЖК
    real_name = driver.execute_script("""
        var h1 = document.querySelector('h1, [class*="title"], [class*="name"], .object-header__title');
        if (h1 && h1.innerText.trim()) return h1.innerText.trim();
        return document.title.split('|')[0].split('-')[0].trim();
    """) or base_info["name"]

    result = {
        "jk_name": real_name,
        "url": base_info["url"],
        "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tech_specs": base_info[" витрина_данные"],  # Характеристики из Шага 2
        "price_ranges": {},                          # Базовые цены от...
        "about": "",
        "features": [],
        "promo": [],
        "mortgage": [],
        "installments": []
    }

    # 2. Извлекаем минимальные стоимости ("Цены от...") по категориям
    try:
        prices_map = driver.execute_script(r"""
            var ranges = {};
            var elements = document.querySelectorAll('div, tr, li, p, span, a, td');
            
            elements.forEach(function(el) {
                var txt = (el.innerText || '').trim();
                if (txt.length > 3 && txt.length < 120) {
                    var roomType = null;
                    
                    if (/студия|студии/i.test(txt)) roomType = 'Студия';
                    else if (/1-к\.кв|1ккв|1-комн/i.test(txt)) roomType = '1-к.кв';
                    else if (/2-к\.кв|2ккв|2-комн/i.test(txt)) roomType = '2-к.кв';
                    else if (/3-к\.кв|3ккв|3-комн/i.test(txt)) roomType = '3-к.кв';
                    else if (/4-к\.кв|4ккв|4-комн/i.test(txt)) roomType = '4-к.кв';
                    else if (/2Е-к\.кв|2Е/i.test(txt)) roomType = '2Е-к.кв';
                    else if (/3Е-к\.кв|3Е/i.test(txt)) roomType = '3Е-к.кв';
                    else if (/4Е-к\.кв|4Е/i.test(txt)) roomType = '4Е-к.кв';
                    
                    if (roomType) {
                        var cleanTxt = txt.replace(/\\s(?=\\d)/g, '');
                        var matches = cleanTxt.match(/\\d[\\d\\s\\.,]*\\d/g);
                        
                        if (matches) {
                            matches.forEach(function(numStr) {
                                var num = parseFloat(numStr.replace(/\\s/g, '').replace(',', '.'));
                                if (num > 150000 || (num >= 1.0 && num <= 200.0 && txt.includes('млн'))) {
                                    if (num <= 200.0 && txt.includes('млн')) {
                                        num = Math.round(num * 1000000);
                                    }
                                    // Ищем именно минимальную цену ("от")
                                    if (!ranges[roomType] || num < ranges[roomType]) {
                                        ranges[roomType] = num;
                                    }
                                }
                            });
                        }
                    }
                }
            });
            
            // Форматируем обратно в читаемые строки "от X ₽"
            var formatted = {};
            for (var k in ranges) {
                formatted[k] = "от " + ranges[k].toString().replace(/\\B(?=(\\d{3})+(?!\\d))/g, " ") + " ₽";
            }
            return formatted;
        """)
        if prices_map:
            result["price_ranges"] = prices_map
    except: pass

    # 3. Ловим block_id для финансовых микросервисов
    block_id = ""
    for _ in range(8):
        block_id = driver.execute_script("""
            var entries = performance.getEntriesByType('resource');
            for (var i = 0; i < entries.length; i++) {
                var n = entries[i].name;
                if (n.includes('trendagent.ru')) {
                    var m = n.match(/blocks\\/([a-f0-9]{24})/);
                    if (m) return m[1];
                }
            }
            return '';
        """)
        if block_id: break
        time.sleep(0.4)

    result["block_id"] = block_id

    # 4. Текст описания комплекса
    try:
        result["about"] = driver.execute_script("""
            var els = document.querySelectorAll('.object-about p, [class*="about"] p');
            if (els.length) return Array.from(els).map(e => e.innerText.trim()).filter(t => t).join('\\n\\n');
            var el = document.querySelector('.object-about__text, .object-about');
            return el ? el.innerText.trim() : '';
        """) or ""
    except: pass

    # 5. Преимущества
    try:
        result["features"] = driver.execute_script("""
            var items = document.querySelectorAll('.object-about__item, [class*="advantage"], .advantages-list__item');
            return Array.from(items).map(el => el.innerText.trim()).filter(t => t);
        """) or []
    except: pass

    if not block_id:
        return result

    # 6. Запросы к API (Ипотека, Рассрочки, Акции)
    city_id = "58c665588b6aa52311afa01b"
    cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
    token = cookies.get("auth_token", "")

    # Ипотека
    try:
        mort_url = f"https://mortgage-api.trendagent.ru/blocks/{block_id}/?premiseType=apartment"
        if token: mort_url += f"&auth_token={token}&city={city_id}&lang=ru"
        resp = requests.get(mort_url, timeout=8)
        if resp.status_code == 200:
            results = resp.json().get("data", {}).get("results", [])
            seen_progs = set()
            for p in results:
                rate = p.get("rate", {}).get("min")
                bank = p.get("bank", {}).get("name", "Банк")
                if rate and bank not in seen_progs:
                    seen_progs.add(bank)
                    result["mortgage"].append({
                        "bank": bank,
                        "rate": f"{rate}%",
                        "pv": f"от {p.get('firstpay')}%" if p.get('firstpay') else ""
                    })
    except: pass

    # Рассрочки
    try:
        inst_url = f"https://tiny-installments-api.trendagent.ru/v1/blocks/{block_id}?city={city_id}&lang=ru"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = requests.get(inst_url, headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for d in data:
                    if 'trend' not in d.get("name", "").lower():
                        result["installments"].append({
                            "name": d.get("name", ""),
                            "firstpay": d.get("firstpay", ""),
                            "term": d.get("term", ""),
                            "price": d.get("price", "")
                        })
    except: pass

    # Акции
    try:
        disc_url = f"https://discounts.trendagent.ru/blocks/{block_id}/discounts?builder=58c665588b6aa52311afa0c3&city={city_id}&lang=ru"
        if token: disc_url += f"&auth_token={token}"
        resp = requests.get(disc_url, timeout=8)
        if resp.status_code == 200:
            discounts = resp.json().get("discounts", [])
            for d in discounts:
                if d.get("is_active", True) and 'trend' not in d.get("name", "").lower():
                    result["promo"].append({
                        "name": d.get("name", ""),
                        "value": d.get("value", "") or "",
                        "description": re.sub(r'<[^>]+>', '', d.get("description", "") or "").strip()
                    })
    except: pass

    return result


def main():
    print("=" * 60)
    print("ЗАПУСК ШАГА 4: МАССОВЫЙ ПАРСЕР ЖК (БАЗОВЫЕ ХАРАКТЕРИСТИКИ + ЦЕНЫ ОТ...)")
    print("=" * 60)

    if not LINKS_FILE.exists():
        print(f"[❌] Ошибка: Файл {LINKS_FILE.name} не найден. Сначала запусти Шаг 2.")
        return

    with open(LINKS_FILE, "r", encoding="utf-8") as f:
        jk_targets = json.load(f)

    print(f"[•] Всего объектов для обработки: {len(jk_targets)}")

    db_data = {}
    if FINAL_DB_FILE.exists():
        try:
            with open(FINAL_DB_FILE, "r", encoding="utf-8") as f:
                db_data = json.load(f)
            print(f"[•] Восстановлено ЖК из существующей базы: {len(db_data)}")
        except: pass

    driver = get_driver()
    try:
        # Прогреваем сессию платформы через список, чтобы SPA инициализировалась штатно
        print("[•] Инициализация сессии на платформе…")
        driver.get(LIST_URL)
        wait_for_page_ready(driver)
        time.sleep(5)

        for index, target in enumerate(jk_targets, 1):
            url = target["url"]
            
            # Пропуск уже обработанных URL (защита от прерываний)
            if any(obj.get("url") == url for obj in db_data.values()):
                continue

            print(f"  [{index}/{len(jk_targets)}] Заходим на {target['name']}…")
            try:
                driver.get(url)
                wait_for_page_ready(driver)
                time.sleep(3.5)

                if "sso.trend.tech" in driver.current_url or "login" in driver.current_url:
                    print("\n[!] Сессия устарела. Авторизуйтесь и нажмите ENTER…")
                    input()
                    driver.get(url)
                    wait_for_page_ready(driver)
                    time.sleep(2)

                # Запуск глубокого сбора данных по ЖК
                jk_full_info = parse_single_jk(driver, target)
                
                clean_name = jk_full_info["jk_name"]
                db_data[clean_name] = jk_full_info
                
                # Инкрементальное сохранение базы на каждом шаге
                with open(FINAL_DB_FILE, "w", encoding="utf-8") as f:
                    json.dump(db_data, f, ensure_ascii=False, indent=2)

                print(f"    [✓] Сохранен: {clean_name}")
                print(f"        ↳ Цен собрано: {len(jk_full_info['price_ranges'])}, Ипотека: {len(jk_full_info['mortgage'])}, Рассрочки: {len(jk_full_info['installments'])}, Акции: {len(jk_full_info['promo'])}")

            except Exception as item_error:
                print(f"    [❌] Ошибка парсинга этого ЖК: {item_error}")
                time.sleep(2)

        print("\n" + "=" * 50)
        print("МАССОВЫЙ ПАРСИНГ УСПЕШНО ЗАВЕРШЕН!")
        print(f"[✓] Итоговая база сформирована. Всего ЖК: {len(db_data)} шт.")
        print(f"[•] Файл с базой: {FINAL_DB_FILE.name}")
        print("=" * 50)

    finally:
        driver.quit()


if __name__ == "__main__":
    main()