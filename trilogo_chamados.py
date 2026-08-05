# -*- coding: utf-8 -*-
"""
Robô de LEITURA do Trílogo -> tabela `chamados` no Supabase.
Loga na conta, captura o token, chama a API ListTicketsByUser (paginada) e faz upsert.
Roda 1 conta por execução (o workflow chama 2x: Instalações e Civil).

Variáveis de ambiente (segredos no GitHub):
  TRILOGO_EMAIL, TRILOGO_SENHA, ABA (CIVIL|INSTALACOES),
  SUPABASE_URL, SUPABASE_SERVICE_KEY, DIAS (opcional, padrão 45)
"""
import os, sys, re, json, urllib.request, urllib.error
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

EMAIL  = os.environ["TRILOGO_EMAIL"]
SENHA  = os.environ["TRILOGO_SENHA"]
ABA    = os.environ.get("ABA", "").upper()
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
DIAS   = int(os.environ.get("DIAS", "45"))

LOGIN_URL = "https://mercadinhossaoluiz.trilogo.app/"
TICKETS_URL = "https://mercadinhossaoluiz.trilogo.app/tickets"
API = "https://web.api.trilogo.app/api/Ticket/ListTicketsByUser"
STATUS = "7,5,6"     # Executado, Vistoriado, Em execução
LIMIT = 50

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

def coletar():
    token = {"v": None}
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        ctx = br.new_context()
        page = ctx.new_page()

        def on_req(req):
            if token["v"]: return
            if "web.api.trilogo.app" in req.url:
                a = req.headers.get("authorization")
                if a: token["v"] = a
        page.on("request", on_req)

        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.get_by_placeholder(re.compile("e-mail", re.I)).fill(EMAIL)
        page.get_by_role("button", name=re.compile("continuar", re.I)).click()
        page.locator("input[type=password]").wait_for(timeout=20000)
        page.locator("input[type=password]").fill(SENHA)
        try:
            page.get_by_role("button", name=re.compile("entrar|continuar|acessar", re.I)).click(timeout=4000)
        except Exception:
            page.keyboard.press("Enter")

        # espera capturar o token (a app chama a API ao logar)
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
    corte = datetime.now() - timedelta(days=DIAS)
    rows = []
    for t in tickets:
        dt = parse_dt(t.get("creationDateTime") or t.get("creationDate"))
        if dt and dt < corte: continue
        rows.append({
            "numero": str(t.get("id")),
            "aba": aba_from((t.get("serviceCompany") or {}).get("name")),
            "loja": t.get("companyName") or (t.get("company") or {}).get("name"),
            "descricao": t.get("description"),
            "status": str(t.get("status")) if t.get("status") is not None else None,
            "data_criacao": dt.strftime("%Y-%m-%d") if dt else None,
        })
    print(f"{len(tickets)} lidos | {len(rows)} dentro de {DIAS} dias")
    upsert(rows)

if __name__ == "__main__":
    main()
