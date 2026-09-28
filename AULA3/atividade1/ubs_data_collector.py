"""Coleta um CSV georreferenciado e armazena seus pontos GeoJSON no MongoDB."""

import hashlib
import json
import logging
import os
import re
import unicodedata
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

import geopandas as gpd
import pandas as pd
import requests
from dotenv import load_dotenv
from pymongo import MongoClient, ReplaceOne
from pymongo.database import Database
from pymongo.errors import PyMongoError


BASE_DIR = Path(__file__).resolve().parent
REPOSITORY_DIR = BASE_DIR.parent.parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(REPOSITORY_DIR / "AULA2" / ".env")

CSV_URL = os.getenv(
    "CSV_URL",
    "https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br/CNES/"
    "Unidades_Basicas_Saude-UBS_csv.zip",
)
CSV_DELIMITER = os.getenv("CSV_DELIMITER", ";")
CSV_ENCODING = os.getenv("CSV_ENCODING", "utf-8-sig")
LATITUDE_COLUMN = os.getenv("CSV_LATITUDE_COLUMN", "")
LONGITUDE_COLUMN = os.getenv("CSV_LONGITUDE_COLUMN", "")
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "dados_georreferenciados")
MONGO_COLLECTION_NAME = os.getenv("MONGO_COLLECTION_NAME", "ubs")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LATITUDE_ALIASES = ("latitude", "lat", "nu_latitude", "vl_latitude")
LONGITUDE_ALIASES = ("longitude", "lon", "lng", "nu_longitude", "vl_longitude")


def _normalizar_nome(nome: Any) -> str:
    """Normaliza nomes de colunas para comparar variantes de acentuacao e caixa."""
    texto = unicodedata.normalize("NFKD", str(nome))
    texto = texto.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "_", texto).strip("_")


def _resolver_coluna(
    colunas: list[Any], configurada: str, aliases: tuple[str, ...], descricao: str
) -> Any:
    """Localiza a coluna configurada ou uma variante comum de latitude/longitude."""
    por_nome_normalizado = {_normalizar_nome(coluna): coluna for coluna in colunas}
    if configurada:
        nome_normalizado = _normalizar_nome(configurada)
        if nome_normalizado in por_nome_normalizado:
            return por_nome_normalizado[nome_normalizado]
        raise ValueError(
            f"A coluna configurada para {descricao} ({configurada!r}) nao existe no CSV. "
            f"Colunas disponiveis: {', '.join(map(str, colunas))}"
        )

    for alias in aliases:
        if alias in por_nome_normalizado:
            return por_nome_normalizado[alias]
    raise ValueError(
        f"Nao foi possivel identificar a coluna de {descricao}. Configure "
        f"CSV_{descricao.upper()}_COLUMN. Colunas disponiveis: "
        f"{', '.join(map(str, colunas))}"
    )


