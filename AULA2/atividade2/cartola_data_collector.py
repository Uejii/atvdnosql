"""Coleta os dados da rodada atual do Cartola FC e salva no MongoDB."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.database import Database
from pymongo.errors import PyMongoError


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")

API_URL = os.getenv(
    "CARTOLA_API_URL",
    "https://api.cartola.globo.com/atletas/mercado",
)
API_TIMEOUT_SECONDS = int(os.getenv("API_TIMEOUT_SECONDS", "30"))
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "cartola_fc_db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def conectar_mongodb() -> tuple[MongoClient, Database]:
    """Lê as credenciais do ambiente, testa a conexão e retorna cliente e banco."""
    if not MONGO_URI:
        raise ValueError("A variável MONGO_URI não foi definida no ambiente ou arquivo .env.")

    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=API_TIMEOUT_SECONDS * 1000,
    )
    client.admin.command("ping")
    return client, client[MONGO_DB_NAME]


def buscar_dados_mercado() -> dict[str, Any]:
    """Busca e valida o JSON da rodada atual do Cartola FC."""
    response = requests.get(API_URL, timeout=API_TIMEOUT_SECONDS)
    response.raise_for_status()
    dados = response.json()

    if not isinstance(dados, dict):
        raise ValueError("A resposta da API do Cartola não é um objeto JSON.")
    return dados


def _lista_clubes(clubes: Any) -> list[dict[str, Any]]:
    """Normaliza clubes retornados como mapa indexado ou lista."""
    if isinstance(clubes, dict):
        resultado = []
        for clube_id, clube in clubes.items():
            if isinstance(clube, dict):
                documento = dict(clube)
                documento.setdefault("id", int(clube_id))
                resultado.append(documento)
        return resultado
    if isinstance(clubes, list):
        return [clube for clube in clubes if isinstance(clube, dict)]
    raise ValueError("O campo 'clubes' não possui um formato válido.")


def _documento_clube(clube: dict[str, Any]) -> dict[str, Any]:
    """Monta o documento do clube usando o próprio ID como _id."""
    clube_id = clube.get("id", clube.get("clube_id"))
    if clube_id is None:
        raise ValueError("Clube sem campo 'id'.")

    return {
        "_id": clube_id,
        "nome": clube.get("nome"),
        "abreviacao": clube.get("abreviacao"),
        "escudos": clube.get("escudos"),
        "nome_fantasia": clube.get("nome_fantasia"),
    }


def _documento_atleta(atleta: dict[str, Any], timestamp: datetime) -> dict[str, Any]:
    """Preserva os campos da API e padroniza o identificador do atleta."""
    documento = dict(atleta)
    if "atleta_id" not in documento and "id" in documento:
        documento["atleta_id"] = documento.pop("id")
    documento["timestamp_coleta"] = timestamp
    return documento


def processar_e_gravar_dados(db: Database, dados_mercado: dict[str, Any]) -> None:
    """Transforma a resposta da API e atualiza as três coleções do Cartola."""
    timestamp = datetime.now(timezone.utc)

    logger.info("Gravando dados dos clubes...")
    operacoes_clubes = [
        UpdateOne({"_id": documento["_id"]}, {"$set": documento}, upsert=True)
        for documento in (
            _documento_clube(clube) for clube in _lista_clubes(dados_mercado.get("clubes"))
        )
    ]
    if operacoes_clubes:
        db.clubes_rodada_atual.bulk_write(operacoes_clubes, ordered=False)

    logger.info("Gravando dados dos atletas...")
    atletas = dados_mercado.get("atletas")
    if not isinstance(atletas, list):
        raise ValueError("O campo 'atletas' não possui um formato válido.")
    db.atletas_rodada_atual.delete_many({})
    documentos_atletas = [
        _documento_atleta(atleta, timestamp)
        for atleta in atletas
        if isinstance(atleta, dict)
    ]
    if documentos_atletas:
        db.atletas_rodada_atual.insert_many(documentos_atletas)

    logger.info("Gravando status do mercado...")
    documento_status = {
        "rodada_atual": dados_mercado.get("rodada_atual"),
        "status_mercado": dados_mercado.get("status_mercado"),
        "aviso": dados_mercado.get("aviso"),
        "fechamento": dados_mercado.get("fechamento"),
    }
    documento_status["timestamp_coleta"] = timestamp
    db.mercado_rodada_atual.delete_many({})
    db.mercado_rodada_atual.insert_one(documento_status)


def main() -> int:
    """Orquestra a conexão, coleta, transformação e gravação dos dados."""
    client: MongoClient | None = None
    try:
        logger.info("Conectando ao MongoDB...")
        client, db = conectar_mongodb()
        logger.info("Buscando dados na API...")
        dados_mercado = buscar_dados_mercado()
        processar_e_gravar_dados(db, dados_mercado)
        logger.info("Finalizado com sucesso.")
        return 0
    except (requests.RequestException, PyMongoError, ValueError) as error:
        logger.error("Falha na coleta ou gravação dos dados: %s", error)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
