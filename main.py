"""Backend FastAPI do CRM — versão 2.1
Novidades v2.1:
  - GET /api/appointments/search?q=  → busca agendamentos pelo nome do paciente
  - Validação de conflito de horário ao criar/editar consulta (409 Conflict)
"""
from __future__ import annotations

import io
import json
import base64
import os
import random
import secrets
import time
import unicodedata
from binascii import crc_hqx
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import bcrypt
import qrcode
import qrcode.image.svg
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from pywebpush import webpush, WebPushException

from database import SessionLocal, init_db
from models import AnamneseModelo, AnamnesePergunta, AnamneseRemota, Anexo, AssinaturaClinica, Clinica, Config, ConsentimentoMidia, ConsentimentoOrcamento, Consulta, Despesa, DocumentoEmitido, Evolucao, Lancamento, MensagemChat, NotificacaoEnviada, OdontogramaMarca, Orcamento, OrcamentoItem, Paciente, Pesquisa, Plataforma, PlanoSaaS, Profissional, PushSubscription, RetornoConfig, Sessao, Tarefa, Usuario

app = FastAPI(title="CRM Moura — Backend v2.9 (multi-tenant: fundação)")

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

# Envio de e-mail transacional (Resend). Usado só pela Fase 4 (cadastro público
# de clínica): confirmação de e-mail antes do primeiro acesso.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
APP_URL = os.environ.get("APP_URL", "https://crm-moura.onrender.com")

# Cobrança recorrente da PLATAFORMA (assinatura de cada clínica), via Mercado
# Pago Preapproval — só o Access Token da SUA conta Mercado Pago (a que recebe
# o dinheiro das clínicas). Sem isso configurado, os endpoints de assinatura
# continuam existindo mas devolvem erro claro em vez de derrubar o app.
MERCADOPAGO_ACCESS_TOKEN = os.environ.get("MERCADOPAGO_ACCESS_TOKEN", "")


def _mp_configurado():
    if not MERCADOPAGO_ACCESS_TOKEN:
        raise HTTPException(503, "Mercado Pago não configurado neste servidor: defina MERCADOPAGO_ACCESS_TOKEN no Render.")


def _email_configurado():
    if not RESEND_API_KEY:
        raise HTTPException(503, "Envio de e-mail não configurado neste servidor.")


def _enviar_email(destinatario: str, assunto: str, html: str) -> bool:
    if not RESEND_API_KEY:
        return False
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": RESEND_FROM, "to": [destinatario], "subject": assunto, "html": html},
            timeout=15,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False

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
AUTH_LIVRE = ("/api/auth/login", "/api/auth/setup", "/api/auth/status",
              "/api/signup", "/api/verify-email", "/api/resend-verification",
              "/api/plataforma", "/api/portal", "/api/anamnese-publica",
              "/api/webhooks/mercadopago")


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
        # Clínica da plataforma (a do superadmin) não paga assinatura — fica
        # registrada como "ativa" só pra aparecer coerente na tela de Clínicas.
        db.add(AssinaturaClinica(clinicaId=clinica.id, status="ativa"))
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
        if not u.emailVerificado:
            raise HTTPException(403, "Confirme seu e-mail antes de entrar. Verifique sua caixa de entrada.")
        LOGIN_FALHAS.pop(email, None)
        token = _criar_sessao(db, u)
        return {"token": token, "nome": u.nome}


class SignupIn(BaseModel):
    clinicaNome: str
    nome: str
    email: str
    senha: str
    # ── dados legais do consultório — opcionais aqui; quem não preencher agora
    # completa depois em Ajustes > Perfil do Consultório ──
    responsavelTecnico: str = ""
    croResponsavel: str = ""
    cnpj: str = ""
    enderecoCompleto: str = ""
    telefoneWhatsapp: str = ""
    emailClinica: str = ""


def _email_confirmacao_html(nome: str, link: str) -> str:
    return f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto">
      <h2 style="color:#0f766e">Bem-vindo(a), {nome}! 🦷</h2>
      <p>Falta um passo para começar a usar o sistema: confirme seu e-mail clicando no botão abaixo.</p>
      <p style="text-align:center;margin:28px 0">
        <a href="{link}" style="background:#0f766e;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600">Confirmar e-mail</a>
      </p>
      <p style="font-size:13px;color:#666">Se o botão não funcionar, copie e cole este link no navegador:<br>{link}</p>
    </div>"""


@app.post("/api/signup", status_code=201)
def signup(data: SignupIn):
    _email_configurado()
    if len(data.senha) < 8:
        raise HTTPException(422, "A senha precisa de pelo menos 8 caracteres.")
    if not data.clinicaNome.strip():
        raise HTTPException(422, "Informe o nome da clínica.")
    email = data.email.strip().lower()
    with SessionLocal() as db:
        if db.query(Usuario).filter(Usuario.email == email).first():
            raise HTTPException(409, "Já existe uma conta com este e-mail.")
        clinica = Clinica(
            nome=data.clinicaNome.strip(), plano="trial", ativa=True,
            responsavelTecnico=data.responsavelTecnico.strip(), croResponsavel=data.croResponsavel.strip(),
            cnpj=data.cnpj.strip(), enderecoCompleto=data.enderecoCompleto.strip(),
            telefoneWhatsapp=data.telefoneWhatsapp.strip(), email=data.emailClinica.strip(),
        )
        db.add(clinica)
        db.flush()
        db.add(AssinaturaClinica(clinicaId=clinica.id, status="trial"))
        token = secrets.token_urlsafe(32)
        u = Usuario(
            clinicaId=clinica.id, nome=data.nome.strip(), email=email,
            senhaHash=_hash_senha(data.senha), ativo=True, isSuperAdmin=False,
            emailVerificado=False, emailVerifyToken=token,
        )
        db.add(u)
        db.commit()
        link = f"{APP_URL}/?verify={token}"
        enviado = _enviar_email(email, "Confirme seu e-mail — CRM", _email_confirmacao_html(u.nome, link))
        return {"ok": True, "emailEnviado": enviado}


@app.get("/api/verify-email")
def verify_email(token: str):
    with SessionLocal() as db:
        u = db.query(Usuario).filter(Usuario.emailVerifyToken == token).first()
        if not u:
            raise HTTPException(404, "Link de confirmação inválido ou já utilizado.")
        u.emailVerificado = True
        u.emailVerifyToken = None
        db.commit()
        sessao_token = _criar_sessao(db, u)
        return {"token": sessao_token, "nome": u.nome}


class ResendVerificationIn(BaseModel):
    email: str


@app.post("/api/resend-verification")
def resend_verification(data: ResendVerificationIn):
    _email_configurado()
    email = data.email.strip().lower()
    with SessionLocal() as db:
        u = db.query(Usuario).filter(Usuario.email == email).first()
        # Resposta genérica sempre, para não confirmar quais e-mails existem no sistema.
        if not u or u.emailVerificado:
            return {"ok": True}
        token = secrets.token_urlsafe(32)
        u.emailVerifyToken = token
        db.commit()
        link = f"{APP_URL}/?verify={token}"
        _enviar_email(email, "Confirme seu e-mail — CRM", _email_confirmacao_html(u.nome, link))
        return {"ok": True}


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
        if not u.isSuperAdmin:
            # Único jeito de bloquear uma clínica é o superadmin marcar
            # manualmente (tela de Clínicas) — atraso de pagamento sozinho
            # nunca bloqueia, só gera aviso. Superadmin nunca é bloqueado
            # por isso, pra nunca ficar trancado pra fora e poder desbloquear.
            assinatura = db.get(AssinaturaClinica, u.clinicaId)
            if assinatura and assinatura.bloqueadaManualmente:
                raise HTTPException(403, "Acesso suspenso pela plataforma. Entre em contato com o suporte.")
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
        # Gerenciar equipe é sempre da PRÓPRIA clínica de quem está logado —
        # inclusive pro superadmin da plataforma, que enxerga todas as clínicas
        # na tela dedicada de Clínicas, não aqui misturado com sua própria equipe.
        q = db.query(Usuario).filter(Usuario.clinicaId == quem.clinicaId)
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
        if not u or u.clinicaId != quem.clinicaId:
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


# ══════════════════════════════════════════════════════════════════════════════
# MARCA DA PLATAFORMA  (neutra, exibida no login ANTES da autenticação)
# ══════════════════════════════════════════════════════════════════════════════
# GET é público (está em AUTH_LIVRE) — a tela de login precisa saber nome/logo
# antes de qualquer clínica ser identificada. PUT e upload de logo exigem
# login E isSuperAdmin, verificado manualmente dentro de cada handler (o
# middleware não distingue métodos, só prefixo de caminho).
class PlataformaIn(BaseModel):
    nome: str


def _exige_superadmin(request: Request) -> Usuario:
    quem = _usuario_logado(request)
    if not quem.isSuperAdmin:
        raise HTTPException(403, "Só um administrador da plataforma pode alterar isso.")
    return quem


@app.get("/api/clinicas")
def list_clinicas(request: Request):
    """Visão de plataforma pro superadmin: todas as clínicas cadastradas,
    com contagem de usuários/pacientes e o status da assinatura de cada uma.
    Não usa _row() porque Clinica tem campos sensíveis (chavePix etc.) que
    não devem sair aqui."""
    _exige_superadmin(request)
    with SessionLocal() as db:
        rows = db.query(Clinica).order_by(Clinica.created_at.desc()).all()
        result = []
        for c in rows:
            num_usuarios = db.query(Usuario).filter(Usuario.clinicaId == c.id).count()
            num_pacientes = db.query(Paciente).filter(Paciente.clinicaId == c.id).count()
            assinatura = db.get(AssinaturaClinica, c.id)
            plano_saas = db.get(PlanoSaaS, assinatura.planoId) if (assinatura and assinatura.planoId) else None
            result.append({
                "id": c.id, "nome": c.nome, "plano": c.plano, "ativa": c.ativa,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "numUsuarios": num_usuarios, "numPacientes": num_pacientes,
                "assinaturaStatus": assinatura.status if assinatura else "trial",
                "planoSaasId": assinatura.planoId if assinatura else None,
                "planoSaasNome": plano_saas.nome if plano_saas else None,
                "proximoVencimento": assinatura.proximoVencimento if assinatura else None,
                "bloqueadaManualmente": assinatura.bloqueadaManualmente if assinatura else False,
            })
        return result


# ══════════════════════════════════════════════════════════════════════════════
# PLANOS SAAS E ASSINATURA DA PLATAFORMA  (o que cada clínica paga pra usar o
# sistema — cobrança recorrente via Mercado Pago; só o superadmin mexe aqui)
# ══════════════════════════════════════════════════════════════════════════════
class PlanoSaaSIn(BaseModel):
    nome: str
    valor: int  # centavos, mensal
    limitePacientes: int | None = None
    limiteProfissionais: int | None = None
    ativo: bool = True


@app.get("/api/planos-saas")
def list_planos_saas(request: Request):
    """Lista de planos — pública pra qualquer usuário logado, não só
    superadmin, porque a própria clínica também precisa ver o nome/limite do
    plano em que está (ex: no aviso de assinatura atrasada ou de limite)."""
    _usuario_logado(request)
    with SessionLocal() as db:
        rows = db.query(PlanoSaaS).filter(PlanoSaaS.ativo == True).order_by(PlanoSaaS.valor.asc()).all()  # noqa: E712
        return [_row(p) for p in rows]


@app.post("/api/planos-saas", status_code=201)
def create_plano_saas(data: PlanoSaaSIn, request: Request):
    _exige_superadmin(request)
    if data.valor < 0:
        raise HTTPException(422, "Valor inválido.")
    with SessionLocal() as db:
        p = PlanoSaaS(id=_new_id(), **data.model_dump())
        db.add(p)
        db.commit()
        db.refresh(p)
        return _row(p)


@app.put("/api/planos-saas/{pid}")
def update_plano_saas(pid: str, data: PlanoSaaSIn, request: Request):
    _exige_superadmin(request)
    with SessionLocal() as db:
        p = db.get(PlanoSaaS, pid)
        if not p:
            raise HTTPException(404, "Plano não encontrado.")
        for k, v in data.model_dump().items():
            setattr(p, k, v)
        db.commit()
        db.refresh(p)
        return _row(p)


class AtribuirPlanoIn(BaseModel):
    planoId: str | None = None  # None = tira a clínica de qualquer plano (volta pro trial, sem cobrança)


@app.put("/api/clinicas/{cid}/plano")
def set_plano_clinica(cid: int, data: AtribuirPlanoIn, request: Request):
    _exige_superadmin(request)
    with SessionLocal() as db:
        c = db.get(Clinica, cid)
        if not c:
            raise HTTPException(404, "Clínica não encontrada.")
        if data.planoId:
            plano = db.get(PlanoSaaS, data.planoId)
            if not plano:
                raise HTTPException(404, "Plano não encontrado.")
        a = db.get(AssinaturaClinica, cid)
        if not a:
            a = AssinaturaClinica(clinicaId=cid)
            db.add(a)
        a.planoId = data.planoId
        if not data.planoId:
            a.status = "trial"
        db.commit()
        return {"ok": True}


class BloquearClinicaIn(BaseModel):
    bloquear: bool


@app.put("/api/clinicas/{cid}/bloquear")
def bloquear_clinica(cid: int, data: BloquearClinicaIn, request: Request):
    """Único jeito de suspender o acesso de uma clínica — sempre uma decisão
    manual do superadmin, nunca automática por atraso de pagamento."""
    _exige_superadmin(request)
    with SessionLocal() as db:
        c = db.get(Clinica, cid)
        if not c:
            raise HTTPException(404, "Clínica não encontrada.")
        a = db.get(AssinaturaClinica, cid)
        if not a:
            a = AssinaturaClinica(clinicaId=cid)
            db.add(a)
        a.bloqueadaManualmente = data.bloquear
        db.commit()
        return {"ok": True, "bloqueadaManualmente": a.bloqueadaManualmente}


@app.get("/api/minha-assinatura")
def get_minha_assinatura(request: Request):
    """Pra qualquer usuário da clínica ver o status da PRÓPRIA assinatura —
    usado pro aviso de atraso, nunca bloqueia nada por si só."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        a = db.get(AssinaturaClinica, quem.clinicaId)
        plano = db.get(PlanoSaaS, a.planoId) if (a and a.planoId) else None
        if not a:
            return {"status": "trial", "planoNome": None, "proximoVencimento": None}
        return {
            "status": a.status, "planoNome": plano.nome if plano else None,
            "proximoVencimento": a.proximoVencimento,
        }


