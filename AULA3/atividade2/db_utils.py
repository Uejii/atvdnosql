"""Consultas às coleções OpenF1 armazenadas no MongoDB."""

import os
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.database import Database


BASE_DIR = Path(__file__).resolve().parent
REPOSITORY_DIR = BASE_DIR.parent.parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(REPOSITORY_DIR / "AULA2" / ".env")


def get_mongo_db() -> Database:
    """Conecta ao banco OpenF1 configurado no ambiente ou no .env compartilhado."""
    mongo_uri = os.getenv("MONGO_URI", "")
    if not mongo_uri:
        raise ValueError("Configure MONGO_URI em AULA2/.env ou AULA3/atividade2/.env.")

    db_name = os.getenv("MONGO_DB_NAME", "openf1_data")
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except Exception:
        client.close()
        raise
    return client[db_name]


def get_available_years(db: Database) -> list[int]:
    """Retorna os anos que possuem sessões coletadas."""
    years = db.sessions.distinct("year")
    return sorted({int(year) for year in years if year is not None})


def get_race_sessions(db: Database, year: int) -> list[dict[str, Any]]:
    """Busca sessões de um ano, ordenadas pela data de início."""
    return list(
        db.sessions.find({"year": int(year)}, {"_id": 0}).sort("date_start", 1)
    )


def get_session_details(db: Database, session_key: int) -> dict[str, Any] | None:
    """Busca os detalhes de uma sessão pelo identificador OpenF1."""
    return db.sessions.find_one({"session_key": session_key}, {"_id": 0})


def get_drivers_from_session(db: Database, session_key: int) -> list[dict[str, Any]]:
    """Busca os pilotos de uma sessão em ordem numérica."""
    return list(
        db.drivers.find({"session_key": session_key}, {"_id": 0}).sort("driver_number", 1)
    )


def get_laps_for_drivers(
    db: Database, session_key: int, driver_numbers: list[int]
) -> pd.DataFrame:
    """Busca os tempos de volta dos pilotos escolhidos."""
    laps = list(
        db.laps.find(
            {
                "session_key": session_key,
                "driver_number": {"$in": driver_numbers},
            },
            {"_id": 0},
        )
    )
    return pd.DataFrame(laps)