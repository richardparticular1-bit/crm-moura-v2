"""Backend FastAPI do CRM — versão 2.1
Novidades v2.1:
  - GET /api/appointments/search?q=  → busca agendamentos pelo nome do paciente
  - Validação de conflito de horário ao criar/editar consulta (409 Conflict)
"""
from __future__ import annotations

import json
import os
import random
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import bcrypt
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pywebpush import webpush, WebPushException

from database import SessionLocal, init_db
from models import Anexo, Config, Consulta, Evolucao, Lancamento, MensagemChat, NotificacaoEnviada, Orcamento, OrcamentoItem, Paciente, Pesquisa, Profissional, PushSubscription, RetornoConfig, Sessao, Tarefa, Usuario

app = FastAPI(title="CRM Moura — Backend v2.6")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND = Path(__file__).parent / "frontend"

# Chaves VAPID SOMENTE via variáveis de ambiente (sem fallback no código).
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS = {"sub": "mailto:contato@mouraodontologia.com.br"}

scheduler = BackgroundScheduler()


@app.on_event("startup")
def startup():
    init_db()
    if not scheduler.running:
        scheduler.add_job(checar_consultas_proximas, "interval", minutes=5, id="check_appts", replace_existing=True)
        scheduler.add_job(backup_automatico, "cron", hour=6, minute=0, id="backup_diario", replace_existing=True)  # 06:00 UTC = 03:00 BRT
        scheduler.start()


@app.on_event("shutdown")
def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


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


# ══════════════════════════════════════════════════════════════════════════════
# AUTENTICAÇÃO  (bcrypt + sessões com token opaco; TODA a API exige login)
# ══════════════════════════════════════════════════════════════════════════════
SESSAO_DIAS = 30
AUTH_LIVRE = ("/api/auth/login", "/api/auth/setup", "/api/auth/status")


def _hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


