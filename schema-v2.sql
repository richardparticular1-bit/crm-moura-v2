
CREATE TABLE crm_v2.clinicas (
	id SERIAL NOT NULL, 
	nome VARCHAR(150) NOT NULL, 
	slug VARCHAR(60) NOT NULL, 
	plano VARCHAR(30) NOT NULL, 
	ativa BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	"logoPath" VARCHAR(300), 
	"responsavelTecnico" VARCHAR(150) NOT NULL, 
	"croResponsavel" VARCHAR(30) NOT NULL, 
	"croUf" VARCHAR(4) NOT NULL, 
	cnpj VARCHAR(20) NOT NULL, 
	"enderecoCompleto" VARCHAR(300) NOT NULL, 
	"telefoneWhatsapp" VARCHAR(30) NOT NULL, 
	email VARCHAR(120) NOT NULL, 
	cidade VARCHAR(60) NOT NULL, 
	"chavePix" VARCHAR(140) NOT NULL, 
	"tipoChavePix" VARCHAR(20) NOT NULL, 
	"tokenInfo" VARCHAR(64), 
	PRIMARY KEY (id), 
	UNIQUE ("tokenInfo")
)

;
ALTER TABLE crm_v2."clinicas" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."clinicas" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.planos_saas (
	id VARCHAR(20) NOT NULL, 
	nome VARCHAR(60) NOT NULL, 
	valor INTEGER NOT NULL, 
	"limitePacientes" INTEGER, 
	"limiteProfissionais" INTEGER, 
	ativo BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)

;
ALTER TABLE crm_v2."planos_saas" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."planos_saas" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.plataforma (
	id INTEGER NOT NULL, 
	nome VARCHAR(150) NOT NULL, 
	"logoPath" VARCHAR(300), 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)

