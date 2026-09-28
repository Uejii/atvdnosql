"""
GeoLog - Plataforma de Telemetria Logística e Persistência Poliglota
UNIPÊ - Tópicos Avançados em Banco de Dados / Arquitetura de Software

Como executar:
    pip install streamlit pymongo folium streamlit-folium plotly pandas
    docker run -d --name mongo -p 27017:27017 mongo:7      # ou MongoDB local
    streamlit run geolog_app.py

Variáveis de ambiente opcionais:
    MONGO_URI    (padrão: mongodb://localhost:27017)
    SQLITE_PATH  (padrão: logitech.db)

Arquitetura:
    SQLite  -> dados cadastrais/transacionais (motoristas, veiculos)
    MongoDB -> telemetria GPS/sensores (GeoJSON + índice 2dsphere)
    Join poliglota feito em memória com pandas.
"""
import copy
import html
import math
import os
import random
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import folium
import pandas as pd
import plotly.express as px
import streamlit as st
import streamlit.components.v1 as components
from pymongo import ASCENDING, DESCENDING, GEOSPHERE, MongoClient
from pymongo.errors import PyMongoError

try:
    from streamlit_folium import st_folium
except ImportError:  # sem streamlit-folium usamos o HTML do folium direto
    st_folium = None

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = "geolog_db"
MONGO_COLLECTION = "telemetria"
SQLITE_PATH = os.getenv("SQLITE_PATH", "logitech.db")

LIMITE_VELOCIDADE = 80          # km/h
RAIO_TERRA_KM = 6378.1          # usado no $centerSphere e no haversine
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Pontos de referência (latitude, longitude) - região de João Pessoa/PB
PONTOS_REFERENCIA = {
    "Centro de João Pessoa": (-7.115, -34.873),
    "Cabo Branco": (-7.121, -34.832),
    "Tibiri / BR-230": (-7.150, -34.950),
}

# ---------------------------------------------------------------------------
# Seed inicial (dados sugeridos no enunciado)
# ---------------------------------------------------------------------------
MOTORISTAS = [
    (1, "Carlos Andrade", "123456789", "Ativo"),
    (2, "Mariana Silva", "987654321", "Ativo"),
    (3, "Roberto Souza", "456789123", "Em Descanso"),
]

VEICULOS = [
    (101, "ABC-1A23", "Volvo FH 540", 1),
    (102, "XYZ-9876", "Scania R450", 2),
    (103, "KGB-4567", "Mercedes Actros", 3),
]

# GeoJSON usa [longitude, latitude]
TELEMETRIA_SEED = [
    {
        "veiculo_id": 101,
        "location": {"type": "Point", "coordinates": [-34.873, -7.115]},  # Centro
        "temperatura": 4.2,
        "velocidade": 65,
        "timestamp": "2026-09-11T10:00:00Z",
    },
    {
        "veiculo_id": 102,
        "location": {"type": "Point", "coordinates": [-34.832, -7.121]},  # Cabo Branco
        "temperatura": -18.5,  # carga congelada
        "velocidade": 85,      # alerta de velocidade
        "timestamp": "2026-09-11T10:05:00Z",
    },
    {
        "veiculo_id": 103,
        "location": {"type": "Point", "coordinates": [-34.950, -7.150]},  # Tibiri / BR-230
        "temperatura": 22.0,
        "velocidade": 0,
        "timestamp": "2026-09-11T09:45:00Z",
    },
]

DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS motoristas (
    id     INTEGER PRIMARY KEY,
    nome   TEXT NOT NULL,
    cnh    TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('Ativo', 'Em Descanso', 'Inativo'))
);
CREATE TABLE IF NOT EXISTS veiculos (
    id           INTEGER PRIMARY KEY,
    placa        TEXT NOT NULL UNIQUE,
    modelo       TEXT NOT NULL,
    motorista_id INTEGER NOT NULL REFERENCES motoristas (id)
);
"""


def gerar_telemetria_seed() -> list[dict]:
    """Os 3 registros do enunciado (mais recentes) + um histórico anterior.

    O histórico (6 leituras a cada 10 min, antes de cada registro do enunciado)
    existe só para o gráfico de temperatura e a trilha no mapa terem mais de um
    ponto por veículo. É determinístico (seed fixa).
    """
    rng = random.Random(42)
    docs = copy.deepcopy(TELEMETRIA_SEED)  # deepcopy: o pymongo injeta _id nos dicts

    for base in TELEMETRIA_SEED:
        lon, lat = base["location"]["coordinates"]
        parado = base["velocidade"] == 0
        t0 = datetime.strptime(base["timestamp"], TS_FORMAT)
        for k in range(1, 7):
            if not parado:  # caminhão em movimento: passeio aleatório para trás no tempo
                lon += rng.uniform(-0.004, 0.004)
                lat += rng.uniform(-0.004, 0.004)
            velocidade = 0 if parado else max(0, base["velocidade"] + rng.randint(-15, 15))
            docs.append({
                "veiculo_id": base["veiculo_id"],
                "location": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                "temperatura": round(base["temperatura"] + rng.uniform(-1.5, 1.5), 1),
                "velocidade": velocidade,
                "timestamp": (t0 - timedelta(minutes=10 * k)).strftime(TS_FORMAT),
            })
    return docs


# ---------------------------------------------------------------------------
# Módulo 1 - Persistência poliglota: SQLite (relacional)
# ---------------------------------------------------------------------------
def conectar_sqlite() -> sqlite3.Connection:
    # Uma conexão por operação: o sqlite3 não pode ser compartilhado entre threads
    # e o Streamlit executa cada rerun em uma thread diferente.
    conn = sqlite3.connect(SQLITE_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def inicializar_sqlite() -> None:
    with closing(conectar_sqlite()) as conn:
        with conn:  # commit automático
            conn.executescript(DDL_SQLITE)
            conn.executemany(
                "INSERT OR IGNORE INTO motoristas (id, nome, cnh, status) VALUES (?, ?, ?, ?)",
                MOTORISTAS,
            )
            conn.executemany(
                "INSERT OR IGNORE INTO veiculos (id, placa, modelo, motorista_id) VALUES (?, ?, ?, ?)",
                VEICULOS,
            )


def carregar_cadastro() -> pd.DataFrame:
    sql = """
        SELECT v.id AS veiculo_id, v.placa, v.modelo,
               m.id AS motorista_id, m.nome AS motorista, m.status
        FROM veiculos v
        JOIN motoristas m ON m.id = v.motorista_id
        ORDER BY v.id
    """
    with closing(conectar_sqlite()) as conn:
        return pd.read_sql_query(sql, conn)


def carregar_status_motoristas() -> pd.DataFrame:
    sql = "SELECT status, COUNT(*) AS total FROM motoristas GROUP BY status ORDER BY status"
    with closing(conectar_sqlite()) as conn:
        return pd.read_sql_query(sql, conn)


# ---------------------------------------------------------------------------
# Módulo 1 - Persistência poliglota: MongoDB (documentos + geoespacial)
# ---------------------------------------------------------------------------
@st.cache_resource
def conectar_mongo() -> MongoClient:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")  # falha rápido se o servidor não estiver no ar
    return client


def inicializar_mongo(colecao) -> None:
    # Índice obrigatório do desafio: 2dsphere sobre o campo GeoJSON "location"
    colecao.create_index([("location", GEOSPHERE)], name="idx_location_2dsphere")
    # Apoia a consulta "última leitura de cada veículo"
    colecao.create_index(
        [("veiculo_id", ASCENDING), ("timestamp", DESCENDING)], name="idx_veiculo_timestamp"
    )
    if colecao.count_documents({}) == 0:
        colecao.insert_many(gerar_telemetria_seed())


def buscar_ultimas_telemetrias(colecao) -> list[dict]:
    """Última leitura de cada veículo (documentos completos, com _id)."""
    pipeline = [
        {"$sort": {"veiculo_id": 1, "timestamp": -1}},
        {"$group": {"_id": "$veiculo_id", "doc": {"$first": "$$ROOT"}}},
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$sort": {"veiculo_id": 1}},
    ]
    return list(colecao.aggregate(pipeline))


def buscar_historico(colecao, veiculo_ids=None) -> list[dict]:
    filtro = {} if veiculo_ids is None else {"veiculo_id": {"$in": list(veiculo_ids)}}
    return list(colecao.find(filtro, {"_id": 0}).sort("timestamp", ASCENDING))


# ---------------------------------------------------------------------------
# Módulo 2 - Geoprocessamento (busca por raio)
# ---------------------------------------------------------------------------
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * RAIO_TERRA_KM * math.asin(math.sqrt(a))


def buscar_por_raio(colecao, lat: float, lon: float, raio_km: float, operador: str) -> list[dict]:
    """Veículos cuja ÚLTIMA posição está a até raio_km do ponto (lat, lon).

    A geoconsulta roda no MongoDB ($near ou $geoWithin); o filtro por _id
    garante que só a posição atual de cada veículo seja considerada.
    """
    ids_ultimas = [d["_id"] for d in buscar_ultimas_telemetrias(colecao)]

    if operador == "$near":
        filtro_geo = {"$near": {
            "$geometry": {"type": "Point", "coordinates": [lon, lat]},
            "$maxDistance": raio_km * 1000,  # metros
        }}
    else:  # $geoWithin
        filtro_geo = {"$geoWithin": {"$centerSphere": [[lon, lat], raio_km / RAIO_TERRA_KM]}}

    encontrados = list(colecao.find({"_id": {"$in": ids_ultimas}, "location": filtro_geo}))
    for doc in encontrados:
        d_lon, d_lat = doc["location"]["coordinates"]
        doc["distancia_km"] = haversine_km(lat, lon, d_lat, d_lon)
    return sorted(encontrados, key=lambda d: d["distancia_km"])


def zoom_para_raio(raio_km: float) -> int:
    for limite, zoom in ((3, 13), (8, 12), (20, 11), (40, 10)):
        if raio_km <= limite:
            return zoom
    return 9


def construir_mapa(lat, lon, raio_km, dentro, fora, cadastro: pd.DataFrame, historico: list[dict]):
    info = cadastro.set_index("veiculo_id").to_dict("index")
    mapa = folium.Map(location=[lat, lon], zoom_start=zoom_para_raio(raio_km))

    folium.Circle(
        [lat, lon], radius=raio_km * 1000, color="#1f77b4",
        fill=True, fill_opacity=0.08, tooltip=f"Raio de busca: {raio_km} km",
    ).add_to(mapa)
    folium.Marker(
        [lat, lon], tooltip="Ponto de referência", icon=folium.Icon(color="blue", icon="home")
    ).add_to(mapa)

    for doc in dentro:
        d_lon, d_lat = doc["location"]["coordinates"]
        dados = info.get(doc["veiculo_id"], {})
        placa = html.escape(str(dados.get("placa", doc["veiculo_id"])))
        motorista = html.escape(str(dados.get("motorista", "-")))
        alerta = doc["velocidade"] > LIMITE_VELOCIDADE
        popup = (
            f"<b>{placa}</b> ({html.escape(str(dados.get('modelo', '-')))})<br>"
            f"Motorista: {motorista}<br>"
            f"Temperatura: {doc['temperatura']} °C<br>"
            f"Velocidade: {doc['velocidade']} km/h{' - alerta' if alerta else ''}<br>"
            f"Distância: {doc['distancia_km']:.2f} km"
        )
        folium.Marker(
            [d_lat, d_lon],
            popup=folium.Popup(popup, max_width=260),
            tooltip=f"{placa} - {doc['distancia_km']:.1f} km",
            icon=folium.Icon(color="red" if alerta else "green", icon="truck", prefix="fa"),
        ).add_to(mapa)

        # Rota recente do veículo (histórico de posições em ordem cronológica)
        trilha = [
            [p["location"]["coordinates"][1], p["location"]["coordinates"][0]]
            for p in historico if p["veiculo_id"] == doc["veiculo_id"]
        ]
        if len(trilha) > 1:
            folium.PolyLine(trilha, color="#2ca02c", weight=3, opacity=0.7).add_to(mapa)

    for doc in fora:  # veículos fora do raio aparecem em cinza
        d_lon, d_lat = doc["location"]["coordinates"]
        placa = html.escape(str(info.get(doc["veiculo_id"], {}).get("placa", doc["veiculo_id"])))
        folium.CircleMarker(
            [d_lat, d_lon], radius=6, color="gray", fill=True, fill_opacity=0.7,
            tooltip=f"{placa} (fora do raio)",
        ).add_to(mapa)
    return mapa


def exibir_mapa(mapa: folium.Map) -> None:
    if st_folium is not None:
        try:
            st_folium(mapa, height=520, use_container_width=True, returned_objects=[])
        except TypeError:  # versões do streamlit-folium sem use_container_width
            st_folium(mapa, height=520, returned_objects=[])
    else:
        components.html(mapa.get_root().render(), height=540)


# ---------------------------------------------------------------------------
# Módulo 3 - Join poliglota em memória (SQLite x MongoDB)
# ---------------------------------------------------------------------------
def montar_visao_unificada(cadastro: pd.DataFrame, ultimas: list[dict]) -> pd.DataFrame:
    colunas = ["veiculo_id", "temperatura", "velocidade", "latitude", "longitude", "timestamp"]
    telemetria = pd.DataFrame(
        [
            {
                "veiculo_id": d["veiculo_id"],
                "temperatura": d["temperatura"],
                "velocidade": d["velocidade"],
                "latitude": d["location"]["coordinates"][1],
                "longitude": d["location"]["coordinates"][0],
                "timestamp": d["timestamp"],
            }
            for d in ultimas
        ],
        columns=colunas,
    )
    telemetria["veiculo_id"] = telemetria["veiculo_id"].astype("int64")
    return cadastro.merge(telemetria, on="veiculo_id", how="left")


def tabela_enriquecida(unificado: pd.DataFrame) -> pd.DataFrame:
    coordenadas = [f"{la:.5f}, {lo:.5f}" for la, lo in zip(unificado["latitude"], unificado["longitude"])]
    alertas = [
        "Excesso de velocidade" if pd.notna(v) and v > LIMITE_VELOCIDADE else "OK"
        for v in unificado["velocidade"]
    ]
    return pd.DataFrame({
        "Nome do Motorista": unificado["motorista"],
        "Placa": unificado["placa"],
        "Última Temperatura (°C)": unificado["temperatura"],
        "Velocidade (km/h)": unificado["velocidade"],
        "Coordenadas Atualizadas": coordenadas,
        "Última leitura": unificado["timestamp"],
        "Alerta": alertas,
    })


# ---------------------------------------------------------------------------
# Bônus - Simulador de telemetria em tempo real
# ---------------------------------------------------------------------------
def gerar_novo_ponto(ultimo: dict, rng=random) -> dict:
    lon, lat = ultimo["location"]["coordinates"]
    agora = datetime.now(timezone.utc).replace(microsecond=0)
    ultimo_ts = datetime.strptime(ultimo["timestamp"], TS_FORMAT).replace(tzinfo=timezone.utc)
    novo_ts = max(agora, ultimo_ts + timedelta(seconds=1))  # sempre posterior à última leitura
    return {
        "veiculo_id": ultimo["veiculo_id"],
        "location": {
            "type": "Point",
            "coordinates": [
                round(lon + rng.uniform(-0.003, 0.003), 6),
                round(lat + rng.uniform(-0.003, 0.003), 6),
            ],
        },
        "temperatura": round(ultimo["temperatura"] + rng.uniform(-0.8, 0.8), 1),
        "velocidade": max(0, min(120, ultimo["velocidade"] + rng.randint(-20, 20))),
        "timestamp": novo_ts.strftime(TS_FORMAT),
    }


def simular_movimentacao(colecao) -> int:
    novos = [gerar_novo_ponto(d) for d in buscar_ultimas_telemetrias(colecao)]
    if novos:
        colecao.insert_many(novos)
    return len(novos)


# ---------------------------------------------------------------------------
# Interface Streamlit
# ---------------------------------------------------------------------------
def renderizar_sidebar(colecao) -> tuple[float, float, float, str]:
    st.sidebar.header("Busca por raio")
    opcoes = list(PONTOS_REFERENCIA) + ["Personalizado"]
    escolhido = st.sidebar.selectbox("Ponto de referência", opcoes)
    if escolhido == "Personalizado":
        lat = st.sidebar.number_input("Latitude", value=-7.115, min_value=-90.0, max_value=90.0, format="%.5f")
        lon = st.sidebar.number_input("Longitude", value=-34.873, min_value=-180.0, max_value=180.0, format="%.5f")
    else:
        lat, lon = PONTOS_REFERENCIA[escolhido]
    raio_km = st.sidebar.slider("Raio de busca (km)", min_value=1, max_value=50, value=6)
    operador = st.sidebar.radio("Operador geoespacial", ["$near", "$geoWithin"], horizontal=True)

    # Os botões rodam ANTES das abas serem desenhadas, então o mapa e o
    # dashboard já saem atualizados no mesmo rerun (sem reiniciar o app).
    st.sidebar.divider()
    st.sidebar.header("Simulador de telemetria")
    if st.sidebar.button("Simular Movimentação"):
        total = simular_movimentacao(colecao)
        st.sidebar.success(f"{total} novos pontos GPS gravados no MongoDB.")
    if st.sidebar.button("Resetar telemetria"):
        colecao.delete_many({})
        colecao.insert_many(gerar_telemetria_seed())
        st.sidebar.info("Telemetria restaurada para o seed inicial.")
    return lat, lon, float(raio_km), operador


def renderizar_busca_por_raio(colecao, cadastro, ultimas, lat, lon, raio_km, operador) -> None:
    dentro = buscar_por_raio(colecao, lat, lon, raio_km, operador)
    ids_dentro = {d["veiculo_id"] for d in dentro}
    fora = [d for d in ultimas if d["veiculo_id"] not in ids_dentro]

    st.subheader(f"{len(dentro)} veículo(s) a até {raio_km:.0f} km do ponto de referência")
    st.caption(f"Consulta executada no MongoDB com `{operador}` sobre o índice 2dsphere.")

    if dentro:
        info = cadastro.set_index("veiculo_id").to_dict("index")
        st.dataframe(
            pd.DataFrame([
                {
                    "Placa": info[d["veiculo_id"]]["placa"],
                    "Motorista": info[d["veiculo_id"]]["motorista"],
                    "Distância (km)": round(d["distancia_km"], 2),
                    "Temperatura (°C)": d["temperatura"],
                    "Velocidade (km/h)": d["velocidade"],
                }
                for d in dentro
            ]),
            hide_index=True,
        )
    else:
        st.info("Nenhum veículo dentro do raio. Aumente o raio ou troque o ponto de referência.")

    historico = buscar_historico(colecao, ids_dentro)
    exibir_mapa(construir_mapa(lat, lon, raio_km, dentro, fora, cadastro, historico))


def renderizar_visao_unificada(cadastro, ultimas) -> None:
    st.subheader("Visão unificada: cadastro (SQLite) + última telemetria (MongoDB)")
    st.caption("Join feito em memória com pandas, usando `veiculo_id` como chave.")
    st.dataframe(tabela_enriquecida(montar_visao_unificada(cadastro, ultimas)), hide_index=True)


def renderizar_dashboard(colecao, cadastro, ultimas) -> None:
    unificado = montar_visao_unificada(cadastro, ultimas)
    ativos = int((unificado["status"] == "Ativo").sum())
    media_temp = unificado["temperatura"].mean()
    excessos = unificado[unificado["velocidade"] > LIMITE_VELOCIDADE]

    c1, c2, c3 = st.columns(3)
    c1.metric("Frotas ativas", ativos, help="Veículos cujo motorista está com status 'Ativo'.")
    c2.metric("Temperatura média da carga", "-" if pd.isna(media_temp) else f"{media_temp:.1f} °C")
    c3.metric(f"Alertas de velocidade (> {LIMITE_VELOCIDADE} km/h)", len(excessos))

    for r in excessos.itertuples():
        st.warning(f"{r.placa} ({r.motorista}) está a {r.velocidade} km/h.")

    col_a, col_b = st.columns([2, 1])

    with col_a:
        historico = pd.DataFrame(buscar_historico(colecao))
        if historico.empty:
            st.info("Sem telemetria para exibir.")
        else:
            historico = historico.merge(cadastro[["veiculo_id", "placa"]], on="veiculo_id")
            historico["timestamp"] = pd.to_datetime(historico["timestamp"], utc=True)
            placas = sorted(historico["placa"].unique())
            selecionadas = st.multiselect("Veículos no gráfico", placas, default=placas)
            filtrado = historico[historico["placa"].isin(selecionadas)].sort_values("timestamp")
            if filtrado.empty:
                st.info("Selecione ao menos um veículo.")
            else:
                fig = px.line(
                    filtrado, x="timestamp", y="temperatura", color="placa", markers=True,
                    title="Histórico de temperatura por veículo",
                    labels={"timestamp": "Horário (UTC)", "temperatura": "Temperatura (°C)", "placa": "Placa"},
                )
                st.plotly_chart(fig)

    with col_b:
        status = carregar_status_motoristas()
        fig = px.pie(
            status, names="status", values="total", hole=0.4,
            title="Status dos motoristas",
        )
        st.plotly_chart(fig)


def main() -> None:
    st.set_page_config(page_title="GeoLog", layout="wide")
    st.title("GeoLog - Telemetria Logística e Persistência Poliglota")
    st.caption("LogiTech Express | SQLite (cadastros) + MongoDB (telemetria geoespacial)")

    try:
        colecao = conectar_mongo()[MONGO_DB][MONGO_COLLECTION]
        inicializar_mongo(colecao)
    except PyMongoError as exc:
        st.error(
            f"Não foi possível conectar ao MongoDB em {MONGO_URI}. "
            "Suba o serviço (ex.: `docker run -d -p 27017:27017 mongo:7`) e recarregue a página.\n\n"
            f"Detalhe: {exc}"
        )
        st.stop()
    inicializar_sqlite()

    lat, lon, raio_km, operador = renderizar_sidebar(colecao)
    cadastro = carregar_cadastro()
    ultimas = buscar_ultimas_telemetrias(colecao)

    tab_mapa, tab_unificada, tab_dash = st.tabs(
        ["Busca por Raio", "Visão Unificada", "Dashboard"]
    )
    with tab_mapa:
        renderizar_busca_por_raio(colecao, cadastro, ultimas, lat, lon, raio_km, operador)
    with tab_unificada:
        renderizar_visao_unificada(cadastro, ultimas)
    with tab_dash:
        renderizar_dashboard(colecao, cadastro, ultimas)


if __name__ == "__main__":
    main()