def _verifica_senha(senha: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(senha.encode(), h.encode())
    except ValueError:
        return False


def _usuario_do_token(db, token: str) -> Usuario | None:
    if not token:
        return None
    s = db.get(Sessao, token)
    if not s or s.expiresAt < datetime.now():
        return None
    u = db.get(Usuario, s.usuarioId)
    return u if (u and u.ativo) else None


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if request.method != "OPTIONS" and path.startswith("/api") and not any(path.startswith(w) for w in AUTH_LIVRE):
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        with SessionLocal() as db:
            if _usuario_do_token(db, token) is None:
                return JSONResponse({"detail": "Não autenticado. Faça login."}, status_code=401)
    return await call_next(request)


class SetupIn(BaseModel):
    nome: str
    email: str
    senha: str


class LoginIn(BaseModel):
    email: str
    senha: str


def _criar_sessao(db, usuario: Usuario) -> str:
    # higiene: remove sessões expiradas do usuário
    db.query(Sessao).filter(Sessao.usuarioId == usuario.id, Sessao.expiresAt < datetime.now()).delete()
    token = secrets.token_urlsafe(32)
    db.add(Sessao(token=token, usuarioId=usuario.id, expiresAt=datetime.now() + timedelta(days=SESSAO_DIAS)))
    db.commit()
    return token


@app.get("/api/auth/status")
def auth_status():
    """Diz ao frontend se o sistema ainda precisa do primeiro usuário (setup)."""
    with SessionLocal() as db:
        return {"setup": db.query(Usuario).count() == 0}


@app.post("/api/auth/setup", status_code=201)
def auth_setup(data: SetupIn):
    """Cria o PRIMEIRO usuário. Só funciona enquanto não existe nenhum."""
    if len(data.senha) < 8:
        raise HTTPException(422, "A senha precisa de pelo menos 8 caracteres.")
    with SessionLocal() as db:
        if db.query(Usuario).count() > 0:
            raise HTTPException(403, "O sistema já foi configurado. Faça login.")
        u = Usuario(nome=data.nome.strip(), email=data.email.strip().lower(), senhaHash=_hash_senha(data.senha), ativo=True)
        db.add(u)
        db.commit()
        db.refresh(u)
        token = _criar_sessao(db, u)
        return {"token": token, "nome": u.nome}


# Rate limiting do login: 5 tentativas erradas bloqueiam o e-mail por 15 minutos.
# Em memória (reinicia com o deploy) — suficiente para barrar força bruta casual.
LOGIN_FALHAS: dict[str, list] = {}  # email -> [tentativas, bloqueado_ate_epoch]
LOGIN_MAX_TENTATIVAS = 5
LOGIN_BLOQUEIO_SEG = 15 * 60


@app.post("/api/auth/login")
def auth_login(data: LoginIn):
    email = data.email.strip().lower()
    agora = time.time()
    reg = LOGIN_FALHAS.get(email)
    if reg and reg[1] > agora:
        restam = int((reg[1] - agora) // 60) + 1
        raise HTTPException(429, f"Muitas tentativas. Tente novamente em {restam} min.")
    with SessionLocal() as db:
        u = db.query(Usuario).filter(Usuario.email == email).first()
        if not u or not _verifica_senha(data.senha, u.senhaHash):
            reg = LOGIN_FALHAS.setdefault(email, [0, 0])
            reg[0] += 1
            if reg[0] >= LOGIN_MAX_TENTATIVAS:
                reg[1] = agora + LOGIN_BLOQUEIO_SEG
                reg[0] = 0
            raise HTTPException(401, "E-mail ou senha incorretos.")
        if not u.ativo:
            raise HTTPException(403, "Usuário desativado. Fale com o administrador.")
        LOGIN_FALHAS.pop(email, None)
        token = _criar_sessao(db, u)
        return {"token": token, "nome": u.nome}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    with SessionLocal() as db:
        if token:
            db.query(Sessao).filter(Sessao.token == token).delete()
            db.commit()
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    with SessionLocal() as db:
        u = _usuario_do_token(db, token)
        if not u:
            raise HTTPException(401, "Não autenticado.")
        return {"id": u.id, "nome": u.nome, "email": u.email}


# ── gestão de usuários (qualquer usuário logado; equipe pequena) ───────────────
class UsuarioIn(BaseModel):
    nome: str
    email: str
    senha: str | None = None  # obrigatória ao criar; opcional ao editar (troca)
    ativo: bool = True


@app.get("/api/usuarios")
def list_usuarios():
    with SessionLocal() as db:
        return [
            {"id": u.id, "nome": u.nome, "email": u.email, "ativo": u.ativo}
            for u in db.query(Usuario).order_by(Usuario.nome).all()
        ]


@app.post("/api/usuarios", status_code=201)
def create_usuario(data: UsuarioIn):
    if not data.senha or len(data.senha) < 8:
        raise HTTPException(422, "A senha precisa de pelo menos 8 caracteres.")
    with SessionLocal() as db:
        if db.query(Usuario).filter(Usuario.email == data.email.strip().lower()).first():
            raise HTTPException(409, "Já existe um usuário com este e-mail.")
        u = Usuario(nome=data.nome.strip(), email=data.email.strip().lower(), senhaHash=_hash_senha(data.senha), ativo=data.ativo)
        db.add(u)
        db.commit()
        return {"id": u.id, "nome": u.nome, "email": u.email, "ativo": u.ativo}


@app.put("/api/usuarios/{uid}")
def update_usuario(uid: int, data: UsuarioIn):
    with SessionLocal() as db:
        u = db.get(Usuario, uid)
        if not u:
            raise HTTPException(404, "Usuário não encontrado")
        if not data.ativo and u.ativo:
            ativos = db.query(Usuario).filter(Usuario.ativo == True).count()  # noqa: E712
            if ativos <= 1:
                raise HTTPException(409, "Não é possível desativar o último usuário ativo.")
        novo_email = data.email.strip().lower()
        existente = db.query(Usuario).filter(Usuario.email == novo_email, Usuario.id != uid).first()
        if existente:
            raise HTTPException(409, "Já existe um usuário com este e-mail.")
        u.nome = data.nome.strip()
        u.email = novo_email
        u.ativo = data.ativo
        if data.senha:
            if len(data.senha) < 8:
                raise HTTPException(422, "A senha precisa de pelo menos 8 caracteres.")
            u.senhaHash = _hash_senha(data.senha)
            db.query(Sessao).filter(Sessao.usuarioId == uid).delete()  # troca de senha derruba sessões
        db.commit()
        return {"id": u.id, "nome": u.nome, "email": u.email, "ativo": u.ativo}


# ── helpers ────────────────────────────────────────────────────────────────────
def _row(obj) -> dict[str, Any]:
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    d.pop("updated_at", None)
    return d

def _new_id() -> str:
    return str(random.randint(100000, 999999)) + str(int(time.time()))[-4:]


def _minutos(hhmm: str) -> int | None:
    """'14:30' → 870. Retorna None se o formato for inválido."""
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _consulta_conflitante(db, data: "ConsultaIn", exclude_id: str | None = None) -> Consulta | None:
    """Verifica se já existe consulta do MESMO profissional, na MESMA data,
    com horário sobreposto. Retorna a consulta conflitante ou None.
    Consultas canceladas não contam como conflito."""
    if not data.profissionalId:
        return None
    inicio_novo = _minutos(data.time)
    if inicio_novo is None:
        return None
    fim_novo = inicio_novo + (data.duracaoMinutos or 60)

    q = db.query(Consulta).filter(
        Consulta.profissionalId == data.profissionalId,
        Consulta.date == data.date,
        Consulta.status != "cancelado",
    )
    if exclude_id:
        q = q.filter(Consulta.id != exclude_id)

    for c in q.all():
        inicio_c = _minutos(c.time)
        if inicio_c is None:
            continue
        fim_c = inicio_c + (c.duracaoMinutos or 60)
        if inicio_novo < fim_c and inicio_c < fim_novo:  # sobreposição
            return c
    return None


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
    numProntuario: str = ""
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
# CONSULTAS  (com profissional, prontuário, orçamento, duração)
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


# IMPORTANTE: esta rota fica ANTES de qualquer rota /api/appointments/{aid}
@app.get("/api/appointments/search")
def search_appointments(q: str, limit: int = 50):
    """Busca agendamentos pelo nome do paciente (parcial, sem case).
    Retorna ordenado por data/hora, com nome do paciente e do profissional."""
    termo = (q or "").strip()
    if len(termo) < 2:
        return []
    with SessionLocal() as db:
        pacientes = (
            db.query(Paciente)
            .filter(Paciente.name.ilike(f"%{termo}%"))
            .all()
        )
        if not pacientes:
            return []
        por_id = {p.id: p for p in pacientes}
        rows = (
            db.query(Consulta)
            .filter(Consulta.patientId.in_(list(por_id.keys())))
            .order_by(Consulta.date.desc(), Consulta.time.desc())
            .limit(limit)
            .all()
        )
        result = []
        for a in rows:
            r = _row(a)
            pac = por_id.get(a.patientId)
            r["patientName"] = pac.name if pac else ""
            r["patientPhone"] = pac.phone if pac else ""
            if a.profissionalId:
                prof = db.get(Profissional, a.profissionalId)
                r["profissionalNome"] = prof.nome if prof else ""
                r["profissionalCor"] = prof.cor if prof else "#0ea5e9"
            else:
                r["profissionalNome"] = ""
                r["profissionalCor"] = "#64748b"
            result.append(r)
        return result


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
        conflito = _consulta_conflitante(db, data)
        if conflito:
            pac = db.get(Paciente, conflito.patientId)
            nome = pac.name if pac else "outro paciente"
            raise HTTPException(
                409,
                f"Conflito de horário: {nome} já está agendado às {conflito.time} "
                f"({conflito.duracaoMinutos or 60} min) com este profissional.",
            )
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
        conflito = _consulta_conflitante(db, data, exclude_id=aid)
        if conflito:
            pac = db.get(Paciente, conflito.patientId)
            nome = pac.name if pac else "outro paciente"
            raise HTTPException(
                409,
                f"Conflito de horário: {nome} já está agendado às {conflito.time} "
                f"({conflito.duracaoMinutos or 60} min) com este profissional.",
            )
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
# PUSH NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════
class PushSubIn(BaseModel):
    endpoint: str
    keys: dict[str, str]
    profissionalId: int | None = None
    label: str = ""


@app.get("/api/push/vapid-public-key")
def get_vapid_public_key():
    return {"publicKey": VAPID_PUBLIC_KEY}


@app.post("/api/push/subscribe", status_code=201)
def push_subscribe(data: PushSubIn):
    with SessionLocal() as db:
        existing = db.query(PushSubscription).filter(PushSubscription.endpoint == data.endpoint).first()
        if existing:
            existing.p256dh = data.keys.get("p256dh", "")
            existing.auth = data.keys.get("auth", "")
            existing.profissionalId = data.profissionalId
            existing.label = data.label
            existing.ativo = True
            db.commit()
            return {"ok": True, "updated": True}
        sub = PushSubscription(
            endpoint=data.endpoint,
            p256dh=data.keys.get("p256dh", ""),
            auth=data.keys.get("auth", ""),
            profissionalId=data.profissionalId,
            label=data.label,
            ativo=True,
        )
        db.add(sub)
        db.commit()
        return {"ok": True, "created": True}


class PushUnsubIn(BaseModel):
    endpoint: str


@app.post("/api/push/unsubscribe")
def push_unsubscribe(data: PushUnsubIn):
    with SessionLocal() as db:
        db.query(PushSubscription).filter(PushSubscription.endpoint == data.endpoint).delete()
        db.commit()
        return {"ok": True}


def _enviar_push(subscription: PushSubscription, title: str, body: str, url: str = "/", db=None):
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS.copy(),
        )
        return True
    except WebPushException as ex:
        # Endpoint expirado/inválido (410 Gone) -> desativa a inscrição
        if "410" in str(ex) or "404" in str(ex):
            if db is not None:
                subscription.ativo = False
                db.commit()
        return False


class PushTestIn(BaseModel):
    profissionalId: int | None = None


@app.post("/api/push/test")
def push_test(data: PushTestIn):
    with SessionLocal() as db:
        q = db.query(PushSubscription).filter(PushSubscription.ativo == True)  # noqa: E712
        if data.profissionalId:
            q = q.filter(PushSubscription.profissionalId == data.profissionalId)
        subs = q.all()
        sent = 0
        for s in subs:
            if _enviar_push(s, "CRM Moura", "Notificação de teste 🦷 — tudo funcionando!", "/", db):
                sent += 1
        return {"ok": True, "sent": sent, "total": len(subs)}


def checar_consultas_proximas():
    """Job do scheduler: roda a cada 5 minutos, verifica consultas que vão começar
    dentro do prazo configurado (notifLembreteMinutos) e dispara push, evitando duplicar."""
    with SessionLocal() as db:
        ativas = db.get(Config, "notifAtivas")
        if ativas and ativas.value == "false":
            return
        lembrete_cfg = db.get(Config, "notifLembreteMinutos")
        lembrete_min = int(lembrete_cfg.value) if lembrete_cfg and lembrete_cfg.value.isdigit() else 60

        agora = datetime.now()
        janela_fim = agora + timedelta(minutes=lembrete_min)
        janela_inicio = agora + timedelta(minutes=max(0, lembrete_min - 5))

        candidatas = db.query(Consulta).filter(Consulta.status.in_(["agendado", "confirmado"])).all()
        for a in candidatas:
            try:
                dt = datetime.strptime(f"{a.date} {a.time}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if not (janela_inicio <= dt <= janela_fim):
                continue
            ja_enviado = (
                db.query(NotificacaoEnviada)
                .filter(NotificacaoEnviada.appointmentId == a.id, NotificacaoEnviada.tipo == "lembrete")
                .first()
            )
            if ja_enviado:
                continue
            paciente = db.get(Paciente, a.patientId)
            nome_pac = paciente.name if paciente else "Paciente"
            prof = db.get(Profissional, a.profissionalId) if a.profissionalId else None

            subs_q = db.query(PushSubscription).filter(PushSubscription.ativo == True)  # noqa: E712
            if a.profissionalId:
                subs_q = subs_q.filter(
                    (PushSubscription.profissionalId == a.profissionalId) | (PushSubscription.profissionalId.is_(None))
                )
            subs = subs_q.all()

            titulo = f"Consulta em {lembrete_min} min"
            corpo = f"{nome_pac} às {a.time}" + (f" — {a.procedure}" if a.procedure else "")
            if prof:
                corpo += f" · {prof.nome}"

            for s in subs:
                _enviar_push(s, titulo, corpo, "/", db)

            db.add(NotificacaoEnviada(appointmentId=a.id, tipo="lembrete"))
            db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# ANEXOS DO PRONTUÁRIO  (binários no Supabase Storage; bucket privado "prontuarios")
# ══════════════════════════════════════════════════════════════════════════════
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ANEXO_BUCKET = "prontuarios"
ANEXO_MAX_BYTES = 15 * 1024 * 1024  # 15 MB
ANEXO_MIMES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
    "application/pdf",
}
ANEXO_CATEGORIAS = {"radiografia", "documento", "consentimento", "foto", "outro"}


def _sb_headers(content_type: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "apikey": SUPABASE_SERVICE_KEY}
    if content_type:
        h["Content-Type"] = content_type
    return h


def _sb_configurado():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise HTTPException(503, "Storage não configurado: defina SUPABASE_URL e SUPABASE_SERVICE_KEY no Render.")


def _sb_upload(path: str, conteudo: bytes, mime: str):
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{ANEXO_BUCKET}/{path}",
        headers=_sb_headers(mime), data=conteudo, timeout=60,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(502, f"Falha ao enviar ao storage ({r.status_code}). O bucket '{ANEXO_BUCKET}' existe no Supabase?")


def _sb_signed_url(path: str, segundos: int = 300) -> str:
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/sign/{ANEXO_BUCKET}/{path}",
        headers=_sb_headers("application/json"), json={"expiresIn": segundos}, timeout=30,
    )
    if r.status_code != 200:
        raise HTTPException(502, "Falha ao gerar link do arquivo.")
    return f"{SUPABASE_URL}/storage/v1{r.json()['signedURL']}"


