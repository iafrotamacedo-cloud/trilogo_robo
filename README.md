# Robô Trílogo → chamados (Supabase)

Lê os chamados **Executado + Vistoriado + Em execução** (StatusActions "7,5,6") das 2 contas
(Instalações/jonas e Civil/humberto) e grava na tabela `chamados` do Supabase (dedup por número+aba).
Roda sozinho todo dia às 07h (BRT) pelo GitHub Actions, e também no botão "Run workflow".

## Como colocar no ar
1. Crie um repositório **privado** no GitHub (ex.: `trilogo-robo`) e suba estes arquivos
   (`trilogo_chamados.py` e a pasta `.github/workflows/`).
2. No repositório: **Settings → Secrets and variables → Actions → New repository secret** e crie:
   - `TRILOGO_EMAIL_INSTALACOES` / `TRILOGO_SENHA_INSTALACOES`  (conta jonas)
   - `TRILOGO_EMAIL_CIVIL` / `TRILOGO_SENHA_CIVIL`              (conta humberto)
   - `SUPABASE_URL`            (https://faalgfbugvekbuhhtatt.supabase.co)
   - `SUPABASE_SERVICE_KEY`    (Supabase → Project Settings → API → **service_role** — NUNCA no app/front)
3. Rode manualmente a 1ª vez: aba **Actions → Trílogo → chamados → Run workflow**.
   Veja o log; deve terminar com "upsert OK".

## Ajustes
- Período: variável `DIAS` no workflow (padrão 45).
- Horário: `cron` no `.yml`.
- Se o login mudar de layout, ajustam-se os seletores em `trilogo_chamados.py` (parte do login).
