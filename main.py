"""Backend FastAPI do CRM — versão 2.0"""
from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import SessionLocal, init_db
from models import Config, Consulta, MensagemChat, Paciente, Pesquisa, Profissional, Tarefa

app = FastAPI(title="CRM Moura — Backend v2")

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


# ── frontend ───────────────────────────────────────────────────────────────────
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

def _new_id() -> str:
    return str(random.randint(100000, 999999)) + str(int(time.time()))[-4:]


# ══════════════════════════════════════════════════════════════════════════════
# PROFISSIONAIS
# ══════════════════════════════════════════════════════════════════════════════
class ProfissionalIn(BaseModel):
    nome: str
    cro: str = ""
    especialidade: str = ""
    telefone: str = ""
    email: str = ""
    cor: str = "#0ea5e9"
    ativo: bool = True


@app.get("/api/profissionais")
def list_profissionais():
    with SessionLocal() as db:
        return [_row(p) for p in db.query(Profissional).order_by(Profissional.nome).all()]


@app.post("/api/profissionais", status_code=201)
def create_profissional(data: ProfissionalIn):
    with SessionLocal() as db:
        p = Profissional(**data.model_dump())
        db.add(p)
        db.commit()
        db.refresh(p)
        return _row(p)


@app.put("/api/profissionais/{pid}")
def update_profissional(pid: int, data: ProfissionalIn):
    with SessionLocal() as db:
        p = db.get(Profissional, pid)
        if not p:
            raise HTTPException(404, "Profissional não encontrado")
        for k, v in data.model_dump().items():
            setattr(p, k, v)
        db.commit()
        db.refresh(p)
        return _row(p)


@app.delete("/api/profissionais/{pid}", status_code=204)
def delete_profissional(pid: int):
    with SessionLocal() as db:
        p = db.get(Profissional, pid)
        if not p:
            raise HTTPException(404)
        db.delete(p)
        db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# PACIENTES
# ══════════════════════════════════════════════════════════════════════════════
class PacienteIn(BaseModel):
    id: str | None = None
    name: str
    phone: str = ""
    email: str = ""
    birth: str | None = None
    lastVisit: str | None = None
    notes: str = ""
    reactivateSentAt: str | None = None
    birthdaySentYear: str | None = None


@app.get("/api/patients")
def list_patients():
    with SessionLocal() as db:
        return [_row(p) for p in db.query(Paciente).order_by(Paciente.name).all()]


@app.post("/api/patients", status_code=201)
def create_patient(data: PacienteIn):
    pid = data.id or _new_id()
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


# ══════════════════════════════════════════════════════════════════════════════
# CONSULTAS  (agora com profissional, prontuário, orçamento, duração)
# ══════════════════════════════════════════════════════════════════════════════
class ConsultaIn(BaseModel):
    id: str | None = None
    patientId: str
    profissionalId: int | None = None
    numProntuario: str = ""
    numOrcamento: str = ""
    date: str
    time: str
    duracaoMinutos: int = 60
    procedure: str = "Avaliação"
    status: str = "agendado"
    confirmSent: bool = False


@app.get("/api/appointments")
def list_appointments(date: str | None = None):
    with SessionLocal() as db:
        q = db.query(Consulta)
        if date:
            q = q.filter(Consulta.date == date)
        rows = q.order_by(Consulta.date, Consulta.time).all()
        result = []
        for a in rows:
            r = _row(a)
            # Injeta nome do profissional para facilitar o frontend
            if a.profissionalId:
                prof = db.get(Profissional, a.profissionalId)
                r["profissionalNome"] = prof.nome if prof else ""
                r["profissionalCor"] = prof.cor if prof else "#0ea5e9"
            else:
                r["profissionalNome"] = ""
                r["profissionalCor"] = "#64748b"
            result.append(r)
        return result


@app.post("/api/appointments", status_code=201)
def create_appointment(data: ConsultaIn):
    aid = data.id or _new_id()
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


# ══════════════════════════════════════════════════════════════════════════════
# PESQUISAS NPS
# ══════════════════════════════════════════════════════════════════════════════
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
    sid = data.id or _new_id()
    with SessionLocal() as db:
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


# ══════════════════════════════════════════════════════════════════════════════
# TAREFAS  (kanban: pendente | em_andamento | concluida)
# ══════════════════════════════════════════════════════════════════════════════
class TarefaIn(BaseModel):
    titulo: str
    descricao: str = ""
    status: str = "pendente"
    prioridade: str = "normal"
    responsavelId: int | None = None
    patientId: str | None = None
    dataVencimento: str | None = None