def _sb_delete(path: str):
    requests.delete(
        f"{SUPABASE_URL}/storage/v1/object/{ANEXO_BUCKET}/{path}",
        headers=_sb_headers(), timeout=30,
    )  # best-effort: se falhar, o metadado já terá sido removido do banco


def _nome_seguro(nome: str) -> str:
    import re
    base = re.sub(r"[^\w.\-]", "_", nome or "arquivo")
    return base[:120]


@app.get("/api/anexos")
def list_anexos(patient_id: str):
    with SessionLocal() as db:
        rows = (
            db.query(Anexo)
            .filter(Anexo.patientId == patient_id)
            .order_by(Anexo.created_at.desc())
            .all()
        )
        return [{**_row(a), "created_at": a.created_at.isoformat() if a.created_at else None} for a in rows]


@app.post("/api/anexos", status_code=201)
async def upload_anexo(
    patientId: str = Form(...),
    categoria: str = Form("documento"),
    file: UploadFile = File(...),
):
    _sb_configurado()
    if categoria not in ANEXO_CATEGORIAS:
        categoria = "outro"
    mime = (file.content_type or "").lower()
    if mime not in ANEXO_MIMES:
        raise HTTPException(422, "Tipo de arquivo não permitido. Use imagens (JPG, PNG, WEBP, GIF) ou PDF.")
    conteudo = await file.read()
    if len(conteudo) == 0:
        raise HTTPException(422, "Arquivo vazio.")
    if len(conteudo) > ANEXO_MAX_BYTES:
        raise HTTPException(422, "Arquivo maior que 15 MB.")
    with SessionLocal() as db:
        if not db.get(Paciente, patientId):
            raise HTTPException(404, "Paciente não encontrado.")
        aid = _new_id()
        nome = _nome_seguro(file.filename or "arquivo")
        path = f"{patientId}/{aid}_{nome}"
        _sb_upload(path, conteudo, mime)
        a = Anexo(id=aid, patientId=patientId, categoria=categoria, nome=file.filename or nome,
                  mimeType=mime, tamanho=len(conteudo), storagePath=path)
        db.add(a)
        db.commit()
        return {**_row(a), "created_at": a.created_at.isoformat()}


