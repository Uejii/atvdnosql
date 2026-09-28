# Atividade 2 - Coleta do Cartola FC

O script `cartola_data_collector.py` consulta o mercado atual do Cartola FC e grava os dados no MongoDB nas colecoes:

- `mercado_rodada_atual`
- `atletas_rodada_atual`
- `clubes_rodada_atual`

## Configuracao

Use o arquivo `AULA2/.env` existente ou crie um a partir de `.env.example`. Defina pelo menos `MONGO_URI`. O banco padrao e `cartola_fc_db`.

## Execucao

Na raiz do projeto, com o ambiente virtual ativado:

```powershell
python AULA2\atividade2\cartola_data_collector.py
```

As dependencias ja estao listadas em `AULA2/requirements.txt`.
