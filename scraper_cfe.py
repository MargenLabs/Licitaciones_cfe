#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import warnings
warnings.filterwarnings("ignore", message=".*LibreSSL 2\\.8\\.3.*")

import logging
import os
import json
import time
import shutil
import requests
import pandas as pd
import traceback

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException
from selenium.common.exceptions import TimeoutException
from selenium.common.exceptions import NoSuchElementException

# ─── Configuración básica ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Usar una ruta absoluta garantiza que state.json se cargue y guarde en el
# mismo directorio del script, sin importar desde dónde se ejecute.
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(ROOT_DIR, "state.json")
CLAVES     = ["CFE-0201", "CFE-0604"]
BOT_TOKEN  = os.getenv("TELEGRAM_TOKEN")
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("Define TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en el entorno")

# ─── Funciones auxiliares ─────────────────────────────────────────────────────
def enviar_telegram(texto: str):
    """Envía un mensaje de texto a Telegram."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = dict(chat_id=CHAT_ID, text=texto, parse_mode="Markdown")
    try:
        logging.info("Enviando Telegram…")
        r = requests.post(url, data=payload, timeout=10)
        logging.info("Telegram respondió: %d %s", r.status_code, r.text)
        r.raise_for_status()
    except Exception as e:
        logging.error("Error en enviar_telegram: %s", e)

def load_state() -> dict:
    """Carga el JSON de estado previo o devuelve {}."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Normaliza nombres de campo si tuvieras cambios de esquema
        return {
            pid: {
                "Estado":           v.get("Estado", ""),
                "Adjudicado a":     v.get("Adjudicado a", ""),
                "Monto Adjudicado": v.get("Monto Adjudicado", v.get("Monto", ""))
            }
            for pid, v in raw.items()
        }
    else:
        return {}

def save_state(state: dict):
    """Guarda el JSON de estado."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def setup_driver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--enable-logging")
    options.add_argument("--v=1")
    # Capturamos logs de browser y driver desde Chrome
    options.set_capability('goog:loggingPrefs', {'browser': 'ALL', 'driver': 'ALL'})

    chromedriver = shutil.which("chromedriver")
    service = Service(executable_path=chromedriver)
    return webdriver.Chrome(service=service, options=options)

# ─── Bloque principal ────────────────────────────────────────────────────────
def main():
    state = load_state()
        # ———————————— INTEGRIDAD: inicializamos conjunto de PIDs vistos en la web
    current_pids = set()
    logging.info("Claves en state previo: %s", list(state.keys()))

    driver = setup_driver()
    wait   = WebDriverWait(driver, 30)

    for clave in CLAVES:
        # 1) Navegar al portal
        driver.get("https://msc.cfe.mx/Aplicaciones/NCFE/Concursos/")
        # 2) Rellenar número de procedimiento
        inp = wait.until(EC.visibility_of_element_located(
            (By.XPATH, '//input[@placeholder="Número de procedimiento"]')
        ))
        inp.clear()
        inp.send_keys(clave)
        btn = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.btn.btn-success")
        ))
        logging.info("🔎 Buscando procedimiento: '%s'", clave)
        btn.click()

        # 3) Esperar a que aparezca al menos una fila
        try:
            wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//table//tbody//tr")
                )
            )
        except TimeoutException:
            logging.info("🔍 Sin resultados para %s", clave)
            continue

        # 4) Procesar todas las páginas de resultados
        data = []
        while True:
            rows = driver.find_elements(By.XPATH, "//table//tbody//tr")
            if not rows:
                break  # no hay filas, salimos

            # 5) Por cada fila extraemos datos y los guardamos
            for row in rows:
                pid    = row.find_element(By.XPATH, "./td[1]").text
                estado = row.find_element(By.XPATH, "./td[2]").text
                adj    = row.find_element(By.XPATH, "./td[3]").text
                monto  = row.find_element(By.XPATH, "./td[4]").text
                desc   = row.find_element(By.XPATH, "./td[5]").text
                fecha  = row.find_element(By.XPATH, "./td[6]").text
                
                current_pids.add(pid)
                data.append({
                    "Número de Procedimiento": pid,
                    "Descripción":             desc,
                    "Fecha Publicación":       fecha,
                    "Estado":                  estado,
                    "Adjudicado A":            adj,
                    "Monto Adjudicado":        monto,
                })

            # 6) Intentar ir a la siguiente página
            try:
                btn_next = driver.find_element(By.LINK_TEXT, "Siguiente")
                if "disabled" in btn_next.get_attribute("class"):
                    break
                btn_next.click()
                wait.until(EC.staleness_of(rows[0]))
            except NoSuchElementException:
                break

        df = pd.DataFrame(data)
        logging.info("Scrapeó %d licitaciones para %s", len(df), clave)

        # 5) Detectar nuevas o cambios
        for _, row in df.iterrows():
            pid      = row["Número de Procedimiento"]
            estado   = row["Estado"]
            adjud    = row["Adjudicado A"]
            monto    = row["Monto Adjudicado"]
            desc     = row["Descripción"]
            fecha    = row["Fecha Publicación"]

            if pid not in state:
                # nueva licitación
                msg = (
                    f"⚠️ *Nueva licitación*:\n"
                    f"- {desc}\n"
                    f"- {pid}\n"
                    f"- Fecha: {fecha}"
                )
                enviar_telegram(msg)
                state[pid] = {"Estado": estado, "Adjudicado a": adjud, "Monto Adjudicado": monto}
                save_state(state)

            else:
                prev = state[pid]
                diffs = []
                if estado != prev["Estado"]:
                    diffs.append(f"Estado: {prev['Estado']} → {estado}")
                if adjud != prev["Adjudicado a"]:
                    diffs.append(f"Adjudicado a: {prev['Adjudicado a']} → {adjud}")
                pm = prev.get("Monto Adjudicado", "")
                if pm and monto != pm:
                    diffs.append(f"Monto: {pm} → {monto}")

                if diffs:
                    msg = (
                        f"ℹ️ *Cambio detectado*:\n"
                        f"- {desc}\n"
                        f"- {pid}\n"
                        + "\n".join(f"- {d}" for d in diffs)
                    )
                    enviar_telegram(msg)
                    state[pid] = {"Estado": estado, "Adjudicado a": adjud, "Monto Adjudicado": monto}
                    save_state(state)
    # ———————————— INTEGRIDAD: comparamos con lo que ya teníamos en state.json
    existing_pids = set(state.keys())
    missing = current_pids - existing_pids
    extra   = existing_pids - current_pids
    if missing:
        logging.warning("⚠️ Estas PIDs FALTAN en state.json (pág. web): %s", missing)
    if extra:
        logging.warning("⚠️ Estas PIDs están en state.json PERO ya no aparecen en la web: %s", extra)
    if not missing and not extra:
        logging.info("✅ state.json coincide 100%% con los PIDs visibles en la web (%d).", len(current_pids))
    # ———————————— PURGAR los PIDs que sobraban
    for pid in extra:
        state.pop(pid, None)
    save_state(state)
    # volcamos logs internos antes de cerrar
    #for entry in driver.get_log("browser"):
        #logging.info("📘 Browser log: %s", entry)
    #for entry in driver.get_log("driver"):
        #logging.info("🛠 Driver log: %s", entry)

    driver.quit()
    logging.info("Driver cerrado.")

if __name__ == "__main__":
    main()
