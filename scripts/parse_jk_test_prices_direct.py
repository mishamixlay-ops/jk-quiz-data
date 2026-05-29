#!/usr/bin/env python3
"""
Парсер TrendAgent — Исправленный прямой тест парсинга цен с инициализацией сессии
"""

import os
import sys
import time
import json
import re
from datetime import datetime
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# ──────────────────────────────────────────────
# ССЫЛКИ ДЛЯ ТЕСТИРОВАНИЯ
# ──────────────────────────────────────────────
TEST_URL = "https://spb.trendagent.ru/object/annino-siti/"
LIST_URL = "https://spb.trendagent.ru/objects/list?apartments-level_type%5B0%5D=65b8bfec34b5a527f81042ae&apartments-level_type%5B1%5D=65b8bfec34b5a527f81042af"

SCRIPT_DIR     = Path(__file__).parent
CHROME_PROFILE = SCRIPT_DIR / "chrome_profile"
OUTPUT_FILE    = SCRIPT_DIR / "debug_prices_result.json"


def get_driver():
    """Инициализирует Chrome с флагами против вылетов и сохранения сессии."""
    CHROME_PROFILE.mkdir(exist_ok=True)
    opts = Options()
    opts.add_argument(f"--user-data-dir={CHROME_PROFILE}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--start-maximized")
    
    # Жесткие флаги для предотвращения аварийного завершения Chrome (Crash)
    opts.add_argument("--remote-allow-origins=*")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    
    driver = webdriver.Chrome(options=opts)
    
    # Исправлено: корректный метод CDP для скрытия автоматизации
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
    )
    return driver


def wait_for_page_ready(driver, timeout=15):
    """Ожидает полной загрузки страницы."""
    for _ in range(timeout):
        if driver.execute_script("return document.readyState") == "complete":
            break
        time.sleep(1)


def ensure_platform_session(driver):
    """Прогревает B2B-сессию через каталог, чтобы SPA подгрузила токены."""
    print("[•] Шаг А: Инициализируем платформу через общий список…")
    driver.get(LIST_URL)
    wait_for_page_ready(driver)
    time.sleep(5)
    
    if "sso.trend.tech" in driver.current_url or "login" in driver.current_url:
        print("[!] Сессия не активна. Войдите в аккаунт и нажмите ENTER в терминале…")
        input()
        driver.get(LIST_URL)
        wait_for_page_ready(driver)
        time.sleep(3)
    else:
        print("[✓] Сессия платформы успешно подтверждена.")


def parse_prices_global_search(driver) -> dict:
    """Переходит на страницу ЖК и собирает вилку цен."""
    print(f"[•] Шаг Б: Переходим на прямую страницу ЖК и скроллим для рендеринга…")
    driver.get(TEST_URL)
    wait_for_page_ready(driver)
    time.sleep(5)  # Даём SPA-компонентам время на отрисовку стоимости

    for pct in [0.4, 0.8, 1.0]:
        driver.execute_script(f"window.scrollTo(0, document.body.scrollHeight * {pct});")
        time.sleep(1)
    driver.execute_script("window.scrollTo(0, 0);")
    time.sleep(0.5)

    title = driver.execute_script("""
        var h1 = document.querySelector('h1, [class*="title"], [class*="name"], .object-header__title');
        if (h1 && h1.innerText.trim()) return h1.innerText.trim();
        return document.title.split('|')[0].split('-')[0].trim();
    """) or "Неизвестный ЖК"

    result = {
        "jk_name": title,
        "url": driver.current_url,
        "parsed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "price_ranges": {}
    }

    try:
        prices_map = driver.execute_script("""
            var ranges = {};
            var elements = document.querySelectorAll('div, tr, li, p, span, [class*="price"], [class*="offer"]');
            
            elements.forEach(function(el) {
                var txt = el.innerText || '';
                if (txt.length > 3 && txt.length < 150) {
                    var lines = txt.split('\\n').map(s => s.trim()).filter(s => s);
                    
                    // Паттерн 1: Двухстрочный (тип планировки сверху, цена снизу)
                    for (var i = 0; i < lines.length - 1; i++) {
                        var line = lines[i];
                        var nextLine = lines[i+1];
                        if (/(студи|к\\.кв|комн|\\dЕ-|планиров)/i.test(line)) {
                            if (/(от|до|млн|₽|\\d)/i.test(nextLine) && !/(кв\\.м|корпус|сдача|этаж|агент|высота)/i.test(nextLine)) {
                                var k = line.replace(/\\s+/g, ' ').trim();
                                var v = nextLine.replace(/\\s+/g, ' ').trim();
                                if (k.length < 40 && v.length < 40 && k.length > 2) {
                                    ranges[k] = v;
                                }
                            }
                        }
                    }
                    
                    // Паттерн 2: Однострочный ("Студия от X млн ₽")
                    if (lines.length === 1) {
                        var singleLine = lines[0];
                        if (/(студи|к\\.кв|комн|\\dЕ-)/i.test(singleLine) && /(от|до|млн|₽)/i.test(singleLine)) {
                            var match = singleLine.match(/(.*?)(от\\s*\\d+.*|до\\s*\\d+.*|\\d+\\s*млн.*|\\d+\\s*₽.*)/i);
                            if (match) {
                                var k2 = match[1].trim();
                                var v2 = match[2].trim();
                                if (k2.length < 40 && v2.length < 40 && k2.length > 2) {
                                    ranges[k2] = v2;
                                }
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
        print(f"  [!] Ошибка выполнения JS: {e}")

    return result


def main():
    print("=" * 60)
    print("ОБНОВЛЕННЫЙ ТЕСТ ПАРСИНГА ЦЕН ЖК С ИНИЦИАЛИЗАЦИЕЙ СЕССИИ")
    print("=" * 60)

    driver = get_driver()
    try:
        # Прогружаем сессию платформы
        ensure_platform_session(driver)
        
        # Парсим карточку ЖК
        data = parse_prices_global_search(driver)

        print("\n" + "=" * 50)
        print(f"РЕЗУЛЬТАТ ДЛЯ ЖК: {data['jk_name']}")
        print("=" * 50)
        if data["price_ranges"]:
            for room, prc in data["price_ranges"].items():
                print(f"  🟢 {room} ──> {prc}")
        else:
            print("  ❌ Цены не найдены. Объекту требуется ручной разбор структуры.")
        print("=" * 50)

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n[•] Лог сохранен в: {OUTPUT_FILE.name}")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()