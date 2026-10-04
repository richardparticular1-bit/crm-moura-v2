"""Backups clínicos versionados, sem credenciais ou sessões de usuários."""
import base64
import hashlib
import json
import secrets
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import DateTime, Integer
import models

GROUPS = [
    ("profissionais", models.Profissional), ("patients", models.Paciente),
    ("appointments", models.Consulta), ("surveys", models.Pesquisa),
    ("tarefas", models.Tarefa), ("chat_mensagens", models.MensagemChat),
    ("orcamentos", models.Orcamento), ("orcamento_itens", models.OrcamentoItem),
    ("lancamentos", models.Lancamento), ("evolucoes", models.Evolucao),
    ("anexos", models.Anexo), ("retorno_config", models.RetornoConfig),
    ("despesas", models.Despesa), ("odontograma_marcas", models.OdontogramaMarca),
    ("consentimentos_midia", models.ConsentimentoMidia),
    ("consentimentos_orcamento", models.ConsentimentoOrcamento),
    ("documentos_emitidos", models.DocumentoEmitido),
    ("anamnese_modelos", models.AnamneseModelo),
    ("anamnese_perguntas", models.AnamnesePergunta),
    ("anamneses_remotas", models.AnamneseRemota),
]
MAX_FILE_TOTAL = 8 * 1024 * 1024


def export_records(db, clinic_id):
    result = {"backupVersion": 2, "scope": "clinical", "exportedAt": datetime.now().isoformat()}
    for name, model in GROUPS:
        result[name] = []
        for obj in db.query(model).filter(model.clinicaId == clinic_id).all():
            row = {}
            for column in model.__table__.columns:
                if column.name == "updated_at":
                    continue
                value = getattr(obj, column.name)
                row[column.name] = value.isoformat() if isinstance(value, datetime) else value
            result[name].append(row)
    result["settings"] = {c.key: c.value for c in db.query(models.Config).filter(models.Config.clinicaId == clinic_id).all() if not c.key.startswith("_import_")}
    result["files"] = {}
    result["filesIncluded"] = False
    return result


def file_paths(data):
    paths = {value for name, _ in GROUPS for row in data.get(name, [])
             for key, value in row.items() if key.endswith("Path") and value}
    return paths | {s["path"] for s in data.get("signatures", [])}


def include_files(data, download):
    total = 0
    for path in sorted(file_paths(data)):
        content, mime = download(path)
        total += len(content)
        if total > MAX_FILE_TOTAL:
            raise HTTPException(413, "O backup excede 8 MiB de arquivos. É necessário exportar o armazenamento separadamente.")
        data["files"][path] = {"content": base64.b64encode(content).decode(), "mime": mime}
    data["filesIncluded"] = True
    return data


