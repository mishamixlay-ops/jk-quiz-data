#!/usr/bin/env python3
"""
Парсер TrendAgent — Шаг 1: Авторизация и переход к списку ЖК (Обновлённый URL)
"""

import os
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# ──────────────────────────────────────────────
# НАСТРОЙКИ И ССЫЛКИ
# ──────────────────────────────────────────────
# Твоя новая ссылка на список ЖК с применёнными фильтрами
LIST_URL = "https://spb.trendagent.ru/objects/list?apartments-level_type%5B0%5D=65b8bfec34b5a527f81042ae&apartments-level_type%5B1%5D=65b8bfec34b5a527f81042af"

SCRIPT_DIR     = Path(__file__).parent
CHROME_PROFILE = SCRIPT_DIR / "chrome_profile"


# ──────────────────────────────────────────────
# УПРАВЛЕНИЕ БРАУЗЕРОМ
# ──────────────────────────────────────────────
def get_driver():
    """Инициализирует Chrome с сохранением сессии в локальную папку."""
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
    """Ожидает полную загрузку страницы и стабилизацию URL."""
    for _ in range(timeout):
        if driver.execute_script("return document.readyState") == "complete":
            break
        time.sleep(1)
    prev_url = ""
    for _ in range(10):
        cur_url = driver.current_url
        if cur_url == prev_url:
            break
        prev_url = cur_url
        time.sleep(1)


def is_on_sso(driver):
    """Проверяет, находится ли браузер на странице авторизации."""
    return "sso.trend.tech" in driver.current_url or "login" in driver.current_url


def ensure_auth(driver):
    """Проверяет состояние сессии и при необходимости ждет ручной авторизации."""
    print(f"[•] Профиль браузера: {CHROME_PROFILE}")
    
    for attempt in range(3):
        driver.get(LIST_URL)
        wait_for_page_ready(driver)
        time.sleep(4)  # Время на восстановление SPA-состояния
        cur = driver.current_url
        
        if is_on_sso(driver):
            break  
        if "apartments-level_type" in cur:
            break  # Фильтры в URL на месте, сессия активна
            
        print(f"[•] Попытка {attempt+1}: Сброс фильтров, переходим заново…")
        time.sleep(2)

    if not is_on_sso(driver):
        print("[✓] Авторизация не требуется, сессия активна.")
        return

    print("[!] Необходим вход в систему. Пожалуйста, авторизуйтесь в открывшемся окне Chrome.")
    input("    После успешного входа нажмите ENTER в этом окне терминала…\n")
    
    print("[•] Ожидаем завершения авторизации…", end=" ", flush=True)
    for _ in range(30):
        if not is_on_sso(driver):
            break
        time.sleep(1)
    print("Готово.")
    time.sleep(5)

    print("[•] Переходим на страницу списка ЖК с фильтрами…")
    driver.get(LIST_URL)
    wait_for_page_ready(driver)
    time.sleep(3)


# ──────────────────────────────────────────────
# ОСНОВНОЙ ИСПОЛНЯЕМЫЙ БЛОК
# ──────────────────────────────────────────────
def main():
    print("=" * 60)
    print("ЗАПУСК ШАГА 1: ПРОВЕРКА ДОСТУПА И ПЕРЕХОД К СПИСКУ ЖК")
    print("=" * 60)
    
    driver = get_driver()
    try:
        # Проверяем авторизацию и загружаем целевую страницу
        ensure_auth(driver)
        
        # Фиксация итогового состояния
        final_url = driver.current_url
        print("\n" + "=" * 50)
        print("РЕЗУЛЬТАТ ВЫПОЛНЕНИЯ:")
        print(f"[✓] Браузер успешно перешёл на целевую страницу.")
        print(f"[•] Текущий URL в Chrome: {final_url[:90]}...")
        print("=" * 50)
        print("\nБраузер остаётся открытым. Проверь, загрузились ли плитки ЖК по новым фильтрам.")
        
        # Удерживаем скрипт, чтобы браузер не закрылся сразу
        input("\nНажми ENTER в терминале, чтобы завершить шаг и закрыть браузер...")

    finally:
        driver.quit()
        print("\n[✓] Сессия закрыта. Готовы переходить к следующему шагу.")


if __name__ == "__main__":
    main()