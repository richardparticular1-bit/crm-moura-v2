"""Migração controlada: banco exclusivo da v2 e ponte temporária de Storage.

As credenciais ficam em arquivos locais ignorados pelo Git. Nenhuma chamada
escreve na origem. Cada cópia é verificada por SHA-256 antes do commit.
"""
import argparse
import json
import os
import secrets
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from sqlalchemy import select, text

from backup_service import GROUPS, restore_records
import models


class StorageBridge:
    def __init__(self, config, report_path):
        self.config = config
        self.report_path = report_path
        self.copied = {}
        self.planned = {}
        self.futures = []
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.lock = threading.Lock()
        self.url = "https://ahunkumobdexrcrrppvs.supabase.co/functions/v1/crm-v2-storage-migration"

    def request(self, source, destination, action="copy"):
        for attempt in range(3):
            try:
                r = requests.post(self.url, headers={"x-migration-token": self.config["token"]},
                                      json={"source": source, "destination": destination, "action": action}, timeout=90)
                if r.status_code == 200 and r.json().get("ok"):
                    return r.json()
                if r.status_code < 500:
                    raise ValueError(f"Ponte recusou a operação ({r.status_code}, {r.json().get('error')}).")
            except requests.RequestException:
                pass
            if attempt < 2:
                time.sleep(2)
        raise RuntimeError("Não foi possível copiar/verificar um arquivo após três tentativas.")

    def copy(self, source, destination):
        result = self.request(source, destination)
        with self.lock:
            self.copied[destination] = {"source": source, **result}
            self.report_path.write_text(json.dumps({"status": "copying", "files": self.copied}), encoding="utf-8")
            if len(self.copied) % 10 == 0 or len(self.copied) == len(self.config["sourceFiles"]):
                print(f"Arquivos copiados e verificados: {len(self.copied)}/{len(self.config['sourceFiles'])}", flush=True)

    def schedule(self, source, destination):
        self.planned[destination] = source
        self.futures.append(self.executor.submit(self.copy, source, destination))

    def finish(self):
        self.executor.shutdown(wait=True)
        for future in as_completed(self.futures):
            future.result()

    def delete(self, destination):
        source = self.planned.get(destination) or self.copied.get(destination, {}).get("source")
        if source:
            self.request(source, destination, "delete")


def execute(plan, config, sessions, bridge, *, dry_run=False):
    clinic_id = config["clinicId"]
    profile = plan.get("clinicProfile", {})
    extra_path = None
    with sessions() as db:
        # Também serializa duas execuções da ferramenta para o mesmo destino.
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 820000 + clinic_id})
        clinic = db.scalar(select(models.Clinica).where(models.Clinica.id == clinic_id).with_for_update())
        if not clinic:
            raise ValueError("Clínica de destino não encontrada.")
        if any(db.query(model).filter_by(clinicaId=clinic_id).count() for _, model in GROUPS):
            raise ValueError("Destino contém dados clínicos. Nenhum registro será sobrescrito.")

        def before_commit(transaction):
            nonlocal extra_path
            actual = {name: transaction.query(model).filter_by(clinicaId=clinic_id).count() for name, model in GROUPS}
            if actual != plan["summary"]["counts"]:
                raise ValueError("Totais de destino divergentes antes do commit.")
            fields = ("nome", "responsavelTecnico", "croResponsavel", "croUf", "cnpj", "enderecoCompleto",
                      "telefoneWhatsapp", "email", "cidade", "chavePix", "tipoChavePix")
            for field in fields:
                if field in profile:
                    setattr(clinic, field, profile[field] or "")
            if profile.get("logoPath"):
                extra_path = f"clinicas/{clinic_id}/imports/{config['runId']}/{secrets.token_hex(16)}"
                bridge.schedule(profile["logoPath"], extra_path)
                clinic.logoPath = extra_path
            bridge.finish()
            # Uma clínica com um único profissional pode associar o seu dono,
            # sem copiar contas/senhas do ambiente original.
            if len(plan["records"]["profissionais"]) == 1:
                prof = transaction.query(models.Profissional).filter_by(clinicaId=clinic_id).one()
                for user in transaction.query(models.Usuario).filter_by(clinicaId=clinic_id, isClinicaAdmin=True):
                    if user.profissionalId is None:
                        user.profissionalId = prof.id
            transaction.flush()

        try:
            result = restore_records(db, clinic_id, plan["records"], lambda *_: None, bridge.delete,
                                     external_copy=bridge.schedule,
                                     storage_prefix=f"clinicas/{clinic_id}/imports/{config['runId']}",
                                     before_commit=before_commit, dry_run=dry_run)
        except Exception:
            if extra_path:
                bridge.delete(extra_path)
            raise
        if dry_run:
            return {"status": "dry_run_passed", "records": result["records"]}
        actual = {name: db.query(model).filter_by(clinicaId=clinic_id).count() for name, model in GROUPS}
        if actual != plan["summary"]["counts"]:
            raise RuntimeError("Totais divergentes após commit; requer revisão.")
        return {"status": "complete", "records": result["records"], "counts": actual,
                "files": bridge.copied, "originalModified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=Path(".migration/plan.json"))
    parser.add_argument("--config", type=Path, default=Path(".migration/bridge-config.json"))
    parser.add_argument("--dry-run", action="store_true", help="Valida e desfaz a transação, sem copiar arquivos.")
    args = parser.parse_args()
    for line in Path(".env.v2").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key] = value
    from database import SessionLocal
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report_path = args.plan.parent / "execution-report.json"
    if args.dry_run:
        class DryRunBridge:
            copied = {}
            def schedule(self, *_): pass
            def finish(self): pass
            def delete(self, *_): pass
        result = execute(plan, config, SessionLocal, DryRunBridge(), dry_run=True)
        print(f"Transação PostgreSQL validada e desfeita: {result['records']} registros; nenhum arquivo copiado.", flush=True)
        return
    bridge = StorageBridge(config, report_path)
    report = execute(plan, config, SessionLocal, bridge)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Migração concluída: {report['records']} registros, {len(report['files'])} arquivos verificados.", flush=True)


if __name__ == "__main__":
    main()
