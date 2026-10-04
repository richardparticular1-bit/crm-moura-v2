# CRM Moura v2 — Render Free + Supabase Free

Preparado em 04/10/2026 no projeto cmd-moura (ahunkumobdexrcrrppvs).

## Recursos separados

- Schema crm_v2: 30 tabelas independentes do original.
- Usuário crm_v2_app: sem privilégios administrativos e sem acesso de leitura/escrita às tabelas public do original.
- RLS habilitada em todas as tabelas; acesso pelo proprietário através do backend, sem políticas públicas.
- Bucket privado prontuarios-v2, com limite de 15 MiB por arquivo.
- Senha aleatória em .env.v2 local, ignorado pelo Git. Não publicar esse arquivo.

Infraestrutura e quotas são compartilhadas. A chave service_role usada pelo Storage tem alcance no projeto inteiro; o backend da v2 deve usar exclusivamente seu bucket.

## Publicação

Serviço Free ativo: https://crm-moura-v2.onrender.com, workspace My Workspace.
Conexão via Session pooler, porta 5432, com usuário exclusivo crm_v2_app.
SUPABASE_SERVICE_KEY foi configurada diretamente no Render. Conta inicial criada e salvamento de backup no bucket separado confirmado pelo usuário.

O Blueprint define DATABASE_SCHEMA=crm_v2, SUPABASE_BUCKET=prontuarios-v2 e a URL do projeto. A aplicação exige crm_v2_app e recusa o bucket original no Render. Links usam RENDER_EXTERNAL_URL.

E-mail, push e assinatura de clínicas exigem configuração própria posterior.

O banco temporário vazio crm-moura-v2-db do Render não é usado pelo Blueprint atualizado. Ainda existe e expira em 03/11/2026; não foi excluído.

## Migração futura

Exportar o schema e os arquivos do bucket, preservando IDs e caminhos. Provisionar o usuário e atualizar DATABASE_URL, SUPABASE_URL e a chave server-side no novo projeto. Verificar dados e arquivos antes de trocar o ambiente.

## Verificação

SQL confirmou 30 tabelas, proprietário exclusivo, RLS habilitada, login do usuário e zero tabelas public acessíveis a ele. Bucket confirmado privado. Conexão via pooler e execução HTTP no Render verificadas.

Advisors informou ausência de políticas RLS na v2, esperado para acesso exclusivo pelo proprietário via backend. Também apontou três tabelas do original sem RLS, que não foram alteradas: https://supabase.com/docs/guides/database/database-linter?lint=0013_rls_disabled_in_public

## Correções de 04/10/2026

Permissões de administração da equipe, validação de vínculos entre clínica/paciente/consulta, recebimentos positivos, preservação de históricos na exclusão e escape de valores em eventos do frontend.

Backup clínico versão 2 inclui 20 conjuntos de registros e até 8 MiB de arquivos, recriando IDs e vínculos na restauração. Não inclui contas de acesso nem configuração da plataforma. Falhas desfazem os registros da importação; repetição do mesmo arquivo é recusada. Backups antigos são recusados porque o formato anterior omitia dados. A migração dos dados reais foi concluída em 04/10/2026 pela ferramenta específica.

Validação automatizada: 17 testes Python em banco descartável e quatro casos JavaScript, incluindo preservação de vínculos e falha de Storage.

## Ferramentas de migração

prepare_migration.py monta um plano a partir do backup antigo e de um snapshot completo em leitura. execute_migration.py importa apenas em uma clínica sem dados clínicos, com o usuário crm_v2_app e cópias verificadas por SHA-256 através de uma Edge Function temporária. verify_migration.py confere totais, respostas de anamnese, documentos, anexo e logo pela API, revogando a sessão de diagnóstico ao terminar.

Snapshots, plano, credenciais temporárias e relatórios privados ficam em .migration/, excluída do Git. A Edge Function deve ser desativada após a execução; o token temporário expira em uma hora. A origem é usada exclusivamente para leitura e cópia.

A restauração remapeia também as perguntas no JSON de respostas de anamnese e as referências de consultas a orçamentos. A interface usa o caminho de logo salvo no cadastro, permitindo arquivos migrados para caminhos novos.

Validação ampliada: 26 testes Python, quatro casos JavaScript e validação da transação completa no PostgreSQL antes do commit definitivo.

## Migração concluída

Foram importados 532 registros: 118 pacientes, 31 consultas, 57 lançamentos financeiros, 216 anexos e os conjuntos clínicos complementares. Foram copiados 227 arquivos, incluindo assinaturas e logo, com comparação SHA-256 da origem e do destino. A conta de acesso da v2 foi preservada; senhas e sessões do original não foram copiadas.

A API confirmou pacientes e consultas, leitura de anexo, documento, todas as cinco anamneses e logo. A sessão temporária de diagnóstico foi revogada. A Edge Function de cópia foi substituída por uma função inerte (410) com verificação de JWT habilitada. O original permanece com 532 registros clínicos e 236 objetos no bucket; a v2 tem 228 objetos, contando o backup anterior.

O volume migrado supera o limite de 8 MiB de arquivos do backup JSON atual. Esse botão recusa o volume completo; um backup completo exige exportação separada dos arquivos e dos registros. Essa melhoria ainda não foi implementada.
