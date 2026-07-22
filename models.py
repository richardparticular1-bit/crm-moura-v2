"""Modelos do banco do CRM — versão 2.3 (+ identidade da clínica e marca da plataforma)"""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String, Text, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Profissional(Base):
    __tablename__ = "profissionais"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
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
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
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
    # ── identificação civil (v2.7) ──
    rg: Mapped[str] = mapped_column(String(30), default="")
    orgaoExpedidor: Mapped[str] = mapped_column(String(20), default="")
    cpf: Mapped[str] = mapped_column(String(20), default="")
    naturalidade: Mapped[str] = mapped_column(String(100), default="")
    nacionalidade: Mapped[str] = mapped_column(String(60), default="")
    estadoCivil: Mapped[str] = mapped_column(String(30), default="")
    profissao: Mapped[str] = mapped_column(String(100), default="")
    localTrabalho: Mapped[str] = mapped_column(String(150), default="")
    enderecoResidencial: Mapped[str] = mapped_column(String(250), default="")
    indicadoPor: Mapped[str] = mapped_column(String(150), default="")
    # ── responsável legal (paciente menor) ──
    respNome: Mapped[str] = mapped_column(String(150), default="")
    respRg: Mapped[str] = mapped_column(String(30), default="")
    respCpf: Mapped[str] = mapped_column(String(20), default="")
    respTelefone: Mapped[str] = mapped_column(String(30), default="")
    respEmail: Mapped[str] = mapped_column(String(120), default="")
    # ── alertas clínicos ──
    alergias: Mapped[str] = mapped_column(Text, default="")
    medicacoes: Mapped[str] = mapped_column(Text, default="")
    condicoesSistemicas: Mapped[str] = mapped_column(Text, default="")  # ex: "Diabetes, Hipertensão"
    # ── foto ──
    fotoPath: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Token opaco e imprevisível pro portal público do paciente (não é o id
    # interno — o id é previsível/sequencial e nunca deve abrir uma página
    # sem login). Gerado sob demanda na primeira vez que a carteirinha é
    # emitida; None até lá.
    tokenPortal: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Consulta(Base):
    __tablename__ = "appointments"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
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
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
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
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
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
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profissionalId: Mapped[int] = mapped_column(Integer, ForeignKey("profissionais.id"))
    canal: Mapped[str] = mapped_column(String(60), default="geral")
    conteudo: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Clinica(Base):
    """Tenant do sistema multi-clínica. Cada clínica enxerga apenas seus próprios dados."""
    __tablename__ = "clinicas"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(150))
    slug: Mapped[str] = mapped_column(String(60), default="")
    plano: Mapped[str] = mapped_column(String(30), default="trial")
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # ── identidade e dados legais do consultório (v2.3) — usados no cabeçalho do
    # app, na anamnese impressa e em qualquer documento que precise identificar
    # legalmente a clínica. logoPath aponta pro Supabase Storage (mesmo padrão
    # de fotoPath em Paciente); os demais são texto livre preenchido no cadastro
    # ou em Ajustes > Perfil do Consultório.
    logoPath: Mapped[str | None] = mapped_column(String(300), nullable=True)
    responsavelTecnico: Mapped[str] = mapped_column(String(150), default="")  # dentista responsável técnico
    croResponsavel: Mapped[str] = mapped_column(String(30), default="")       # CRO do responsável técnico
    cnpj: Mapped[str] = mapped_column(String(20), default="")
    enderecoCompleto: Mapped[str] = mapped_column(String(300), default="")
    telefoneWhatsapp: Mapped[str] = mapped_column(String(30), default="")
    email: Mapped[str] = mapped_column(String(120), default="")              # e-mail institucional da clínica
    cidade: Mapped[str] = mapped_column(String(60), default="")   # usado no payload do Pix (campo "merchant city")
    chavePix: Mapped[str] = mapped_column(String(140), default="")  # CPF/CNPJ/e-mail/telefone/chave aleatória


