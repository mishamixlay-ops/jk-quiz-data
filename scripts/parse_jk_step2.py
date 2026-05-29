#!/usr/bin/env python3
"""
Парсер TrendAgent — Шаг 2: Сбор ссылок на ЖК + парсинг базовых параметров с витрины
"""

import os
import sys
import time
import json
import re
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

LIST_URL = "https://spb.trendagent.ru/objects/list?apartments-level_type%5B0%5D=65b8bfec34b5a527f81042ae&apartments-level_type%5B1%5D=65b8bfec34b5a527f81042af"

SCRIPT_DIR     = Path(__file__).parent
CHROME_PROFILE = SCRIPT_DIR / "chrome_profile"
LINKS_FILE     = SCRIPT_DIR / "jk_links.json"


def get_driver():
    CHROME_PROFILE.mkdir(exist_ok=True)
    opts = Options()
    opts.add_argument(f"--user-data-dir={CHROME_PROFILE}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--start-maximized")
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
    driver.get(LIST_URL)
    wait_for_page_ready(driver)
    time.sleep(5)
    if "sso.trend.tech" in driver.current_url or "login" in driver.current_url:
        print("[!] Сессия устарела. Авторизуйтесь снова и нажмите ENTER…")
        input()
        driver.get(LIST_URL)
        wait_for_page_ready(driver)
    else:
        print("[✓] Авторизация подтверждена.")


def expand_list(driver):
    print("[•] Разворачиваем список ЖК…")
    click_num = 0
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2.5)
        btn = None
        for el in driver.find_elements(By.CSS_SELECTOR, "button"):
            try:
                if el.is_displayed() and ("Показать ещё" in el.text or "Показать еще" in el.text):
                    btn = el
                    break
            except Exception: continue
        if not btn:
            print(f"[✓] Список развёрнут. Сделано кликов: {click_num}")
            break
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.5)
        try: btn.click()
        except Exception: driver.execute_script("arguments[0].click();", btn)
        click_num += 1
        time.sleep(2)


def collect_and_parse_cards(driver) -> list:
    print("[•] Извлекаем и парсим данные плиток ЖК…")
    raw_data = driver.execute_script("""
        var result = [];
        document.querySelectorAll('a').forEach(function(a) {
            var href = a.href || '';
            if (href.includes('/object/') && !href.includes('/flat/')) {
                var text = a.innerText || '';
                if (text.trim().length > 10) {
                    result.push({url: href, raw_text: text});
                }
            }
        });
        return result;
    """)

    processed_jk = []
    seen_urls = set()

    for item in raw_data:
        base_url = item['url'].split("?")[0].rstrip("/")
        if base_url in seen_urls:
            continue
        seen_urls.add(base_url)

        lines = [line.strip() for line in item['raw_text'].split('\n') if line.strip()]
        if not lines:
            continue

        # Базовый разбор по строкам витрины
        jk_name = lines[0]
        
        # Заглушки под параметры
        deadline = "Не указан"
        metro = "Не указано"
        developer = "Не указан"
        finishing = "Не указана"
        district = "Не указан"

        for line in lines:
            if re.search(r'\d\s*кв\.', line) or re.search(r'Сдан', line):
                deadline = line
            elif 'Застройщик:' in line:
                developer = line.replace('Застройщик:', '').strip()
            elif any(word in line.lower() for word in ['отделка', 'отделки', 'под ключ', 'черновая', 'чистовая']):
                finishing = line.split('·')[0].strip() if '·' in line else line
            elif any(word in line.lower() for word in ['р-н', 'район', 'область']):
                district = line.split(',')[0].strip()

        # Попытка выцепить метро (обычно идет после срока сдачи или перед минутами)
        for i, line in enumerate(lines):
            if 'минут' in line and i > 0:
                metro = f"{lines[i-1]} ({line})"

        processed_jk.append({
            "name": jk_name,
            "url": base_url,
            " витрина_данные": {
                "Застройщик": developer,
                "Срок сдачи": deadline,
                "Метро": metro,
                "Район": district,
                "Отделка": finishing
            }
        })

    return processed_jk


def main():
    print("=" * 60)
    print("ОБНОВЛЕННЫЙ ШАГ 2: СБОР И СТРУКТУРИРОВАНИЕ ВИДЖЕТОВ ЖК")
    print("=" * 60)
    driver = get_driver()
    try:
        ensure_auth(driver)
        expand_list(driver)
        jk_list = collect_and_parse_cards(driver)
        
        print(f"\n[✓] Итого обработано ЖК: {len(jk_list)} шт.")
        with open(LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(jk_list, f, ensure_ascii=False, indent=2)
        print(f"[•] Результаты сохранены в {LINKS_FILE.name}")
    finally:
        driver.quit()

if __name__ == "__main__":
    main()