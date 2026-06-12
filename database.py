"""Conexão com o banco SQLite do CRM."""
import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base

# Na nuvem (Render) usa /tmp; localmente usa a pasta do projeto
if os.environ.get("RENDER"):
    DB_PATH = Path("/tmp/crm.db")
else:
    DB_PATH = Path(__file__).parent / "crm.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

DEFAULT_SETTINGS = {
    "googleReviewLink": "https://g.page/r/SEU_LINK_AQUI/review",
    "inactivityMonths": "6",
    "msgConfirm": "Olá {nome}! Aqui é da Moura Odontologia e Associados. 😊 Passando para confirmar sua consulta de {procedimento} no dia {data} às {hora}. Podemos confirmar sua presença? Responda SIM para confirmar ou nos avise se precisar remarcar.",
    "msgReactivate": "Olá {nome}! Aqui é da Moura Odontologia e Associados. Sentimos sua falta! 🦷 Sua última visita foi em {ultimaVisita} e é importante manter a saúde bucal em dia. Que tal agendar uma avaliação? Temos horários disponíveis esta semana!",
    "msgSurvey": "Olá {nome}! Obrigado por sua visita à Moura Odontologia e Associados. 💙 Sua opinião é muito importante: de 0 a 10, qual a chance de você nos recomendar a um amigo ou familiar? É só responder com a nota!",
    "msgReview": "Olá {nome}! Que alegria saber que você teve uma ótima experiência conosco! 🌟 Você nos ajudaria muito deixando uma avaliação no Google? Leva menos de 1 minuto: {link}",
}


def init_db():
    Base.metadata.create_all(bind=engine)
    # Insere configurações padrão se não existirem
    from models import Config
    with SessionLocal() as db:
        for k, v in DEFAULT_SETTINGS.items():
            if not db.get(Config, k):
                db.add(Config(key=k, value=v))
        db.commit()
