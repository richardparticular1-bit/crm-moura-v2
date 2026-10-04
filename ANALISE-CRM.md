# Análise do CRM Moura v2

Data: 04/10/2026. Código analisado: a5badb011b8b639a5df98d9450d56e51c8606a20.
Revisão local do código, sem acesso ao banco, configurações ou serviço de produção. Nenhum código funcional foi alterado.

## Estrutura e recursos

Backend FastAPI e SQLAlchemy, SQLite local e PostgreSQL por DATABASE_URL. Interface PWA em um único HTML com JavaScript integrado. Inclui pacientes, agenda, pesquisas, tarefas, chat, prontuário, odontograma, anamnese, documentos, assinaturas, orçamento, financeiro, portal do paciente e administração de várias clínicas. Há integrações de armazenamento, e-mail, cobrança de assinaturas e notificações.

Pontos positivos: bcrypt para senhas, tokens aleatórios para sessões e links públicos, expiração de sessões, revogação ao trocar senha, filtros por clínica em muitas rotas, valores financeiros em centavos, verificação de conflitos de agenda e bloqueio de inicialização no Render sem DATABASE_URL.

## Achados prioritários

### 1. Alta: execução de JavaScript por interpolação em onclick

frontend/index.html:469, 2751, 2874, 5277. esc() escapa HTML, mas nomes são interpolados dentro de strings JavaScript em atributos onclick. O navegador decodifica &#39; antes de executar o comando. Um nome com aspas pode quebrar o botão; um nome malicioso pode executar JavaScript quando alguém clicar. O token de sessão é acessível no localStorage (linha 251), ampliando o impacto de XSS.

Exemplo para reprodução em ambiente isolado: nome `Teste');globalThis.prova=1;//` no botão de edição de usuário. A decodificação do atributo produz um segundo comando executável. A estrutura do comando foi reproduzida em vm do Node; a aplicação completa não foi explorada.

Correção: remover interpolação em eventos inline; usar addEventListener e parâmetros em memória ou data attributes corretamente codificados. Verificar todos os usos, inclusive IDs aceitos do cliente.

### 2. Alta: backup não permite restauração completa

main.py:4158, 4247, 4256. O dump inclui profissionais, tarefas, evoluções, orçamentos, itens, anexos, retornos e despesas. DumpIn só aceita pacientes, consultas, pesquisas, lançamentos e settings; a importação ignora os demais conjuntos. Além disso, o próprio dump omite odontograma, consentimentos, documentos emitidos, anamneses e outros registros. Arquivos binários não são incluídos, apenas referências de alguns registros.

Ao restaurar em um banco vazio, referências a profissionais podem falhar no PostgreSQL; registros clínicos omitidos não voltam. Ao importar no mesmo banco, IDs existentes são ignorados, sem restauração de alterações. O backup usado antes da exclusão definitiva de clínica também é incompleto.

Correção: definir formato versionado, cobertura explícita, preservação de arquivos e restauração transacional com mapeamento de IDs. Testar exportação e restauração em banco vazio.

### 3. Alta: edição e importação aceitam referências a pacientes sem validar clínica

main.py:2413, 2555, 4071, 4256. A criação de consultas, tarefas e lançamentos verifica o paciente. A edição desses registros não repete a validação do patientId. A importação aceita IDs de relacionamento sem verificar propriedade. models.py:73, por exemplo, define patientId como string sem chave estrangeira.

Consequência: um registro da clínica A pode passar a apontar para paciente da B, se seu ID for conhecido, ou para um paciente inexistente. Isso quebra a integridade e abre risco de mistura de dados; não foi demonstrado vazamento generalizado entre clínicas.

Correção: validar todas as referências em criação, edição e importação, e reforçar a integridade no banco.

### 4. Alta: qualquer membro pode alterar contas da própria clínica

main.py:437, 460. Basta estar autenticado para criar usuários, desativar colegas ou trocar suas senhas. Não existe papel administrativo da clínica nessas operações. Inclusive um membro da clínica do superadmin pode alterar a conta desse superadmin, pois a checagem exige apenas a mesma clínica.

Correção: separar administração de clínica e plataforma; limitar alterações de terceiros a administradores; proteger explicitamente contas de superadmin.

