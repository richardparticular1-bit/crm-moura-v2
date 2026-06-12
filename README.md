# CRM Moura — Backend

Adiciona persistência SQLite ao CRM PWA da Moura Odontologia, substituindo o `localStorage` por uma API REST local.

## Como rodar

```powershell
cd "G:\MOURA ODONTOLOGIA\crm-backend"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py          # http://127.0.0.1:8001
```

Abra o navegador em **http://127.0.0.1:8001**

## Portas

| Sistema             | Porta  | URL                        |
|---------------------|--------|----------------------------|
| CRM (este projeto)  | 8001   | http://127.0.0.1:8001      |
| Clínica-Agentes     | 8000   | http://127.0.0.1:8000      |

## Migração dos dados antigos

Se você já usou o CRM antigo e quer trazer os dados para o banco:

1. No CRM antigo (arquivo index.html aberto no navegador), vá em **Ajustes → Exportar backup (.json)**
2. No novo CRM, vá em **Ajustes → Importar backup (.json)** e selecione o arquivo

## Sincronização com a Clínica-Agentes

Execute manualmente quando quiser sincronizar pacientes entre os dois sistemas:

```powershell
python sync.py
```

Ou em modo contínuo (sincroniza a cada 60 segundos):

```powershell
python sync.py --watch
```

O que a sincronização faz:
- Pacientes novos no CRM → copiados para a Clínica-Agentes
- Pacientes novos na Clínica-Agentes → copiados para o CRM
- Consultas concluídas na Clínica → atualiza `lastVisit` no CRM

## Estrutura

```
crm-backend/
├── main.py          # API FastAPI (pacientes, consultas, pesquisas, ajustes)
├── models.py        # Modelos SQLAlchemy
├── database.py      # Engine SQLite + configurações padrão
├── sync.py          # Sincronizador bidirecional com clinica-agentes
├── run.py           # Inicia na porta 8001
├── requirements.txt
└── frontend/
    ├── index.html   # CRM PWA (agora usa API em vez de localStorage)
    ├── sw.js
    └── manifest.webmanifest
```
