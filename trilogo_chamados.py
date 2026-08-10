# -*- coding: utf-8 -*-
"""
Robô de LEITURA do Trílogo -> tabela `chamados` no Supabase.
Loga na conta, captura o token, chama a API ListTicketsByUser (paginada) e faz upsert.
Roda 1 conta por execução (o workflow chama 2x: Instalações e Civil).

Dois modos (variável MODO):
  MODO=inicial  -> carga inicial: chamados dos últimos 60 dias (por DATA DE CRIAÇÃO)
  MODO=rotina   -> rotina: chamados ATUALIZADOS nas últimas 72h (pega troca de status)
  (sem MODO)    -> compatível: usa DIAS (padrão 45) por data de criação

Variáveis de ambiente (segredos no GitHub):
  TRILOGO_EMAIL, TRILOGO_SENHA, ABA (CIVIL|INSTALACOES),
  SUPABASE_URL, SUPABASE_SERVICE_KEY, MODO (inicial|rotina) | DIAS (opcional)
"""
import os, sys, re, json, urllib.request, urllib.error
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

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
STATUS = "1,7,5,6"   # Aberto(1), Executado(7), Vistoriado(5), Em execução(6)
LIMIT = 50

STATUS_LABEL = {1: "Aberto", 6: "Em execução", 7: "Executado", 5: "Vistoriado"}
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

def main():
    tickets = coletar()

    # ---------- DIAGNÓSTICO (aparece no log do Actions) ----------
    print("DIAG total lidos:", len(tickets))
    dist = {}
    for t in tickets:
        s = t.get("status"); dist[s] = dist.get(s, 0) + 1
    print("DIAG distribuição de status:", dist)
    if tickets:
        print("DIAG row[0] mapeado:", json.dumps(_row(tickets[0]), ensure_ascii=False)[:600])
    # ------------------------------------------------------------

    agora = datetime.now()
    if MODO == "inicial":
        campo, jan_dias, jan_horas = "criacao", 60, None
    elif MODO == "rotina":
        campo, jan_dias, jan_horas = "mudanca", None, 72
    else:
        campo, jan_dias, jan_horas = "criacao", DIAS, None
    print(f"MODO={MODO or '(compat DIAS)'} | filtro por {campo}"
          f"{f' {jan_dias}d' if jan_dias else f' {jan_horas}h'}")

    rows = []
    for t in tickets:
        dtc = parse_dt(t.get("creationDateTime") or t.get("creationDate"))
        dtu = parse_dt(t.get("dateOfLastChange"))
        if campo == "criacao":
            if not dtc or dtc < agora - timedelta(days=jan_dias): continue
        else:  # mudanca (rotina)
            ref = dtu or dtc
            if not ref or ref < agora - timedelta(hours=jan_horas): continue
        rows.append(_row(t))

    print(f"{len(tickets)} lidos | {len(rows)} dentro da janela")
    upsert(rows)

if __name__ == "__main__":
    main()