### 5. Média: exclusão de clínica pode falhar por chave estrangeira

main.py:835 e models.py:163. excluir_clinica apaga Usuario antes de FeedbackSAC, mas FeedbackSAC.usuarioId referencia usuarios.id. Em PostgreSQL com feedback existente, a exclusão de usuários pode gerar erro de integridade e a transação falhar.

Correção: excluir feedbacks antes dos usuários e testar exclusão com todas as entidades relacionadas.

### 6. Média: exclusão de profissional não trata vínculos

main.py:1296. O profissional é apagado diretamente, embora consultas, evoluções, documentos, usuários e outras tabelas o referenciem. PostgreSQL pode rejeitar a operação; SQLite sem enforcement de FKs pode deixar referências inválidas.

Correção: preferir desativação preservando o histórico, ou bloquear exclusão com mensagem clara quando houver vínculos.

### 7. Média: recebimento zero ou negativo é tratado como pagamento integral

main.py:4094, especialmente 4104. valorRecebido=0 ou negativo cai no fallback l.valor, marca o lançamento pago e retorna sucesso. Valor inválido deve ser rejeitado, enquanto apenas None deve significar pagamento integral.

Correção: validar inteiro estritamente positivo quando informado. Testar None, zero, negativo, parcial e valor acima da parcela.

### 8. Média: sincronizador não acompanha o modelo de múltiplas clínicas

sync.py: sincronizar_pacientes percorre todos os pacientes sem escopo e cria CrmPaciente sem clinicaId, obrigatório em models.py:27. O fluxo Clínica→CRM pode falhar por NOT NULL; correspondência apenas por telefone pode misturar pessoas que compartilham número. O script usa exclusivamente SQLite e não DATABASE_URL. A descrição promete last-write-wins, mas pacientes existentes não são atualizados nesse fluxo.

Correção: explicitar clínica de destino, identidade de integração, banco suportado e política de atualização.

## Manutenção e outras lacunas

- main.py tem mais de 4 mil linhas; frontend/index.html, mais de 5 mil. Separar módulos ajudará a corrigir e verificar alterações.
- Não foram encontrados testes, lock de dependências ou workflow de CI no checkout. requirements.txt usa somente limites mínimos, permitindo mudanças de versão em novos deploys.
- init_db usa create_all; não há mecanismo versionado de migração. create_all não atualiza colunas de tabelas já existentes.
- Datas, horários, duração e vários estados são strings ou números sem validação adequada. _minutos aceita horários fora do intervalo; edições financeiras não repetem a validação positiva da criação.
- Verificação de conflito e gravação da consulta são operações separadas, permitindo corrida em requisições simultâneas.
- O autor profissional do chat e da evolução é recebido do cliente e validado apenas por clínica; não fica vinculado automaticamente ao usuário autenticado.
- APP_URL tem fallback para o endereço do CRM original. O ambiente da v2 precisa definir seu próprio endereço para links públicos e verificação de e-mail.
- Copiar o repositório não cria isolamento de banco, armazenamento ou integrações. Um eventual deploy da v2 deve receber recursos próprios antes de qualquer teste com escrita.
- Exclusão de paciente remove consultas, mas deixa outros registros associados. É necessário definir e implementar uma política coerente de preservação ou exclusão.

## Validação realizada

- AST: sintaxe válida nos cinco arquivos Python.
- Node vm.Script: sintaxe válida no bloco JavaScript inline.
- Reprodução isolada da estrutura do onclick: demonstrada execução de comando após quebra da string.
- Revisão de rotas, modelos, backup/importação, autenticação, sincronização e service worker.
- As dependências de execução não estão instaladas no Python disponível. Não foram executados testes HTTP, interface completa, banco real ou integrações externas. Os achados de banco e API são derivados dos caminhos de código, não de testes de produção.

## Ordem sugerida

1. Corrigir interpolação de eventos, autorização de usuários e referências entre clínicas.
2. Implementar backup e restauração completos com testes.
3. Corrigir integridade de exclusões e validação financeira.
4. Criar ambiente isolado para executar testes da API e banco.
5. Adicionar migrações, dependências reproduzíveis e CI; modularizar progressivamente.
