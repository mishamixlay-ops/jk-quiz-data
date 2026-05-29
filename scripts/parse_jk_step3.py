#!/usr/bin/env python3
"""
Парсер TrendAgent — Шаг 3: Тестовое обогащение данных одного ЖК
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

SCRIPT_DIR     = Path(__file__).parent
CHROME_PROFILE = SCRIPT_DIR / "chrome_profile"
LINKS_FILE     = SCRIPT_DIR / "jk_links.json"
OUTPUT_FILE    = SCRIPT_DIR / "test_jk_output.json"


def get_driver():
    CHROME_PROFILE.mkdir(exist_ok=True)
    opts = Options()
    opts.add_argument(f"--user-data-dir={CHROME_PROFILE}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    
    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
    )
    return driver


def parse_jk_page(driver, base_info: dict) -> dict:
    # Скроллинг
    for pct in [0.3, 0.7, 1.0]:
        driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {pct});")
        time.sleep(1)
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(1)

    # Соединяем параметры с Шага 2 и новые блоки
    result = {
        "jk_name": base_info["name"],
        "url": base_info["url"],
        "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tech_specs": base_info[" витрина_данные"], 
        "about": "",
        "features": [],
        "promo": [],
        "mortgage": [],
        "installments": []
    }

    # Перехват block_id
    block_id = ""
    for _ in range(10):
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
        time.sleep(0.5)
        
    result["block_id"] = block_id

    # Текст "Об объекте"
    try:
        result["about"] = driver.execute_script("""
            var els = document.querySelectorAll('.object-about p, [class*="about"] p');
            if (els.length) return Array.from(els).map(e => e.innerText.trim()).filter(t => t).join('\\n\\n');
            var el = document.querySelector('.object-about__text, .object-about');
            return el ? el.innerText.trim() : '';
        """) or ""
    except: pass

    # Преимущества
    try:
        result["features"] = driver.execute_script("""
            var items = document.querySelectorAll('.object-about__item, [class*="advantage"], .advantages-list__item');
            return Array.from(items).map(el => el.innerText.trim()).filter(t => t);
        """) or []
    except: pass

    if not block_id:
        print("  [!] block_id для финансовых блоков не найден.")
        return result

    # Сбор акций и ипотеки (через API)
    city_id = "58c665588b6aa52311afa01b"
    cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
    token = cookies.get("auth_token", "")

    # Ипотека
    try:
        mort_url = f"https://mortgage-api.trendagent.ru/blocks/{block_id}/?premiseType=apartment"
        if token: mort_url += f"&auth_token={token}&city={city_id}&lang=ru"
        resp = requests.get(mort_url, timeout=10)
        if resp.status_code == 200:
            results = resp.json().get("data", {}).get("results", [])
            for p in results:
                rate = p.get("rate", {}).get("min")
                if rate:
                    result["mortgage"].append({
                        "bank": p.get("bank", {}).get("name", "Банк"),
                        "rate": f"{rate}%",
                        "pv": f"от {p.get('firstpay')}%"
                    })
    except: pass

    # Акции
    try:
        disc_url = f"https://discounts.trendagent.ru/blocks/{block_id}/discounts?builder=58c665588b6aa52311afa0c3&city={city_id}&lang=ru"
        if token: disc_url += f"&auth_token={token}"
        resp = requests.get(disc_url, timeout=10)
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
    print("ОБНОВЛЕННЫЙ ШАГ 3: ТЕСТ ОБОГАЩЕНИЯ ЖК")
    print("=" * 60)
    
    with open(LINKS_FILE, "r", encoding="utf-8") as f:
        jk_list = json.load(f)

    # Для теста ипотеки и акций давай выберем объект, у которого точно есть открытые цены.
    # Если первый объект в списке — снова Meltzer Hall, мы можем принудительно переключиться на ЖК с ценами,
    # но давай сначала просто возьмем первый из твоего свежего списка jk_links.json
    target_jk = jk_list[0]
    print(f"[•] Тестируем ЖК: {target_jk['name']}")
    
    driver = get_driver()
    try:
        driver.get(target_jk['url'])
        time.sleep(4)
        
        jk_data = parse_jk_page(driver, target_jk)
        
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(jk_data, f, ensure_ascii=False, indent=2)
            
        print(f"\n[✓] ТЕСТ ЗАВЕРШЕН!")
        print(f"[•] Название: {jk_data['jk_name']}")
        print(f"[•] Собрано тех. параметров витрины: {len(jk_data['tech_specs'])}")
        print(f"[•] Программ ипотеки найдено: {len(jk_data['mortgage'])}")
        print(f"[•] Акций найдено: {len(jk_data['promo'])}")
        
    finally:
        driver.quit()

if __name__ == "__main__":
    main()