class Plataforma(Base):
    """Marca neutra da plataforma (ex: OdontoDesk), exibida na tela de login
    ANTES do usuário se autenticar — quando o sistema ainda não sabe de qual
    clínica se trata. Linha única, id sempre 1. Editável só por superadmin."""
    __tablename__ = "plataforma"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    nome: Mapped[str] = mapped_column(String(150), default="OdontoDesk")
    logoPath: Mapped[str | None] = mapped_column(String(300), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class Config(Base):
    __tablename__ = "settings"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
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
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    appointmentId: Mapped[str] = mapped_column(String(20))
    tipo: Mapped[str] = mapped_column(String(30))  # ex: "lembrete_60min", "lembrete_dia"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Lancamento(Base):
    """Módulo financeiro: cada linha é uma cobrança (à vista ou uma parcela).
    IMPORTANTE: valor é armazenado em CENTAVOS (inteiro) para evitar erros de
    arredondamento de ponto flutuante. R$ 150,00 => 15000."""
    __tablename__ = "lancamentos"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
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
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
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
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
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
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    orcamentoId: Mapped[str] = mapped_column(String(20), ForeignKey("orcamentos.id"))
    procedimento: Mapped[str] = mapped_column(String(150))
    denteRegiao: Mapped[str] = mapped_column(String(60), default="")
    quantidade: Mapped[int] = mapped_column(Integer, default=1)
    valor: Mapped[int] = mapped_column(Integer, default=0)  # CENTAVOS, unitário


class Usuario(Base):
    """Usuário do sistema. Senha SEMPRE armazenada como hash bcrypt."""
    __tablename__ = "usuarios"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nome: Mapped[str] = mapped_column(String(150))
    email: Mapped[str] = mapped_column(String(120), unique=True)
    senhaHash: Mapped[str] = mapped_column(String(100))
    profissionalId: Mapped[int | None] = mapped_column(Integer, ForeignKey("profissionais.id"), nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    isSuperAdmin: Mapped[bool] = mapped_column(Boolean, default=False)  # acesso multi-clínica (plataforma)
    emailVerificado: Mapped[bool] = mapped_column(Boolean, default=True)  # False só para cadastros públicos (Fase 4)
    emailVerifyToken: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Sessao(Base):
    """Sessão de login: token opaco com validade. Logout = excluir a linha."""
    __tablename__ = "sessoes"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    usuarioId: Mapped[int] = mapped_column(Integer, ForeignKey("usuarios.id"))
    expiresAt: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Anexo(Base):
    """Arquivo anexado ao prontuário do paciente (radiografia, documento,
    consentimento assinado, foto). O binário vive no Supabase Storage
    (bucket privado); aqui fica só o metadado + caminho."""
    __tablename__ = "anexos"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patientId: Mapped[str] = mapped_column(String(20))
    categoria: Mapped[str] = mapped_column(String(30), default="documento")  # radiografia | documento | consentimento | foto | outro
    nome: Mapped[str] = mapped_column(String(200))          # nome original do arquivo
    mimeType: Mapped[str] = mapped_column(String(100))
    tamanho: Mapped[int] = mapped_column(Integer, default=0)  # bytes
    storagePath: Mapped[str] = mapped_column(String(300))   # caminho no bucket
    # Se este anexo é uma CÓPIA EDITADA (recorte, marcação, ajuste) de outro
    # anexo já existente, origemId aponta pro original. O original nunca é
    # sobrescrito — toda edição nasce como um anexo novo e independente,
    # preservando o arquivo-fonte intacto para fins legais/diagnósticos.
    origemId: Mapped[str | None] = mapped_column(String(20), ForeignKey("anexos.id"), nullable=True)
    # Campos opcionais usados só por categorias específicas de imagem clínica:
    # - categoria="elemento": dente (e opcionalmente face) identificam de qual
    #   dente/face é a foto — mesmo vocabulário do odontograma (face="dente"
    #   quando a foto é do dente inteiro, não de uma face específica).
    # - categoria="periapical" / "facial": posicao identifica o slot fixo da
    #   grade (ex: "sup_canino_dir", "perfil_direito").
    dente: Mapped[str | None] = mapped_column(String(3), nullable=True)
    face: Mapped[str | None] = mapped_column(String(20), nullable=True)
    posicao: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RetornoConfig(Base):
    """Retorno preventivo por paciente. meses=None usa o padrão da clínica.
    Tabela separada (em vez de coluna em patients) para nascer via create_all
    sem SQL manual no Supabase."""
    __tablename__ = "retorno_config"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
    patientId: Mapped[str] = mapped_column(String(20), primary_key=True)
    meses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sentAt: Mapped[str | None] = mapped_column(String(10), nullable=True)  # último convite de retorno enviado


class Despesa(Base):
    """Gasto do consultório (aluguel, material, laboratório, salário...).
    Comprovante opcional no Supabase Storage (mesma infra dos anexos)."""
    __tablename__ = "despesas"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    categoria: Mapped[str] = mapped_column(String(30), default="outro")
    descricao: Mapped[str] = mapped_column(String(200), default="")
    valor: Mapped[int] = mapped_column(Integer, default=0)  # CENTAVOS
    data: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    comprovanteNome: Mapped[str | None] = mapped_column(String(200), nullable=True)
    comprovanteMime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    comprovantePath: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class OdontogramaMarca(Base):
    """Odontograma: registro clínico IMUTÁVEL por dente/face (mesmo princípio das
    evoluções — corrigir é registrar uma marcação nova, não editar a antiga).
    face='dente' representa o dente inteiro (ausente, implante, coroa, canal...);
    demais valores de face são áreas específicas (oclusal/vestibular/lingual/mesial/distal)."""
    __tablename__ = "odontograma_marcas"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patientId: Mapped[str] = mapped_column(String(20))
    profissionalId: Mapped[int] = mapped_column(Integer, ForeignKey("profissionais.id"))
    dente: Mapped[str] = mapped_column(String(3))   # "11".."48" (permanentes), "51".."85" (decíduos)
    face: Mapped[str] = mapped_column(String(20))   # oclusal | vestibular | lingual | mesial | distal | dente
    status: Mapped[str] = mapped_column(String(20))
    observacao: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ConsentimentoMidia(Base):
    """Consentimento do paciente para usos de mídia clínica (fotos/vídeos,
    radiografias, divulgação científica ou de marketing). Registro IMUTÁVEL,
    um por tipo de autorização — revogar não edita a linha anterior, cria uma
    nova com status='revogado'; o estado vigente de cada tipo é sempre a
    linha mais recente daquele tipo. Vale por prazo indeterminado até ser
    revogado; não expira sozinho."""
    __tablename__ = "consentimentos_midia"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    patientId: Mapped[str] = mapped_column(String(20))
    tipo: Mapped[str] = mapped_column(String(40))    # ver TIPOS_CONSENTIMENTO_MIDIA no main.py
    status: Mapped[str] = mapped_column(String(20))  # autorizado | revogado
    assinaturaPath: Mapped[str | None] = mapped_column(String(300), nullable=True)  # rubrica daquele evento, no Storage
    profissionalId: Mapped[int] = mapped_column(Integer, ForeignKey("profissionais.id"))  # quem colheu
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ConsentimentoOrcamento(Base):
    """Prova de que o paciente concordou com um plano de tratamento e seu
    valor — rubrica do paciente E do profissional coletadas no momento da
    aprovação do orçamento. Registro IMUTÁVEL, um por aprovação; não existe
    edição nem reaprovação — um orçamento recusado e reapresentado gera um
    orçamento novo, com seu próprio consentimento."""
    __tablename__ = "consentimentos_orcamento"
    clinicaId: Mapped[int] = mapped_column(Integer, ForeignKey("clinicas.id"))
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    orcamentoId: Mapped[str] = mapped_column(String(20), ForeignKey("orcamentos.id"))
    patientId: Mapped[str] = mapped_column(String(20))
    profissionalId: Mapped[int] = mapped_column(Integer, ForeignKey("profissionais.id"))
    assinaturaPacientePath: Mapped[str] = mapped_column(String(300))
    assinaturaProfissionalPath: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