@app.get("/api/anexos/{aid}/url")
def anexo_url(aid: str):
    _sb_configurado()
    with SessionLocal() as db:
        a = db.get(Anexo, aid)
        if not a:
            raise HTTPException(404, "Anexo não encontrado.")
        return {"url": _sb_signed_url(a.storagePath), "nome": a.nome, "mimeType": a.mimeType}


@app.delete("/api/anexos/{aid}", status_code=204)
def delete_anexo(aid: str):
    _sb_configurado()
    with SessionLocal() as db:
        a = db.get(Anexo, aid)
        if not a:
            raise HTTPException(404)
        path = a.storagePath
        db.delete(a)
        db.commit()
    _sb_delete(path)


# ══════════════════════════════════════════════════════════════════════════════
# PRONTUÁRIO  (evoluções clínicas IMUTÁVEIS: apenas criação e leitura)
# ══════════════════════════════════════════════════════════════════════════════
class EvolucaoIn(BaseModel):
    patientId: str
    profissionalId: int
    denteRegiao: str = ""
    procedimento: str = ""
    conteudo: str


@app.get("/api/evolucoes")
def list_evolucoes(patient_id: str):
    with SessionLocal() as db:
        rows = (
            db.query(Evolucao)
            .filter(Evolucao.patientId == patient_id)
            .order_by(Evolucao.created_at.desc())
            .all()
        )
        result = []
        for e in rows:
            r = _row(e)
            r["created_at"] = e.created_at.isoformat() if e.created_at else None
            prof = db.get(Profissional, e.profissionalId)
            r["profissionalNome"] = prof.nome if prof else "—"
            r["profissionalCor"] = prof.cor if prof else "#64748b"
            result.append(r)
        return result


