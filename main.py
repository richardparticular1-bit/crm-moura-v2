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
from sqlalchemy import event
from sqlalchemy.orm import Session

from database import SessionLocal, init_db
from models import Anexo, Clinica, Config, Consulta, Despesa, Evolucao, Lancamento, MensagemChat, NotificacaoEnviada, OdontogramaMarca, Orcamento, OrcamentoItem, Paciente, Pesquisa, Profissional, PushSubscription, RetornoConfig, Sessao, Tarefa, Usuario

app = FastAPI(title="CRM Moura — Backend v2.8 (multi-tenant: fundação)")

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

# ══════════════════════════════════════════════════════════════════════════════
# MULTI-TENANT — FUNDAÇÃO (Fase 1)
# ══════════════════════════════════════════════════════════════════════════════
# TEMPORÁRIO: enquanto os módulos (pacientes, agenda, financeiro...) ainda não
# filtram explicitamente por clínica (isso é a Fase 2, módulo a módulo), este
# listener carimba clinicaId automaticamente em QUALQUER registro novo que não
# o tenha definido, usando a única clínica existente. Isso garante que nada
# quebra hoje. Conforme cada módulo ganha isolamento de verdade na Fase 2, ele
# passa a definir clinicaId explicitamente e este shim vira um no-op para ele.
DEFAULT_CLINICA_ID: int | None = None


@event.listens_for(Session, "before_flush")
def _auto_carimba_clinica(session, flush_context, instances):
    if DEFAULT_CLINICA_ID is None:
        return
    for obj in session.new:
        if hasattr(obj, "clinicaId") and getattr(obj, "clinicaId", None) is None:
            obj.clinicaId = DEFAULT_CLINICA_ID


def _carregar_clinica_padrao():
    """Roda no startup: descobre a clínica existente para o shim acima."""
    global DEFAULT_CLINICA_ID
    with SessionLocal() as db:
        c = db.query(Clinica).order_by(Clinica.id).first()
        DEFAULT_CLINICA_ID = c.id if c else None


@app.on_event("startup")
def startup():
    init_db()
    _carregar_clinica_padrao()
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
    clinicaNome: str = ""  # nome da clínica; se vazio, usa um padrão


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
    """Cria a PRIMEIRA clínica e o PRIMEIRO usuário (superadmin da plataforma).
    Só funciona enquanto não existe nenhum usuário no sistema."""
    if len(data.senha) < 8:
        raise HTTPException(422, "A senha precisa de pelo menos 8 caracteres.")
    with SessionLocal() as db:
        if db.query(Usuario).count() > 0:
            raise HTTPException(403, "O sistema já foi configurado. Faça login.")
        clinica = Clinica(nome=(data.clinicaNome.strip() or "Minha Clínica"), plano="ativo", ativa=True)
        db.add(clinica)
        db.flush()  # garante clinica.id antes de criar o usuário
        global DEFAULT_CLINICA_ID
        DEFAULT_CLINICA_ID = clinica.id  # ambientes novos: a clínica só passa a existir agora
        u = Usuario(
            clinicaId=clinica.id, nome=data.nome.strip(), email=data.email.strip().lower(),
            senhaHash=_hash_senha(data.senha), ativo=True, isSuperAdmin=True,
        )
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
        return {"id": u.id, "nome": u.nome, "email": u.email, "clinicaId": u.clinicaId, "isSuperAdmin": u.isSuperAdmin}


# ── gestão de usuários (escopada por clínica; qualquer usuário logado, equipe pequena) ──
def _usuario_logado(request: Request) -> Usuario:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    with SessionLocal() as db:
        u = _usuario_do_token(db, token)
        if not u:
            raise HTTPException(401, "Não autenticado.")
        db.expunge(u)
        return u


class UsuarioIn(BaseModel):
    nome: str
    email: str
    senha: str | None = None  # obrigatória ao criar; opcional ao editar (troca)
    ativo: bool = True


