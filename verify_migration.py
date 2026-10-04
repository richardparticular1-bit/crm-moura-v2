"""Confere contagens, vínculos e leitura autenticada da v2 sem expor prontuários."""
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import requests

import models
from backup_service import GROUPS


def main():
    for line in Path(".env.v2").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key] = value
    from database import SessionLocal
    plan = json.loads(Path(".migration/plan.json").read_text(encoding="utf-8"))
    report = json.loads(Path(".migration/execution-report.json").read_text(encoding="utf-8"))
    if report["status"] != "complete" or len(report["files"]) != 227:
        raise ValueError("Execução incompleta.")
    token = secrets.token_hex(32)
    checks = {}
    with SessionLocal() as db:
        counts = {name: db.query(model).filter_by(clinicaId=1).count() for name, model in GROUPS}
        if counts != plan["summary"]["counts"]:
            raise ValueError("Contagens divergentes.")
        questions = {q.id for q in db.query(models.AnamnesePergunta).filter_by(clinicaId=1)}
        for row in db.query(models.AnamneseRemota).filter_by(clinicaId=1):
            answers = json.loads(row.respostas) if row.respostas else {}
            if any(key not in questions for key in answers.get("respostasClinicas", {})):
                raise ValueError("Resposta clínica sem pergunta correspondente.")
            if answers.get("modeloId") and answers["modeloId"] != row.modeloId:
                raise ValueError("Modelo da resposta divergente.")
        admin = db.query(models.Usuario).filter_by(clinicaId=1, ativo=True, isClinicaAdmin=True).first()
        if not admin:
            raise ValueError("Administrador não encontrado.")
        db.add(models.Sessao(token=token, usuarioId=admin.id, expiresAt=datetime.now()+timedelta(minutes=10)))
        db.commit()
        sample_anexo = db.query(models.Anexo).filter_by(clinicaId=1).first().id
        sample_doc = db.query(models.DocumentoEmitido).filter_by(clinicaId=1).first().id
        anamnese_ids = [a.id for a in db.query(models.AnamneseRemota).filter_by(clinicaId=1)]
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + token
    base = "https://crm-moura-v2.onrender.com/api"
    def get(path):
        r = session.get(base+path, timeout=60)
        if r.status_code != 200:
            raise ValueError(f"Falha HTTP na verificação ({r.status_code}).")
        return r.json()
    try:
        if len(get("/patients")) != counts["patients"] or len(get("/appointments")) != counts["appointments"]:
            raise ValueError("Contagens HTTP divergentes.")
        checks["patientsAndAppointments"] = "passed"
        info = get(f"/anexos/{sample_anexo}/url")
        sample = requests.get(info["url"], timeout=60)
        sample.raise_for_status()
        hashes = {item["sha256"] for item in report["files"].values()}
        if hashlib.sha256(sample.content).hexdigest() not in hashes:
            raise ValueError("Conteúdo do anexo divergente na leitura pelo CRM.")
        checks["attachmentDownload"] = "passed"
        doc = get(f"/documentos/{sample_doc}")
        if not doc.get("patientName") or not doc.get("profissionalNome"):
            raise ValueError("Documento sem vínculo de paciente ou profissional.")
        checks["documentRead"] = "passed"
        for aid in anamnese_ids:
            get(f"/anamnese-remota/{aid}")
        checks["anamneseRead"] = "passed"
        clinic = get("/clinica")
        if not clinic.get("logoUrl"):
            raise ValueError("Logo migrado não disponível.")
        image = requests.get(clinic["logoUrl"], timeout=60)
        image.raise_for_status()
        checks["clinicLogo"] = "passed"
    finally:
        with SessionLocal() as db:
            diagnostic = db.get(models.Sessao, token)
            if diagnostic:
                db.delete(diagnostic)
                db.commit()
        checks["diagnosticSessionRevoked"] = True
    Path(".migration/verification-report.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    print(json.dumps(checks, indent=2))


if __name__ == "__main__":
    main()