def _mp_request(metodo: str, caminho: str, corpo: dict | None = None) -> dict:
    r = requests.request(
        metodo, f"https://api.mercadopago.com{caminho}",
        headers={"Authorization": f"Bearer {MERCADOPAGO_ACCESS_TOKEN}", "Content-Type": "application/json"},
        json=corpo, timeout=30,
    )
    if r.status_code >= 300:
        raise HTTPException(502, f"Mercado Pago recusou a requisição ({r.status_code}): {r.text[:300]}")
    return r.json()


@app.post("/api/clinicas/{cid}/gerar-cobranca-mp", status_code=201)
def gerar_cobranca_mp(cid: int, request: Request):
    """Cria a assinatura recorrente no Mercado Pago (produto "Preapproval")
    pro plano atual da clínica, e devolve o link de checkout — o superadmin
    copia e manda pra clínica autorizar o pagamento recorrente (cartão).
    Isso NÃO é cobrança via Pix: o Preapproval do Mercado Pago é sempre
    cartão; Pix não tem cobrança recorrente nativa no Brasil."""
    _exige_superadmin(request)
    _mp_configurado()
    with SessionLocal() as db:
        c = db.get(Clinica, cid)
        if not c:
            raise HTTPException(404, "Clínica não encontrada.")
        a = db.get(AssinaturaClinica, cid)
        if not a or not a.planoId:
            raise HTTPException(409, "Defina um plano pra esta clínica antes de gerar a cobrança.")
        plano = db.get(PlanoSaaS, a.planoId)
        if not plano:
            raise HTTPException(404, "Plano não encontrado.")
        # payer_email é obrigatório pro Mercado Pago — nem toda clínica
        # preenche o e-mail institucional em Ajustes, então cai pro e-mail
        # de algum usuário dela (sempre existe, é obrigatório no cadastro).
        email_pagador = c.email.strip() if c.email else ""
        if not email_pagador:
            usuario_clinica = db.query(Usuario).filter(Usuario.clinicaId == cid, Usuario.ativo == True).order_by(Usuario.id.asc()).first()  # noqa: E712
            email_pagador = usuario_clinica.email if usuario_clinica else ""
        if not email_pagador:
            raise HTTPException(422, "Esta clínica não tem nenhum e-mail cadastrado (nem no perfil, nem em nenhum usuário) — impossível gerar a cobrança sem isso.")
        resp = _mp_request("POST", "/preapproval", {
            "reason": f"Assinatura {plano.nome} — {c.nome}",
            "auto_recurring": {
                "frequency": 1, "frequency_type": "months",
                "transaction_amount": round(plano.valor / 100, 2),
                "currency_id": "BRL",
            },
            "back_url": APP_URL,
            "payer_email": email_pagador,
            "external_reference": str(cid),
            "status": "pending",
        })
        a.mpPreapprovalId = resp.get("id")
        db.commit()
        return {"ok": True, "initPoint": resp.get("init_point"), "preapprovalId": resp.get("id")}


@app.post("/api/webhooks/mercadopago")
async def webhook_mercadopago(request: Request):
    """Notificação do Mercado Pago sobre mudanças na assinatura (autorizada,
    pagamento recebido, cancelada...). Rota pública — o Mercado Pago não
    manda o Bearer token da sua conta, então a validação real é: só
    atualizamos uma AssinaturaClinica cujo mpPreapprovalId bate com o que
    veio na notificação. Configure esta URL (/api/webhooks/mercadopago) no
    painel do Mercado Pago, em Webhooks, pro evento "subscription_preapproval"."""
    try:
        payload = await request.json()
    except Exception:
        return {"ok": True}  # corpo inválido — apenas confirma recebimento, sem processar
    preapproval_id = (payload.get("data") or {}).get("id") or payload.get("id")
    if not preapproval_id or not MERCADOPAGO_ACCESS_TOKEN:
        return {"ok": True}
    try:
        info = _mp_request("GET", f"/preapproval/{preapproval_id}")
    except HTTPException:
        return {"ok": True}  # não derruba o webhook por falha de consulta — Mercado Pago reenvia depois
    status_mp = info.get("status")  # authorized | paused | cancelled | pending
    mapa_status = {"authorized": "ativa", "paused": "atrasada", "cancelled": "cancelada", "pending": "trial"}
    with SessionLocal() as db:
        a = db.query(AssinaturaClinica).filter(AssinaturaClinica.mpPreapprovalId == preapproval_id).first()
        if a:
            a.status = mapa_status.get(status_mp, a.status)
            db.commit()
    return {"ok": True}


@app.get("/api/plataforma")
def get_plataforma():
    with SessionLocal() as db:
        p = db.get(Plataforma, 1)
        if not p:
            p = Plataforma(id=1)
            db.add(p)
            db.commit()
            db.refresh(p)
        logo_url = _sb_signed_url_or_none(_path_logo_plataforma(), segundos=3600) if p.logoPath else None
        return {"nome": p.nome, "logoUrl": logo_url}


@app.put("/api/plataforma")
def set_plataforma(data: PlataformaIn, request: Request):
    _exige_superadmin(request)
    nome = data.nome.strip()
    if not nome:
        raise HTTPException(422, "Informe um nome.")
    with SessionLocal() as db:
        p = db.get(Plataforma, 1)
        if not p:
            p = Plataforma(id=1)
            db.add(p)
        p.nome = nome
        db.commit()
    return {"ok": True}


ASSINATURA_MAX_BYTES = 2 * 1024 * 1024  # 2 MB — assinatura/logo é simples, nunca deveria chegar perto disso


class AssinaturaIn(BaseModel):
    dataUrl: str  # data URL do canvas/imagem, ex: "data:image/png;base64,iVBORw0KG..."


def _decodificar_assinatura(data_url: str) -> bytes:
    if not data_url or "," not in data_url or "image/png" not in data_url.split(",", 1)[0]:
        raise HTTPException(422, "Imagem inválida — envie um PNG.")
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        raise HTTPException(422, "Imagem inválida.")
    if len(raw) == 0:
        raise HTTPException(422, "Imagem vazia.")
    if len(raw) > ASSINATURA_MAX_BYTES:
        raise HTTPException(422, "Imagem maior que 2 MB.")
    return raw


def _path_logo_plataforma() -> str:
    return "_plataforma/logo.png"


@app.post("/api/plataforma/logo", status_code=201)
def upload_logo_plataforma(data: AssinaturaIn, request: Request):
    # reaproveita AssinaturaIn (só tem um campo dataUrl em base64) — mesmo formato de entrada
    _exige_superadmin(request)
    _sb_configurado()
    raw = _decodificar_assinatura(data.dataUrl)
    _sb_upload(_path_logo_plataforma(), raw, "image/png")
    with SessionLocal() as db:
        p = db.get(Plataforma, 1)
        if not p:
            p = Plataforma(id=1)
            db.add(p)
        p.logoPath = _path_logo_plataforma()
        db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# PERFIL DO CONSULTÓRIO  (dados da própria clínica; qualquer usuário logado vê e edita)
# ══════════════════════════════════════════════════════════════════════════════
class ClinicaPerfilIn(BaseModel):
    nome: str
    responsavelTecnico: str = ""
    croResponsavel: str = ""
    cnpj: str = ""
    enderecoCompleto: str = ""
    telefoneWhatsapp: str = ""
    email: str = ""
    cidade: str = ""
    chavePix: str = ""
    tipoChavePix: str = ""  # cpf | cnpj | email | telefone | aleatoria


def _normalizar_chave_pix(tipo: str, chave: str) -> str:
    """Formata a chave Pix conforme o tipo declarado. Isso importa de
    verdade: um CPF e um telefone (DDD+9+número) têm os dois exatamente 11
    dígitos — sem saber o tipo, não dá pra saber se falta prefixo +55 ou
    não, e uma chave de telefone salva sem +55 é "estruturalmente válida"
    no payload (o QR lê normal) mas o banco não reconhece a chave."""
    import re
    chave = (chave or "").strip()
    if not chave or not tipo:
        return chave
    if tipo == "cpf":
        digitos = re.sub(r"\D", "", chave)
        if len(digitos) != 11:
            raise HTTPException(422, "CPF inválido para chave Pix — precisa ter 11 dígitos.")
        return digitos
    if tipo == "cnpj":
        digitos = re.sub(r"\D", "", chave)
        if len(digitos) != 14:
            raise HTTPException(422, "CNPJ inválido para chave Pix — precisa ter 14 dígitos.")
        return digitos
    if tipo == "telefone":
        digitos = re.sub(r"\D", "", chave)
        if digitos.startswith("55") and len(digitos) in (12, 13):
            pass  # já vem com código do país
        elif len(digitos) in (10, 11):
            digitos = "55" + digitos
        else:
            raise HTTPException(422, "Telefone inválido para chave Pix — informe DDD + número.")
        return "+" + digitos
    if tipo == "email":
        if "@" not in chave or "." not in chave.split("@")[-1]:
            raise HTTPException(422, "E-mail inválido para chave Pix.")
        return chave.lower()
    return chave  # aleatoria (UUID) — mantém como veio


