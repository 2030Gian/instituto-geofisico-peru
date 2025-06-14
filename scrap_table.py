import requests
from bs4 import BeautifulSoup
import boto3
import uuid
#jaja
def lambda_handler(event, context):
    url = "https://ultimosismo.igp.gob.pe/ultimo-sismo/sismos-reportados"

    response = requests.get(url)
    if response.status_code != 200:
        return {'statusCode': response.status_code, 'body': 'Error al acceder a la página web'}

    soup = BeautifulSoup(response.content, 'html.parser')
    table = soup.find('table')
    if not table:
        return {'statusCode': 404, 'body': 'No se encontró la tabla en la página web'}

    headers = [th.get_text(strip=True) for th in table.find_all('th')]

    rows = []
    for tr in table.find_all('tr')[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all('td')]
        if len(cells) != len(headers):
            continue
        row_data = {headers[i]: cells[i] for i in range(len(headers))}
        rows.append(row_data)

    dynamodb = boto3.resource('dynamodb')
    table_db = dynamodb.Table('TablaWebScrapping')

    existing = table_db.scan().get('Items', [])
    with table_db.batch_writer() as batch:
        for it in existing:
            batch.delete_item(Key={'id': it['id']})

    for idx, row in enumerate(rows, start=1):
        item = row.copy()
        item['#'] = idx
        item['id'] = str(uuid.uuid4())
        table_db.put_item(Item=item)

    return {'statusCode': 200, 'body': rows}
