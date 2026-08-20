# -*- coding: utf-8 -*-
"""
Robô de LEITURA do Trílogo -> tabela `chamados` no Supabase.
Loga na conta, captura o token, chama a API ListTicketsByUser (paginada) e faz upsert.
Roda 1 conta por execução (o workflow chama 2x: Instalações e Civil).

RECARGA COMPLETA a cada rodada: lê os chamados dos últimos JANELA_DIAS (padrão 90,
por DATA DE CRIAÇÃO), com TODOS os status, ZERA só a aba desta conta e recarrega.
Assim o status fica sempre atual e o banco limitado à janela. (MODO não é mais usado.)

Variáveis de ambiente (segredos no GitHub):
  TRILOGO_EMAIL, TRILOGO_SENHA, ABA (CIVIL|INSTALACOES),
  SUPABASE_URL, SUPABASE_SERVICE_KEY
  Opcionais: JANELA_DIAS (padrão 90), STATUS_ACTIONS (padrão amplo)
"""
import os, sys, re, json, urllib.request, urllib.error
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

# ============ CONTADOR DE REVISÃO DO ROBÔ ============
ROBO_REV = 8   # + marcos de TRANSIÇÃO persistidos (em_execucao_em / executado_em):
               #   a recarga apaga e regrava, então antes de zerar lemos os carimbos antigos
               #   e carregamos adiante; chamado que aparece pela 1ª vez em execução/concluído
               #   ganha carimbo (backfill inicial: data da vistoria quando houver, senão hoje).
# ====================================================

EMAIL  = os.environ["TRILOGO_EMAIL"]
SENHA  = os.environ["TRILOGO_SENHA"]
ABA    = os.environ.get("ABA", "").upper()
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
MODO   = os.environ.get("MODO", "").strip().lower()
DIAS   = int(os.environ.get("DIAS", "45"))

LOGIN_URL = "https://mercadinhossaoluiz.trilogo.app/"
TICKETS_URL = "https://mercadinhossaoluiz.trilogo.app/tickets"
API = "https://web.api.trilogo.app/api/Ticket/ListTicketsByUser"
# amplo p/ capturar TODOS os status (inclui Fechado/Arquivado). Ajustável por env se algum código quebrar.
STATUS = os.environ.get("STATUS_ACTIONS", "1,2,3,4,5,6,7,8,9,10")
LIMIT = 50

# rótulos conhecidos; códigos novos (ex.: Fechado/Arquivado) aparecem no DIAG e a gente mapeia aqui depois
STATUS_LABEL = {1: "Aberto", 6: "Em execução", 7: "Executado", 5: "Vistoriado", 3: "Arquivado"}
PRIOR_LABEL  = {1: "Baixa", 2: "Média", 3: "Alta", 4: "Urgente"}

def aba_from(sc_name):
    n = (sc_name or "").upper()
    if "CIVIL" in n: return "CIVIL"
    if "INSTALA" in n: return "INSTALACOES"
    return ABA or "INSTALACOES"

def parse_dt(s):
    if not s: return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "")[:19])
    except Exception:
        pass
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(s))
    if m: return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None

def _nome(v):
    """Extrai um texto de um campo que pode vir como string OU objeto {name/description/...}."""
    if v is None: return None
    if isinstance(v, dict):
        for k in ("name", "description", "title", "fullName", "label"):
            if v.get(k): return str(v[k]).strip()
        return None
    s = str(v).strip()
    return s or None

def _label(mapa, v):
    try: return mapa.get(int(v), str(v))
    except Exception: return _nome(v)