;
ALTER TABLE crm_v2."plataforma" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."plataforma" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.anamnese_modelos (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	nome VARCHAR(100) NOT NULL, 
	ativo BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."anamnese_modelos" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."anamnese_modelos" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.anexos (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	categoria VARCHAR(30) NOT NULL, 
	nome VARCHAR(200) NOT NULL, 
	"mimeType" VARCHAR(100) NOT NULL, 
	tamanho INTEGER NOT NULL, 
	"storagePath" VARCHAR(300) NOT NULL, 
	"origemId" VARCHAR(20), 
	dente VARCHAR(3), 
	face VARCHAR(20), 
	posicao VARCHAR(40), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("origemId") REFERENCES crm_v2.anexos (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."anexos" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."anexos" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.assinaturas_clinica (
	"clinicaId" INTEGER NOT NULL, 
	"planoId" VARCHAR(20), 
	status VARCHAR(20) NOT NULL, 
	"mpPreapprovalId" VARCHAR(80), 
	"proximoVencimento" VARCHAR(10), 
	"bloqueadaManualmente" BOOLEAN NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY ("clinicaId"), 
	FOREIGN KEY("planoId") REFERENCES crm_v2.planos_saas (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."assinaturas_clinica" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."assinaturas_clinica" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.despesas (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	categoria VARCHAR(30) NOT NULL, 
	descricao VARCHAR(200) NOT NULL, 
	valor INTEGER NOT NULL, 
	data VARCHAR(10) NOT NULL, 
	"comprovanteNome" VARCHAR(200), 
	"comprovanteMime" VARCHAR(100), 
	"comprovantePath" VARCHAR(300), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."despesas" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."despesas" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.notificacoes_enviadas (
	"clinicaId" INTEGER NOT NULL, 
	id SERIAL NOT NULL, 
	"appointmentId" VARCHAR(20) NOT NULL, 
	tipo VARCHAR(30) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."notificacoes_enviadas" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."notificacoes_enviadas" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.patients (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	name VARCHAR(150) NOT NULL, 
	phone VARCHAR(30) NOT NULL, 
	email VARCHAR(120) NOT NULL, 
	birth VARCHAR(10), 
	"lastVisit" VARCHAR(10), 
	"numProntuario" VARCHAR(30) NOT NULL, 
	notes TEXT NOT NULL, 
	"reactivateSentAt" VARCHAR(10), 
	"birthdaySentYear" VARCHAR(4), 
	rg VARCHAR(30) NOT NULL, 
	"orgaoExpedidor" VARCHAR(20) NOT NULL, 
	cpf VARCHAR(20) NOT NULL, 
	naturalidade VARCHAR(100) NOT NULL, 
	nacionalidade VARCHAR(60) NOT NULL, 
	"estadoCivil" VARCHAR(30) NOT NULL, 
	profissao VARCHAR(100) NOT NULL, 
	"localTrabalho" VARCHAR(150) NOT NULL, 
	"enderecoResidencial" VARCHAR(250) NOT NULL, 
	"indicadoPor" VARCHAR(150) NOT NULL, 
	"respNome" VARCHAR(150) NOT NULL, 
	"respRg" VARCHAR(30) NOT NULL, 
	"respCpf" VARCHAR(20) NOT NULL, 
	"respTelefone" VARCHAR(30) NOT NULL, 
	"respEmail" VARCHAR(120) NOT NULL, 
	alergias TEXT NOT NULL, 
	medicacoes TEXT NOT NULL, 
	"condicoesSistemicas" TEXT NOT NULL, 
	"fotoPath" VARCHAR(300), 
	"tokenPortal" VARCHAR(64), 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE ("tokenPortal"), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."patients" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."patients" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.profissionais (
	"clinicaId" INTEGER NOT NULL, 
	id SERIAL NOT NULL, 
	nome VARCHAR(150) NOT NULL, 
	cro VARCHAR(30) NOT NULL, 
	especialidade VARCHAR(100) NOT NULL, 
	telefone VARCHAR(30) NOT NULL, 
	email VARCHAR(120) NOT NULL, 
	cor VARCHAR(20) NOT NULL, 
	ativo BOOLEAN NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."profissionais" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."profissionais" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.retorno_config (
	"clinicaId" INTEGER NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	meses INTEGER, 
	"sentAt" VARCHAR(10), 
	PRIMARY KEY ("patientId"), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."retorno_config" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."retorno_config" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.settings (
	"clinicaId" INTEGER NOT NULL, 
	key VARCHAR(60) NOT NULL, 
	value TEXT NOT NULL, 
	PRIMARY KEY ("clinicaId", key), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."settings" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."settings" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.surveys (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"appointmentId" VARCHAR(20) NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	"sentAt" VARCHAR(10), 
	score INTEGER, 
	"reviewSent" BOOLEAN NOT NULL, 
	"reviewSentAt" VARCHAR(10), 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."surveys" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."surveys" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.anamnese_perguntas (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"modeloId" VARCHAR(20) NOT NULL, 
	ordem INTEGER NOT NULL, 
	texto VARCHAR(300) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("modeloId") REFERENCES crm_v2.anamnese_modelos (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."anamnese_perguntas" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."anamnese_perguntas" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.anamneses_remotas (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	"modeloId" VARCHAR(20), 
	token VARCHAR(64) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	respostas TEXT, 
	"assinaturaPath" VARCHAR(300), 
	"aprovadoPor" INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	"filledAt" TIMESTAMP WITHOUT TIME ZONE, 
	"aprovadoEm" TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	UNIQUE (token), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id), 
	FOREIGN KEY("aprovadoPor") REFERENCES crm_v2.profissionais (id), 
	FOREIGN KEY("modeloId") REFERENCES crm_v2.anamnese_modelos (id)
)

;
ALTER TABLE crm_v2."anamneses_remotas" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."anamneses_remotas" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.appointments (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	"profissionalId" INTEGER, 
	"numProntuario" VARCHAR(30) NOT NULL, 
	"numOrcamento" VARCHAR(30) NOT NULL, 
	date VARCHAR(10) NOT NULL, 
	time VARCHAR(5) NOT NULL, 
	"duracaoMinutos" INTEGER NOT NULL, 
	procedure VARCHAR(120) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	"confirmSent" BOOLEAN NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id), 
	FOREIGN KEY("profissionalId") REFERENCES crm_v2.profissionais (id)
)

;
ALTER TABLE crm_v2."appointments" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."appointments" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.chat_mensagens (
	"clinicaId" INTEGER NOT NULL, 
	id SERIAL NOT NULL, 
	"profissionalId" INTEGER NOT NULL, 
	canal VARCHAR(60) NOT NULL, 
	conteudo TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id), 
	FOREIGN KEY("profissionalId") REFERENCES crm_v2.profissionais (id)
)

;
ALTER TABLE crm_v2."chat_mensagens" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."chat_mensagens" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.consentimentos_midia (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	tipo VARCHAR(40) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	"assinaturaPath" VARCHAR(300), 
	"profissionalId" INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("profissionalId") REFERENCES crm_v2.profissionais (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."consentimentos_midia" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."consentimentos_midia" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.documentos_emitidos (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	"profissionalId" INTEGER NOT NULL, 
	tipo VARCHAR(20) NOT NULL, 
	conteudo TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id), 
	FOREIGN KEY("profissionalId") REFERENCES crm_v2.profissionais (id)
)

;
ALTER TABLE crm_v2."documentos_emitidos" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."documentos_emitidos" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.evolucoes (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	"profissionalId" INTEGER NOT NULL, 
	"denteRegiao" VARCHAR(60) NOT NULL, 
	procedimento VARCHAR(120) NOT NULL, 
	conteudo TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("profissionalId") REFERENCES crm_v2.profissionais (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."evolucoes" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."evolucoes" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.lancamentos (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	"profissionalId" INTEGER, 
	"appointmentId" VARCHAR(20), 
	descricao VARCHAR(200) NOT NULL, 
	valor INTEGER NOT NULL, 
	vencimento VARCHAR(10) NOT NULL, 
	"pagoEm" VARCHAR(10), 
	"formaPagamento" VARCHAR(30) NOT NULL, 
	"numOrcamento" VARCHAR(30) NOT NULL, 
	observacoes TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("profissionalId") REFERENCES crm_v2.profissionais (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."lancamentos" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."lancamentos" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.odontograma_marcas (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	"profissionalId" INTEGER NOT NULL, 
	dente VARCHAR(3) NOT NULL, 
	face VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	observacao TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id), 
	FOREIGN KEY("profissionalId") REFERENCES crm_v2.profissionais (id)
)

;
ALTER TABLE crm_v2."odontograma_marcas" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."odontograma_marcas" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.orcamentos (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	"profissionalId" INTEGER, 
	data VARCHAR(10) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	observacoes TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("profissionalId") REFERENCES crm_v2.profissionais (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."orcamentos" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."orcamentos" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.push_subscriptions (
	"clinicaId" INTEGER NOT NULL, 
	id SERIAL NOT NULL, 
	endpoint TEXT NOT NULL, 
	p256dh VARCHAR(255) NOT NULL, 
	auth VARCHAR(255) NOT NULL, 
	"profissionalId" INTEGER, 
	label VARCHAR(100) NOT NULL, 
	ativo BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (endpoint), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id), 
	FOREIGN KEY("profissionalId") REFERENCES crm_v2.profissionais (id)
)

;
ALTER TABLE crm_v2."push_subscriptions" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."push_subscriptions" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.tarefas (
	"clinicaId" INTEGER NOT NULL, 
	id SERIAL NOT NULL, 
	titulo VARCHAR(200) NOT NULL, 
	descricao TEXT NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	prioridade VARCHAR(20) NOT NULL, 
	"responsavelId" INTEGER, 
	"patientId" VARCHAR(20), 
	"dataVencimento" VARCHAR(10), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("responsavelId") REFERENCES crm_v2.profissionais (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."tarefas" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."tarefas" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.usuarios (
	"clinicaId" INTEGER NOT NULL, 
	id SERIAL NOT NULL, 
	nome VARCHAR(150) NOT NULL, 
	email VARCHAR(120), 
	username VARCHAR(60), 
	"senhaHash" VARCHAR(100) NOT NULL, 
	"profissionalId" INTEGER, 
	ativo BOOLEAN NOT NULL, 
	"isSuperAdmin" BOOLEAN NOT NULL, 
	"emailVerificado" BOOLEAN NOT NULL, 
	"emailVerifyToken" VARCHAR(64), 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (email), 
	UNIQUE (username), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id), 
	FOREIGN KEY("profissionalId") REFERENCES crm_v2.profissionais (id)
)

;
ALTER TABLE crm_v2."usuarios" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."usuarios" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.consentimentos_orcamento (
	"clinicaId" INTEGER NOT NULL, 
	id VARCHAR(20) NOT NULL, 
	"orcamentoId" VARCHAR(20) NOT NULL, 
	"patientId" VARCHAR(20) NOT NULL, 
	"profissionalId" INTEGER NOT NULL, 
	"assinaturaPacientePath" VARCHAR(300) NOT NULL, 
	"assinaturaProfissionalPath" VARCHAR(300) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("orcamentoId") REFERENCES crm_v2.orcamentos (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id), 
	FOREIGN KEY("profissionalId") REFERENCES crm_v2.profissionais (id)
)

;
ALTER TABLE crm_v2."consentimentos_orcamento" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."consentimentos_orcamento" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.feedback_sac (
	id VARCHAR(20) NOT NULL, 
	"clinicaId" INTEGER NOT NULL, 
	"usuarioId" INTEGER NOT NULL, 
	categoria VARCHAR(20) NOT NULL, 
	titulo VARCHAR(150) NOT NULL, 
	mensagem TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	resposta TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	"respondidoEm" TIMESTAMP WITHOUT TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id), 
	FOREIGN KEY("usuarioId") REFERENCES crm_v2.usuarios (id)
)

;
ALTER TABLE crm_v2."feedback_sac" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."feedback_sac" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.orcamento_itens (
	"clinicaId" INTEGER NOT NULL, 
	id SERIAL NOT NULL, 
	"orcamentoId" VARCHAR(20) NOT NULL, 
	procedimento VARCHAR(150) NOT NULL, 
	"denteRegiao" VARCHAR(60) NOT NULL, 
	quantidade INTEGER NOT NULL, 
	valor INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY("orcamentoId") REFERENCES crm_v2.orcamentos (id), 
	FOREIGN KEY("clinicaId") REFERENCES crm_v2.clinicas (id)
)

;
ALTER TABLE crm_v2."orcamento_itens" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."orcamento_itens" ENABLE ROW LEVEL SECURITY;

CREATE TABLE crm_v2.sessoes (
	token VARCHAR(64) NOT NULL, 
	"usuarioId" INTEGER NOT NULL, 
	"expiresAt" TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (token), 
	FOREIGN KEY("usuarioId") REFERENCES crm_v2.usuarios (id)
)

;
ALTER TABLE crm_v2."sessoes" OWNER TO crm_v2_app;
ALTER TABLE crm_v2."sessoes" ENABLE ROW LEVEL SECURITY;