def restore_records(db, clinic_id, data, upload, delete):
    if data.get("backupVersion") != 2 or data.get("scope") != "clinical":
        raise HTTPException(422, "Backup antigo ou incompleto. Use um backup clínico versão 2; não importe dados reais com o formato antigo.")
    if any(name not in data or not isinstance(data[name], list) for name, _ in GROUPS):
        raise HTTPException(422, "Backup incompleto: faltam conjuntos de registros.")
    if (any(not isinstance(row, dict) for name, _ in GROUPS for row in data[name])
            or not isinstance(data.get("settings", {}), dict)
            or not isinstance(data.get("files", {}), dict)
            or not isinstance(data.get("signatures", []), list)
            or any(not isinstance(s, dict) or not isinstance(s.get("path"), str)
                   for s in data.get("signatures", []))
            or any(not isinstance(value, str) for name, _ in GROUPS for row in data[name]
                   for key, value in row.items() if key.endswith("Path") and value)):
        raise HTTPException(422, "Estrutura de backup inválida.")
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    marker = "_import_" + digest
    if db.get(models.Config, (clinic_id, marker)):
        raise HTTPException(409, "Este backup já foi importado nesta clínica.")
    paths = file_paths(data)
    files = data.get("files", {})
    if paths and (not data.get("filesIncluded") or any(path not in files for path in paths)):
        raise HTTPException(422, "O backup não contém todos os arquivos necessários.")
    decoded = {}
    total = 0
    try:
        for path in paths:
            payload = files[path]
            content = base64.b64decode(payload["content"], validate=True)
            total += len(content)
            if len(content) > 15 * 1024 * 1024 or total > MAX_FILE_TOTAL:
                raise ValueError("size")
            decoded[path] = (content, payload.get("mime", "application/octet-stream"))
    except (KeyError, ValueError, TypeError):
        raise HTTPException(422, "Conteúdo de arquivo inválido ou acima do limite.")
    # Os nomes de origem nunca são usados para escrever no bucket: gera caminhos novos.
    path_map = {path: f"clinicas/{clinic_id}/imports/{secrets.token_hex(16)}" for path in paths}
    maps = {}
    objects = []
    uploaded = []
    try:
        for name, model in GROUPS:
            maps[name] = {}
            primary = next(c for c in model.__table__.primary_key.columns)
            for row in data[name]:
                old_id = row.get(primary.name)
                if old_id is None or str(old_id) in maps[name]:
                    raise HTTPException(422, f"ID inválido ou duplicado em {name}.")
                values = {k: v for k, v in row.items() if k in model.__table__.columns and k != "clinicaId"}
                for column in model.__table__.columns:
                    if isinstance(column.type, DateTime) and values.get(column.name):
                        values[column.name] = datetime.fromisoformat(values[column.name])
                    if column.name.endswith("Path") and values.get(column.name):
                        values[column.name] = path_map[values[column.name]]
                if primary.name == "id":
                    values.pop("id", None)
                    if not isinstance(primary.type, Integer):
                        values["id"] = secrets.token_hex(10)
                for field, target in (("patientId", "patients"), ("appointmentId", "appointments"),
                                      ("profissionalId", "profissionais"), ("responsavelId", "profissionais"),
                                      ("aprovadoPor", "profissionais"), ("orcamentoId", "orcamentos"),
                                      ("modeloId", "anamnese_modelos")):
                    if field in values and values[field] is not None:
                        original = str(values[field])
                        if original not in maps.get(target, {}):
                            raise HTTPException(422, f"Referência inválida: {name}.{field}.")
                        values[field] = maps[target][original]
                if values.get("numOrcamento") and str(values["numOrcamento"]) in maps.get("orcamentos", {}):
                    values["numOrcamento"] = maps["orcamentos"][str(values["numOrcamento"])]
                if "tokenPortal" in values:
                    values["tokenPortal"] = None
                if "token" in values:
                    values["token"] = secrets.token_urlsafe(24)
                origin = values.pop("origemId", None)
                obj = model(**values, clinicaId=clinic_id)
                db.add(obj)
                db.flush()
                maps[name][str(old_id)] = getattr(obj, primary.name)
                objects.append((obj, origin))
        for obj, origin in objects:
            if origin is not None:
                if str(origin) not in maps["anexos"]:
                    raise HTTPException(422, "Origem de anexo inválida.")
                obj.origemId = maps["anexos"][str(origin)]
        for signature in data.get("signatures", []):
            group = {"paciente": "patients", "profissional": "profissionais"}.get(signature.get("type"))
            if not group or str(signature.get("id")) not in maps[group]:
                raise HTTPException(422, "Assinatura sem paciente ou profissional correspondente.")
            path_map[signature["path"]] = f"_assinaturas/{clinic_id}/{signature['type']}/{maps[group][str(signature['id'])]}.png"
        for key, value in data.get("settings", {}).items():
            if key.startswith("_import_"):
                continue
            cfg = db.get(models.Config, (clinic_id, key))
            if cfg:
                cfg.value = str(value)
            else:
                db.add(models.Config(clinicaId=clinic_id, key=key, value=str(value)))
        db.flush()
        for path, (content, mime) in decoded.items():
            uploaded.append(path_map[path])
            upload(path_map[path], content, mime)
        db.add(models.Config(clinicaId=clinic_id, key=marker, value=datetime.now().isoformat()))
        db.commit()
        return {"ok": True, "patients": len(data["patients"]), "appointments": len(data["appointments"]),
                "records": sum(len(data[name]) for name, _ in GROUPS), "files": len(uploaded)}
    except Exception as exc:
        db.rollback()
        for path in uploaded:
            try:
                delete(path)
            except Exception:
                pass  # arquivo órfão é preferível a sobrescrever ou apagar arquivos existentes
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(422, "Backup inválido ou falha de restauração; os registros não foram importados.") from exc