def coletar():
    token = {"v": None}
    with sync_playwright() as p:
        UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        br = p.chromium.launch(headless=True)
        ctx = br.new_context(user_agent=UA)
        page = ctx.new_page()

        def on_req(req):
            if token["v"]: return
            if "web.api.trilogo.app" in req.url:
                a = req.headers.get("authorization")
                if a: token["v"] = a
        page.on("request", on_req)

        page.goto(LOGIN_URL, wait_until="networkidle")
        EMAIL_SEL = ("input[type=email], input[placeholder*='mail' i], input[name*='mail' i], "
                     "input[id*='mail' i], input[type=text]")
        try:
            page.wait_for_selector(EMAIL_SEL, timeout=30000)
            page.locator(EMAIL_SEL).first.fill(EMAIL)
        except Exception:
            try:
                campos = page.eval_on_selector_all(
                    "input", "els => els.map(e => ({t:e.type, ph:e.placeholder, n:e.name, id:e.id}))")
                print("DIAG login falhou. URL=", page.url)
                print("DIAG inputs na página:", campos)
            except Exception:
                pass
            br.close(); raise
        try:
            page.get_by_role("button", name=re.compile("continuar|prosseguir|avan|próximo|proximo|entrar|login", re.I)).click(timeout=5000)
        except Exception:
            page.keyboard.press("Enter")
        page.wait_for_selector("input[type=password]", timeout=30000)
        page.locator("input[type=password]").first.fill(SENHA)
        try:
            page.get_by_role("button", name=re.compile("entrar|continuar|acessar|login", re.I)).click(timeout=5000)
        except Exception:
            page.keyboard.press("Enter")

        for _ in range(60):
            if token["v"]: break
            page.wait_for_timeout(500)
        if not token["v"]:
            try: page.goto(TICKETS_URL, wait_until="networkidle")
            except Exception: pass
            for _ in range(40):
                if token["v"]: break
                page.wait_for_timeout(500)
        if not token["v"]:
            br.close(); raise RuntimeError("não capturei o token de autenticação")

        tickets, offset = [], 0
        req = ctx.request
        while True:
            r = req.post(API, headers={"authorization": token["v"], "content-type": "application/json"},
                         data=json.dumps({"StatusActions": STATUS, "OnlyUnread": False,
                                          "Offset": offset, "Limit": LIMIT}))
            if not r.ok:
                print("Falha na API:", r.status, r.text()[:300]); break
            batch = (r.json() or {}).get("tickets") or []
            tickets.extend(batch)
            if len(batch) < LIMIT: break
            offset += LIMIT
        br.close()
        return tickets

def _row(t):
    dtc = parse_dt(t.get("creationDateTime") or t.get("creationDate"))
    dtp = parse_dt(t.get("deadlineDate") or t.get("deadLine"))
    dtu = parse_dt(t.get("dateOfLastChange"))
    dti = parse_dt(t.get("dateOfLastInspection"))   # data da vistoria = MARCO de "atendido"
    return {
        "numero": str(t.get("id")),
        "aba": aba_from((t.get("serviceCompany") or {}).get("name")),
        "loja": _nome(t.get("companyName")) or _nome(t.get("companyGroup")) or _nome(t.get("company")),
        "descricao": t.get("description"),
        "status": STATUS_LABEL.get(t.get("status"), str(t.get("status")) if t.get("status") is not None else None),
        "tipo_predial": _nome(t.get("buildingServiceType")) or _nome(t.get("serviceType")) or _nome(t.get("issueType")),
        "prioridade": _label(PRIOR_LABEL, t.get("priority")) if t.get("priority") is not None else None,
        "solicitante": _nome(t.get("creator")),
        "responsavel": _nome(t.get("executant")) or _nome(t.get("assignee")) or _nome(t.get("serviceCompanyAssignee")),
        "data_criacao": dtc.strftime("%Y-%m-%d") if dtc else None,
        "prazo": dtp.strftime("%Y-%m-%d") if dtp else None,
        "data_atualizacao": dtu.isoformat() if dtu else None,
        # MARCO "atendido": passou por vistoriado alguma vez (persiste mesmo depois de fechado/arquivado)
        "atendido": bool(dti),
        "atendido_em": dti.strftime("%Y-%m-%d") if dti else None,
        "vistoriado_por": _nome(t.get("inspectedBy")),
    }

def upsert(rows):
    if not rows:
        print("nada a gravar"); return
    body = json.dumps(rows).encode()
    url = f"{SB_URL}/rest/v1/chamados?on_conflict=numero,aba"
    rq = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey": SB_KEY, "authorization": f"Bearer {SB_KEY}",
        "content-type": "application/json",
        "prefer": "resolution=merge-duplicates,return=minimal"})
    try:
        urllib.request.urlopen(rq); print(f"upsert OK: {len(rows)} chamados")
    except urllib.error.HTTPError as e:
        print("Supabase erro:", e.code, e.read().decode()[:400]); sys.exit(1)

def carrega_marcos():
    """Lê os carimbos de transição já gravados (numero -> em_execucao_em/executado_em) desta ABA,
       para carregá-los adiante na recarga (que apaga e regrava tudo)."""
    url=f"{SB_URL}/rest/v1/chamados?aba=eq.{ABA}&select=numero,em_execucao_em,executado_em&limit=20000"
    rq=urllib.request.Request(url, headers={"apikey":SB_KEY,"authorization":f"Bearer {SB_KEY}"})
    try:
        rows=json.loads(urllib.request.urlopen(rq).read().decode()) or []
        return {str(r.get("numero")):(r.get("em_execucao_em"), r.get("executado_em")) for r in rows}
    except Exception as e:
        print("marcos: não li os carimbos antigos (colunas em_execucao_em/executado_em existem?):", str(e)[:160])
        return {}

