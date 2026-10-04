# CRM Moura v2 — Render Free + Supabase Free

Preparado em 04/10/2026 no projeto cmd-moura (ahunkumobdexrcrrppvs).

## Recursos separados

- Schema crm_v2: 30 tabelas vazias.
- Usuário crm_v2_app: sem privilégios administrativos e sem acesso de leitura/escrita às tabelas public do original.
- RLS habilitada em todas as tabelas; acesso pelo proprietário através do backend, sem políticas públicas.
- Bucket privado prontuarios-v2, com limite de 15 MiB por arquivo.
- Senha aleatória em .env.v2 local, ignorado pelo Git. Não publicar esse arquivo.

Infraestrutura e quotas são compartilhadas. A chave service_role usada pelo Storage tem alcance no projeto inteiro; o backend da v2 deve usar exclusivamente seu bucket.

## Publicação pendente

1. Obter o host exato de Connect → Session pooler no Supabase, porta 5432.
2. Montar DATABASE_URL com usuário crm_v2_app.ahunkumobdexrcrrppvs, senha local da v2, host do pooler, banco postgres e sslmode=require.
3. Aplicar https://dashboard.render.com/blueprint/new?repo=https://github.com/richardparticular1-bit/crm-moura-v2 no workspace My Workspace.
4. Preencher DATABASE_URL e SUPABASE_SERVICE_KEY diretamente no painel. A segunda é a chave server-side service_role para Storage; não usar anon/publishable nem colocá-la no frontend ou no Git.
5. Após Live, criar conta de teste e verificar pacientes, agenda, anexos e backup.

O Blueprint define DATABASE_SCHEMA=crm_v2, SUPABASE_BUCKET=prontuarios-v2 e a URL do projeto. A aplicação exige crm_v2_app e recusa o bucket original no Render. Links usam RENDER_EXTERNAL_URL.

E-mail, push e assinatura de clínicas exigem configuração própria posterior.

O banco temporário vazio crm-moura-v2-db do Render não é usado pelo Blueprint atualizado. Ainda existe e expira em 03/11/2026; não foi excluído.

## Migração futura

Exportar o schema e os arquivos do bucket, preservando IDs e caminhos. Provisionar o usuário e atualizar DATABASE_URL, SUPABASE_URL e a chave server-side no novo projeto. Verificar dados e arquivos antes de trocar o ambiente.

## Verificação

SQL confirmou 30 tabelas, proprietário exclusivo, RLS habilitada, login do usuário e zero tabelas public acessíveis a ele. Bucket confirmado privado. A conexão autenticada via pooler e a execução HTTP no Render ainda precisam ser verificadas.

Advisors informou ausência de políticas RLS na v2, esperado para acesso exclusivo pelo proprietário via backend. Também apontou três tabelas do original sem RLS, que não foram alteradas: https://supabase.com/docs/guides/database/database-linter?lint=0013_rls_disabled_in_public
