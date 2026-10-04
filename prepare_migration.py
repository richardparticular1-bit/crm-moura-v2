"""Prepara e valida uma migração local. Nunca escreve no banco de produção.

O snapshot precisa conter os 20 conjuntos clínicos e o inventário de Storage
obtidos em leitura consistente do projeto de origem. O plano não é um backup
importável: os arquivos reais ainda precisam ser copiados e verificados.
"""
import argparse
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.orm import Session

import models
from backup_service import GROUPS, file_paths, include_files, restore_records


def prepare(legacy, snapshot):
    names = [name for name, _ in GROUPS]
    if any(not isinstance(snapshot.get(name), list) for name in names):
        raise ValueError("Snapshot incompleto; faltam conjuntos clínicos.")
    clinics = {row.get("clinicaId") for name in names for row in snapshot[name]}
    if len(clinics) != 1 or None in clinics:
        raise ValueError("O snapshot deve pertencer a uma única clínica.")
    source_id = next(iter(clinics))
    if any(row.get("clinicaId") != source_id for name in names
           for row in legacy.get(name, [])):
        raise ValueError("O backup e o snapshot pertencem a clínicas diferentes.")
    data = {name: copy.deepcopy(snapshot[name]) for name in names}
    for rows in data.values():
        for row in rows:
            row.pop("updated_at", None)
    data.update(backupVersion=2, scope="clinical", settings=snapshot.get("settings", {}),
                signatures=[], files={}, filesIncluded=False)
    inventory = {obj["path"]: obj for obj in snapshot.get("storageObjects", [])}
    for kind, group in (("paciente", "patients"), ("profissional", "profissionais")):
        for row in data[group]:
            path = f"_assinaturas/{source_id}/{kind}/{row['id']}.png"
            if path in inventory:
                data["signatures"].append({"type": kind, "id": row["id"], "path": path})
    paths = file_paths(data)
    missing = paths - inventory.keys()
    if missing:
        raise ValueError(f"{len(missing)} arquivo(s) referenciado(s) não encontrado(s) no Storage.")
    if any(int(inventory[path].get("size") or 0) > 15 * 1024 * 1024 for path in paths):
        raise ValueError("Existe arquivo acima do limite de 15 MiB do bucket de destino.")

    # Apenas em memória: conteúdo fictício para testar a transação e os vínculos.
    # Esses bytes nunca são salvos no plano ou enviados a um Storage real.
    simulation = copy.deepcopy(data)
    include_files(simulation, lambda _: (b"dry-run-placeholder", "application/octet-stream"))
    engine = create_engine("sqlite:///:memory:")
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
    models.Base.metadata.create_all(engine)
    copied = []
    try:
        with Session(engine) as db:
            db.add(models.Clinica(id=2, nome="Destino descartável"))
            db.commit()
            result = restore_records(db, 2, simulation, lambda path, *_: copied.append(path), lambda _: None)
            counts = {name: db.query(model).filter(model.clinicaId == 2).count() for name, model in GROUPS}
            if counts != {name: len(data[name]) for name in names} or len(copied) != len(paths):
                raise ValueError("A restauração de teste não preservou os totais.")
            assert result["records"] == sum(counts.values())
    finally:
        engine.dispose()
    recovered = {name: len(data[name]) for name in names if name not in legacy}
    changed = {}
    for name in names:
        if name not in legacy:
            continue
        primary = next(iter(dict(GROUPS)[name].__table__.primary_key.columns)).name
        date_fields = {c.name for c in dict(GROUPS)[name].__table__.columns if isinstance(c.type, DateTime)}
        def normalized(field, value):
            return datetime.fromisoformat(value) if field in date_fields and value else value
        old = {str(row[primary]): row for row in legacy[name]}
        new = {str(row[primary]): row for row in data[name]}
        changed[name] = sum(key not in new or any(normalized(k, new[key].get(k)) != normalized(k, v) for k, v in row.items())
                            for key, row in old.items()) + len(new.keys() - old.keys())
    return {
        "planVersion": 1, "status": "requires_file_copy_and_verification", "sourceClinicId": source_id,
        "sourceBucket": "prontuarios", "destinationBucket": "prontuarios-v2",
        "records": data, "clinicProfile": snapshot.get("clinicProfile", {}),
        "filesToCopy": [inventory[path] for path in sorted(paths)],
        "summary": {"counts": counts, "recoveredGroups": recovered,
                    "changedSinceLegacyExport": changed, "files": len(paths),
                    "fileBytes": sum(int(inventory[path].get("size") or 0) for path in paths),
                    "dryRunRecordsAndLinks": "passed", "fileContentsVerified": False},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    legacy = json.loads(args.backup.read_text(encoding="utf-8-sig"))
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8-sig"))
    plan = prepare(legacy, snapshot)
    plan["sourceSnapshotSha256"] = hashlib.sha256(args.snapshot.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(plan["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