_ATIVOS=("Em execução","Executado","Vistoriado","Arquivado")
_CONCLUIDOS=("Executado","Vistoriado","Arquivado")
def aplica_marcos(rows, marcos):
    """MARCO DE ATENDIMENTO = entrada em execução. Preserva carimbo antigo; na 1ª vez
       usa a data da vistoria (se houver) como aproximação, senão a data de hoje."""
    hoje=datetime.now().strftime("%Y-%m-%d")
    for r in rows:
        em_prev, ex_prev = marcos.get(str(r.get("numero")), (None, None))
        em=em_prev; ex=ex_prev
        if not em and (r.get("status") in _ATIVOS):
            em = r.get("atendido_em") or hoje
        if not ex and (r.get("status") in _CONCLUIDOS):
            ex = r.get("atendido_em") or hoje
        r["em_execucao_em"]=em; r["executado_em"]=ex
    return rows

def zera_aba():
    """Apaga os chamados SÓ desta ABA (recarga completa da janela). As outras contas não são tocadas."""
    url = f"{SB_URL}/rest/v1/chamados?aba=eq.{ABA}"
    rq = urllib.request.Request(url, method="DELETE", headers={
        "apikey": SB_KEY, "authorization": f"Bearer {SB_KEY}", "prefer": "return=minimal"})
    try:
        urllib.request.urlopen(rq); print(f"zerado: chamados aba={ABA}")
    except urllib.error.HTTPError as e:
        print("Supabase DELETE erro:", e.code, e.read().decode()[:300]); sys.exit(1)

def main():
    print(f"===== ROBÔ TRÍLOGO — REV {ROBO_REV} | aba={ABA} =====")
    tickets = coletar()

    # ---------- DIAGNÓSTICO (aparece no log do Actions) ----------
    print("DIAG total lidos:", len(tickets))
    dist = {}
    for t in tickets:
        s = t.get("status"); dist[s] = dist.get(s, 0) + 1
    print("DIAG distribuição de status (codigo: qtd):", dist,
          " <- se aparecer código novo (fechado/arquivado), me avise para rotular")
    if tickets:
        print("DIAG row[0] mapeado:", json.dumps(_row(tickets[0]), ensure_ascii=False)[:700])
        atend = sum(1 for t in tickets if t.get("dateOfLastInspection"))
        print(f"DIAG atendidos (com data de vistoria) no lote: {atend}/{len(tickets)}")
    # ------------------------------------------------------------

    # RECARGA COMPLETA: últimos JANELA_DIAS (por data de criação), TODOS os status.
    # Zera só esta aba e recarrega -> status sempre atual e banco limitado à janela.
    JAN = int(os.environ.get("JANELA_DIAS", "90"))
    agora = datetime.now()
    print(f"RECARGA COMPLETA | aba={ABA} | janela {JAN} dias por data de criação")

    # lojas fora do escopo de atendimento — nunca entram no banco
    EXCLUIR = ("juazeiro", "lagoa seca", "crato")   # "novo juazeiro" cai em "juazeiro"
    def _fora_escopo(r):
        lj = (r.get("loja") or "").lower()
        return any(x in lj for x in EXCLUIR)
    rows = []
    for t in tickets:
        dtc = parse_dt(t.get("creationDateTime") or t.get("creationDate"))
        if not dtc or dtc < agora - timedelta(days=JAN): continue
        r = _row(t)
        if _fora_escopo(r): continue
        rows.append(r)

    # dedup por (numero, aba): o mesmo chamado pode vir repetido (reaberturas/paginação).
    # mantém o registro mais recente (por data_atualizacao).
    uniq = {}
    for r in rows:
        k = (r.get("numero"), r.get("aba"))
        p = uniq.get(k)
        if p is None or (r.get("data_atualizacao") or "") >= (p.get("data_atualizacao") or ""):
            uniq[k] = r
    dups = len(rows) - len(uniq)
    rows = list(uniq.values())

    print(f"{len(tickets)} lidos | {len(rows)} únicos dentro dos {JAN} dias | {dups} duplicados removidos")
    if not rows:
        print("AVISO: 0 chamados no resultado — NÃO vou zerar o banco (evita apagar tudo por falha de leitura).")
        return
    marcos=carrega_marcos()          # carimbos antigos (sobrevivem à recarga)
    rows=aplica_marcos(rows, marcos) # marco de atendimento = entrada em execução
    print(f"marcos: {sum(1 for r in rows if r.get('em_execucao_em'))} com em_execucao_em · {sum(1 for r in rows if r.get('executado_em'))} com executado_em")
    zera_aba()      # limpa só esta aba
    upsert(rows)    # recarrega do zero

if __name__ == "__main__":
    main()