@app.get("/api/tarefas")
def list_tarefas(status: str | None = None):
    with SessionLocal() as db:
        q = db.query(Tarefa)
        if status:
            q = q.filter(Tarefa.status == status)
        rows = q.order_by(Tarefa.dataVencimento.asc().nullslast(), Tarefa.id.asc()).all()
        result = []
        for t in rows:
            r = _row(t)
            r["created_at"] = t.created_at.isoformat() if t.created_at else None
            if t.responsavelId:
                prof = db.get(Profissional, t.responsavelId)
                r["responsavelNome"] = prof.nome if prof else ""
            else:
                r["responsavelNome"] = ""
            if t.patientId:
                pac = db.get(Paciente, t.patientId)
                r["patientName"] = pac.name if pac else ""
            else:
                r["patientName"] = ""
            result.append(r)
        return result


@app.post("/api/tarefas", status_code=201)
def create_tarefa(data: TarefaIn):
    with SessionLocal() as db:
        t = Tarefa(**data.model_dump())
        db.add(t)
        db.commit()
        db.refresh(t)
        return _row(t)


@app.put("/api/tarefas/{tid}")
def update_tarefa(tid: int, data: TarefaIn):
    with SessionLocal() as db:
        t = db.get(Tarefa, tid)
        if not t:
            raise HTTPException(404, "Tarefa não encontrada")
        for k, v in data.model_dump().items():
            setattr(t, k, v)
        db.commit()
        db.refresh(t)
        return _row(t)


@app.delete("/api/tarefas/{tid}", status_code=204)
def delete_tarefa(tid: int):
    with SessionLocal() as db:
        t = db.get(Tarefa, tid)
        if not t:
            raise HTTPException(404)
        db.delete(t)
        db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# CHAT DA EQUIPE
# ══════════════════════════════════════════════════════════════════════════════
class MensagemIn(BaseModel):
    profissionalId: int
    canal: str = "geral"
    conteudo: str


@app.get("/api/chat")
def list_mensagens(canal: str = "geral", limit: int = 100):
    with SessionLocal() as db:
        rows = (
            db.query(MensagemChat)
            .filter(MensagemChat.canal == canal)
            .order_by(MensagemChat.created_at.desc())
            .limit(limit)
            .all()
        )
        result = []
        for m in reversed(rows):
            r = _row(m)
            r["created_at"] = m.created_at.isoformat()
            prof = db.get(Profissional, m.profissionalId)
            r["profissionalNome"] = prof.nome if prof else "Desconhecido"
            r["profissionalCor"] = prof.cor if prof else "#64748b"
            result.append(r)
        return result


@app.post("/api/chat", status_code=201)
def send_mensagem(data: MensagemIn):
    with SessionLocal() as db:
        m = MensagemChat(**data.model_dump())
        db.add(m)
        db.commit()
        db.refresh(m)
        r = _row(m)
        r["created_at"] = m.created_at.isoformat()
        prof = db.get(Profissional, m.profissionalId)
        r["profissionalNome"] = prof.nome if prof else ""
        r["profissionalCor"] = prof.cor if prof else "#64748b"
        return r


@app.get("/api/chat/canais")
def list_canais():
    """Retorna canais únicos existentes + 'geral' sempre presente."""
    with SessionLocal() as db:
        rows = db.query(MensagemChat.canal).distinct().all()
        canais = list({r[0] for r in rows} | {"geral"})
        return sorted(canais)


# ══════════════════════════════════════════════════════════════════════════════
# ANIVERSARIANTES DO DIA
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/aniversariantes")
def aniversariantes_hoje():
    """Retorna pacientes que fazem aniversário hoje (MM-DD)."""
    from datetime import date
    hoje = date.today().strftime("%m-%d")
    with SessionLocal() as db:
        pacientes = db.query(Paciente).filter(Paciente.birth.isnot(None)).all()
        result = []
        for p in pacientes:
            if p.birth and len(p.birth) == 10:
                mm_dd = p.birth[5:]  # "YYYY-MM-DD" → "MM-DD"
                if mm_dd == hoje:
                    r = _row(p)
                    result.append(r)
        return result


@app.put("/api/patients/{pid}/birthday-sent")
def mark_birthday_sent(pid: str):
    """Marca que o parabéns foi enviado neste ano."""
    from datetime import date
    ano = str(date.today().year)
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p:
            raise HTTPException(404)
        p.birthdaySentYear = ano
        db.commit()
        return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURAÇÕES
# ══════════════════════════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════════════════════════
# DUMP / IMPORT
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/dump")
def dump():
    with SessionLocal() as db:
        return {
            "patients": [_row(p) for p in db.query(Paciente).all()],
            "appointments": [_row(a) for a in db.query(Consulta).all()],
            "surveys": [_row(s) for s in db.query(Pesquisa).all()],
            "profissionais": [_row(p) for p in db.query(Profissional).all()],
            "tarefas": [_row(t) for t in db.query(Tarefa).all()],
            "settings": {c.key: c.value for c in db.query(Config).all()},
        }


class DumpIn(BaseModel):
    patients: list[dict] = []
    appointments: list[dict] = []
    surveys: list[dict] = []
    settings: dict[str, Any] = {}


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
