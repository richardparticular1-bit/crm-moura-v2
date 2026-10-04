"""Gera DDL do schema separado, sem acessar nenhum banco."""
from pathlib import Path
from sqlalchemy import MetaData
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy.dialects import postgresql
from models import Base

metadata = MetaData(schema="crm_v2")
for table in Base.metadata.sorted_tables:
    table.to_metadata(metadata, schema="crm_v2")
dialect = postgresql.dialect()
statements = []
for table in metadata.sorted_tables:
    statements.append(str(CreateTable(table).compile(dialect=dialect)) + ";")
    for index in table.indexes:
        statements.append(str(CreateIndex(index).compile(dialect=dialect)) + ";")
    statements.append(f'ALTER TABLE crm_v2."{table.name}" OWNER TO crm_v2_app;')
    statements.append(f'ALTER TABLE crm_v2."{table.name}" ENABLE ROW LEVEL SECURITY;')
Path("schema-v2.sql").write_text("\n".join(statements), encoding="utf-8")
print(f"DDL gerado para {len(metadata.tables)} tabelas da v2.")
