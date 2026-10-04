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

Backup clínico versão 2 inclui 20 conjuntos de registros e até 8 MiB de arquivos, recriando IDs e vínculos na restauração. Não inclui contas de acesso nem configuração da plataforma. Falhas desfazem os registros da importação; repetição do mesmo arquivo é recusada. Backups antigos são recusados porque o formato anterior omitia dados. A migração dos dados reais do original exige procedimento específico e ainda não foi executada.

Validação automatizada: 17 testes Python em banco descartável e quatro casos JavaScript, incluindo preservação de vínculos e falha de Storage.
