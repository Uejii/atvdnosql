# Atividade 2 - OpenF1 Data Explorer

Aplicacao Streamlit para explorar no MongoDB sessoes, pilotos e tempos de volta coletados pela atividade OpenF1 de AULA2. Os anexos `streamlit_app.py` e `db_utils.py` eram relacionados a esta atividade; a consulta foi alinhada as colecoes `sessions`, `drivers` e `laps` do coletor existente. Dados simulados foram removidos para que a interface nao confunda exemplos com registros reais.

## Preparacao

Instale as dependencias na raiz do repositorio, com o ambiente virtual ativado:

```powershell
pip install -r AULA3\atividade2\requirements.txt
```

A aplicacao usa `MONGO_URI` e `MONGO_DB_NAME` de `AULA2/.env`. Se preferir uma configuracao exclusiva, crie `AULA3/atividade2/.env` com base em `.env.example`.

Antes de abrir o explorador, colete pelo menos uma sessao para o MongoDB:

```powershell
python AULA2\atividade1\f1_data_collector.py
```

## Execucao

Na raiz do repositorio:

```powershell
streamlit run AULA3\atividade2\streamlit_app.py
```

A interface lista os anos e sessoes existentes no banco e permite comparar os tempos de volta dos pilotos selecionados.