def buscar_csv() -> pd.DataFrame:
    """Baixa CSV ou ZIP da URL configurada e carrega o arquivo em um DataFrame."""
    if not CSV_URL:
        raise ValueError("Configure CSV_URL com a URL direta do arquivo CSV.")

    response = requests.get(CSV_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    conteudo = BytesIO(response.content)
    if CSV_URL.lower().split("?", maxsplit=1)[0].endswith(".zip"):
        try:
            with ZipFile(conteudo) as arquivo_zip:
                arquivos_csv = [
                    nome for nome in arquivo_zip.namelist() if nome.lower().endswith(".csv")
                ]
                if len(arquivos_csv) != 1:
                    raise ValueError(
                        "O ZIP deve conter exatamente um arquivo CSV; "
                        f"foram encontrados {len(arquivos_csv)}."
                    )
                with arquivo_zip.open(arquivos_csv[0]) as arquivo_csv:
                    dados = pd.read_csv(
                        arquivo_csv,
                        sep=CSV_DELIMITER,
                        encoding=CSV_ENCODING,
                        low_memory=False,
                    )
        except BadZipFile as error:
            raise ValueError("O endereco configurado nao retornou um arquivo ZIP valido.") from error
    else:
        dados = pd.read_csv(
            conteudo,
            sep=CSV_DELIMITER,
            encoding=CSV_ENCODING,
            low_memory=False,
        )
    if dados.empty:
        raise ValueError("O CSV nao possui registros.")
    return dados


def converter_para_geojson(dados: pd.DataFrame) -> tuple[list[dict[str, Any]], int]:
    """Valida coordenadas e converte os registros em Features GeoJSON Point."""
    if dados.empty:
        raise ValueError("Nao ha registros para converter.")

    coluna_latitude = _resolver_coluna(
        list(dados.columns), LATITUDE_COLUMN, LATITUDE_ALIASES, "latitude"
    )
    coluna_longitude = _resolver_coluna(
        list(dados.columns), LONGITUDE_COLUMN, LONGITUDE_ALIASES, "longitude"
    )

    latitude = pd.to_numeric(
        dados[coluna_latitude].astype("string").str.strip().str.replace(",", ".", regex=False),
        errors="coerce",
    )
    longitude = pd.to_numeric(
        dados[coluna_longitude].astype("string").str.strip().str.replace(",", ".", regex=False),
        errors="coerce",
    )
    coordenadas_validas = (
        latitude.between(-90, 90) & longitude.between(-180, 180)
    ).fillna(False)
    registros_invalidos = int((~coordenadas_validas).sum())
    if not coordenadas_validas.any():
        raise ValueError("Nenhum registro possui coordenadas validas.")

    registros = dados.loc[coordenadas_validas].copy()
    geometria = gpd.points_from_xy(
        longitude.loc[coordenadas_validas],
        latitude.loc[coordenadas_validas],
        crs="EPSG:4326",
    )
    geodados = gpd.GeoDataFrame(registros, geometry=geometria, crs="EPSG:4326")
    colecao = json.loads(geodados.to_json(drop_id=True, na="null"))

    features = colecao["features"]
    for feature in features:
        identidade = json.dumps(
            {"geometry": feature["geometry"], "properties": feature["properties"]},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        feature_id = hashlib.sha256(identidade.encode("utf-8")).hexdigest()
        feature["id"] = feature_id
        feature["_id"] = feature_id

    return features, registros_invalidos


def conectar_mongodb() -> tuple[MongoClient, Database]:
    """Conecta ao MongoDB e retorna o cliente e o banco configurado."""
    if not MONGO_URI:
        raise ValueError("A variavel MONGO_URI nao foi definida no ambiente ou arquivo .env.")

    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=REQUEST_TIMEOUT_SECONDS * 1000,
    )
    client.admin.command("ping")
    return client, client[MONGO_DB_NAME]


def gravar_features(db: Database, features: list[dict[str, Any]]) -> None:
    """Cria o indice geoespacial e atualiza os pontos pela identidade da Feature."""
    colecao = db[MONGO_COLLECTION_NAME]
    colecao.create_index([("geometry", "2dsphere")], name="geometry_2dsphere")
    operacoes = [
        ReplaceOne({"_id": feature["_id"]}, feature, upsert=True)
        for feature in features
    ]
    for inicio in range(0, len(operacoes), 1000):
        colecao.bulk_write(operacoes[inicio : inicio + 1000], ordered=False)


def main() -> int:
    """Executa a coleta, conversao GeoJSON e persistencia no MongoDB."""
    client: MongoClient | None = None
    try:
        logger.info("Baixando CSV da fonte configurada...")
        dados = buscar_csv()
        features, invalidos = converter_para_geojson(dados)
        if invalidos:
            logger.warning("%s registro(s) ignorado(s) por coordenadas invalidas.", invalidos)

        logger.info("Conectando ao MongoDB...")
        client, db = conectar_mongodb()
        gravar_features(db, features)
        logger.info(
            "Coleta concluida: %s Feature(s) gravada(s) em %s.%s.",
            len(features),
            MONGO_DB_NAME,
            MONGO_COLLECTION_NAME,
        )
        return 0
    except (
        requests.RequestException,
        PyMongoError,
        ValueError,
        pd.errors.ParserError,
    ) as error:
        logger.error("Falha na coleta ou gravacao: %s", error)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())