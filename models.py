"""Modelos do banco do CRM — versão 2.0"""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Profissional(Base):
    __tablename__ = "profissionais"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(150))
    cro: Mapped[str] = mapped_column(String(30), default="")
    especialidade: Mapped[str] = mapped_column(String(100), default="")
    telefone: Mapped[str] = mapped_column(String(30), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    cor: Mapped[str] = mapped_column(String(20), default="#0ea5e9")  # cor na agenda
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Paciente(Base):
    __tablename__ = "patients"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    phone: Mapped[str] = mapped_column(String(30), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    birth: Mapped[str | None] = mapped_column(String(10), nullable=True)
    lastVisit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    reactivateSentAt: Mapped[str | None] = mapped_column(String(10), nullable=True)
    birthdaySentYear: Mapped[str | None] = mapped_column(String(4), nullable=True)  # ano do último envio de aniversário
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Consulta(Base):
    __tablename__ = "appointments"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patientId: Mapped[str] = mapped_column(String(20))
    profissionalId: Mapped[int | None] = mapped_column(Integer, ForeignKey("profissionais.id"), nullable=True)
    numProntuario: Mapped[str] = mapped_column(String(30), default="")
    numOrcamento: Mapped[str] = mapped_column(String(30), default="")
    date: Mapped[str] = mapped_column(String(10))
    time: Mapped[str] = mapped_column(String(5))
    duracaoMinutos: Mapped[int] = mapped_column(Integer, default=60)
    procedure: Mapped[str] = mapped_column(String(120), default="Avaliação")
    status: Mapped[str] = mapped_column(String(20), default="agendado")
    confirmSent: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Pesquisa(Base):
    __tablename__ = "surveys"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    appointmentId: Mapped[str] = mapped_column(String(20))
    patientId: Mapped[str] = mapped_column(String(20))
    sentAt: Mapped[str | None] = mapped_column(String(10), nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewSent: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewSentAt: Mapped[str | None] = mapped_column(String(10), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Tarefa(Base):
    __tablename__ = "tarefas"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    titulo: Mapped[str] = mapped_column(String(200))
    descricao: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="pendente")   # pendente | em_andamento | concluida
    prioridade: Mapped[str] = mapped_column(String(20), default="normal")  # baixa | normal | alta
    responsavelId: Mapped[int | None] = mapped_column(Integer, ForeignKey("profissionais.id"), nullable=True)
    patientId: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dataVencimento: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class MensagemChat(Base):
    __tablename__ = "chat_mensagens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profissionalId: Mapped[int] = mapped_column(Integer, ForeignKey("profissionais.id"))
    canal: Mapped[str] = mapped_column(String(60), default="geral")
    conteudo: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Config(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")