def _path_logo_clinica(clinica_id: int) -> str:
    return f"_logos/{clinica_id}.png"


@app.get("/api/clinica")
def get_clinica(request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        c = db.get(Clinica, quem.clinicaId)
        if not c:
            raise HTTPException(404, "Clínica não encontrada.")
        logo_url = _sb_signed_url_or_none(_path_logo_clinica(c.id), segundos=3600) if c.logoPath else None
        return {
            "nome": c.nome, "responsavelTecnico": c.responsavelTecnico, "croResponsavel": c.croResponsavel,
            "cnpj": c.cnpj, "enderecoCompleto": c.enderecoCompleto, "telefoneWhatsapp": c.telefoneWhatsapp,
            "email": c.email, "cidade": c.cidade, "chavePix": c.chavePix, "tipoChavePix": c.tipoChavePix,
            "logoUrl": logo_url,
        }


@app.put("/api/clinica")
def update_clinica(data: ClinicaPerfilIn, request: Request):
    quem = _usuario_logado(request)
    nome = data.nome.strip()
    if not nome:
        raise HTTPException(422, "Informe o nome da clínica.")
    chave_normalizada = _normalizar_chave_pix(data.tipoChavePix.strip(), data.chavePix)
    with SessionLocal() as db:
        c = db.get(Clinica, quem.clinicaId)
        if not c:
            raise HTTPException(404, "Clínica não encontrada.")
        c.nome = nome
        c.responsavelTecnico = data.responsavelTecnico.strip()
        c.croResponsavel = data.croResponsavel.strip()
        c.cnpj = data.cnpj.strip()
        c.enderecoCompleto = data.enderecoCompleto.strip()
        c.telefoneWhatsapp = data.telefoneWhatsapp.strip()
        c.email = data.email.strip()
        c.cidade = data.cidade.strip()
        c.chavePix = chave_normalizada
        c.tipoChavePix = data.tipoChavePix.strip()
        db.commit()
    return {"ok": True}


@app.post("/api/clinica/logo", status_code=201)
def upload_logo_clinica(data: AssinaturaIn, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    raw = _decodificar_assinatura(data.dataUrl)
    _sb_upload(_path_logo_clinica(quem.clinicaId), raw, "image/png")
    with SessionLocal() as db:
        c = db.get(Clinica, quem.clinicaId)
        if c:
            c.logoPath = _path_logo_clinica(quem.clinicaId)
            db.commit()
    return {"ok": True}


@app.delete("/api/clinica/logo", status_code=204)
def remover_logo_clinica(request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        c = db.get(Clinica, quem.clinicaId)
        if c:
            c.logoPath = None
            db.commit()
    _sb_delete(_path_logo_clinica(quem.clinicaId))


# ── helpers ────────────────────────────────────────────────────────────────────
def _row(obj) -> dict[str, Any]:
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    d.pop("updated_at", None)
    return d

def _new_id() -> str:
    return str(random.randint(100000, 999999)) + str(int(time.time()))[-4:]


def _prof_da_clinica(db, prof_id: int | None, clinica_id: int) -> bool:
    """True se prof_id for None (campo opcional, não informado) OU se apontar
    para um profissional que de fato pertence à clínica de quem está fazendo
    a requisição. Usar sempre que um profissionalId vier do corpo da
    requisição (não de um registro já existente no banco) — sem isso, nada
    impede que o cliente referencie um profissional de outra clínica."""
    if prof_id is None:
        return True
    prof = db.get(Profissional, prof_id)
    return bool(prof and prof.clinicaId == clinica_id)


def _plano_da_clinica(db, clinica_id: int) -> PlanoSaaS | None:
    """Plano de assinatura ATUAL da clínica (o que ela paga pra usar o
    sistema), ou None se não estiver em nenhum plano com limite (trial sem
    plano atribuído = sem limite nenhum ainda)."""
    a = db.get(AssinaturaClinica, clinica_id)
    return db.get(PlanoSaaS, a.planoId) if (a and a.planoId) else None


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
        plano = _plano_da_clinica(db, quem.clinicaId)
        if plano and plano.limiteProfissionais is not None:
            total = db.query(Profissional).filter(Profissional.clinicaId == quem.clinicaId).count()
            if total >= plano.limiteProfissionais:
                raise HTTPException(403, f"Limite de {plano.limiteProfissionais} profissionais do plano {plano.nome} atingido. Fale com o suporte para fazer upgrade.")
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


def _prontuario_duplicado(db, clinica_id: int, num_prontuario: str, exclude_id: str | None = None) -> Paciente | None:
    """Retorna o paciente que já usa esse número de prontuário na mesma
    clínica, ou None se estiver livre. Número vazio nunca é considerado
    duplicado — "ainda não atribuído" é um estado válido."""
    num = (num_prontuario or "").strip()
    if not num:
        return None
    q = db.query(Paciente).filter(Paciente.clinicaId == clinica_id, Paciente.numProntuario == num)
    if exclude_id:
        q = q.filter(Paciente.id != exclude_id)
    return q.first()


@app.post("/api/patients", status_code=201)
def create_patient(data: PacienteIn, request: Request):
    quem = _usuario_logado(request)
    pid = data.id or _new_id()
    with SessionLocal() as db:
        dup = _prontuario_duplicado(db, quem.clinicaId, data.numProntuario)
        if dup:
            raise HTTPException(409, f"Número de prontuário já usado por {dup.name}.")
        plano = _plano_da_clinica(db, quem.clinicaId)
        if plano and plano.limitePacientes is not None:
            total = db.query(Paciente).filter(Paciente.clinicaId == quem.clinicaId).count()
            if total >= plano.limitePacientes:
                raise HTTPException(403, f"Limite de {plano.limitePacientes} pacientes do plano {plano.nome} atingido. Fale com o suporte para fazer upgrade.")
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
        dup = _prontuario_duplicado(db, quem.clinicaId, data.numProntuario, exclude_id=pid)
        if dup:
            raise HTTPException(409, f"Número de prontuário já usado por {dup.name}.")
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


def _garantir_token_portal(db, p: Paciente) -> str:
    """Gera (na primeira vez) e devolve o token opaco do portal público do
    paciente. NUNCA usar o id interno pra isso — o id é curto e previsível
    (aleatório + sufixo de timestamp), o token é longo e imprevisível
    (256 bits), porque é a ÚNICA credencial que protege o link sem login."""
    if not p.tokenPortal:
        p.tokenPortal = secrets.token_urlsafe(24)
        db.commit()
        db.refresh(p)
    return p.tokenPortal


@app.get("/api/patients/{pid}/qrcode.svg")
def get_patient_qrcode(pid: str, request: Request):
    """QR da carteirinha: aponta pro portal público do paciente (token
    opaco, sem exigir login) — pra ele ver a própria consulta, pendência
    financeira e falar com a clínica direto do celular."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p or p.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        token = _garantir_token_portal(db, p)
    url = f"{APP_URL}/?portal={token}"
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


# ══════════════════════════════════════════════════════════════════════════════
# PORTAL PÚBLICO DO PACIENTE  (sem login — protegido só pelo token do QR)
# ══════════════════════════════════════════════════════════════════════════════
def _sem_acento_maiusculo(txto: str, max_len: int) -> str:
    """Normaliza texto pro padrão exigido pelos campos do Pix: só ASCII,
    maiúsculo, sem acento. Usado no nome/cidade do recebedor no payload."""
    s = unicodedata.normalize("NFKD", txto or "").encode("ascii", "ignore").decode("ascii")
    s = "".join(c for c in s if c.isalnum() or c == " ").strip().upper()
    return (s or "NA")[:max_len]


def _pix_tlv(id_: str, value: str) -> str:
    return f"{id_}{len(value):02d}{value}"


def _gerar_pix_copia_cola(chave: str, nome: str, cidade: str, valor_centavos: int, txid: str = "") -> str:
    """Monta o payload Pix "copia e cola" (padrão BR Code / EMV do Bacen).
    Testado byte a byte contra uma implementação independente do CRC16 e
    com round-trip real (gerar QR -> decodificar -> comparar) antes de
    entrar em produção."""
    chave = (chave or "").strip()
    nome_fmt = _sem_acento_maiusculo(nome, 25)
    cidade_fmt = _sem_acento_maiusculo(cidade, 15)
    txid_fmt = "".join(c for c in (txid or "") if c.isalnum())[:25] or "***"
    valor = (Decimal(valor_centavos) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    conta = _pix_tlv("00", "BR.GOV.BCB.PIX") + _pix_tlv("01", chave)
    payload = (
        _pix_tlv("00", "01")
        + _pix_tlv("26", conta)
        + _pix_tlv("52", "0000")
        + _pix_tlv("53", "986")
        + _pix_tlv("54", str(valor))
        + _pix_tlv("58", "BR")
        + _pix_tlv("59", nome_fmt)
        + _pix_tlv("60", cidade_fmt)
        + _pix_tlv("62", _pix_tlv("05", txid_fmt))
    )
    base = payload + "6304"
    crc = crc_hqx(base.encode("ascii"), 0xFFFF)
    return base + f"{crc:04X}"


@app.get("/api/portal/{token}")
def get_portal_paciente(token: str):
    with SessionLocal() as db:
        p = db.query(Paciente).filter(Paciente.tokenPortal == token).first()
        if not p:
            raise HTTPException(404, "Link inválido.")
        clinica = db.get(Clinica, p.clinicaId)
        hoje = datetime.now().date().isoformat()
        proxima = (
            db.query(Consulta)
            .filter(
                Consulta.patientId == p.id, Consulta.clinicaId == p.clinicaId,
                Consulta.date >= hoje, Consulta.status.in_(["agendado", "confirmado"]),
            )
            .order_by(Consulta.date.asc(), Consulta.time.asc())
            .first()
        )
        prof_prox = db.get(Profissional, proxima.profissionalId) if (proxima and proxima.profissionalId) else None
        lancs_abertos = (
            db.query(Lancamento)
            .filter(Lancamento.patientId == p.id, Lancamento.clinicaId == p.clinicaId, Lancamento.pagoEm.is_(None))
            .order_by(Lancamento.vencimento.asc())
            .all()
        )
        total_aberto = sum(l.valor for l in lancs_abertos)
        pix_copia_cola = None
        if total_aberto > 0 and clinica and clinica.chavePix.strip():
            pix_copia_cola = _gerar_pix_copia_cola(
                clinica.chavePix, clinica.nome, clinica.cidade,
                total_aberto, f"CRM{(p.numProntuario or p.id)[:10]}",
            )
        return {
            "nome": p.name,
            "proximaConsulta": {
                "data": proxima.date, "hora": proxima.time,
                "profissionalNome": prof_prox.nome if prof_prox else None,
            } if proxima else None,
            "ultimaVisita": p.lastVisit,
            "clinica": {
                "nome": clinica.nome if clinica else "",
                "endereco": clinica.enderecoCompleto if clinica else "",
                "telefoneWhatsapp": clinica.telefoneWhatsapp if clinica else "",
            },
            "totalAberto": total_aberto,
            "lancamentosAbertos": [
                {"descricao": l.descricao, "vencimento": l.vencimento, "valor": l.valor} for l in lancs_abertos
            ],
            "pixCopiaCola": pix_copia_cola,
        }




# ══════════════════════════════════════════════════════════════════════════════
# ANAMNESE REMOTA  (link/QR pro PACIENTE preencher sozinho, sem login;
# equipe revisa e aprova depois — só então os dados entram no cadastro)
# ══════════════════════════════════════════════════════════════════════════════
# Campos fixos de identificação/responsável que o paciente pode preencher no
# formulário público — espelham o que openPatientForm já coleta manualmente.
# As perguntas CLÍNICAS variam por MODELO (Padrão, Infantil, Ortodôntica,
# Cirurgia e Implante...) e vêm de AnamneseModelo/AnamnesePergunta — sempre no
# formato Sim/Não/Não sei + "informações adicionais" (texto opcional), igual
# ao padrão do Codental.
ANAMNESE_CAMPOS_IDENTIFICACAO = [
    "birth", "rg", "orgaoExpedidor", "cpf", "naturalidade", "nacionalidade",
    "estadoCivil", "profissao", "localTrabalho", "enderecoResidencial", "indicadoPor", "email",
]
ANAMNESE_CAMPOS_RESPONSAVEL = ["respNome", "respRg", "respCpf", "respTelefone", "respEmail"]

# Palavras-chave (no texto da própria pergunta) usadas só pra decidir em qual
# campo de alerta clínico do cadastro (alergias / medicações / condições
# sistêmicas) uma resposta "Sim" deve cair — só se aplica às perguntas dos
# modelos padrão que a própria clínica pode editar depois; se o texto não
# bater com nenhuma palavra-chave, a resposta continua preservada na
# evolução gerada, só não some pro resumo rápido do cadastro.
_KW_ALERGIA = ("alergia",)
_KW_MEDICACAO = ("medicament", "medicação", "medicacao", "remédio", "remedio")
_KW_CONDICAO = ("sanguín", "sanguin", "hemorrag", "cardiovascular", "diabét", "diabet",
                "gestante", "gravidez", "hepatite", "renal", "respirat", "osteoporose",
                "autoimune", "anticoagulante", "pressão", "pressao")

# ── modelos padrão semeados na primeira vez que a clínica abre a tela ──
_MODELOS_PADRAO_SEED: dict[str, list[str]] = {
    "Anamnese Padrão": [
        "Possui alguma alergia? (Como penicilinas, AAS ou outra)",
        "Possui alguma alteração sanguínea?",
        "Já teve hemorragia diagnosticada?",
        "Possui alguma alteração cardiovascular? (pressão alta, sopro no coração, etc.)",
        "É diabético?",
        "Está gestante ou amamentando?",
        "Possui alguma doença renal?",
        "Possui hepatite ou outra doença no fígado?",
        "Faz uso contínuo de alguma medicação?",
        "Já foi hospitalizado(a) ou passou por cirurgia?",
        "É fumante?",
        "Possui alguma outra condição de saúde que devemos saber?",
    ],
    "Anamnese Infantil": [
        "A criança possui alguma alergia? (medicamentos, alimentos, látex...)",
        "Já teve alguma reação à anestesia local?",
        "Possui alguma doença respiratória (asma, bronquite)?",
        "Faz uso contínuo de alguma medicação?",
        "Já foi hospitalizada ou passou por cirurgia?",
        "Tem hábito de chupar dedo, chupeta ou roer unhas?",
        "Respira normalmente pelo nariz ou é \"respiradora bucal\"?",
        "Já teve traumatismo dental (queda, batida nos dentes)?",
        "Possui alguma condição neurológica ou de desenvolvimento que devemos saber?",
        "A gestação ou o parto tiveram alguma complicação relevante?",
    ],
    "Anamnese Ortodôntica": [
        "Possui alguma alergia? (metais, látex, medicamentos)",
        "Possui alguma alteração cardiovascular?",
        "É diabético?",
        "Possui alguma doença óssea ou articular (ATM, artrite)?",
        "Já fez tratamento ortodôntico antes?",
        "Range ou aperta os dentes (bruxismo)?",
        "Tem hábito de respirar pela boca?",
        "Já teve alguma cirurgia na face ou mandíbula?",
        "Possui algum problema na articulação da mandíbula (estalos, dor, dificuldade de abrir a boca)?",
        "Faz uso contínuo de alguma medicação?",
        "Possui alguma outra condição de saúde que devemos saber?",
    ],
    "Anamnese de Cirurgia e Implante": [
        "Possui alguma alergia? (anestésicos, antibióticos, látex...)",
        "Possui alguma alteração sanguínea ou dificuldade de coagulação?",
        "Já teve hemorragia diagnosticada?",
        "Possui alguma alteração cardiovascular (pressão alta, marca-passo, sopro)?",
        "É diabético?",
        "Possui osteoporose ou já usou medicação à base de bisfosfonatos?",
        "Está gestante ou amamentando?",
        "Faz uso de anticoagulante ou antiagregante plaquetário?",
        "É fumante?",
        "Já fez alguma cirurgia odontológica antes? Teve alguma complicação?",
        "Possui alguma doença autoimune?",
        "Faz uso contínuo de alguma medicação?",
        "Possui alguma outra condição de saúde que devemos saber?",
    ],
}


def _seed_modelos_padrao(db, clinica_id: int) -> list[AnamneseModelo]:
    criados = []
    for nome, perguntas in _MODELOS_PADRAO_SEED.items():
        m = AnamneseModelo(id=_new_id(), clinicaId=clinica_id, nome=nome, ativo=True)
        db.add(m)
        db.flush()
        for i, texto in enumerate(perguntas):
            db.add(AnamnesePergunta(id=_new_id(), clinicaId=clinica_id, modeloId=m.id, ordem=i, texto=texto))
        criados.append(m)
    db.commit()
    return criados


def _modelo_row(db, m: AnamneseModelo) -> dict[str, Any]:
    perguntas = (
        db.query(AnamnesePergunta)
        .filter(AnamnesePergunta.modeloId == m.id)
        .order_by(AnamnesePergunta.ordem.asc())
        .all()
    )
    return {"id": m.id, "nome": m.nome, "perguntas": [{"id": p.id, "texto": p.texto} for p in perguntas]}


@app.get("/api/anamnese-modelos")
def list_anamnese_modelos(request: Request):
    """Lista os modelos de anamnese da clínica — semeia os 4 padrão
    (Padrão, Infantil, Ortodôntica, Cirurgia e Implante) na primeira vez."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        modelos = (
            db.query(AnamneseModelo)
            .filter(AnamneseModelo.clinicaId == quem.clinicaId, AnamneseModelo.ativo == True)  # noqa: E712
            .order_by(AnamneseModelo.created_at.asc())
            .all()
        )
        if not modelos:
            modelos = _seed_modelos_padrao(db, quem.clinicaId)
        return [_modelo_row(db, m) for m in modelos]


class AnamneseRespostasIn(BaseModel):
    """Corpo enviado pelo PRÓPRIO paciente (rota pública, sem login)."""
    birth: str | None = None
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
    email: str = ""
    respNome: str = ""
    respRg: str = ""
    respCpf: str = ""
    respTelefone: str = ""
    respEmail: str = ""
    queixaPrincipal: str = ""
    # perguntaId -> {"resposta": "sim"|"nao"|"nao_sei", "info": "texto opcional"}
    respostasClinicas: dict[str, dict] = {}
    assinaturaDataUrl: str = ""  # rubrica do paciente, PNG em base64 — obrigatória

    @field_validator("birth")
    @classmethod
    def _valida_birth(cls, v):
        # Campo aceito como texto livre (rota pública, sem login) — precisa
        # ser validado no formato ANTES de virar Paciente.birth ou entrar em
        # qualquer HTML (modal de revisão, impressão), senão vira um vetor de
        # XSS: alguém pode enviar direto pra API, sem passar pelo formulário
        # com <input type="date">, que é o que hoje impede isso no navegador.
        if not v:
            return None
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("Data de nascimento inválida.")
        return v


class AnamneseRemotaCriarIn(BaseModel):
    modeloId: str


@app.post("/api/patients/{pid}/anamnese-remota", status_code=201)
def criar_anamnese_remota(pid: str, data: AnamneseRemotaCriarIn, request: Request):
    """A equipe gera (ou reaproveita) o link pra este paciente preencher a
    própria anamnese, escolhendo o modelo (Padrão, Infantil...). Se já
    existir um link pendente/preenchido ainda não aprovado, devolve o mesmo
    em vez de criar outro — evita links órfãos quando a pessoa clica
    "gerar link" duas vezes por engano. Se o link ainda estiver "pendente"
    (o paciente não respondeu ainda), o modelo escolhido agora substitui o
    anterior; se já foi "preenchido", o modelo original é preservado."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        pac = db.get(Paciente, pid)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        modelo = db.get(AnamneseModelo, data.modeloId)
        if not modelo or modelo.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Modelo de anamnese não encontrado.")
        existente = (
            db.query(AnamneseRemota)
            .filter(AnamneseRemota.patientId == pid, AnamneseRemota.clinicaId == quem.clinicaId,
                    AnamneseRemota.status != "aprovado")
            .order_by(AnamneseRemota.created_at.desc())
            .first()
        )
        if existente:
            a = existente
            if a.status == "pendente":
                a.modeloId = data.modeloId
                db.commit()
        else:
            a = AnamneseRemota(id=_new_id(), clinicaId=quem.clinicaId, patientId=pid, modeloId=data.modeloId,
                                token=secrets.token_urlsafe(24), status="pendente")
            db.add(a)
            db.commit()
            db.refresh(a)
        return {"id": a.id, "token": a.token, "link": f"{APP_URL}/?anamnese={a.token}", "status": a.status}


@app.get("/api/anamnese-remota/{aid}/qrcode.svg")
def get_anamnese_qrcode(aid: str, request: Request):
    """QR pro botão "Compartilhar anamnese" — mesmo padrão do QR da
    carteirinha (token opaco, sem exigir login do paciente pra abrir)."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        a = db.get(AnamneseRemota, aid)
        if not a or a.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Anamnese não encontrada.")
        token = a.token
    url = f"{APP_URL}/?anamnese={token}"
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


@app.get("/api/anamnese-publica/{token}")
def get_anamnese_publica(token: str):
    """Rota pública (sem login) que o PACIENTE acessa pelo link/QR."""
    with SessionLocal() as db:
        a = db.query(AnamneseRemota).filter(AnamneseRemota.token == token).first()
        if not a:
            raise HTTPException(404, "Link inválido.")
        pac = db.get(Paciente, a.patientId)
        clinica = db.get(Clinica, a.clinicaId) if pac else None
        modelo = db.get(AnamneseModelo, a.modeloId) if a.modeloId else None
        perguntas = []
        if modelo:
            perguntas = [
                {"id": p.id, "texto": p.texto}
                for p in db.query(AnamnesePergunta).filter(AnamnesePergunta.modeloId == modelo.id)
                .order_by(AnamnesePergunta.ordem.asc()).all()
            ]
        return {
            "status": a.status,
            "patientName": pac.name if pac else "",
            "clinicaNome": clinica.nome if clinica else "",
            "modeloNome": modelo.nome if modelo else "",
            "perguntas": perguntas,
        }


@app.post("/api/anamnese-publica/{token}", status_code=201)
def enviar_anamnese_publica(token: str, data: AnamneseRespostasIn):
    """O PACIENTE envia as respostas. A assinatura é opcional aqui — se não
    vier, a anamnese fica com "assinatura pendente" e a equipe pode colher
    depois (presencialmente ou reenviando o pedido)."""
    with SessionLocal() as db:
        a = db.query(AnamneseRemota).filter(AnamneseRemota.token == token).first()
        if not a:
            raise HTTPException(404, "Link inválido.")
        if a.status == "aprovado":
            raise HTTPException(410, "Esta anamnese já foi revisada pela clínica.")
        if data.assinaturaDataUrl:
            _sb_configurado()
            raw = _decodificar_assinatura(data.assinaturaDataUrl)
            path = f"_assinaturas_anamnese/{a.clinicaId}/{a.id}.png"
            _sb_upload(path, raw, "image/png")
            a.assinaturaPath = path
        a.respostas = json.dumps(data.model_dump(exclude={"assinaturaDataUrl"}), ensure_ascii=False)
        a.status = "preenchido"
        a.filledAt = datetime.now()
        db.commit()
        return {"ok": True}


@app.get("/api/anamnese-remota")
def list_anamneses_pendentes(request: Request):
    """Fila de revisão: anamneses que o paciente já preencheu e aguardam
    confirmação da equipe."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        rows = (
            db.query(AnamneseRemota)
            .filter(AnamneseRemota.clinicaId == quem.clinicaId, AnamneseRemota.status == "preenchido")
            .order_by(AnamneseRemota.filledAt.asc())
            .all()
        )
        result = []
        for a in rows:
            pac = db.get(Paciente, a.patientId)
            result.append({
                "id": a.id, "patientId": a.patientId,
                "patientName": pac.name if pac else "Paciente removido",
                "patientPhone": pac.phone if pac else "",
                "filledAt": a.filledAt.isoformat() if a.filledAt else None,
            })
        return result


@app.get("/api/anamnese-remota/{aid}")
def get_anamnese_detalhe(aid: str, request: Request):
    """Detalhe completo pra tela de revisão — respostas do paciente já
    decodificadas, com o texto de cada pergunta do modelo usado."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        a = db.get(AnamneseRemota, aid)
        if not a or a.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Anamnese não encontrada.")
        pac = db.get(Paciente, a.patientId)
        modelo = db.get(AnamneseModelo, a.modeloId) if a.modeloId else None
        perguntas = []
        if modelo:
            perguntas = [
                {"id": p.id, "texto": p.texto}
                for p in db.query(AnamnesePergunta).filter(AnamnesePergunta.modeloId == modelo.id)
                .order_by(AnamnesePergunta.ordem.asc()).all()
            ]
        respostas = json.loads(a.respostas) if a.respostas else {}
        return {
            "id": a.id, "patientId": a.patientId,
            "patientName": pac.name if pac else "Paciente removido",
            "status": a.status, "respostas": respostas,
            "modeloNome": modelo.nome if modelo else "",
            "perguntas": perguntas,
            "temAssinatura": bool(a.assinaturaPath),
            "filledAt": a.filledAt.isoformat() if a.filledAt else None,
        }


class AprovarAnamneseIn(BaseModel):
    profissionalId: int
    # Campos revisados (possivelmente editados pela equipe) — mesmo shape de AnamneseRespostasIn.
    birth: str | None = None
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
    email: str = ""
    respNome: str = ""
    respRg: str = ""
    respCpf: str = ""
    respTelefone: str = ""
    respEmail: str = ""
    queixaPrincipal: str = ""
    respostasClinicas: dict[str, dict] = {}

    @field_validator("birth")
    @classmethod
    def _valida_birth(cls, v):
        # Mesma validação de AnamneseRespostasIn — essencial aqui também: este
        # modelo é usado tanto pela revisão de anamnese remota quanto (via
        # herança, em AnamnesePresencialIn) pelo preenchimento presencial.
        if not v:
            return None
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("Data de nascimento inválida.")
        return v


def _resumo_respostas_clinicas(perguntas: list[dict], respostas_clinicas: dict) -> tuple[str, str, str, list[str]]:
    """A partir das perguntas do modelo e das respostas (possivelmente
    editadas pela equipe), devolve: (alergias, medicacoes, condicoesSistemicas,
    linhas_para_evolucao). Os três primeiros só levam o que bateu com as
    palavras-chave — o restante das respostas "Sim"/"Não sei" continua
    preservado na lista de linhas, que vai inteira pra evolução."""
    rotulo = {"sim": "Sim", "nao": "Não", "nao_sei": "Não sei"}
    alergias_bits, medic_bits, cond_bits, linhas = [], [], [], []
    for p in perguntas:
        r = respostas_clinicas.get(p["id"]) or {}
        resposta = (r.get("resposta") or "").strip()
        info = (r.get("info") or "").strip()
        if not resposta:
            continue
        linhas.append(f"{p['texto']}: {rotulo.get(resposta, resposta)}" + (f" — {info}" if info else ""))
        if resposta != "sim":
            continue
        texto_low = p["texto"].lower()
        if any(k in texto_low for k in _KW_ALERGIA):
            alergias_bits.append(info or p["texto"])
        elif any(k in texto_low for k in _KW_MEDICACAO):
            medic_bits.append(info or p["texto"])
        elif any(k in texto_low for k in _KW_CONDICAO):
            cond_bits.append(info or p["texto"])
    return ("; ".join(alergias_bits), "; ".join(medic_bits), "; ".join(cond_bits), linhas)


def _aplicar_anamnese_ao_cadastro(db, a: AnamneseRemota, quem: Usuario, data: "AprovarAnamneseIn", origem: str = "remota") -> None:
    """Núcleo comum entre aprovar uma anamnese enviada pelo paciente e o
    profissional preencher direto no sistema: aplica identificação/
    responsável ao cadastro, resume as respostas clínicas nos campos de
    alerta e registra queixa + Q&A como evolução. `a` já precisa existir
    (persistido ou pendente de flush) e pertencer à clínica de quem chama."""
    if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
        raise HTTPException(404, "Profissional não encontrado.")
    pac = db.get(Paciente, a.patientId)
    if not pac:
        raise HTTPException(404, "Paciente não encontrado.")
    for campo in ANAMNESE_CAMPOS_IDENTIFICACAO + ANAMNESE_CAMPOS_RESPONSAVEL:
        valor = getattr(data, campo)
        if valor:  # só sobrescreve o que veio preenchido — não apaga dado já existente com vazio
            setattr(pac, campo, valor)

    modelo = db.get(AnamneseModelo, a.modeloId) if a.modeloId else None
    perguntas = []
    if modelo:
        perguntas = [
            {"id": p.id, "texto": p.texto}
            for p in db.query(AnamnesePergunta).filter(AnamnesePergunta.modeloId == modelo.id)
            .order_by(AnamnesePergunta.ordem.asc()).all()
        ]
    alergias_novas, medic_novas, cond_novas, linhas = _resumo_respostas_clinicas(perguntas, data.respostasClinicas)
    if alergias_novas:
        pac.alergias = (pac.alergias + "; " + alergias_novas) if pac.alergias else alergias_novas
    if medic_novas:
        pac.medicacoes = (pac.medicacoes + "; " + medic_novas) if pac.medicacoes else medic_novas
    if cond_novas:
        pac.condicoesSistemicas = (pac.condicoesSistemicas + "; " + cond_novas) if pac.condicoesSistemicas else cond_novas

    origem_label = "preenchida remotamente pelo paciente" if origem == "remota" else "preenchida presencialmente pelo profissional"
    queixa = data.queixaPrincipal.strip() or "[Anamnese] Sem queixa principal relatada."
    conteudo_evolucao = queixa
    if linhas:
        titulo_modelo = f" ({modelo.nome})" if modelo else ""
        conteudo_evolucao += f"\n\n--- Respostas da anamnese{titulo_modelo} ---\n" + "\n".join(linhas)
    db.add(Evolucao(
        id=_new_id(), clinicaId=quem.clinicaId, patientId=a.patientId, profissionalId=data.profissionalId,
        denteRegiao="", procedimento=f"Anamnese inicial ({origem_label})",
        conteudo=conteudo_evolucao,
    ))
    a.status = "aprovado"
    a.aprovadoPor = data.profissionalId
    a.aprovadoEm = datetime.now()
    a.respostas = json.dumps(data.model_dump(exclude={"profissionalId", "modeloId", "assinaturaDataUrl"}), ensure_ascii=False)


@app.post("/api/anamnese-remota/{aid}/aprovar", status_code=201)
def aprovar_anamnese(aid: str, data: AprovarAnamneseIn, request: Request):
    """Confirma a anamnese enviada pelo paciente: aplica os campos de
    identificação ao cadastro, resume as respostas clínicas em alergias/
    medicações/condições sistêmicas e registra a queixa principal + o Q&A
    completo como evolução — exige o profissional que revisou. A assinatura
    do paciente, colhida no envio, é preservada como está — não é refeita
    aqui mesmo que a equipe edite algum campo na revisão."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        a = db.get(AnamneseRemota, aid)
        if not a or a.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Anamnese não encontrada.")
        if a.status == "aprovado":
            raise HTTPException(409, "Esta anamnese já foi aprovada.")
        _aplicar_anamnese_ao_cadastro(db, a, quem, data, origem="remota")
        db.commit()
        return {"ok": True}


class AnamnesePresencialIn(AprovarAnamneseIn):
    modeloId: str
    assinaturaDataUrl: str = ""  # rubrica do paciente, PNG em base64 — obrigatória


@app.post("/api/patients/{pid}/anamnese-presencial", status_code=201)
def preencher_anamnese_presencial(pid: str, data: AnamnesePresencialIn, request: Request):
    """O PROFISSIONAL preenche a anamnese direto no sistema (presencial),
    sem passar pelo link remoto — aplica ao cadastro na hora, sem etapa de
    revisão separada. A assinatura do paciente é opcional aqui também: se
    não for colhida agora, fica "pendente" e pode ser colhida depois."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        pac = db.get(Paciente, pid)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        modelo = db.get(AnamneseModelo, data.modeloId)
        if not modelo or modelo.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Modelo de anamnese não encontrado.")
        a = AnamneseRemota(id=_new_id(), clinicaId=quem.clinicaId, patientId=pid, modeloId=data.modeloId,
                            token=secrets.token_urlsafe(24), status="preenchido", filledAt=datetime.now())
        db.add(a)
        db.flush()
        if data.assinaturaDataUrl:
            _sb_configurado()
            raw = _decodificar_assinatura(data.assinaturaDataUrl)
            path = f"_assinaturas_anamnese/{quem.clinicaId}/{a.id}.png"
            _sb_upload(path, raw, "image/png")
            a.assinaturaPath = path
        _aplicar_anamnese_ao_cadastro(db, a, quem, data, origem="presencial")
        db.commit()
        return {"ok": True, "id": a.id}


@app.post("/api/anamnese-remota/{aid}/assinatura", status_code=201)
def colher_assinatura_anamnese(aid: str, data: AssinaturaIn, request: Request):
    """Colhe (ou substitui) a assinatura de uma anamnese depois do
    preenchimento — usada quando ficou "assinatura pendente" e o paciente
    volta pra assinar, presencialmente ou reabrindo o link."""
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        a = db.get(AnamneseRemota, aid)
        if not a or a.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Anamnese não encontrada.")
        raw = _decodificar_assinatura(data.dataUrl)
        path = f"_assinaturas_anamnese/{quem.clinicaId}/{a.id}.png"
        _sb_upload(path, raw, "image/png")
        a.assinaturaPath = path
        db.commit()
        return {"ok": True}


@app.get("/api/anamnese-remota/{aid}/assinatura/url")
def get_assinatura_anamnese(aid: str, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        a = db.get(AnamneseRemota, aid)
        if not a or a.clinicaId != quem.clinicaId or not a.assinaturaPath:
            raise HTTPException(404, "Sem assinatura registrada.")
    url = _sb_signed_url_or_none(a.assinaturaPath, segundos=600)
    if not url:
        raise HTTPException(404, "Sem assinatura registrada.")
    return {"url": url}


@app.get("/api/patients/{pid}/anamneses")
def list_anamneses_paciente(pid: str, request: Request):
    """Histórico de anamneses (remotas e presenciais) deste paciente, pra
    reimprimir ou continuar uma revisão pendente a partir da Ficha."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        pac = db.get(Paciente, pid)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        rows = (
            db.query(AnamneseRemota)
            .filter(AnamneseRemota.patientId == pid, AnamneseRemota.clinicaId == quem.clinicaId)
            .order_by(AnamneseRemota.created_at.desc())
            .all()
        )
        result = []
        for a in rows:
            modelo = db.get(AnamneseModelo, a.modeloId) if a.modeloId else None
            result.append({
                "id": a.id, "status": a.status, "modeloNome": modelo.nome if modelo else "",
                "temAssinatura": bool(a.assinaturaPath),
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "filledAt": a.filledAt.isoformat() if a.filledAt else None,
                "aprovadoEm": a.aprovadoEm.isoformat() if a.aprovadoEm else None,
            })
        return result


@app.delete("/api/anamnese-remota/{aid}", status_code=204)
def delete_anamnese(aid: str, request: Request):
    """Exclui o registro da anamnese (link, respostas e assinatura, se
    houver). NÃO desfaz o que já foi aplicado ao cadastro do paciente nem
    remove a evolução gerada numa aprovação — esses continuam como registro
    clínico, mesmo que a anamnese de origem (ex: um teste) seja excluída
    depois."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        a = db.get(AnamneseRemota, aid)
        if not a or a.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Anamnese não encontrada.")
        path = a.assinaturaPath
        db.delete(a)
        db.commit()
    if path:
        _sb_delete(path)

# ══════════════════════════════════════════════════════════════════════════════
# DOCUMENTOS EMITIDOS  (prescrição, atestado — com timbre e assinatura do profissional)
# ══════════════════════════════════════════════════════════════════════════════
DOCUMENTO_TIPOS_VALIDOS = {"prescricao", "atestado", "recibo"}


class DocumentoIn(BaseModel):
    profissionalId: int
    tipo: str
    # prescrição:
    itens: list[dict] = []  # [{"medicamento": "...", "posologia": "..."}]
    observacoes: str = ""
    # atestado:
    motivo: str = ""
    dias: int | None = None
    dataAtendimento: str | None = None
    horaAtendimento: str = ""
    cid: str = ""
    # recibo:
    valor: int = 0  # centavos
    descricao: str = ""
    formaPagamento: str = ""
    dataPagamento: str | None = None
    lancamentoId: str | None = None  # opcional — vincula a um lançamento já existente do financeiro


@app.post("/api/patients/{pid}/documentos", status_code=201)
def criar_documento(pid: str, data: DocumentoIn, request: Request):
    quem = _usuario_logado(request)
    if data.tipo not in DOCUMENTO_TIPOS_VALIDOS:
        raise HTTPException(422, "Tipo de documento inválido.")
    with SessionLocal() as db:
        pac = db.get(Paciente, pid)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
            raise HTTPException(404, "Profissional não encontrado.")
        if data.tipo == "prescricao":
            itens = [i for i in data.itens if (i.get("medicamento") or "").strip()]
            if not itens:
                raise HTTPException(422, "Adicione pelo menos um medicamento.")
        if data.tipo == "atestado" and not data.motivo.strip() and not data.dias:
            raise HTTPException(422, "Informe o motivo ou os dias de afastamento.")
        if data.tipo == "recibo":
            if data.valor <= 0:
                raise HTTPException(422, "Informe um valor maior que zero.")
            if data.lancamentoId:
                lanc = db.get(Lancamento, data.lancamentoId)
                if not lanc or lanc.clinicaId != quem.clinicaId or lanc.patientId != pid:
                    raise HTTPException(404, "Lançamento não encontrado.")
        conteudo = data.model_dump(exclude={"profissionalId", "tipo"})
        d = DocumentoEmitido(
            id=_new_id(), clinicaId=quem.clinicaId, patientId=pid, profissionalId=data.profissionalId,
            tipo=data.tipo, conteudo=json.dumps(conteudo, ensure_ascii=False),
        )
        db.add(d)
        db.commit()
        return {"id": d.id}


@app.get("/api/patients/{pid}/documentos")
def list_documentos_paciente(pid: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        pac = db.get(Paciente, pid)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        rows = (
            db.query(DocumentoEmitido)
            .filter(DocumentoEmitido.patientId == pid, DocumentoEmitido.clinicaId == quem.clinicaId)
            .order_by(DocumentoEmitido.created_at.desc())
            .all()
        )
        result = []
        for d in rows:
            prof = db.get(Profissional, d.profissionalId)
            result.append({
                "id": d.id, "tipo": d.tipo, "profissionalNome": prof.nome if prof else "",
                "created_at": d.created_at.isoformat() if d.created_at else None,
            })
        return result


@app.get("/api/documentos/{did}")
def get_documento(did: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        d = db.get(DocumentoEmitido, did)
        if not d or d.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Documento não encontrado.")
        pac = db.get(Paciente, d.patientId)
        prof = db.get(Profissional, d.profissionalId)
        conteudo = json.loads(d.conteudo) if d.conteudo else {}
        return {
            "id": d.id, "tipo": d.tipo, "conteudo": conteudo,
            "patientName": pac.name if pac else "Paciente removido",
            "patientCpf": pac.cpf if pac else "",
            "profissionalId": d.profissionalId,
            "profissionalNome": prof.nome if prof else "",
            "profissionalCro": prof.cro if prof else "",
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }


@app.delete("/api/documentos/{did}", status_code=204)
def delete_documento(did: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        d = db.get(DocumentoEmitido, did)
        if not d or d.clinicaId != quem.clinicaId:
            raise HTTPException(404)
        db.delete(d)
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
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
            raise HTTPException(404, "Profissional não encontrado")
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
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
            raise HTTPException(404, "Profissional não encontrado")
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
def list_surveys(request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        return [_row(s) for s in db.query(Pesquisa).filter(Pesquisa.clinicaId == quem.clinicaId).all()]


@app.post("/api/surveys", status_code=201)
def create_survey(data: PesquisaIn, request: Request):
    quem = _usuario_logado(request)
    sid = data.id or _new_id()
    with SessionLocal() as db:
        pac = db.get(Paciente, data.patientId)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado")
        db.query(Pesquisa).filter(Pesquisa.appointmentId == data.appointmentId, Pesquisa.clinicaId == quem.clinicaId).delete()
        s = Pesquisa(**{k: v for k, v in data.model_dump().items() if k != "id"}, id=sid, clinicaId=quem.clinicaId)
        db.add(s)
        db.commit()
        db.refresh(s)
        return _row(s)


@app.put("/api/surveys/{sid}")
def update_survey(sid: str, data: PesquisaIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        s = db.get(Pesquisa, sid)
        if not s or s.clinicaId != quem.clinicaId:
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
def list_tarefas(request: Request, status: str | None = None):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        q = db.query(Tarefa).filter(Tarefa.clinicaId == quem.clinicaId)
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
def create_tarefa(data: TarefaIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        if data.patientId:
            pac = db.get(Paciente, data.patientId)
            if not pac or pac.clinicaId != quem.clinicaId:
                raise HTTPException(404, "Paciente não encontrado")
        if not _prof_da_clinica(db, data.responsavelId, quem.clinicaId):
            raise HTTPException(404, "Responsável não encontrado")
        t = Tarefa(**data.model_dump(), clinicaId=quem.clinicaId)
        db.add(t)
        db.commit()
        db.refresh(t)
        return _row(t)


@app.put("/api/tarefas/{tid}")
def update_tarefa(tid: int, data: TarefaIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        t = db.get(Tarefa, tid)
        if not t or t.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Tarefa não encontrada")
        if not _prof_da_clinica(db, data.responsavelId, quem.clinicaId):
            raise HTTPException(404, "Responsável não encontrado")
        for k, v in data.model_dump().items():
            setattr(t, k, v)
        db.commit()
        db.refresh(t)
        return _row(t)


@app.delete("/api/tarefas/{tid}", status_code=204)
def delete_tarefa(tid: int, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        t = db.get(Tarefa, tid)
        if not t or t.clinicaId != quem.clinicaId:
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
def list_mensagens(request: Request, canal: str = "geral", limit: int = 100):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        rows = (
            db.query(MensagemChat)
            .filter(MensagemChat.canal == canal, MensagemChat.clinicaId == quem.clinicaId)
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
def send_mensagem(data: MensagemIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
            raise HTTPException(404, "Profissional não encontrado")
        m = MensagemChat(**data.model_dump(), clinicaId=quem.clinicaId)
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
def list_canais(request: Request):
    """Retorna canais únicos existentes na clínica + 'geral' sempre presente."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        rows = db.query(MensagemChat.canal).filter(MensagemChat.clinicaId == quem.clinicaId).distinct().all()
        canais = list({r[0] for r in rows} | {"geral"})
        return sorted(canais)


# ══════════════════════════════════════════════════════════════════════════════
# ANIVERSARIANTES DO DIA
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/aniversariantes")
def aniversariantes_hoje(request: Request):
    """Retorna pacientes que fazem aniversário hoje (MM-DD), da clínica do usuário logado."""
    quem = _usuario_logado(request)
    from datetime import date
    hoje = date.today().strftime("%m-%d")
    with SessionLocal() as db:
        pacientes = db.query(Paciente).filter(Paciente.birth.isnot(None), Paciente.clinicaId == quem.clinicaId).all()
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
def push_subscribe(data: PushSubIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
            raise HTTPException(404, "Profissional não encontrado")
        existing = db.query(PushSubscription).filter(PushSubscription.endpoint == data.endpoint).first()
        if existing:
            if existing.clinicaId != quem.clinicaId:
                raise HTTPException(409, "Este dispositivo já está inscrito em outra clínica.")
            existing.p256dh = data.keys.get("p256dh", "")
            existing.auth = data.keys.get("auth", "")
            existing.profissionalId = data.profissionalId
            existing.label = data.label
            existing.ativo = True
            db.commit()
            return {"ok": True, "updated": True}
        sub = PushSubscription(
            clinicaId=quem.clinicaId,
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
def push_unsubscribe(data: PushUnsubIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        db.query(PushSubscription).filter(
            PushSubscription.endpoint == data.endpoint, PushSubscription.clinicaId == quem.clinicaId
        ).delete()
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
def push_test(data: PushTestIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        q = db.query(PushSubscription).filter(PushSubscription.ativo == True, PushSubscription.clinicaId == quem.clinicaId)  # noqa: E712
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
    dentro do prazo configurado (notifLembreteMinutos) e dispara push, evitando duplicar.
    Processa CADA CLÍNICA separadamente — settings, consultas e inscrições push
    nunca se misturam entre clínicas."""
    with SessionLocal() as db:
        clinica_ids = [c.id for c in db.query(Clinica.id).filter(Clinica.ativa == True).all()]  # noqa: E712
        for cid in clinica_ids:
            ativas = db.get(Config, (cid, "notifAtivas"))
            if ativas and ativas.value == "false":
                continue
            lembrete_cfg = db.get(Config, (cid, "notifLembreteMinutos"))
            lembrete_min = int(lembrete_cfg.value) if lembrete_cfg and lembrete_cfg.value.isdigit() else 60

            agora = datetime.now()
            janela_fim = agora + timedelta(minutes=lembrete_min)
            janela_inicio = agora + timedelta(minutes=max(0, lembrete_min - 5))

            candidatas = db.query(Consulta).filter(
                Consulta.status.in_(["agendado", "confirmado"]), Consulta.clinicaId == cid
            ).all()
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

                subs_q = db.query(PushSubscription).filter(PushSubscription.ativo == True, PushSubscription.clinicaId == cid)  # noqa: E712
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

                db.add(NotificacaoEnviada(appointmentId=a.id, tipo="lembrete", clinicaId=cid))
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
ANEXO_CATEGORIAS = {"radiografia", "documento", "consentimento", "foto", "outro", "periapical", "facial", "elemento"}

# Levantamento radiográfico periapical — série completa de 14 películas
PERIAPICAL_POSICOES = {
    "sup_incisivos": "Incisivos superiores",
    "sup_canino_dir": "Canino superior direito",
    "sup_canino_esq": "Canino superior esquerdo",
    "sup_pm_dir": "Pré-molares superiores direito",
    "sup_molar_dir": "Molares superiores direito",
    "sup_pm_esq": "Pré-molares superiores esquerdo",
    "sup_molar_esq": "Molares superiores esquerdo",
    "inf_incisivos": "Incisivos inferiores",
    "inf_canino_dir": "Canino inferior direito",
    "inf_canino_esq": "Canino inferior esquerdo",
    "inf_pm_dir": "Pré-molares inferiores direito",
    "inf_molar_dir": "Molares inferiores direito",
    "inf_pm_esq": "Pré-molares inferiores esquerdo",
    "inf_molar_esq": "Molares inferiores esquerdo",
}

# Documentação fotográfica facial/intrabucal — 9 posições padrão
FACIAL_POSICOES = {
    "frontal_repouso": "Frontal em repouso",
    "frontal_sorrindo": "Frontal sorrindo",
    "perfil_direito": "Perfil direito",
    "perfil_esquerdo": "Perfil esquerdo",
    "frontal_oclusao": "Frontal em oclusão",
    "lateral_dir_oclusao": "Lateral direita em oclusão",
    "lateral_esq_oclusao": "Lateral esquerda em oclusão",
    "oclusal_superior": "Oclusal superior",
    "oclusal_inferior": "Oclusal inferior",
}


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


def _sb_signed_url_or_none(path: str, segundos: int = 300) -> str | None:
    """Como _sb_signed_url, mas retorna None em vez de lançar erro quando o
    objeto não existe. Usado para checar se algo (ex: assinatura) já foi
    salvo, sem precisar de uma coluna própria no banco pra marcar isso."""
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/sign/{ANEXO_BUCKET}/{path}",
        headers=_sb_headers("application/json"), json={"expiresIn": segundos}, timeout=30,
    )
    if r.status_code != 200:
        return None
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
    origemId: str | None = Form(None),
    dente: str | None = Form(None),
    face: str | None = Form(None),
    posicao: str | None = Form(None),
):
    quem = _usuario_logado(request)
    _sb_configurado()
    if categoria not in ANEXO_CATEGORIAS:
        categoria = "outro"
    # cada categoria de imagem clínica tem sua própria localização obrigatória —
    # fora dela, dente/face/posicao são descartados (não fazem sentido lá)
    if categoria == "periapical":
        if posicao not in PERIAPICAL_POSICOES:
            raise HTTPException(422, "Posição inválida na série periapical.")
        dente = None; face = None
    elif categoria == "facial":
        if posicao not in FACIAL_POSICOES:
            raise HTTPException(422, "Posição inválida na documentação facial.")
        dente = None; face = None
    elif categoria == "elemento":
        if dente not in DENTES_VALIDOS:
            raise HTTPException(422, "Número de dente inválido.")
        if face and face not in FACES_VALIDAS:
            raise HTTPException(422, "Face inválida.")
        face = face or "dente"; posicao = None
    else:
        dente = None; face = None; posicao = None
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
        if origemId:
            origem = db.get(Anexo, origemId)
            if not origem or origem.clinicaId != quem.clinicaId or origem.patientId != patientId:
                raise HTTPException(404, "Anexo original não encontrado.")
        aid = _new_id()
        nome = _nome_seguro(file.filename or "arquivo")
        path = f"{quem.clinicaId}/{patientId}/{aid}_{nome}"
        _sb_upload(path, conteudo, mime)
        a = Anexo(id=aid, clinicaId=quem.clinicaId, patientId=patientId, categoria=categoria, nome=file.filename or nome,
                  mimeType=mime, tamanho=len(conteudo), storagePath=path, origemId=origemId or None,
                  dente=dente, face=face, posicao=posicao)
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


class AnexoRenameIn(BaseModel):
    nome: str


@app.put("/api/anexos/{aid}")
def rename_anexo(aid: str, data: AnexoRenameIn, request: Request):
    """Renomeia um anexo (só o nome de exibição — o arquivo em si e seu
    caminho no storage não mudam)."""
    quem = _usuario_logado(request)
    novo_nome = data.nome.strip()
    if not novo_nome:
        raise HTTPException(422, "Informe um nome para o arquivo.")
    with SessionLocal() as db:
        a = db.get(Anexo, aid)
        if not a or a.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Anexo não encontrado.")
        a.nome = novo_nome
        db.commit()
        db.refresh(a)
        return {**_row(a), "created_at": a.created_at.isoformat() if a.created_at else None}


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
        comp_path = f"_despesas/{quem.clinicaId}/{did}_{_nome_seguro(comprovante.filename)}"
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
        path = f"_fotos/{quem.clinicaId}/{pid}.{ext}"
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
# ASSINATURAS DIGITAIS  (paciente e profissional; PNG do canvas, no Supabase Storage)
# ══════════════════════════════════════════════════════════════════════════════
# Caminho determinístico (um arquivo fixo por paciente/profissional) — dispensa
# qualquer coluna nova no banco: a existência do arquivo no Storage já diz se a
# assinatura foi coletada ou não.


def _path_assinatura(clinica_id: int, tipo: str, entidade_id) -> str:
    return f"_assinaturas/{clinica_id}/{tipo}/{entidade_id}.png"


@app.post("/api/patients/{pid}/assinatura", status_code=201)
def salvar_assinatura_paciente(pid: str, data: AssinaturaIn, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p or p.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
    raw = _decodificar_assinatura(data.dataUrl)
    _sb_upload(_path_assinatura(quem.clinicaId, "paciente", pid), raw, "image/png")
    return {"ok": True}


@app.get("/api/patients/{pid}/assinatura/url")
def get_assinatura_paciente(pid: str, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p or p.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
    url = _sb_signed_url_or_none(_path_assinatura(quem.clinicaId, "paciente", pid), segundos=600)
    if not url:
        raise HTTPException(404, "Sem assinatura registrada.")
    return {"url": url}


@app.delete("/api/patients/{pid}/assinatura", status_code=204)
def remover_assinatura_paciente(pid: str, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        p = db.get(Paciente, pid)
        if not p or p.clinicaId != quem.clinicaId:
            raise HTTPException(404)
    _sb_delete(_path_assinatura(quem.clinicaId, "paciente", pid))


@app.post("/api/profissionais/{prof_id}/assinatura", status_code=201)
def salvar_assinatura_profissional(prof_id: int, data: AssinaturaIn, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        prof = db.get(Profissional, prof_id)
        if not prof or prof.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Profissional não encontrado.")
    raw = _decodificar_assinatura(data.dataUrl)
    _sb_upload(_path_assinatura(quem.clinicaId, "profissional", prof_id), raw, "image/png")
    return {"ok": True}


@app.get("/api/profissionais/{prof_id}/assinatura/url")
def get_assinatura_profissional(prof_id: int, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        prof = db.get(Profissional, prof_id)
        if not prof or prof.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Profissional não encontrado.")
    url = _sb_signed_url_or_none(_path_assinatura(quem.clinicaId, "profissional", prof_id), segundos=600)
    if not url:
        raise HTTPException(404, "Sem assinatura registrada.")
    return {"url": url}


@app.delete("/api/profissionais/{prof_id}/assinatura", status_code=204)
def remover_assinatura_profissional(prof_id: int, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        prof = db.get(Profissional, prof_id)
        if not prof or prof.clinicaId != quem.clinicaId:
            raise HTTPException(404)
    _sb_delete(_path_assinatura(quem.clinicaId, "profissional", prof_id))


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
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
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
def get_odontograma(patient_id: str, request: Request, ate: str | None = None):
    """Estado do odontograma. Por padrão reflete a situação mais recente.
    Com `ate=YYYY-MM-DD`, reconstrói o estado como estava até o fim daquele
    dia — usa só o histórico até ali, sem descartar nada: o odontograma é
    registro imutável, então "voltar no tempo" é só filtrar o mesmo dado."""
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        todas = (
            db.query(OdontogramaMarca)
            .filter(OdontogramaMarca.patientId == patient_id, OdontogramaMarca.clinicaId == quem.clinicaId)
            .order_by(OdontogramaMarca.created_at.asc())
            .all()
        )
        datas_disponiveis = sorted({m.created_at.date().isoformat() for m in todas})

        rows = todas
        if ate:
            try:
                limite = datetime.strptime(ate, "%Y-%m-%d") + timedelta(days=1) - timedelta(seconds=1)
            except ValueError:
                raise HTTPException(422, "Data inválida.")
            rows = [m for m in todas if m.created_at <= limite]

        # estado na data de referência: última marcação vence, por (dente, face)
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

        profs = {p.id: p for p in db.query(Profissional).filter(Profissional.clinicaId == quem.clinicaId).all()}
        historico = []
        for m in reversed(rows[-200:]):  # últimas 200 até a data de referência, mais recente primeiro
            prof = profs.get(m.profissionalId)
            historico.append({
                "id": m.id, "dente": m.dente, "face": m.face, "status": m.status,
                "observacao": m.observacao, "profissionalNome": prof.nome if prof else "—",
                "created_at": m.created_at.isoformat(),
            })
        return {"estado": estado, "historico": historico, "datasDisponiveis": datas_disponiveis}


# ══════════════════════════════════════════════════════════════════════════════
# CONSENTIMENTO DE MÍDIA E DIVULGAÇÃO  (granular, indeterminado, registro imutável)
# ══════════════════════════════════════════════════════════════════════════════
# Cada tipo é uma autorização independente — LGPD pede consentimento
# específico por finalidade, não um "aceito tudo" genérico. Vale por prazo
# indeterminado; revogar não apaga nem edita nada, só soma um evento novo.
TIPOS_CONSENTIMENTO_MIDIA = {
    "fotos_video": "Fotos e vídeos clínicos, armazenados no prontuário",
    "radiografias": "Radiografias, armazenadas no prontuário",
    "divulgacao_cientifica": "Uso do caso para divulgação científica/educacional, sem identificação nominal",
    "divulgacao_procedimento": "Uso de imagem mostrando só o procedimento, sem mostrar rosto/identificação",
    "divulgacao_identificado": "Uso de imagem com identificação plena (rosto e nome), para divulgação/marketing",
}


class ConsentimentoMidiaIn(BaseModel):
    patientId: str
    profissionalId: int
    autorizados: list[str]  # tipos que devem ficar "autorizado"; os demais tipos válidos ficam "revogado"
    dataUrl: str            # assinatura/rubrica do paciente, PNG em base64


def _estado_consentimentos(rows: list) -> dict[str, str]:
    estado: dict[str, str] = {}
    for r in rows:  # rows já em ordem cronológica — a última de cada tipo vence
        estado[r.tipo] = r.status
    return estado


@app.get("/api/consentimentos")
def get_consentimentos(patient_id: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        pac = db.get(Paciente, patient_id)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        rows = (
            db.query(ConsentimentoMidia)
            .filter(ConsentimentoMidia.patientId == patient_id, ConsentimentoMidia.clinicaId == quem.clinicaId)
            .order_by(ConsentimentoMidia.created_at.asc())
            .all()
        )
        estado = _estado_consentimentos(rows)
        profs = {p.id: p for p in db.query(Profissional).filter(Profissional.clinicaId == quem.clinicaId).all()}
        historico = []
        for r in reversed(rows):
            prof = profs.get(r.profissionalId)
            historico.append({
                "id": r.id, "tipo": r.tipo, "status": r.status,
                "profissionalNome": prof.nome if prof else "—",
                "created_at": r.created_at.isoformat(),
            })
        return {"estado": estado, "historico": historico, "tipos": TIPOS_CONSENTIMENTO_MIDIA}


@app.post("/api/consentimentos", status_code=201)
def salvar_consentimentos(data: ConsentimentoMidiaIn, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    autorizados = set(data.autorizados) & set(TIPOS_CONSENTIMENTO_MIDIA.keys())
    with SessionLocal() as db:
        pac = db.get(Paciente, data.patientId)
        if not pac or pac.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Paciente não encontrado.")
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
            raise HTTPException(404, "Profissional não encontrado.")
        rows = (
            db.query(ConsentimentoMidia)
            .filter(ConsentimentoMidia.patientId == data.patientId, ConsentimentoMidia.clinicaId == quem.clinicaId)
            .order_by(ConsentimentoMidia.created_at.asc())
            .all()
        )
        estado_atual = _estado_consentimentos(rows)
        mudancas = []
        for tipo in TIPOS_CONSENTIMENTO_MIDIA:
            novo_status = "autorizado" if tipo in autorizados else "revogado"
            if estado_atual.get(tipo) != novo_status:
                mudancas.append((tipo, novo_status))
        if not mudancas:
            return {"ok": True, "alterado": False}
        raw = _decodificar_assinatura(data.dataUrl)
        evento_id = _new_id()
        path = f"_consentimentos/{quem.clinicaId}/{data.patientId}/{evento_id}.png"
        _sb_upload(path, raw, "image/png")
        for tipo, novo_status in mudancas:
            db.add(ConsentimentoMidia(
                id=_new_id(), clinicaId=quem.clinicaId, patientId=data.patientId,
                tipo=tipo, status=novo_status, assinaturaPath=path, profissionalId=data.profissionalId,
            ))
        db.commit()
        return {"ok": True, "alterado": True}


@app.get("/api/consentimentos/{cid}/assinatura/url")
def get_assinatura_consentimento(cid: str, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        c = db.get(ConsentimentoMidia, cid)
        if not c or c.clinicaId != quem.clinicaId or not c.assinaturaPath:
            raise HTTPException(404, "Sem assinatura.")
    url = _sb_signed_url_or_none(c.assinaturaPath, segundos=600)
    if not url:
        raise HTTPException(404, "Sem assinatura registrada.")
    return {"url": url}


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
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
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
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
            raise HTTPException(404, "Profissional não encontrado")
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
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
            raise HTTPException(404, "Profissional não encontrado")
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
    if data.status == "aprovado":
        raise HTTPException(409, "Aprovação exige assinatura do paciente e do profissional — use o fluxo de aprovação dedicado.")
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o or o.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Orçamento não encontrado")
        if o.status == "aprovado":
            raise HTTPException(409, "Orçamento já aprovado não pode voltar a rascunho/apresentado/recusado por aqui.")
        o.status = data.status
        db.commit()
        return _orc_row(db, o)


class AprovarOrcamentoIn(BaseModel):
    profissionalId: int
    assinaturaPaciente: str  # dataUrl base64 PNG
    assinaturaProfissional: str  # dataUrl base64 PNG


@app.post("/api/orcamentos/{oid}/aprovar", status_code=201)
def aprovar_orcamento(oid: str, data: AprovarOrcamentoIn, request: Request):
    """Aprova o orçamento E registra a prova de consentimento — rubrica do
    paciente e do profissional colhidas neste exato momento. É o único
    caminho que leva um orçamento a 'aprovado'."""
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o or o.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Orçamento não encontrado")
        if o.status == "aprovado":
            raise HTTPException(409, "Este orçamento já está aprovado.")
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
            raise HTTPException(404, "Profissional não encontrado.")
        raw_pac = _decodificar_assinatura(data.assinaturaPaciente)
        raw_prof = _decodificar_assinatura(data.assinaturaProfissional)
        cid = _new_id()
        path_pac = f"_consentimentos_orcamento/{quem.clinicaId}/{oid}/{cid}_paciente.png"
        path_prof = f"_consentimentos_orcamento/{quem.clinicaId}/{oid}/{cid}_profissional.png"
        _sb_upload(path_pac, raw_pac, "image/png")
        _sb_upload(path_prof, raw_prof, "image/png")
        o.status = "aprovado"
        db.add(ConsentimentoOrcamento(
            id=cid, clinicaId=quem.clinicaId, orcamentoId=oid, patientId=o.patientId,
            profissionalId=data.profissionalId,
            assinaturaPacientePath=path_pac, assinaturaProfissionalPath=path_prof,
        ))
        db.commit()
        return _orc_row(db, o)


@app.get("/api/orcamentos/{oid}/consentimento")
def get_consentimento_orcamento(oid: str, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        o = db.get(Orcamento, oid)
        if not o or o.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Orçamento não encontrado")
        c = (
            db.query(ConsentimentoOrcamento)
            .filter(ConsentimentoOrcamento.orcamentoId == oid, ConsentimentoOrcamento.clinicaId == quem.clinicaId)
            .order_by(ConsentimentoOrcamento.created_at.desc())
            .first()
        )
        if not c:
            return {"existe": False}
        prof = db.get(Profissional, c.profissionalId)
        return {
            "existe": True, "id": c.id,
            "profissionalNome": prof.nome if prof else "—",
            "created_at": c.created_at.isoformat(),
        }


@app.get("/api/consentimentos-orcamento/{cid}/assinatura-paciente/url")
def get_assinatura_paciente_orcamento(cid: str, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        c = db.get(ConsentimentoOrcamento, cid)
        if not c or c.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Não encontrado.")
    url = _sb_signed_url_or_none(c.assinaturaPacientePath, segundos=600)
    if not url:
        raise HTTPException(404, "Sem assinatura registrada.")
    return {"url": url}


@app.get("/api/consentimentos-orcamento/{cid}/assinatura-profissional/url")
def get_assinatura_profissional_orcamento(cid: str, request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    with SessionLocal() as db:
        c = db.get(ConsentimentoOrcamento, cid)
        if not c or c.clinicaId != quem.clinicaId:
            raise HTTPException(404, "Não encontrado.")
    url = _sb_signed_url_or_none(c.assinaturaProfissionalPath, segundos=600)
    if not url:
        raise HTTPException(404, "Sem assinatura registrada.")
    return {"url": url}


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
            # Cooldown: convite enviado há menos de 30 dias sai da fila
            if cfg and cfg.sentAt and cfg.sentAt > (date.today() - timedelta(days=30)).isoformat():
                continue
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
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
            raise HTTPException(404, "Profissional não encontrado")
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
        if not _prof_da_clinica(db, data.profissionalId, quem.clinicaId):
            raise HTTPException(404, "Profissional não encontrado")
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
def _dump_data(db, clinica_id: int) -> dict:
    return {
        "patients": [_row(p) for p in db.query(Paciente).filter(Paciente.clinicaId == clinica_id).all()],
        "appointments": [_row(a) for a in db.query(Consulta).filter(Consulta.clinicaId == clinica_id).all()],
        "surveys": [_row(s) for s in db.query(Pesquisa).filter(Pesquisa.clinicaId == clinica_id).all()],
        "profissionais": [_row(p) for p in db.query(Profissional).filter(Profissional.clinicaId == clinica_id).all()],
        "tarefas": [_row(t) for t in db.query(Tarefa).filter(Tarefa.clinicaId == clinica_id).all()],
        "lancamentos": [_row(l) for l in db.query(Lancamento).filter(Lancamento.clinicaId == clinica_id).all()],
        "evolucoes": [{**_row(e), "created_at": e.created_at.isoformat() if e.created_at else None}
                      for e in db.query(Evolucao).filter(Evolucao.clinicaId == clinica_id).all()],
        "orcamentos": [_row(o) for o in db.query(Orcamento).filter(Orcamento.clinicaId == clinica_id).all()],
        "orcamento_itens": [_row(i) for i in db.query(OrcamentoItem).filter(OrcamentoItem.clinicaId == clinica_id).all()],
        "anexos": [{**_row(a), "created_at": a.created_at.isoformat() if a.created_at else None}
                   for a in db.query(Anexo).filter(Anexo.clinicaId == clinica_id).all()],
        "retorno_config": [_row(r) for r in db.query(RetornoConfig).filter(RetornoConfig.clinicaId == clinica_id).all()],
        "despesas": [{**_row(d), "created_at": d.created_at.isoformat() if d.created_at else None}
                     for d in db.query(Despesa).filter(Despesa.clinicaId == clinica_id).all()],
        "settings": {c.key: c.value for c in db.query(Config).filter(Config.clinicaId == clinica_id).all()},
    }


@app.get("/api/dump")
def dump(request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        return _dump_data(db, quem.clinicaId)


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


BACKUP_RETENCAO = 30  # mantém os 30 backups mais recentes, por clínica


def _fazer_backup(clinica_id: int) -> str:
    """Gera o dump de UMA clínica e envia ao Supabase Storage, em pasta própria.
    Retorna o nome do arquivo."""
    with SessionLocal() as db:
        data = _dump_data(db, clinica_id)
    blob = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    nome = f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    _sb_upload(f"_backups/{clinica_id}/{nome}", blob, "application/json")
    for velho in _sb_list(f"_backups/{clinica_id}")[BACKUP_RETENCAO:]:
        _sb_delete(f"_backups/{clinica_id}/{velho}")
    return nome


def backup_automatico():
    """Job diário do scheduler. Roda uma vez POR CLÍNICA ativa, cada uma na sua
    própria pasta no Storage. Silencioso: nunca derruba a aplicação."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return
    try:
        with SessionLocal() as db:
            clinica_ids = [c.id for c in db.query(Clinica.id).filter(Clinica.ativa == True).all()]  # noqa: E712
        for cid in clinica_ids:
            try:
                _fazer_backup(cid)
            except Exception:
                pass  # uma clínica falhando não deve travar as demais
    except Exception:
        pass


@app.post("/api/backup/agora", status_code=201)
def backup_agora(request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    nome = _fazer_backup(quem.clinicaId)
    return {"ok": True, "arquivo": nome}


@app.get("/api/backups")
def list_backups(request: Request):
    quem = _usuario_logado(request)
    _sb_configurado()
    return {"arquivos": _sb_list(f"_backups/{quem.clinicaId}")[:BACKUP_RETENCAO]}


class DumpIn(BaseModel):
    patients: list[dict] = []
    appointments: list[dict] = []
    surveys: list[dict] = []
    lancamentos: list[dict] = []
    settings: dict[str, Any] = {}


@app.post("/api/import")
def import_dump(data: DumpIn, request: Request):
    quem = _usuario_logado(request)
    with SessionLocal() as db:
        for p in data.patients:
            if not db.get(Paciente, p["id"]):
                db.add(Paciente(**{k: v for k, v in p.items() if hasattr(Paciente, k) and k != "clinicaId"}, clinicaId=quem.clinicaId))
        for a in data.appointments:
            if not db.get(Consulta, a["id"]):
                db.add(Consulta(**{k: v for k, v in a.items() if hasattr(Consulta, k) and k != "clinicaId"}, clinicaId=quem.clinicaId))
        for s in data.surveys:
            if not db.get(Pesquisa, s["id"]):
                db.add(Pesquisa(**{k: v for k, v in s.items() if hasattr(Pesquisa, k) and k != "clinicaId"}, clinicaId=quem.clinicaId))
        for l in data.lancamentos:
            if not db.get(Lancamento, l["id"]):
                db.add(Lancamento(**{k: v for k, v in l.items() if hasattr(Lancamento, k) and k != "clinicaId"}, clinicaId=quem.clinicaId))
        for k, v in data.settings.items():
            cfg = db.get(Config, (quem.clinicaId, k))
            if cfg:
                cfg.value = str(v)
            else:
                db.add(Config(clinicaId=quem.clinicaId, key=k, value=str(v)))
        db.commit()
    return {"ok": True, "patients": len(data.patients), "appointments": len(data.appointments)}