@app.post("/api/evolucoes", status_code=201)
def create_evolucao(data: EvolucaoIn):
    if not data.conteudo.strip():
        raise HTTPException(422, "A evolução precisa de conteúdo.")
    with SessionLocal() as db:
        if not db.get(Profissional, data.profissionalId):
            raise HTTPException(422, "Informe o profissional responsável pela evolução.")
        e = Evolucao(
            id=_new_id(),
            patientId=data.patientId,
            profissionalId=data.profissionalId,
            denteRegiao=data.denteRegiao.strip(),
            procedimento=data.procedimento.strip(),
            conteudo=data.conteudo.strip(),
        )
        db.add(e)
        db.commit()
        db.refresh(e)
        r = _row(e)
        r["created_at"] = e.created_at.isoformat()
        prof = db.get(Profissional, e.profissionalId)
        r["profissionalNome"] = prof.nome if prof else "—"
        return r

# NOTA: intencionalmente NÃO existem PUT nem DELETE para evoluções.
# Prontuário é registro legal imutável (CFO). Correções = nova evolução de retificação.


# ══════════════════════════════════════════════════════════════════════════════
# ORÇAMENTOS  (plano de tratamento; itens em CENTAVOS; aprovado -> gera cobranças)
# ══════════════════════════════════════════════════════════════════════════════
class OrcItemIn(BaseModel):
    procedimento: str
    denteRegiao: str = ""
    quantidade: int = 1
    valor: int = 0  # centavos, unitário


class OrcamentoIn(BaseModel):
    patientId: str
    profissionalId: int | None = None
    data: str
    observacoes: str = ""
    itens: list[OrcItemIn] = []


ORC_STATUS_VALIDOS = {"rascunho", "apresentado", "aprovado", "recusado"}


def _orc_row(db, o: Orcamento, com_itens: bool = False) -> dict[str, Any]:
    r = _row(o)
    r.pop("created_at", None)
    pac = db.get(Paciente, o.patientId)
    r["patientName"] = pac.name if pac else "Paciente removido"
    if o.profissionalId:
        prof = db.get(Profissional, o.profissionalId)
        r["profissionalNome"] = prof.nome if prof else ""
    else:
        r["profissionalNome"] = ""
    itens = db.query(OrcamentoItem).filter(OrcamentoItem.orcamentoId == o.id).order_by(OrcamentoItem.id).all()
    r["total"] = sum(i.valor * i.quantidade for i in itens)
    r["numItens"] = len(itens)
    ja_gerou = db.query(Lancamento).filter(Lancamento.numOrcamento == o.id).first() is not None
    r["cobrancasGeradas"] = ja_gerou
    if com_itens:
        r["itens"] = [_row(i) for i in itens]
    return r


@app.get("/api/orcamentos")
def list_orcamentos():
    with SessionLocal() as db:
        rows = db.query(Orcamento).order_by(Orcamento.data.desc()).all()
        return [_orc_row(db, o) for o in rows]