@app.get("/api/usuarios")
def list_usuarios(request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        q = db.query(Usuario)
        if not quem.isSuperAdmin:
            q = q.filter(Usuario.clinicaId == quem.clinicaId)
        return [
            {"id": u.id, "nome": u.nome, "email": u.email, "ativo": u.ativo}
            for u in q.order_by(Usuario.nome).all()
        ]


@app.post("/api/usuarios", status_code=201)
def create_usuario(data: UsuarioIn, request: Request):
    quem = _usuario_logado(request)
    if not data.senha or len(data.senha) < 8:
        raise HTTPException(422, "A senha precisa de pelo menos 8 caracteres.")
    with SessionLocal() as db:
        if db.query(Usuario).filter(Usuario.email == data.email.strip().lower()).first():
            raise HTTPException(409, "Já existe um usuário com este e-mail.")
        # clinicaId nunca vem do cliente — sempre herda de quem está criando, evitando
        # que alguém se atribua (ou atribua outra pessoa) a uma clínica que não é a sua.
        u = Usuario(clinicaId=quem.clinicaId, nome=data.nome.strip(), email=data.email.strip().lower(),
                    senhaHash=_hash_senha(data.senha), ativo=data.ativo)
        db.add(u)
        db.commit()
        return {"id": u.id, "nome": u.nome, "email": u.email, "ativo": u.ativo}


@app.put("/api/usuarios/{uid}")
def update_usuario(uid: int, data: UsuarioIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        u = db.get(Usuario, uid)
        if not u or (not quem.isSuperAdmin and u.clinicaId != quem.clinicaId):
            raise HTTPException(404, "Usuário não encontrado")
        if not data.ativo and u.ativo:
            ativos = db.query(Usuario).filter(Usuario.ativo == True, Usuario.clinicaId == u.clinicaId).count()  # noqa: E712
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


def _consulta_conflitante(db, data: "ConsultaIn", clinica_id: int, exclude_id: str | None = None) -> Consulta | None:
    """Verifica se já existe consulta do MESMO profissional, na MESMA data,
    com horário sobreposto, DENTRO DA MESMA CLÍNICA. Retorna a consulta
    conflitante ou None. Consultas canceladas não contam como conflito."""
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
        Consulta.clinicaId == clinica_id,
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
def list_profissionais(request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        return [_row(p) for p in db.query(Profissional).filter(Profissional.clinicaId == quem.clinicaId).order_by(Profissional.nome).all()]


@app.post("/api/profissionais", status_code=201)
def create_profissional(data: ProfissionalIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        p = Profissional(**data.model_dump(), clinicaId=quem.clinicaId)
        db.add(p)
        db.commit()
        db.refresh(p)
        return _row(p)


@app.put("/api/profissionais/{pid}")
def update_profissional(pid: int, data: ProfissionalIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        p = db.get(Profissional, pid)
        if not p or p.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Profissional não encontrado")
        for k, v in data.model_dump().items():
            setattr(p, k, v)
        db.commit()
        db.refresh(p)
        return _row(p)


@app.delete("/api/profissionais/{pid}", status_code=204)
def delete_profissional(pid: int, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        p = db.get(Profissional, pid)
        if not p or p.clinicaId != quem.clinicaId:
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
    rg: str = ""
    orgaoExpedidor: str = ""
    cpf: str = ""
    naturalidade: str = ""
    nacionalidade: str = ""
    estadoCivil: str = ""
    profissao: str = ""
    localTrabalho: str = ""
    enderecoResidencial: str = ""
    indicadoPor: str = ""
    respNome: str = ""
    respRg: str = ""
    respCpf: str = ""
    respTelefone: str = ""
    respEmail: str = ""
    alergias: str = ""
    medicacoes: str = ""
    condicoesSistemicas: str = ""


@app.get("/api/patients")
def list_patients(request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        return [_row(p) for p in db.query(Paciente).filter(Paciente.clinicaId == quem.clinicaId).order_by(Paciente.name).all()]


@app.post("/api/patients", status_code=201)
def create_patient(data: PacienteIn, request: Request):
    quem = _usuario_logado(request)
    pid = data.id or _new_id()
    with SessionLocal() as db:
        p = Paciente(**{k: v for k, v in data.model_dump().items() if k != "id"}, id=pid, clinicaId=quem.clinicaId)
        db.add(p)
        db.commit()
        db.refresh(p)
        return _row(p)


@app.put("/api/patients/{pid}")
def update_patient(pid: str, data: PacienteIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p or p.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado")
        for k, v in data.model_dump(exclude={"id"}).items():
            setattr(p, k, v)
        db.commit()
        db.refresh(p)
        return _row(p)


@app.delete("/api/patients/{pid}", status_code=204)
def delete_patient(pid: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p or p.clinicaId != quem.clinicaId:
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
def search_appointments(q: str, request: Request, limit: int = 50):
    """Busca agendamentos pelo nome do paciente (parcial, sem case).
    Retorna ordenado por data/hora, com nome do paciente e do profissional."""
    quem = _usuario_logado(request)
    termo = (q or "").strip()
    if len(termo) < 2:
        return []
    with SessionLocal() as db:
        pacientes = (
            db.query(Paciente)
            .filter(Paciente.name.ilike(f"%{termo}%"), Paciente.clinicaId == quem.clinicaId)
            .all()
        )
        if not pacientes:
            return []
        por_id = {p.id: p for p in pacientes}
        rows = (
            db.query(Consulta)
            .filter(Consulta.patientId.in_(list(por_id.keys())), Consulta.clinicaId == quem.clinicaId)
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
def list_appointments(request: Request, date: str | None = None):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        q = db.query(Consulta).filter(Consulta.clinicaId == quem.clinicaId)
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
def create_appointment(data: ConsultaIn, request: Request):
    quem = _usuario_logado(request)
    aid = data.id or _new_id()
    with SessionLocal() as db:
        pac = db.get(Paciente, data.patientId)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado")
        conflito = _consulta_conflitante(db, data, quem.clinicaId)
        if conflito:
            pac2 = db.get(Paciente, conflito.patientId)
            nome = pac2.name if pac2 else "outro paciente"
            raise HTTPException(
                409,
                f"Conflito de horário: {nome} já está agendado às {conflito.time} "
                f"({conflito.duracaoMinutos or 60} min) com este profissional.",
            )
        a = Consulta(**{k: v for k, v in data.model_dump().items() if k != "id"}, id=aid, clinicaId=quem.clinicaId)
        db.add(a)
        db.commit()
        db.refresh(a)
        return _row(a)


@app.put("/api/appointments/{aid}")
def update_appointment(aid: str, data: ConsultaIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        a = db.get(Consulta, aid)
        if not a or a.clinicaId != quem.clinicaId:
            raise HTTPException(404)
        conflito = _consulta_conflitante(db, data, quem.clinicaId, exclude_id=aid)
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
def delete_appointment(aid: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        a = db.get(Consulta, aid)
        if not a or a.clinicaId != quem.clinicaId:
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
def mark_birthday_sent(pid: str, request: Request):
    """Marca que o parabéns foi enviado neste ano."""
    quem = _usuario_logado(request)
    from datetime import date
    ano = str(date.today().year)
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p or p.clinicaId != quem.clinicaId:
            raise HTTPException(404)
        p.birthdaySentYear = ano
        db.commit()
        return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURAÇÕES
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/settings")
def get_settings(request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        return {c.key: c.value for c in db.query(Config).filter(Config.clinicaId == quem.clinicaId).all()}


@app.put("/api/settings")
def save_settings(data: dict[str, str], request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        for k, v in data.items():
            cfg = db.get(Config, (quem.clinicaId, k))
            if cfg:
                cfg.value = v
            else:
                db.add(Config(clinicaId=quem.clinicaId, key=k, value=v))
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
        ativas = db.get(Config, (DEFAULT_CLINICA_ID, "notifAtivas"))
        if ativas and ativas.value == "false":
            return
        lembrete_cfg = db.get(Config, (DEFAULT_CLINICA_ID, "notifLembreteMinutos"))
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
def list_anexos(patient_id: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        rows = (
            db.query(Anexo)
            .filter(Anexo.patientId == patient_id, Anexo.clinicaId == quem.clinicaId)
            .order_by(Anexo.created_at.desc())
            .all()
        )
        return [{**_row(a), "created_at": a.created_at.isoformat() if a.created_at else None} for a in rows]


@app.post("/api/anexos", status_code=201)
async def upload_anexo(
    request: Request,
    patientId: str = Form(...),
    categoria: str = Form("documento"),
    file: UploadFile = File(...),
):
    quem = _usuario_logado(request)
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
        pac = db.get(Paciente, patientId)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        aid = _new_id()
        nome = _nome_seguro(file.filename or "arquivo")
        path = f"{patientId}/{aid}_{nome}"
        _sb_upload(path, conteudo, mime)
        a = Anexo(id=aid, clinicaId=quem.clinicaId, patientId=patientId, categoria=categoria, nome=file.filename or nome,
                  mimeType=mime, tamanho=len(conteudo), storagePath=path)
        db.add(a)
        db.commit()
        return {**_row(a), "created_at": a.created_at.isoformat()}


@app.get("/api/anexos/{aid}/url")
def anexo_url(aid: str, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        a = db.get(Anexo, aid)
        if not a or a.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Anexo não encontrado.")
        return {"url": _sb_signed_url(a.storagePath), "nome": a.nome, "mimeType": a.mimeType}


@app.delete("/api/anexos/{aid}", status_code=204)
def delete_anexo(aid: str, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        a = db.get(Anexo, aid)
        if not a or a.clinicaId != quem.clinicaId:
            raise HTTPException(404)
        path = a.storagePath
        db.delete(a)
        db.commit()
    _sb_delete(path)


# ══════════════════════════════════════════════════════════════════════════════
# GASTOS DO CONSULTÓRIO  (despesas com comprovante opcional no Storage)
# ══════════════════════════════════════════════════════════════════════════════
DESPESA_CATEGORIAS = {"aluguel", "material", "laboratorio", "salario", "marketing", "equipamento", "outro"}


@app.get("/api/despesas")
def list_despesas(request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        rows = db.query(Despesa).filter(Despesa.clinicaId == quem.clinicaId).order_by(Despesa.data.desc()).all()
        return [{**_row(d), "created_at": d.created_at.isoformat() if d.created_at else None} for d in rows]


@app.post("/api/despesas", status_code=201)
async def create_despesa(
    request: Request,
    categoria: str = Form("outro"),
    descricao: str = Form(""),
    valor: int = Form(...),
    data: str = Form(...),
    comprovante: UploadFile | None = File(None),
):
    quem = _usuario_logado(request)
    if categoria not in DESPESA_CATEGORIAS:
        categoria = "outro"
    if valor <= 0:
        raise HTTPException(422, "Informe um valor maior que zero.")
    did = _new_id()
    comp_nome = comp_mime = comp_path = None
    if comprovante is not None and comprovante.filename:
        _sb_configurado()
        mime = (comprovante.content_type or "").lower()
        if mime not in ANEXO_MIMES:
            raise HTTPException(422, "Comprovante: use imagem (JPG, PNG, WEBP, GIF) ou PDF.")
        conteudo = await comprovante.read()
        if len(conteudo) > ANEXO_MAX_BYTES:
            raise HTTPException(422, "Comprovante maior que 15 MB.")
        comp_nome = comprovante.filename
        comp_mime = mime
        comp_path = f"_despesas/{did}_{_nome_seguro(comprovante.filename)}"
        _sb_upload(comp_path, conteudo, mime)
    with SessionLocal() as db:
        d = Despesa(id=did, clinicaId=quem.clinicaId, categoria=categoria, descricao=descricao.strip(), valor=valor, data=data,
                    comprovanteNome=comp_nome, comprovanteMime=comp_mime, comprovantePath=comp_path)
        db.add(d)
        db.commit()
        return {**_row(d), "created_at": d.created_at.isoformat()}


@app.get("/api/despesas/{did}/comprovante-url")
def despesa_comprovante_url(did: str, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        d = db.get(Despesa, did)
        if not d or d.clinicaId != quem.clinicaId or not d.comprovantePath:
            raise HTTPException(404, "Sem comprovante.")
        return {"url": _sb_signed_url(d.comprovantePath), "nome": d.comprovanteNome}


@app.delete("/api/despesas/{did}", status_code=204)
def delete_despesa(did: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        d = db.get(Despesa, did)
        if not d or d.clinicaId != quem.clinicaId:
            raise HTTPException(404)
        path = d.comprovantePath
        db.delete(d)
        db.commit()
    if path:
        _sb_delete(path)


# ══════════════════════════════════════════════════════════════════════════════
# FOTO DO PACIENTE  (Supabase Storage; substitui as iniciais na ficha)
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/api/patients/{pid}/foto", status_code=201)
async def upload_foto_paciente(pid: str, request: Request, file: UploadFile = File(...)):
    quem = _usuario_logado(request)
    _sb_configurado()
    mime = (file.content_type or "").lower()
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(422, "Use uma imagem JPG, PNG ou WEBP.")
    conteudo = await file.read()
    if len(conteudo) > ANEXO_MAX_BYTES:
        raise HTTPException(422, "Imagem maior que 15 MB.")
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p or p.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
        path = f"_fotos/{pid}.{ext}"
        _sb_upload(path, conteudo, mime)
        p.fotoPath = path
        db.commit()
        return {"ok": True}


@app.get("/api/patients/{pid}/foto/url")
def foto_paciente_url(pid: str, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p or p.clinicaId != quem.clinicaId or not p.fotoPath:
            raise HTTPException(404, "Sem foto.")
        return {"url": _sb_signed_url(p.fotoPath, segundos=600)}


@app.delete("/api/patients/{pid}/foto", status_code=204)
def delete_foto_paciente(pid: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p or p.clinicaId != quem.clinicaId:
            raise HTTPException(404)
        path = p.fotoPath
        p.fotoPath = None
        db.commit()
    if path:
        _sb_delete(path)


# ══════════════════════════════════════════════════════════════════════════════
# ODONTOGRAMA  (marcações IMUTÁVEIS por dente/face; estado atual = última por chave)
# ══════════════════════════════════════════════════════════════════════════════
DENTES_VALIDOS = {str(d) for d in list(range(11,19))+list(range(21,29))+list(range(31,39))+list(range(41,49))
                  + list(range(51,56))+list(range(61,66))+list(range(71,76))+list(range(81,86))}
FACES_VALIDAS = {"oclusal", "vestibular", "lingual", "mesial", "distal", "dente"}
STATUS_FACE = {"higido", "cariado", "restaurado", "fraturado"}
STATUS_DENTE = {"ausente", "a_extrair", "implante", "coroa", "canal", "protese", "nenhum"}


class OdontogramaMarcaIn(BaseModel):
    patientId: str
    profissionalId: int
    dente: str
    face: str
    status: str
    observacao: str = ""


@app.post("/api/odontograma", status_code=201)
def create_marca_odontograma(data: OdontogramaMarcaIn, request: Request):
    quem = _usuario_logado(request)
    if data.dente not in DENTES_VALIDOS:
        raise HTTPException(422, "Número de dente inválido.")
    if data.face not in FACES_VALIDAS:
        raise HTTPException(422, "Face inválida.")
    validos = STATUS_DENTE if data.face == "dente" else STATUS_FACE
    if data.status not in validos:
        raise HTTPException(422, f"Status inválido para esta face. Use: {', '.join(sorted(validos))}")
    with SessionLocal() as db:
        pac = db.get(Paciente, data.patientId)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        if not db.get(Profissional, data.profissionalId):
            raise HTTPException(422, "Informe o profissional responsável.")
        m = OdontogramaMarca(
            id=_new_id(), clinicaId=quem.clinicaId, patientId=data.patientId, profissionalId=data.profissionalId,
            dente=data.dente, face=data.face, status=data.status, observacao=data.observacao.strip(),
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        return {"id": m.id, "created_at": m.created_at.isoformat()}


@app.get("/api/odontograma")
def get_odontograma(patient_id: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        rows = (
            db.query(OdontogramaMarca)
            .filter(OdontogramaMarca.patientId == patient_id, OdontogramaMarca.clinicaId == quem.clinicaId)
            .order_by(OdontogramaMarca.created_at.asc())
            .all()
        )
        # estado atual: última marcação vence, por (dente, face)
        latest: dict[tuple, OdontogramaMarca] = {}
        for m in rows:
            latest[(m.dente, m.face)] = m
        estado: dict[str, dict] = {}
        for (dente, face), m in latest.items():
            if m.status in ("higido", "nenhum"):
                continue
            estado.setdefault(dente, {})
            if face == "dente":
                estado[dente]["dente"] = m.status
            else:
                estado[dente].setdefault("faces", {})[face] = m.status

        profs = {p.id: p for p in db.query(Profissional).all()}
        historico = []
        for m in reversed(rows[-200:]):  # últimas 200, mais recente primeiro
            prof = profs.get(m.profissionalId)
            historico.append({
                "id": m.id, "dente": m.dente, "face": m.face, "status": m.status,
                "observacao": m.observacao, "profissionalNome": prof.nome if prof else "—",
                "created_at": m.created_at.isoformat(),
            })
        return {"estado": estado, "historico": historico}


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
def list_evolucoes(patient_id: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        rows = (
            db.query(Evolucao)
            .filter(Evolucao.patientId == patient_id, Evolucao.clinicaId == quem.clinicaId)
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
def create_evolucao(data: EvolucaoIn, request: Request):
    quem = _usuario_logado(request)
    if not data.conteudo.strip():
        raise HTTPException(422, "A evolução precisa de conteúdo.")
    with SessionLocal() as db:
        pac = db.get(Paciente, data.patientId)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        if not db.get(Profissional, data.profissionalId):
            raise HTTPException(422, "Informe o profissional responsável pela evolução.")
        e = Evolucao(
            id=_new_id(),
            clinicaId=quem.clinicaId,
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
def list_orcamentos(request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        rows = db.query(Orcamento).filter(Orcamento.clinicaId == quem.clinicaId).order_by(Orcamento.data.desc()).all()
        if not rows:
            return []
        ids = [o.id for o in rows]
        patient_ids = {o.patientId for o in rows}
        pacientes = {p.id: p for p in db.query(Paciente).filter(Paciente.id.in_(patient_ids)).all()}
        profissionais = {p.id: p for p in db.query(Profissional).all()}
        itens_por_orc: dict[str, list] = {}
        for it in db.query(OrcamentoItem).filter(OrcamentoItem.orcamentoId.in_(ids)).all():
            itens_por_orc.setdefault(it.orcamentoId, []).append(it)
        com_cobranca = {
            row[0] for row in db.query(Lancamento.numOrcamento)
            .filter(Lancamento.numOrcamento.in_(ids), Lancamento.clinicaId == quem.clinicaId).distinct().all()
        }
        result = []
        for o in rows:
            r = _row(o)
            r.pop("created_at", None)
            pac = pacientes.get(o.patientId)
            r["patientName"] = pac.name if pac else "Paciente removido"
            prof = profissionais.get(o.profissionalId) if o.profissionalId else None
            r["profissionalNome"] = prof.nome if prof else ""
            itens = itens_por_orc.get(o.id, [])
            r["total"] = sum(i.valor * i.quantidade for i in itens)
            r["numItens"] = len(itens)
            r["cobrancasGeradas"] = o.id in com_cobranca
            result.append(r)
        return result


@app.get("/api/orcamentos/{oid}")
def get_orcamento(oid: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o or o.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Orçamento não encontrado")
        return _orc_row(db, o, com_itens=True)


@app.post("/api/orcamentos", status_code=201)
def create_orcamento(data: OrcamentoIn, request: Request):
    quem = _usuario_logado(request)
    if not data.itens:
        raise HTTPException(422, "Adicione pelo menos um item ao orçamento.")
    with SessionLocal() as db:
        pac = db.get(Paciente, data.patientId)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado")
        o = Orcamento(
            id=_new_id(),
            clinicaId=quem.clinicaId,
            patientId=data.patientId,
            profissionalId=data.profissionalId,
            data=data.data,
            status="rascunho",
            observacoes=data.observacoes,
        )
        db.add(o)
        for it in data.itens:
            db.add(OrcamentoItem(
                clinicaId=quem.clinicaId,
                orcamentoId=o.id,
                procedimento=it.procedimento.strip(),
                denteRegiao=it.denteRegiao.strip(),
                quantidade=max(1, it.quantidade),
                valor=max(0, it.valor),
            ))
        db.commit()
        return _orc_row(db, o, com_itens=True)


@app.put("/api/orcamentos/{oid}")
def update_orcamento(oid: str, data: OrcamentoIn, request: Request):
    quem = _usuario_logado(request)
    if not data.itens:
        raise HTTPException(422, "Adicione pelo menos um item ao orçamento.")
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o or o.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Orçamento não encontrado")
        pac = db.get(Paciente, data.patientId)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado")
        o.patientId = data.patientId
        o.profissionalId = data.profissionalId
        o.data = data.data
        o.observacoes = data.observacoes
        db.query(OrcamentoItem).filter(OrcamentoItem.orcamentoId == oid).delete()
        for it in data.itens:
            db.add(OrcamentoItem(
                clinicaId=quem.clinicaId,
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
def set_orcamento_status(oid: str, data: OrcStatusIn, request: Request):
    quem = _usuario_logado(request)
    if data.status not in ORC_STATUS_VALIDOS:
        raise HTTPException(422, f"Status inválido. Use: {', '.join(sorted(ORC_STATUS_VALIDOS))}")
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o or o.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Orçamento não encontrado")
        o.status = data.status
        db.commit()
        return _orc_row(db, o)


class GerarCobrancasIn(BaseModel):
    parcelas: int = 1
    vencimento: str  # 1º vencimento


@app.post("/api/orcamentos/{oid}/gerar-cobrancas", status_code=201)
def gerar_cobrancas(oid: str, data: GerarCobrancasIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o or o.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Orçamento não encontrado")
        if o.status != "aprovado":
            raise HTTPException(409, "Só é possível gerar cobranças de orçamentos aprovados.")
        if db.query(Lancamento).filter(Lancamento.numOrcamento == oid, Lancamento.clinicaId == quem.clinicaId).first():
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
                clinicaId=quem.clinicaId,
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
def delete_orcamento(oid: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o or o.clinicaId != quem.clinicaId:
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
def get_retorno(pid: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        pac = db.get(Paciente, pid)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado")
        cfg = db.get(RetornoConfig, pid)
        return {"meses": cfg.meses if cfg else None}


@app.put("/api/retorno/{pid}")
def set_retorno(pid: str, data: RetornoIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        pac = db.get(Paciente, pid)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado")
        cfg = db.get(RetornoConfig, pid)
        if not cfg:
            cfg = RetornoConfig(patientId=pid, clinicaId=quem.clinicaId)
            db.add(cfg)
        cfg.meses = data.meses if (data.meses and data.meses > 0) else None
        db.commit()
        return {"meses": cfg.meses}


@app.post("/api/retorno/{pid}/enviado")
def retorno_enviado(pid: str, request: Request):
    quem = _usuario_logado(request)
    from datetime import date
    with SessionLocal() as db:
        pac = db.get(Paciente, pid)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado")
        cfg = db.get(RetornoConfig, pid)
        if not cfg:
            cfg = RetornoConfig(patientId=pid, clinicaId=quem.clinicaId)
            db.add(cfg)
        cfg.sentAt = date.today().isoformat()
        db.commit()
        return {"ok": True}


@app.get("/api/retornos")
def list_retornos(request: Request):
    """Fila de recall: pacientes cujo retorno vence nos próximos 30 dias ou já venceu,
    sem consulta futura marcada. Escopado à clínica do usuário logado."""
    quem = _usuario_logado(request)
    from datetime import date
    with SessionLocal() as db:
        padrao_cfg = db.get(Config, (quem.clinicaId, "retornoMesesPadrao"))
        padrao = int(padrao_cfg.value) if padrao_cfg and padrao_cfg.value.isdigit() else 6
        hoje = date.today().isoformat()
        janela = (date.today() + timedelta(days=30)).isoformat()
        futuros = {
            a.patientId
            for a in db.query(Consulta).filter(
                Consulta.date >= hoje, Consulta.status.in_(["agendado", "confirmado"]),
                Consulta.clinicaId == quem.clinicaId,
            ).all()
        }
        configs = {
            c.patientId: c for c in db.query(RetornoConfig).filter(RetornoConfig.clinicaId == quem.clinicaId).all()
        }
        out = []
        for p in db.query(Paciente).filter(Paciente.lastVisit.isnot(None), Paciente.clinicaId == quem.clinicaId).all():
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
def list_lancamentos(request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        rows = db.query(Lancamento).filter(Lancamento.clinicaId == quem.clinicaId).order_by(Lancamento.vencimento.desc()).all()
        return [_lanc_row(db, l) for l in rows]


@app.post("/api/financeiro", status_code=201)
def create_lancamento(data: LancamentoIn, request: Request):
    quem = _usuario_logado(request)
    if data.valor <= 0:
        raise HTTPException(422, "Informe um valor maior que zero.")
    n = max(1, min(24, data.parcelas))
    with SessionLocal() as db:
        pac = db.get(Paciente, data.patientId)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado")
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
                clinicaId=quem.clinicaId,
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
def update_lancamento(lid: str, data: LancamentoIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        l = db.get(Lancamento, lid)
        if not l or l.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Lançamento não encontrado")
        for k, v in data.model_dump(exclude={"id", "parcelas"}).items():
            setattr(l, k, v)
        db.commit()
        db.refresh(l)
        return _lanc_row(db, l)


class ReceberIn(BaseModel):
    formaPagamento: str = "dinheiro"
    pagoEm: str | None = None  # default: hoje
    valorRecebido: int | None = None  # centavos; None = valor cheio da parcela
    restanteVencimento: str | None = None  # obrigatório se valorRecebido < valor da parcela


@app.put("/api/financeiro/{lid}/receber")
def receber_lancamento(lid: str, data: ReceberIn, request: Request):
    quem = _usuario_logado(request)
    from datetime import date
    with SessionLocal() as db:
        l = db.get(Lancamento, lid)
        if not l or l.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Lançamento não encontrado")
        if l.pagoEm:
            raise HTTPException(409, "Este lançamento já está pago.")
        recebido = data.valorRecebido if (data.valorRecebido and data.valorRecebido > 0) else l.valor
        if recebido > l.valor:
            raise HTTPException(422, "O valor recebido não pode ser maior que a parcela.")
        restante_id = None
        if recebido < l.valor:
            if not data.restanteVencimento:
                raise HTTPException(422, "Informe o novo vencimento do valor restante.")
            restante = Lancamento(
                id=_new_id(), clinicaId=quem.clinicaId, patientId=l.patientId, profissionalId=l.profissionalId,
                appointmentId=l.appointmentId,
                descricao=(l.descricao or "Cobrança") + " (restante)",
                valor=l.valor - recebido, vencimento=data.restanteVencimento,
                pagoEm=None, formaPagamento="", numOrcamento=l.numOrcamento, observacoes=l.observacoes,
            )
            db.add(restante)
            restante_id = restante.id
        l.valor = recebido
        l.pagoEm = data.pagoEm or date.today().isoformat()
        l.formaPagamento = data.formaPagamento
        db.commit()
        db.refresh(l)
        r = _lanc_row(db, l)
        r["restanteId"] = restante_id
        return r


@app.put("/api/financeiro/{lid}/estornar")
def estornar_lancamento(lid: str, request: Request):
    """Desfaz um recebimento marcado por engano (volta para 'em aberto')."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        l = db.get(Lancamento, lid)
        if not l or l.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Lançamento não encontrado")
        l.pagoEm = None
        l.formaPagamento = ""
        db.commit()
        db.refresh(l)
        return _lanc_row(db, l)


@app.delete("/api/financeiro/{lid}", status_code=204)
def delete_lancamento(lid: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        l = db.get(Lancamento, lid)
        if not l or l.clinicaId != quem.clinicaId:
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
        "despesas": [{**_row(d), "created_at": d.created_at.isoformat() if d.created_at else None} for d in db.query(Despesa).all()],
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
            cfg = db.get(Config, (DEFAULT_CLINICA_ID, k))
            if cfg:
                cfg.value = str(v)
            else:
                db.add(Config(clinicaId=DEFAULT_CLINICA_ID, key=k, value=str(v)))
        db.commit()
    return {"ok": True, "patients": len(data.patients), "appointments": len(data.appointments)}
