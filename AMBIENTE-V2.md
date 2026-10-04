# Ambiente de testes CRM Moura v2

O original permanece no serviço crm-moura. A v2 usa o repositório crm-moura-v2 e um banco próprio.

## Recursos preparados

- Workspace Render: My Workspace.
- PostgreSQL: crm-moura-v2-db, PostgreSQL 17, região Oregon.
- Banco criado em 04/10/2026 no plano gratuito, com expiração informada pelo Render em 03/11/2026.
- Blueprint render.yaml: serviço web crm-moura-v2 no plano gratuito, conectado ao banco existente por fromDatabase.
- Branch main com deploy automático a cada commit após aplicar o Blueprint.
- Links públicos usam RENDER_EXTERNAL_URL quando APP_URL não estiver configurada.

## Concluir a publicação

1. Abra https://dashboard.render.com/blueprint/new?repo=https://github.com/richardparticular1-bit/crm-moura-v2 no navegador em que está conectado ao Render.
2. Selecione My Workspace e a branch main.
3. Confira que será criado apenas o serviço crm-moura-v2, usando crm-moura-v2-db.
4. Aplique o Blueprint e aguarde o deploy ficar Live.
5. Abra a URL exibida pelo Render e configure a primeira conta com suas credenciais de teste. Essa conta será superadmin da nova plataforma.

O banco começa vazio; contas e dados do original não são copiados automaticamente.

## Integrações

Agenda, cadastros, prontuário e financeiro usam o banco separado. Para testar todos os recursos do original, configure posteriormente integrações próprias:

- SUPABASE_URL e SUPABASE_SERVICE_KEY: armazenamento de anexos, imagens, assinaturas e backups em um projeto de testes.
- RESEND_API_KEY e RESEND_FROM: confirmação de e-mail para cadastro público de clínicas.
- VAPID_PRIVATE_KEY e VAPID_PUBLIC_KEY: notificações push.
- MERCADOPAGO_ACCESS_TOKEN: assinatura de clínicas; usar credenciais apropriadas para testes.
- APP_URL: opcional para substituir o endereço automático, por exemplo ao adicionar domínio próprio.

Não reutilize DATABASE_URL ou armazenamento do original: uploads, exclusões e backups da v2 precisam permanecer isolados. Não copie dados reais de pacientes para o ambiente de testes antes de definir o procedimento de cópia e proteção.

## Estado de validação

A publicação só está confirmada quando o Render mostrar Live, a raiz carregar e /api/auth/status responder. A configuração é preparada neste repositório; isso, sozinho, não confirma que o serviço web já foi criado.
