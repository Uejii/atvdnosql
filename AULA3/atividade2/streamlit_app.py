"""Interface para explorar sessoes e tempos de volta da OpenF1 no MongoDB."""

from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
from pymongo.errors import PyMongoError

from db_utils import (
    get_available_years,
    get_drivers_from_session,
    get_laps_for_drivers,
    get_mongo_db,
    get_race_sessions,
)


st.set_page_config(page_title="OpenF1 Data Explorer", page_icon="F1", layout="wide")
st.title("OpenF1 Data Explorer")
st.caption("Explore sessões, pilotos e tempos de volta armazenados no MongoDB.")


@st.cache_resource
def conectar_banco():
    return get_mongo_db()


try:
    db = conectar_banco()
except (PyMongoError, ValueError) as error:
    st.error(f"Nao foi possivel conectar ao MongoDB: {error}")
    st.stop()

years = get_available_years(db)
if not years:
    st.info(
        "Ainda nao ha sessoes no banco. Execute primeiro "
        "AULA2/atividade1/f1_data_collector.py."
    )
    st.stop()

with st.sidebar:
    st.header("Filtros da sessao")
    selected_year = st.selectbox("Ano", options=years, index=len(years) - 1)
    sessions = get_race_sessions(db, selected_year)
    if not sessions:
        st.warning("Nenhuma sessao encontrada para este ano.")
        st.stop()

    session_by_key = {session["session_key"]: session for session in sessions}

    def format_session(session_key: int) -> str:
        session = session_by_key[session_key]
        event = session.get("meeting_name") or session.get("circuit_short_name") or "Evento"
        session_name = session.get("session_name") or "Sessao"
        country = session.get("country_name")
        label = f"{event} - {session_name}"
        return f"{label} ({country})" if country else label

    selected_session_key = st.selectbox(
        "Sessao", options=list(session_by_key), format_func=format_session
    )

session = session_by_key[selected_session_key]
st.subheader(format_session(selected_session_key))
metric_columns = st.columns(3)
metric_columns[0].metric("Ano", session.get("year", selected_year))
metric_columns[1].metric("Pais", session.get("country_name") or "N/A")
metric_columns[2].metric("Circuito", session.get("circuit_short_name") or "N/A")

date_start = session.get("date_start")
if isinstance(date_start, datetime):
    date_label = date_start.strftime("%Y-%m-%d %H:%M UTC")
elif date_start:
    date_label = str(date_start).replace("T", " ")[:16] + " UTC"
else:
    date_label = "N/A"
st.caption(f"Inicio da sessao: {date_label}")

drivers = get_drivers_from_session(db, selected_session_key)
if not drivers:
    st.warning("Esta sessao ainda nao possui pilotos coletados.")
    st.stop()

driver_by_number = {driver["driver_number"]: driver for driver in drivers}


def format_driver(driver_number: int) -> str:
    driver = driver_by_number[driver_number]
    name = driver.get("full_name") or driver.get("name_acronym") or str(driver_number)
    team = driver.get("team_name")
    return f"{name} ({team})" if team else name


st.subheader("Comparativo de voltas")
selected_driver_numbers = st.multiselect(
    "Pilotos",
    options=list(driver_by_number),
    default=list(driver_by_number)[:2],
    format_func=format_driver,
)

if not selected_driver_numbers:
    st.info("Selecione ao menos um piloto para ver os tempos de volta.")
    st.stop()

laps = get_laps_for_drivers(db, selected_session_key, selected_driver_numbers)
if laps.empty:
    st.warning("Nao ha tempos de volta para os pilotos selecionados.")
    st.stop()

required_columns = {"driver_number", "lap_number", "lap_duration"}
if not required_columns.issubset(laps.columns):
    st.error("Os documentos de voltas nao contem os campos esperados pela atividade.")
    st.stop()

laps["lap_number"] = pd.to_numeric(laps["lap_number"], errors="coerce")
laps["lap_duration"] = pd.to_numeric(laps["lap_duration"], errors="coerce")
laps = laps.dropna(subset=["lap_number", "lap_duration"])
laps = laps[laps["lap_duration"] > 0].copy()
if laps.empty:
    st.warning("Nao ha tempos de volta validos para exibir.")
    st.stop()

laps["driver_name"] = laps["driver_number"].map(
    {number: format_driver(number) for number in selected_driver_numbers}
)
figure = px.line(
    laps,
    x="lap_number",
    y="lap_duration",
    color="driver_name",
    markers=True,
    labels={
        "lap_number": "Volta",
        "lap_duration": "Duracao (segundos)",
        "driver_name": "Piloto",
    },
)
st.plotly_chart(figure, use_container_width=True)

with st.expander("Dados das voltas"):
    st.dataframe(
        laps[["driver_name", "lap_number", "lap_duration"]]
        .sort_values(["driver_name", "lap_number"])
        .reset_index(drop=True),
        use_container_width=True,
    )