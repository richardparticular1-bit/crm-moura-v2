"""Backend FastAPI do CRM — API REST completa."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import SessionLocal, init_db
from models import Config, Consulta, Paciente, Pesquisa

app = FastAPI(title="CRM Moura — Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND = Path(__file__).parent / "frontend"


@app.on_event("startup")
def startup():
    init_db()


# ── servir o frontend ──────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/sw.js")
def sw():
    return FileResponse(FRONTEND / "sw.js", media_type="application/javascript")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(FRONTEND / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/icon-192.png")
def icon192():
    return FileResponse(FRONTEND / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png")
def icon512():
    return FileResponse(FRONTEND / "icon-512.png", media_type="image/png")


# ── helpers ────────────────────────────────────────────────────────────────────
def _row(obj) -> dict[str, Any]:
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    d.pop("updated_at", None)
    return d


# ── pacientes ──────────────────────────────────────────────────────────────────
class PacienteIn(BaseModel):
    id: str | None = None
    name: str
    phone: str = ""
    email: str = ""
    birth: str | None = None
    lastVisit: str | None = None
    notes: str = ""
    reactivateSentAt: str | None = None


@app.get("/api/patients")
def list_patients():
    with SessionLocal() as db:
        return [_row(p) for p in db.query(Paciente).order_by(Paciente.name).all()]


@app.post("/api/patients", status_code=201)
def create_patient(data: PacienteIn):
    import random, time
    pid = data.id or (random.randint(100000, 999999).__str__() + str(int(time.time()))[-4:])
    with SessionLocal() as db:
        p = Paciente(**{k: v for k, v in data.model_dump().items() if k != "id"}, id=pid)
        db.add(p)
        db.commit()
        db.refresh(p)
        return _row(p)


@app.put("/api/patients/{pid}")
def update_patient(pid: str, data: PacienteIn):
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p:
            raise HTTPException(404, "Paciente não encontrado")
        for k, v in data.model_dump(exclude={"id"}).items():
            setattr(p, k, v)
        db.commit()
        db.refresh(p)
        return _row(p)


@app.delete("/api/patients/{pid}", status_code=204)
def delete_patient(pid: str):
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p:
            raise HTTPException(404)
        db.query(Consulta).filter(Consulta.patientId == pid).delete()
        db.delete(p)
        db.commit()


# ── consultas ──────────────────────────────────────────────────────────────────
class ConsultaIn(BaseModel):
    id: str | None = None
    patientId: str
    date: str
    time: str
    procedure: str = "Avaliação"
    status: str = "agendado"
    confirmSent: bool = False


@app.get("/api/appointments")
def list_appointments(date: str | None = None):
    with SessionLocal() as db:
        q = db.query(Consulta)
        if date:
            q = q.filter(Consulta.date == date)
        return [_row(a) for a in q.order_by(Consulta.date, Consulta.time).all()]


@app.post("/api/appointments", status_code=201)
def create_appointment(data: ConsultaIn):
    import random, time
    aid = data.id or (random.randint(100000, 999999).__str__() + str(int(time.time()))[-4:])
    with SessionLocal() as db:
        a = Consulta(**{k: v for k, v in data.model_dump().items() if k != "id"}, id=aid)
        db.add(a)
        db.commit()
        db.refresh(a)
        return _row(a)


@app.put("/api/appointments/{aid}")
def update_appointment(aid: str, data: ConsultaIn):
    with SessionLocal() as db:
        a = db.get(Consulta, aid)
        if not a:
            raise HTTPException(404)
        for k, v in data.model_dump(exclude={"id"}).items():
            setattr(a, k, v)
        db.commit()
        db.refresh(a)
        return _row(a)


@app.delete("/api/appointments/{aid}", status_code=204)
def delete_appointment(aid: str):
    with SessionLocal() as db:
        a = db.get(Consulta, aid)
        if not a:
            raise HTTPException(404)
        db.delete(a)
        db.commit()


# ── pesquisas ──────────────────────────────────────────────────────────────────
class PesquisaIn(BaseModel):
    id: str | None = None
    appointmentId: str
    patientId: str
    sentAt: str | None = None
    score: int | None = None
    reviewSent: bool = False
    reviewSentAt: str | None = None


@app.get("/api/surveys")
def list_surveys():
    with SessionLocal() as db:
        return [_row(s) for s in db.query(Pesquisa).all()]


@app.post("/api/surveys", status_code=201)
def create_survey(data: PesquisaIn):
    import random, time
    sid = data.id or (random.randint(100000, 999999).__str__() + str(int(time.time()))[-4:])
    with SessionLocal() as db:
        # Remove pesquisa anterior da mesma consulta
        db.query(Pesquisa).filter(Pesquisa.appointmentId == data.appointmentId).delete()
        s = Pesquisa(**{k: v for k, v in data.model_dump().items() if k != "id"}, id=sid)
        db.add(s)
        db.commit()
        db.refresh(s)
        return _row(s)


@app.put("/api/surveys/{sid}")
def update_survey(sid: str, data: PesquisaIn):
    with SessionLocal() as db:
        s = db.get(Pesquisa, sid)
        if not s:
            raise HTTPException(404)
        for k, v in data.model_dump(exclude={"id"}).items():
            setattr(s, k, v)
        db.commit()
        db.refresh(s)
        return _row(s)


# ── configurações ──────────────────────────────────────────────────────────────
@app.get("/api/settings")
def get_settings():
    with SessionLocal() as db:
        return {c.key: c.value for c in db.query(Config).all()}


@app.put("/api/settings")
def save_settings(data: dict[str, str]):
    with SessionLocal() as db:
        for k, v in data.items():
            cfg = db.get(Config, k)
            if cfg:
                cfg.value = v
            else:
                db.add(Config(key=k, value=v))
        db.commit()
        return {"ok": True}


# ── dump completo (para sincronização) ────────────────────────────────────────
@app.get("/api/dump")
def dump():
    with SessionLocal() as db:
        return {
            "patients": [_row(p) for p in db.query(Paciente).all()],
            "appointments": [_row(a) for a in db.query(Consulta).all()],
            "surveys": [_row(s) for s in db.query(Pesquisa).all()],
            "settings": {c.key: c.value for c in db.query(Config).all()},
        }


# ── importar dump do localStorage (migração única) ────────────────────────────
class DumpIn(BaseModel):
    patients: list[dict] = []
    appointments: list[dict] = []
    surveys: list[dict] = []
    settings: dict[str, Any] = {}


from typing import Any


@app.post("/api/import")
def import_dump(data: DumpIn):
    with SessionLocal() as db:
        for p in data.patients:
            if not db.get(Paciente, p["id"]):
                db.add(Paciente(**{k: v for k, v in p.items() if hasattr(Paciente, k)}))
        for a in data.appointments:
            if not db.get(Consulta, a["id"]):
                db.add(Consulta(**{k: v for k, v in a.items() if hasattr(Consulta, k)}))
        for s in data.surveys:
            if not db.get(Pesquisa, s["id"]):
                db.add(Pesquisa(**{k: v for k, v in s.items() if hasattr(Pesquisa, k)}))
        for k, v in data.settings.items():
            cfg = db.get(Config, k)
            if cfg:
                cfg.value = str(v)
            else:
                db.add(Config(key=k, value=str(v)))
        db.commit()
    return {"ok": True, "patients": len(data.patients), "appointments": len(data.appointments)}
