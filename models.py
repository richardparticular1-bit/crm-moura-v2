"""Modelos do banco do CRM — versão 2.2 (+ módulo financeiro)"""
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
    numProntuario: Mapped[str] = mapped_column(String(30), default="")
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


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(255))
    profissionalId: Mapped[int | None] = mapped_column(Integer, ForeignKey("profissionais.id"), nullable=True)
    label: Mapped[str] = mapped_column(String(100), default="")  # ex: "Celular de Richard"
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class NotificacaoEnviada(Base):
    """Registra que já notificamos uma consulta em determinado lead time, para não duplicar envios."""
    __tablename__ = "notificacoes_enviadas"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    appointmentId: Mapped[str] = mapped_column(String(20))
    tipo: Mapped[str] = mapped_column(String(30))  # ex: "lembrete_60min", "lembrete_dia"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Lancamento(Base):
    """Módulo financeiro: cada linha é uma cobrança (à vista ou uma parcela).
    IMPORTANTE: valor é armazenado em CENTAVOS (inteiro) para evitar erros de
    arredondamento de ponto flutuante. R$ 150,00 => 15000."""
    __tablename__ = "lancamentos"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patientId: Mapped[str] = mapped_column(String(20))
    profissionalId: Mapped[int | None] = mapped_column(Integer, ForeignKey("profissionais.id"), nullable=True)
    appointmentId: Mapped[str | None] = mapped_column(String(20), nullable=True)  # consulta de origem, se houver
    descricao: Mapped[str] = mapped_column(String(200), default="")
    valor: Mapped[int] = mapped_column(Integer, default=0)  # CENTAVOS
    vencimento: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    pagoEm: Mapped[str | None] = mapped_column(String(10), nullable=True)  # null = em aberto
    formaPagamento: Mapped[str] = mapped_column(String(30), default="")  # dinheiro | pix | cartao_credito | cartao_debito | boleto | outro
    numOrcamento: Mapped[str] = mapped_column(String(30), default="")
    observacoes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Evolucao(Base):
    """Prontuário digital: evolução clínica IMUTÁVEL.
    Registro legal — não existe endpoint de edição nem exclusão.
    Erros são corrigidos com uma nova evolução de retificação."""
    __tablename__ = "evolucoes"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patientId: Mapped[str] = mapped_column(String(20))
    profissionalId: Mapped[int] = mapped_column(Integer, ForeignKey("profissionais.id"))  # autor legal, obrigatório
    denteRegiao: Mapped[str] = mapped_column(String(60), default="")   # ex: "2MID", "arco superior"
    procedimento: Mapped[str] = mapped_column(String(120), default="")
    conteudo: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Orcamento(Base):
    """Plano de tratamento orçado. Total = soma dos itens.
    Fluxo de status: rascunho -> apresentado -> aprovado | recusado."""
    __tablename__ = "orcamentos"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patientId: Mapped[str] = mapped_column(String(20))
    profissionalId: Mapped[int | None] = mapped_column(Integer, ForeignKey("profissionais.id"), nullable=True)
    data: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    status: Mapped[str] = mapped_column(String(20), default="rascunho")  # rascunho | apresentado | aprovado | recusado
    observacoes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class OrcamentoItem(Base):
    __tablename__ = "orcamento_itens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    orcamentoId: Mapped[str] = mapped_column(String(20), ForeignKey("orcamentos.id"))
    procedimento: Mapped[str] = mapped_column(String(150))
    denteRegiao: Mapped[str] = mapped_column(String(60), default="")
    quantidade: Mapped[int] = mapped_column(Integer, default=1)
    valor: Mapped[int] = mapped_column(Integer, default=0)  # CENTAVOS, unitário


class Usuario(Base):
    """Usuário do sistema. Senha SEMPRE armazenada como hash bcrypt."""
    __tablename__ = "usuarios"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(150))
    email: Mapped[str] = mapped_column(String(120), unique=True)
    senhaHash: Mapped[str] = mapped_column(String(100))
    profissionalId: Mapped[int | None] = mapped_column(Integer, ForeignKey("profissionais.id"), nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Sessao(Base):
    """Sessão de login: token opaco com validade. Logout = excluir a linha."""
    __tablename__ = "sessoes"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    usuarioId: Mapped[int] = mapped_column(Integer, ForeignKey("usuarios.id"))
    expiresAt: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
