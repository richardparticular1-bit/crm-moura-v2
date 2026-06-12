"""Modelos do banco SQLite do CRM."""
from datetime import date, datetime
from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


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
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Consulta(Base):
    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patientId: Mapped[str] = mapped_column(String(20))
    date: Mapped[str] = mapped_column(String(10))
    time: Mapped[str] = mapped_column(String(5))
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


class Config(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
