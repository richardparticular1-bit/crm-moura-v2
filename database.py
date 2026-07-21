"""Conexão com o banco do CRM."""
import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Segurança: no Render, subir sem DATABASE_URL significaria usar SQLite em /tmp,
# que é apagado a cada restart/deploy — perda silenciosa de dados de produção.
# Melhor o deploy quebrar na hora do que perder prontuário de paciente.
if os.environ.get("RENDER") and not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL não configurada no Render — "
        "os dados seriam perdidos a cada restart."
    )

if DATABASE_URL:
    # pool_pre_ping: testa a conexão antes de usar, evitando erros esporádicos
    # de "server closed the connection unexpectedly" com o pooler do Supabase.
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    print(f"[DB] Usando Postgres: {DATABASE_URL.split('@')[-1]}")  # só host, sem senha
else:
    if os.environ.get("RENDER"):
        DB_PATH = Path("/tmp/crm.db")
    else:
        DB_PATH = Path(__file__).parent / "crm.db"
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
    print(f"[DB] Usando SQLite em {DB_PATH}")

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

DEFAULT_SETTINGS = {
    "googleReviewLink": "https://g.page/r/SEU_LINK_AQUI/review",
    "inactivityMonths": "6",
    "msgConfirm": "Olá {nome}! Aqui é da Moura Odontologia e Associados. 😊 Passando para confirmar sua consulta de {procedimento} no dia {data} às {hora}. Podemos confirmar sua presença? Responda SIM para confirmar ou nos avise se precisar remarcar.",
    "msgReactivate": "Olá {nome}! Aqui é da Moura Odontologia e Associados. Sentimos sua falta! 🦷 Sua última visita foi em {ultimaVisita} e é importante manter a saúde bucal em dia. Que tal agendar uma avaliação? Temos horários disponíveis esta semana!",
    "msgSurvey": "Olá {nome}! Obrigado por sua visita à Moura Odontologia e Associados. 💙 Sua opinião é muito importante: de 0 a 10, qual a chance de você nos recomendar a um amigo ou familiar? É só responder com a nota!",
    "msgReview": "Olá {nome}! Que alegria saber que você teve uma ótima experiência conosco! 🌟 Você nos ajudaria muito deixando uma avaliação no Google? Leva menos de 1 minuto: {link}",
    "msgAniversario": "Olá {nome}! 🎂 A equipe da Moura Odontologia e Associados deseja a você um feliz aniversário! Que este novo ano seja repleto de saúde e sorrisos bonitos. Parabéns! 🎉",
    "notifLembreteMinutos": "60",
    "notifAtivas": "true",
}


def init_db():
    Base.metadata.create_all(bind=engine)
    # Multi-tenant: settings agora tem chave composta (clinicaId, key).
    # Semeia os padrões para cada clínica existente que ainda não os tenha.
    # Se ainda não existe nenhuma clínica (sistema virgem, antes do setup),
    # simplesmente pula — o setup criará a clínica e o próximo boot semeia.
    from models import Clinica, Config
    with SessionLocal() as db:
        clinicas = db.query(Clinica.id).all()
        for (cid,) in clinicas:
            for k, v in DEFAULT_SETTINGS.items():
                if not db.get(Config, (cid, k)):
                    db.add(Config(clinicaId=cid, key=k, value=v))
        db.commit()
