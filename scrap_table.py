import requests
from bs4 import BeautifulSoup
import boto3
import uuid
import time
from botocore.exceptions import ClientError
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import os
import json
import traceback # Para imprimir el stack trace completo en caso de error

# --- CONFIGURACIÓN DE RUTAS PARA CHROMIUM Y CHROMEDRIVER EN LAMBDA ---
# ESTAS RUTAS ASUMEN QUE ESTÁN EN EL DIRECTORIO '/opt/' DE UNA CAPA DE LAMBDA.
# NO LAS CAMBIES A MENOS QUE ESTÉS INCLUYENDO LOS BINARIOS DIRECTAMENTE EN TU PAQUETE DE DESPLIEGUE.
CHROMIUM_PATH = "/opt/chrome/chrome"
CHROMEDRIVER_PATH = "/opt/chromedriver"

def lambda_handler(event, context):
    url = "https://ultimosismo.igp.gob.pe/ultimo-sismo/sismos-reportados"
    
    driver = None # Inicializar driver a None para el bloque finally
    try:
        # Configuración de opciones para Chromium headless
        chrome_options = Options()
        chrome_options.add_argument("--headless")           # Ejecutar en modo sin interfaz gráfica
        chrome_options.add_argument("--no-sandbox")         # Necesario en entornos como Lambda
        chrome_options.add_argument("--disable-gpu")        # Deshabilitar aceleración de hardware
        chrome_options.add_argument("--window-size=1280x1696") # Tamaño de ventana para renderizado
        chrome_options.add_argument("--single-process")     # Solo un proceso (reduce memoria)
        chrome_options.add_argument("--disable-dev-shm-usage") # Reduce el uso de /dev/shm, crucial en Lambda
        chrome_options.add_argument("--disable-dev-tools")  # Deshabilita herramientas de desarrollo
        chrome_options.add_argument("--no-zygote")          # Otro argumento para entornos limitados
        chrome_options.add_argument("--remote-debugging-port=9222") # Útil para depuración (opcional)

        # User-Agent para simular un navegador real
        user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'
        chrome_options.add_argument(f'user-agent={user_agent}')

        # Indicar la ubicación del binario de Chromium (dentro de la capa /opt/)
        chrome_options.binary_location = CHROMIUM_PATH

        # Configurar el servicio de Chromedriver
        service = Service(executable_path=CHROMEDRIVER_PATH)

        # Inicializar el navegador
        print("Iniciando WebDriver de Chrome...")
        driver = webdriver.Chrome(service=service, options=chrome_options)
        print("WebDriver iniciado. Navegando a la URL:", url)
        driver.get(url)

        # Esperar a que la tabla o un elemento dentro de ella esté presente.
        # Esto es CRUCIAL para contenido cargado con JavaScript.
        print("Esperando a que el contenido dinámico de la tabla cargue...")
        try:
            # Buscar la tabla por la etiqueta 'table'. Si la tabla tiene un ID o clase,
            # sería mejor usar By.ID, By.CLASS_NAME o By.CSS_SELECTOR para más precisión.
            WebDriverWait(driver, 25).until( # Espera hasta 25 segundos
                EC.presence_of_element_located((By.TAG_NAME, "table"))
            )
            print("La etiqueta <table> fue detectada en la página.")
        except Exception as e:
            print(f"ERROR: La tabla no apareció después del tiempo de espera. Detalle: {e}")
            print(f"Contenido de la página al fallar la espera (primeros 2000 chars):\n{driver.page_source[:2000]}...")
            return {'statusCode': 404, 'body': 'Tiempo de espera agotado: La tabla no se encontró en la página después de la carga dinámica.'}

        # Obtener el contenido HTML después de que JavaScript ha renderizado la página
        soup = BeautifulSoup(driver.page_source, 'html.parser')

        # Encontrar la tabla con BeautifulSoup
        table = soup.find('table') 

        if not table:
            print("DEBUG: BeautifulSoup no encontró la tabla en el HTML parseado, aunque Selenium la detectó.")
            return {'statusCode': 404, 'body': 'No se encontró la tabla en el HTML parseado por BeautifulSoup.'}

        # Extraer los encabezados de la tabla (th)
        headers_raw = [th.get_text(strip=True) for th in table.find_all('th')]
        
        # Limpiar los nombres de los encabezados para que sean compatibles con DynamoDB.
        # '#' no es un caracter permitido en nombres de atributos sin ser escapado.
        # También se eliminan puntos y se limpian espacios extra.
        headers = []
        for h in headers_raw:
            clean_h = h.replace('#', 'Numero').replace('.', '').strip()
            headers.append(clean_h)
        print(f"Encabezados extraídos y limpiados: {headers}")

        # Extraer las filas de datos de la tabla (td)
        rows = []
        # Omitir la primera fila (que son los encabezados)
        for tr in table.find_all('tr')[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all('td')]
            
            # Validar que el número de celdas coincida con el número de encabezados
            if len(cells) != len(headers):
                print(f"Advertencia: Fila con número de celdas ({len(cells)}) diferente al número de encabezados ({len(headers)}). Saltando fila: {cells}")
                continue # Saltar esta fila y pasar a la siguiente
            
            row_data = {}
            for i, cell_value in enumerate(cells):
                # Asignar el valor de la celda al encabezado correspondiente
                row_data[headers[i]] = cell_value.strip() # Limpiar espacios en blanco de los valores

            rows.append(row_data)
        
        print(f"Se extrajeron {len(rows)} filas de datos de la tabla.")
        if not rows:
            print("No se encontraron filas de datos válidas después del scraping.")
            return {'statusCode': 404, 'body': 'No se encontraron filas de datos en la tabla después del procesamiento.'}

        # --- Guardar los datos en DynamoDB ---
        dynamodb = boto3.resource('dynamodb')
        table_db = dynamodb.Table('TablaSismosSelenium') # Usar el nombre de tabla definido en serverless.yml

        # Limpieza previa: Eliminar todos los elementos existentes en la tabla
        print("Iniciando proceso de limpieza de datos previos en DynamoDB...")
        try:
            # Scanear la tabla para obtener todos los IDs existentes (manejo de paginación)
            scan_response = table_db.scan(ProjectionExpression="id") # Solo proyectar el 'id' para eficiencia
            existing_ids = [item['id'] for item in scan_response.get('Items', [])]
            
            while 'LastEvaluatedKey' in scan_response:
                scan_response = table_db.scan(ExclusiveStartKey=scan_response['LastEvaluatedKey'], ProjectionExpression="id")
                existing_ids.extend([item['id'] for item in scan_response.get('Items', [])])

            if existing_ids:
                print(f"Encontrados {len(existing_ids)} elementos previos para limpiar.")
                with table_db.batch_writer() as batch:
                    for item_id in existing_ids:
                        batch.delete_item(Key={'id': item_id})
                print(f"Limpieza de {len(existing_ids)} elementos previos completada.")
            else:
                print("No se encontraron elementos previos en 'TablaSismosSelenium' para limpiar.")
        except ClientError as e:
            print(f"ERROR DynamoDB al escanear/eliminar datos previos: {e}")
            traceback.print_exc() # Imprimir el stack trace
            return {'statusCode': 500, 'body': f'Error DynamoDB al limpiar datos previos: {str(e)}'}

        # Insertar los nuevos datos en DynamoDB
        print("Iniciando inserción de nuevos datos en DynamoDB...")
        inserted_count = 0
        with table_db.batch_writer() as batch:
            for row_data in rows:
                item_to_put = row_data.copy()
                item_to_put['id'] = str(uuid.uuid4()) # Generar un ID único para cada entrada

                # Convertir todos los valores a string para DynamoDB
                # Esto es una buena práctica para asegurar compatibilidad de tipos
                for key, value in item_to_put.items():
                    item_to_put[key] = str(value)

                batch.put_item(Item=item_to_put)
                inserted_count += 1
        
        print(f"Inserción de {inserted_count} elementos completada en 'TablaSismosSelenium'.")

        # Retornar los datos parseados como JSON en la respuesta HTTP
        return {
            'statusCode': 200,
            'body': json.dumps(rows)
        }

    except Exception as e:
        print(f"ERROR FATAL: Error inesperado en la función Lambda: {e}")
        traceback.print_exc() # Imprimir el stack trace completo para depuración
        return {
            'statusCode': 500,
            'body': f'Error interno del servidor durante el scraping: {str(e)}'
        }
    finally:
        # Asegurarse de cerrar el navegador para liberar recursos en Lambda
        if driver:
            print("Cerrando WebDriver...")
            driver.quit()
            print("WebDriver cerrado.")