@app.get("/api/orcamentos/{oid}")
def get_orcamento(oid: str):
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o:
            raise HTTPException(404, "Orçamento não encontrado")
        return _orc_row(db, o, com_itens=True)


@app.post("/api/orcamentos", status_code=201)
def create_orcamento(data: OrcamentoIn):
    if not data.itens:
        raise HTTPException(422, "Adicione pelo menos um item ao orçamento.")
    with SessionLocal() as db:
        o = Orcamento(
            id=_new_id(),
            patientId=data.patientId,
            profissionalId=data.profissionalId,
            data=data.data,
            status="rascunho",
            observacoes=data.observacoes,
        )
        db.add(o)
        for it in data.itens:
            db.add(OrcamentoItem(
                orcamentoId=o.id,
                procedimento=it.procedimento.strip(),
                denteRegiao=it.denteRegiao.strip(),
                quantidade=max(1, it.quantidade),
                valor=max(0, it.valor),
            ))
        db.commit()
        return _orc_row(db, o, com_itens=True)


@app.put("/api/orcamentos/{oid}")
def update_orcamento(oid: str, data: OrcamentoIn):
    if not data.itens:
        raise HTTPException(422, "Adicione pelo menos um item ao orçamento.")
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o:
            raise HTTPException(404, "Orçamento não encontrado")
        o.patientId = data.patientId
        o.profissionalId = data.profissionalId
        o.data = data.data
        o.observacoes = data.observacoes
        db.query(OrcamentoItem).filter(OrcamentoItem.orcamentoId == oid).delete()
        for it in data.itens:
            db.add(OrcamentoItem(
                orcamentoId=oid,
                procedimento=it.procedimento.strip(),
                denteRegiao=it.denteRegiao.strip(),
                quantidade=max(1, it.quantidade),
                valor=max(0, it.valor),
            ))
        db.commit()
        return _orc_row(db, o, com_itens=True)


class OrcStatusIn(BaseModel):
    status: str


@app.put("/api/orcamentos/{oid}/status")
def set_orcamento_status(oid: str, data: OrcStatusIn):
    if data.status not in ORC_STATUS_VALIDOS:
        raise HTTPException(422, f"Status inválido. Use: {', '.join(sorted(ORC_STATUS_VALIDOS))}")
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o:
            raise HTTPException(404, "Orçamento não encontrado")
        o.status = data.status
        db.commit()
        return _orc_row(db, o)


class GerarCobrancasIn(BaseModel):
    parcelas: int = 1
    vencimento: str  # 1º vencimento


@app.post("/api/orcamentos/{oid}/gerar-cobrancas", status_code=201)
def gerar_cobrancas(oid: str, data: GerarCobrancasIn):
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o:
            raise HTTPException(404, "Orçamento não encontrado")
        if o.status != "aprovado":
            raise HTTPException(409, "Só é possível gerar cobranças de orçamentos aprovados.")
        if db.query(Lancamento).filter(Lancamento.numOrcamento == oid).first():
            raise HTTPException(409, "Este orçamento já tem cobranças geradas no financeiro.")
        itens = db.query(OrcamentoItem).filter(OrcamentoItem.orcamentoId == oid).all()
        total = sum(i.valor * i.quantidade for i in itens)
        if total <= 0:
            raise HTTPException(422, "O orçamento não tem valor.")
        pac = db.get(Paciente, o.patientId)
        desc_base = f"Orçamento {oid}" + (f" — {pac.name.split(' ')[0]}" if pac else "")
        n = max(1, min(24, data.parcelas))
        base = total // n
        resto = total - base * n
        criados = []
        for i in range(n):
            l = Lancamento(
                id=_new_id(),
                patientId=o.patientId,
                profissionalId=o.profissionalId,
                appointmentId=None,
                descricao=desc_base + (f" ({i+1}/{n})" if n > 1 else ""),
                valor=base + (resto if i == 0 else 0),
                vencimento=_add_meses(data.vencimento, i),
                pagoEm=None,
                formaPagamento="",
                numOrcamento=oid,
                observacoes="",
            )
            db.add(l)
            criados.append(l)
        db.commit()
        return {"ok": True, "criados": len(criados), "total": total}


@app.delete("/api/orcamentos/{oid}", status_code=204)
def delete_orcamento(oid: str):
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o:
            raise HTTPException(404)
        db.query(OrcamentoItem).filter(OrcamentoItem.orcamentoId == oid).delete()
        db.delete(o)
        db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# RETORNOS PREVENTIVOS  (recall clínico: paciente deve voltar N meses após a última visita)
# ══════════════════════════════════════════════════════════════════════════════
class RetornoIn(BaseModel):
    meses: int | None = None  # None = usa o padrão da clínica


@app.get("/api/retorno/{pid}")
def get_retorno(pid: str):
    with SessionLocal() as db:
        cfg = db.get(RetornoConfig, pid)
        return {"meses": cfg.meses if cfg else None}


@app.put("/api/retorno/{pid}")
def set_retorno(pid: str, data: RetornoIn):
    with SessionLocal() as db:
        cfg = db.get(RetornoConfig, pid)
        if not cfg:
            cfg = RetornoConfig(patientId=pid)
            db.add(cfg)
        cfg.meses = data.meses if (data.meses and data.meses > 0) else None
        db.commit()
        return {"meses": cfg.meses}


