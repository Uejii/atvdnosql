# Exercicio 1 - Georreferencia com MongoDB e GeoJSON

O coletor baixa um CSV de dados abertos, valida as coordenadas, converte cada registro valido em uma Feature GeoJSON `Point` no sistema WGS 84 (EPSG:4326) e grava as Features no MongoDB. Um indice `2dsphere` e criado no campo `geometry` para permitir consultas geoespaciais.

## Configuracao

Instale as dependencias na raiz do repositorio, com o ambiente virtual ativado:

```powershell
pip install -r AULA3\requirements.txt
```

Crie `AULA3/atividade1/.env` com as configuracoes abaixo. `MONGO_URI` tambem pode ser lida do arquivo compartilhado `AULA2/.env`.

```dotenv
CSV_URL=https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br/CNES/Unidades_Basicas_Saude-UBS_csv.zip
CSV_DELIMITER=;
CSV_ENCODING=utf-8-sig
# Configure se os nomes das colunas nao forem identificados automaticamente.
CSV_LATITUDE_COLUMN=latitude
CSV_LONGITUDE_COLUMN=longitude
MONGO_URI=mongodb://localhost:27017/
MONGO_DB_NAME=dados_georreferenciados
MONGO_COLLECTION_NAME=ubs
```

O endereco padrao aponta para o ZIP oficial de UBS no Portal de Dados Abertos do SUS. O arquivo atual contem um CSV separado por `;`, codificado em UTF-8, com as colunas `LATITUDE` e `LONGITUDE`; o coletor extrai e processa o CSV diretamente em memoria, sem criar arquivo temporario. Se o catalogo mudar o endereco, informe outra URL direta em `CSV_URL`. Coordenadas com virgula decimal sao normalizadas, e nomes comuns como `NU_LATITUDE`/`NU_LONGITUDE`, `latitude`/`lat` e `longitude`/`lon` tambem sao reconhecidos. Ajuste `CSV_DELIMITER` e `CSV_ENCODING` ao usar outro arquivo.

## Execucao

Na raiz do repositorio:

```powershell
python AULA3\atividade1\ubs_data_collector.py
```

O programa ignora registros sem coordenadas numericas ou fora dos limites geograficos. A identificacao de cada Feature e deterministica, entao executar novamente atualiza registros existentes em vez de criar duplicatas.

## Consulta geoespacial

Exemplo de consulta para encontrar unidades em ate 5 km de um ponto. No GeoJSON, a ordem das coordenadas e `[longitude, latitude]`:

```javascript
db.ubs.find({
  geometry: {
    $near: {
      $geometry: { type: "Point", coordinates: [-46.6333, -23.5505] },
      $maxDistance: 5000
    }
  }
})
```