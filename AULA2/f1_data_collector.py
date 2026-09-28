"""Coleta sessões, pilotos e voltas da OpenF1 e sincroniza os dados no MongoDB."""

import os
import sys
from typing import Any

import requests
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.database import Database
from pymongo.errors import PyMongoError

# Carrega as configurações locais; variáveis de ambiente do sistema têm precedência.
load_dotenv()

API_BASE_URL = os.getenv("OPENF1_BASE_URL", "https://api.openf1.org/v1").rstrip("/")
API_TIMEOUT_SECONDS = int(os.getenv("API_TIMEOUT_SECONDS", "30"))
SESSION_KEY = int(os.getenv("SESSION_KEY", "9159"))
MEETING_KEY = int(os.getenv("MEETING_KEY", "1219"))
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "openf1_data")
db: Database


def get_database() -> Database:
	"""Conecta ao MongoDB, verifica a conexão e retorna o banco configurado."""
	if not MONGO_URI:
		raise ValueError("A variável MONGO_URI não foi definida no ambiente ou arquivo .env.")

	client = MongoClient(
		MONGO_URI,
		serverSelectionTimeoutMS=API_TIMEOUT_SECONDS * 1000,
	)
	try:
		# O ping confirma a conexão antes de iniciar a gravação dos dados.
		client.admin.command("ping")
		return client[MONGO_DB_NAME]
	except PyMongoError:
		client.close()
		raise


def fetch_data(endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
	"""Consulta um endpoint da OpenF1 e retorna os registros recebidos."""
	url = f"{API_BASE_URL}/{endpoint.lstrip('/')}"
	print(f"Consultando {url} com filtros {params}...", flush=True)
	response = requests.get(url, params=params, timeout=API_TIMEOUT_SECONDS)
	response.raise_for_status()
	result = response.json()

	if not isinstance(result, list):
		raise ValueError(f"A resposta do endpoint /{endpoint} não é uma lista.")
	print(f"Recebidos {len(result)} registros de /{endpoint}.", flush=True)
	return result


def save_to_collection(
	data: list[dict[str, Any]],
	collection_name: str,
	unique_keys: list[str],
) -> int:
	"""Cria índice único e atualiza ou insere cada documento por sua chave."""
	if not unique_keys:
		raise ValueError("Informe ao menos um campo de chave única.")

	collection = db[collection_name]
	collection.create_index([(key, 1) for key in unique_keys], unique=True)
	saved_count = 0

	for document in data:
		if any(key not in document for key in unique_keys):
			print(
				f"Documento ignorado em {collection_name}: "
				f"faltam campos da chave única {unique_keys}.",
				file=sys.stderr,
			)
			continue

		filter_query = {key: document[key] for key in unique_keys}
		collection.update_one(filter_query, {"$set": document}, upsert=True)
		saved_count += 1

	print(f"Salvos/atualizados {saved_count} documentos em {collection_name}.", flush=True)
	return saved_count


def main() -> int:
	"""Busca a sessão de demonstração, seus pilotos e voltas e salva os dados."""
	global db
	client: MongoClient | None = None

	try:
		print(
			f"Iniciando coleta OpenF1 — sessão {SESSION_KEY}, reunião {MEETING_KEY}.",
			flush=True,
		)
		# Busca cada conjunto de dados filtrando pela sessão solicitada.
		sessions = fetch_data(
			"sessions",
			{"session_key": SESSION_KEY, "meeting_key": MEETING_KEY},
		)
		drivers = fetch_data("drivers", {"session_key": SESSION_KEY})
		laps = fetch_data("laps", {"session_key": SESSION_KEY})

		print("Conectando ao MongoDB local...", flush=True)
		db = get_database()
		client = db.client
		print(f"Conexão estabelecida com o banco {MONGO_DB_NAME}.", flush=True)

		# As chaves compostas mantêm os dados únicos entre sessões e pilotos.
		saved_sessions = save_to_collection(sessions, "sessions", ["session_key"])
		saved_drivers = save_to_collection(
			drivers,
			"drivers",
			["session_key", "driver_number"],
		)
		saved_laps = save_to_collection(
			laps,
			"laps",
			["session_key", "driver_number", "lap_number"],
		)

		print(
			f"Sincronização concluída para a sessão {SESSION_KEY}: "
			f"{saved_sessions} sessões, {saved_drivers} pilotos e {saved_laps} voltas."
		)
		return 0
	except (requests.RequestException, PyMongoError, ValueError) as error:
		print(f"Falha na coleta ou gravação dos dados: {error}", file=sys.stderr)
		return 1
	finally:
		# Fecha o cliente mesmo quando alguma chamada ou gravação falha.
		if client is not None:
			client.close()


if __name__ == "__main__":
	raise SystemExit(main())