@app.post("/api/retorno/{pid}/enviado")
def retorno_enviado(pid: str):
    from datetime import date
    with SessionLocal() as db:
        cfg = db.get(RetornoConfig, pid)
        if not cfg:
            cfg = RetornoConfig(patientId=pid)
            db.add(cfg)
        cfg.sentAt = date.today().isoformat()
        db.commit()
        return {"ok": True}


@app.get("/api/retornos")
def list_retornos():
    """Fila de recall: pacientes cujo retorno vence nos próximos 30 dias ou já venceu,
    sem consulta futura marcada."""
    from datetime import date
    with SessionLocal() as db:
        padrao_cfg = db.get(Config, "retornoMesesPadrao")
        padrao = int(padrao_cfg.value) if padrao_cfg and padrao_cfg.value.isdigit() else 6
        hoje = date.today().isoformat()
        janela = (date.today() + timedelta(days=30)).isoformat()
        futuros = {
            a.patientId
            for a in db.query(Consulta).filter(Consulta.date >= hoje, Consulta.status.in_(["agendado", "confirmado"])).all()
        }
        configs = {c.patientId: c for c in db.query(RetornoConfig).all()}
        out = []
        for p in db.query(Paciente).filter(Paciente.lastVisit.isnot(None)).all():
            if p.id in futuros or not p.lastVisit:
                continue
            cfg = configs.get(p.id)
            meses = cfg.meses if (cfg and cfg.meses) else padrao
            due = _add_meses(p.lastVisit, meses)
            if due <= janela:
                out.append({
                    "patientId": p.id, "name": p.name, "phone": p.phone,
                    "lastVisit": p.lastVisit, "meses": meses,
                    "due": due, "atrasado": due < hoje,
                    "sentAt": cfg.sentAt if cfg else None,
                })
        out.sort(key=lambda x: x["due"])
        return out


# ══════════════════════════════════════════════════════════════════════════════
# FINANCEIRO  (lançamentos: cobranças e parcelas; valor em CENTAVOS)
# ══════════════════════════════════════════════════════════════════════════════
class LancamentoIn(BaseModel):
    id: str | None = None
    patientId: str
    profissionalId: int | None = None
    appointmentId: str | None = None
    descricao: str = ""
    valor: int = 0  # centavos
    vencimento: str
    pagoEm: str | None = None
    formaPagamento: str = ""
    numOrcamento: str = ""
    observacoes: str = ""
    parcelas: int = 1  # somente na criação: >1 divide o valor em N lançamentos mensais


def _add_meses(iso: str, n: int) -> str:
    """Soma n meses a uma data ISO, ajustando o dia ao fim do mês quando necessário."""
    y, m, d = (int(x) for x in iso.split("-"))
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    # dias no mês destino
    if m == 12:
        dias_mes = 31
    else:
        from datetime import date as _date
        dias_mes = (_date(y, m + 1, 1) - _date(y, m, 1)).days
    d = min(d, dias_mes)
    return f"{y:04d}-{m:02d}-{d:02d}"


def _lanc_row(db, l: Lancamento) -> dict[str, Any]:
    r = _row(l)
    pac = db.get(Paciente, l.patientId)
    r["patientName"] = pac.name if pac else "Paciente removido"
    if l.profissionalId:
        prof = db.get(Profissional, l.profissionalId)
        r["profissionalNome"] = prof.nome if prof else ""
    else:
        r["profissionalNome"] = ""
    return r


@app.get("/api/financeiro")
def list_lancamentos():
    with SessionLocal() as db:
        rows = db.query(Lancamento).order_by(Lancamento.vencimento.desc()).all()
        return [_lanc_row(db, l) for l in rows]


@app.post("/api/financeiro", status_code=201)
def create_lancamento(data: LancamentoIn):
    if data.valor <= 0:
        raise HTTPException(422, "Informe um valor maior que zero.")
    n = max(1, min(24, data.parcelas))
    with SessionLocal() as db:
        criados = []
        base = data.valor // n
        resto = data.valor - base * n  # primeira parcela absorve o resto da divisão
        for i in range(n):
            valor_i = base + (resto if i == 0 else 0)
            desc = data.descricao
            if n > 1:
                desc = f"{data.descricao} ({i+1}/{n})"
            l = Lancamento(
                id=_new_id(),
                patientId=data.patientId,
                profissionalId=data.profissionalId,
                appointmentId=data.appointmentId,
                descricao=desc,
                valor=valor_i,
                vencimento=_add_meses(data.vencimento, i),
                pagoEm=None,
                formaPagamento="",
                numOrcamento=data.numOrcamento,
                observacoes=data.observacoes,
            )
            db.add(l)
            criados.append(l)
        db.commit()
        return [_lanc_row(db, l) for l in criados]


