#!/usr/bin/env python3
"""
Парсер TrendAgent — Шаг 3 (Тест цен): Извлечение вилки цен по типам квартир
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
OUTPUT_FILE    = SCRIPT_DIR / "test_jk_prices_output.json"


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


def wait_for_page_ready(driver, timeout=15):
    for _ in range(timeout):
        if driver.execute_script("return document.readyState") == "complete":
            break
        time.sleep(1)


def parse_jk_page_with_prices(driver, base_info: dict) -> dict:
    print(f"[•] Скроллим страницу ЖК для раскрытия блоков предложений…")
    for pct in [0.3, 0.7, 1.0]:
        driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {pct});")
        time.sleep(1)
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.5)

    result = {
        "jk_name": base_info["name"],
        "url": base_info["url"],
        "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tech_specs": base_info[" витрина_данные"],
        "price_ranges": {},  # Сюда запишем вилку цен по типам квартир
        "about": ""
    }

    # ── ИНЪЕКЦИЯ JS: Собираем типы квартир и цены из DOM-верстки ──
    try:
        prices_map = driver.execute_script("""
            var ranges = {};
            // Ищем любые карточки, строки или блоки предложений
            var blocks = document.querySelectorAll('[class*="offer"], [class*="flat"], [class*="price"], [class*="card"], [class*="row"], [class*="item"]');
            
            blocks.forEach(function(b) {
                var txt = b.innerText || '';
                var lines = txt.split('\\n').map(s => s.trim()).filter(s => s);
                
                for (var i = 0; i < lines.length - 1; i++) {
                    var line = lines[i];
                    var nextLine = lines[i+1];
                    
                    // Регулярка ищет упоминание комнатности (студии, 1-к.кв, 3Е-к.кв и т.д.)
                    if (/(студи|к\\.кв|к-кв|-к\\s*в|комн)/i.test(line)) {
                        // Регулярка проверяет, что следующая строка — это цена (есть цифры, ₽, млн, от, до)
                        if (/(от|до|млн|₽|запрос|\\d)/i.test(nextLine) && !/(кв|комн|корпус|сдача|этаж)/i.test(nextLine)) {
                            var key = line.replace(/\\s+/g, ' ').trim();
                            var val = nextLine.replace(/\\s+/g, ' ').trim();
                            
                            // Защита от слишком длинных мусорных строк
                            if (key.length < 30 && val.length < 40) {
                                ranges[key] = val;
                            }
                        }
                    }
                }
            });
            return ranges;
        """)
        if prices_map:
            result["price_ranges"] = prices_map
    except Exception as e:
        print(f"  [!] Ошибка JS-экстрактора цен: {e}")

    # До кучи заберем описание объекта
    try:
        result["about"] = driver.execute_script("""
            var els = document.querySelectorAll('.object-about p, [class*="about"] p');
            if (els.length) return Array.from(els).map(e => e.innerText.trim()).filter(t => t).join('\\n\\n');
            var el = document.querySelector('.object-about__text, .object-about');
            return el ? el.innerText.trim() : '';
        """) or ""
    except: pass

    return result


def main():
    print("=" * 60)
    print("ЗАПУСК ШАГА 3 (ТЕСТ ЦЕН): ОПРЕДЕЛЕНИЕ ВИЛКИ СТОИМОСТИ")
    print("=" * 60)

    if not LINKS_FILE.exists():
        print(f"[❌] Ошибка: Файл {LINKS_FILE.name} не найден. Сначала запусти Шаг 2.")
        return

    with open(LINKS_FILE, "r", encoding="utf-8") as f:
        jk_list = json.load(f)

    # Автоматически ищем в списке ЖК, который НЕ является Meltzer Hall, чтобы были цены
    target_jk = None
    for jk in jk_list:
        if "meltzer" not in jk["url"].lower():
            target_jk = jk
            break
            
    if not target_jk:
        target_jk = jk_list[0]

    print(f"[•] Выбран ЖК с ценами: {target_jk['name']}")
    print(f"[•] URL объекта: {target_jk['url']}\n")

    driver = get_driver()
    try:
        driver.get(target_jk['url'])
        wait_for_page_ready(driver)
        time.sleep(4)

        # Выполняем сбор
        jk_data = parse_jk_page_with_prices(driver, target_jk)

        # Сохраняем тестовый файл
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(jk_data, f, ensure_ascii=False, indent=2)

        print(f"\n[✓] ТЕСТ ЗАВЕРШЕН!")
        print(f"[•] Название объекта: {jk_data['jk_name']}")
        print(f"[•] Найдено категорий цен: {len(jk_data['price_ranges'])}")
        print("\nСобрана следующая вилка цен:")
        for r_type, r_price in jk_data["price_ranges"].items():
            print(f"    • {r_type}: {r_price}")
            
        print(f"\n[•] Полный результат сохранен в: {OUTPUT_FILE.name}")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()