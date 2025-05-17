import warnings
warnings.filterwarnings(
    "ignore",
    message=".*LibreSSL 2\\.8\\.3.*"    # basta con hacer match contra el texto que imprime urllib3
)


import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import pandas as pd
import time
import requests
import os
import json
import shutil


# Archivo donde guardaremos, por licitación, el último Estado/Adjudicado/Monto vistos
STATE_FILE = "state.json"

# Cargar estado previo
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
        # Normalizar claves
        state = {
            pid: {
                "Estado": info.get("Estado", ""),
                "Adjudicado a": info.get("Adjudicado a", ""),
                "Monto Adjudicado": info.get("Monto Adjudicado", info.get("Monto", ""))
            }
            for pid, info in state.items()
        }
else:
    state = {}

# ← Punto A: DEBUG tras cargar el estado previo
logging.info("DEBUG: claves en state previo: %s", list(state.keys()))

# Lista de prefijos a consultar
CLAVES = ["CFE-0201", "CFE-0604"]

# Configura tu bot
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("Debe definir las variables de entorno TELEGRAM_TOKEN y TELEGRAM_CHAT_ID")

def enviar_telegram(texto):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': CHAT_ID,
        'text': texto,
        'parse_mode': 'Markdown'
    }
    try:
        logging.info("Enviando petición a Telegram…")
        resp = requests.post(url, data=payload, timeout=10)
        logging.info("Telegram API respondió: %d — %s", resp.status_code, resp.text)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error("Error en enviar_telegram: %s", e)

# Función para guardar el estado
def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

# Opciones headless, GPU disabled
def make_driver():    
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    service = Service(shutil.which("chromedriver"))
    return webdriver.Chrome(service=service, options=opts)

for clave in CLAVES:
    driver = make_driver()
    wait = WebDriverWait(driver, 30)
    try:
        driver.get("https://msc.cfe.mx/Aplicaciones/NCFE/Concursos/")

# — Aquí va TODO tu bloque de “busca por clave”, scraping, notificaciones, save_state() …

try:
    for clave in CLAVES:
        try:
            # Navegar y buscar por clave
            driver.get("https://msc.cfe.mx/Aplicaciones/NCFE/Concursos/")
            campo = wait.until(EC.visibility_of_element_located((By.XPATH, '//input[@placeholder="Número de procedimiento"]')))
            campo.clear()
            campo.send_keys(clave)
            boton = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'button.btn.btn-success')))
            boton.click()
            time.sleep(5)
            wait.until(EC.presence_of_element_located((By.XPATH, "//table//tr[td]")))

            filas = driver.find_elements(By.XPATH, "//table//tr[td]")
            data = []
            for fila in filas:
                celdas = fila.find_elements(By.TAG_NAME, 'td')
                if len(celdas) >= 10:
                    data.append({
                        "Número de Procedimiento": celdas[0].text.strip(),
                        "Testigo Social": celdas[1].text.strip(),
                        "Entidad Federativa": celdas[2].text.strip(),
                        "Descripción": celdas[3].text.strip(),
                        "Tipo de Procedimiento": celdas[4].text.strip(),
                        "Tipo de Contratación": celdas[5].text.strip(),
                        "Fecha Publicación": celdas[6].text.strip(),
                        "Estado": celdas[7].text.strip(),
                        "Adjudicado A": celdas[8].text.strip(),
                        "Monto Adjudicado": celdas[9].text.strip()
                    })

            df = pd.DataFrame(data)
            csv_name = f"concursos_{clave}.csv"
            df.to_csv(csv_name, index=False)
            logging.info("Se guardaron %d filas en %s", len(df), csv_name)

            # Notificaciones por cada registro
            for _, row in df.iterrows():
                numero_procedimiento    = row["Número de Procedimiento"]
                estado_nuevo            = row["Estado"]
            
                # ← Punto B1: DEBUG del estado que acabas de scrape
                logging.info("DEBUG fila %s: Estado scrapeado = %r", numero_procedimiento, estado_nuevo)

                adjudicado_nuevo        = row["Adjudicado A"]
                monto_nuevo             = row["Monto Adjudicado"]
                descripcion_nuevo       = row["Descripción"]
                fecha_pub_nuevo         = row["Fecha Publicación"]

                if numero_procedimiento not in state:
                    msg = (
                        f"⚠️ Nueva licitación: \n"
                        f"- {descripcion_nuevo}\n"
                        f"- {numero_procedimiento}\n"
                        f"- Fecha {fecha_pub_nuevo}\n"
                    )
                    enviar_telegram(msg)
                    state[numero_procedimiento] = {
                        "Estado": estado_nuevo,
                        "Adjudicado a": adjudicado_nuevo,
                        "Monto Adjudicado": monto_nuevo
                    }
                    save_state()
                else:
                    prev = state[numero_procedimiento]
                    # ← Punto B2: DEBUG del estado previo desde state.json
                    logging.info("DEBUG prev[%s] = %r", numero_procedimiento, prev["Estado"])
                    cambios = []
                    if estado_nuevo != prev["Estado"]:
                        cambios.append(f"Estado: {prev['Estado']} → {estado_nuevo}")
                    if adjudicado_nuevo != prev["Adjudicado a"]:
                        cambios.append(f"Adjudicado a: {prev['Adjudicado a']} → {adjudicado_nuevo}")
                    prev_monto = prev.get("Monto Adjudicado", "")
                    if prev_monto and monto_nuevo != prev_monto:
                        cambios.append(f"Monto Adjudicado: {prev_monto} → {monto_nuevo}")

                    if cambios:
                        mensaje = (
                            f"ℹ️ Cambio de estado: \n"
                            f" - {descripcion_nuevo}\n"
                            f" - {numero_procedimiento}\n"
                            + "\n".join(f"- {c}" for c in cambios)
                        )
                        enviar_telegram(mensaje)
                        state[numero_procedimiento] = {
                            "Estado":           estado_nuevo,
                            "Adjudicado a":     adjudicado_nuevo,
                            "Monto Adjudicado": monto_nuevo
                        }
                        save_state()

        # fin for claves

        except Exception as e:
            logging.exception(f"‼️ Falló scraping para {clave}, saltando al siguiente")
            continue

except Exception as e:
        # Esta excepción solo saltará si falla algo *fuera* del bucle
        logging.exception("‼️ Excepción fatal fuera del loop de claves")   

finally:
    driver.quit()
    logging.info("Driver cerrado.")