@app.put("/api/financeiro/{lid}")
def update_lancamento(lid: str, data: LancamentoIn):
    with SessionLocal() as db:
        l = db.get(Lancamento, lid)
        if not l:
            raise HTTPException(404, "Lançamento não encontrado")
        for k, v in data.model_dump(exclude={"id", "parcelas"}).items():
            setattr(l, k, v)
        db.commit()
        db.refresh(l)
        return _lanc_row(db, l)


class ReceberIn(BaseModel):
    formaPagamento: str = "dinheiro"
    pagoEm: str | None = None  # default: hoje


@app.put("/api/financeiro/{lid}/receber")
def receber_lancamento(lid: str, data: ReceberIn):
    from datetime import date
    with SessionLocal() as db:
        l = db.get(Lancamento, lid)
        if not l:
            raise HTTPException(404, "Lançamento não encontrado")
        l.pagoEm = data.pagoEm or date.today().isoformat()
        l.formaPagamento = data.formaPagamento
        db.commit()
        db.refresh(l)
        return _lanc_row(db, l)


@app.put("/api/financeiro/{lid}/estornar")
def estornar_lancamento(lid: str):
    """Desfaz um recebimento marcado por engano (volta para 'em aberto')."""
    with SessionLocal() as db:
        l = db.get(Lancamento, lid)
        if not l:
            raise HTTPException(404, "Lançamento não encontrado")
        l.pagoEm = None
        l.formaPagamento = ""
        db.commit()
        db.refresh(l)
        return _lanc_row(db, l)


@app.delete("/api/financeiro/{lid}", status_code=204)
def delete_lancamento(lid: str):
    with SessionLocal() as db:
        l = db.get(Lancamento, lid)
        if not l:
            raise HTTPException(404)
        db.delete(l)
        db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# DUMP / IMPORT
# ══════════════════════════════════════════════════════════════════════════════
def _dump_data(db) -> dict:
    return {
        "patients": [_row(p) for p in db.query(Paciente).all()],
        "appointments": [_row(a) for a in db.query(Consulta).all()],
        "surveys": [_row(s) for s in db.query(Pesquisa).all()],
        "profissionais": [_row(p) for p in db.query(Profissional).all()],
        "tarefas": [_row(t) for t in db.query(Tarefa).all()],
        "lancamentos": [_row(l) for l in db.query(Lancamento).all()],
        "evolucoes": [{**_row(e), "created_at": e.created_at.isoformat() if e.created_at else None} for e in db.query(Evolucao).all()],
        "orcamentos": [_row(o) for o in db.query(Orcamento).all()],
        "orcamento_itens": [_row(i) for i in db.query(OrcamentoItem).all()],
        "anexos": [{**_row(a), "created_at": a.created_at.isoformat() if a.created_at else None} for a in db.query(Anexo).all()],
        "retorno_config": [_row(r) for r in db.query(RetornoConfig).all()],
        "settings": {c.key: c.value for c in db.query(Config).all()},
    }


@app.get("/api/dump")
def dump():
    with SessionLocal() as db:
        return _dump_data(db)


def _sb_list(prefixo: str) -> list[str]:
    """Lista nomes de arquivos numa pasta do bucket (mais recentes primeiro)."""
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/list/{ANEXO_BUCKET}",
        headers=_sb_headers("application/json"),
        json={"prefix": prefixo, "limit": 1000, "sortBy": {"column": "name", "order": "desc"}},
        timeout=30,
    )
    if r.status_code != 200:
        return []
    return [o["name"] for o in r.json() if o.get("name")]


BACKUP_RETENCAO = 30  # mantém os 30 backups mais recentes


def _fazer_backup() -> str:
    """Gera o dump completo e envia ao Supabase Storage. Retorna o nome do arquivo."""
    with SessionLocal() as db:
        data = _dump_data(db)
    blob = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    nome = f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    _sb_upload(f"_backups/{nome}", blob, "application/json")
    for velho in _sb_list("_backups")[BACKUP_RETENCAO:]:
        _sb_delete(f"_backups/{velho}")
    return nome


def backup_automatico():
    """Job diário do scheduler. Silencioso: nunca derruba a aplicação."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return
    try:
        _fazer_backup()
    except Exception:
        pass


@app.post("/api/backup/agora", status_code=201)
def backup_agora():
    _sb_configurado()
    nome = _fazer_backup()
    return {"ok": True, "arquivo": nome}


@app.get("/api/backups")
def list_backups():
    _sb_configurado()
    return {"arquivos": _sb_list("_backups")[:BACKUP_RETENCAO]}


class DumpIn(BaseModel):
    patients: list[dict] = []
    appointments: list[dict] = []
    surveys: list[dict] = []
    lancamentos: list[dict] = []
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
        for l in data.lancamentos:
            if not db.get(Lancamento, l["id"]):
                db.add(Lancamento(**{k: v for k, v in l.items() if hasattr(Lancamento, k)}))
        for k, v in data.settings.items():
            cfg = db.get(Config, k)
            if cfg:
                cfg.value = str(v)
            else:
                db.add(Config(key=k, value=str(v)))
        db.commit()
    return {"ok": True, "patients": len(data.patients), "appointments": len(data.appointments)}
