# -*- coding: utf-8 -*-
"""
DIAGNÓSTICO do lançamento no Trílogo — roda no GitHub Actions e imprime um raio-X:
  [1] Que revisão do trilogo_lancar.py está NO REPO (sem executá-lo)
  [2] Runs recentes do trilogo-lancar.yml: status e, se pendurado, EM QUAL STEP está
  [3] Navegador: o Chrome do runner abre? (channel='chrome' / chromium)
  [4] Motor (Render): /robot/lancar_worklist responde? quantos itens?
  [5] Login no Trílogo (conta Instalações) + token
  [6] APIs do Trílogo: ListTicketsByUser (status), GetTicketCosts, e o TESTE DO OFFSET
      (se a API ignora o Offset, o prefetch do adicional-3 entra em loop — adicional-4 corrige)
Só LÊ — não lança, não move, não grava nada.
"""
import os, re, sys, json, time, urllib.request, urllib.parse, functools
print = functools.partial(print, flush=True)

OK = "✔"; ERRO = "✘"
def sec(t): print(f"\n===== {t} =====")

def _http(url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)

# ---------- [1] revisão do arquivo no repo ----------
sec("[1] REVISÃO DO trilogo_lancar.py NO REPO")
try:
    txt = open("trilogo_lancar.py", encoding="utf-8").read()
    m = re.search(r'ROBOT_LANCAR_REV\s*=\s*\(?\s*["\'](.+?)["\']', txt)
    print(f"{OK} rev: {m.group(1)[:110] if m else 'NÃO ACHEI o ROBOT_LANCAR_REV'}")
    print(f"   tem _prefetch_status: {'sim' if '_prefetch_status' in txt else 'NÃO'}"
          f" · blindado (página repetida): {'sim' if 'API repetiu a página' in txt else 'NÃO -> É O adicional-3, TROCAR pelo adicional-4'}")
    tem_chrome = ('channel="chrome"' in txt) or ("channel='chrome'" in txt)
    print(f"   usa Chrome do runner (channel='chrome'): {'sim' if tem_chrome else 'NÃO (script antigo)'}")
except Exception as e:
    print(f"{ERRO} não li o arquivo: {e}")

# ---------- [2] runs recentes e step pendurado ----------
sec("[2] RUNS RECENTES DO trilogo-lancar.yml")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", ""); GH_REPO = os.environ.get("GH_REPO", "")
try:
    h = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "diag"}
    r = json.loads(_http(f"https://api.github.com/repos/{GH_REPO}/actions/workflows/trilogo-lancar.yml/runs?per_page=5", h).read().decode())
    runs = r.get("workflow_runs") or []
    if not runs: print("   (nenhum run encontrado)")
    for run in runs:
        print(f" - run {run['id']} | {run['status']}/{run.get('conclusion')} | criado {run['created_at']} | commit {run['head_sha'][:7]}")
        if run["status"] in ("in_progress", "queued"):
            jr = json.loads(_http(f"https://api.github.com/repos/{GH_REPO}/actions/runs/{run['id']}/jobs", h).read().decode())
            for job in jr.get("jobs", []):
                print(f"     job '{job['name']}': {job['status']}/{job.get('conclusion')}")
                for stp in job.get("steps", []):
                    if stp["status"] != "completed" or stp.get("conclusion") not in ("success", "skipped"):
                        print(f"       >>> step '{stp['name']}': {stp['status']}/{stp.get('conclusion')}   <- AQUI")
except Exception as e:
    print(f"{ERRO} não consultei os runs: {str(e)[:160]}")

# ---------- [3] navegador ----------
sec("[3] NAVEGADOR NO RUNNER")
page = None
try:
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    br = None
    try:
        br = pw.chromium.launch(channel="chrome", headless=True); print(f"{OK} Chrome do runner abriu (channel='chrome')")
    except Exception as e1:
        print(f"{ERRO} channel='chrome' falhou: {str(e1)[:100]}")
        try: br = pw.chromium.launch(headless=True); print(f"{OK} chromium do playwright abriu (fallback)")
        except Exception as e2: print(f"{ERRO} chromium também falhou: {str(e2)[:100]}")
    if br:
        UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        ctx = br.new_context(user_agent=UA); page = ctx.new_page(); page.set_default_timeout(15000)
except Exception as e:
    print(f"{ERRO} playwright: {str(e)[:120]}")

# ---------- [4] motor ----------
sec("[4] MOTOR (Render)")
MOTOR = os.environ.get("MOTOR_URL", "").rstrip("/"); RKEY = os.environ.get("ROBOT_KEY", "")
itens = []
try:
    t0 = time.time()
    r = json.loads(_http(f"{MOTOR}/robot/lancar_worklist", {"x-robot-key": RKEY}, timeout=60).read().decode())
    itens = r.get("itens", [])
    print(f"{OK} worklist respondeu em {time.time()-t0:.1f}s: {len(itens)} orçamento(s) nas pastas 1/4")
except Exception as e:
    print(f"{ERRO} worklist falhou: {str(e)[:160]}")

# ---------- [5] login no Trílogo ----------
sec("[5] LOGIN NO TRÍLOGO (Instalações)")
EMAIL = os.environ.get("TRILOGO_EMAIL", ""); SENHA = os.environ.get("TRILOGO_SENHA", "")
token = None
if page and EMAIL and SENHA:
    try:
        t0 = time.time()
        page.goto("https://mercadinhossaoluiz.trilogo.app/", wait_until="domcontentloaded")
        SEL = ("input[type=email], input[placeholder*='mail' i], input[name*='mail' i], input[id*='mail' i], input[type=text]")
        page.wait_for_selector(SEL, timeout=25000); page.locator(SEL).first.fill(EMAIL)
        try: page.get_by_role("button", name=re.compile("continuar|prosseguir|avan|entrar|login", re.I)).click(timeout=5000)
        except Exception: page.keyboard.press("Enter")
        page.wait_for_selector("input[type=password]", timeout=25000)
        page.locator("input[type=password]").first.fill(SENHA)
        try: page.get_by_role("button", name=re.compile("entrar|continuar|acessar|login", re.I)).click(timeout=5000)
        except Exception: page.keyboard.press("Enter")
        for _ in range(20):
            token = page.evaluate("() => { try { return JSON.parse(localStorage.getItem('session')||'{}').accessToken||null } catch(e){ return null } }")
            if token: break
            page.wait_for_timeout(500)
        print((f"{OK} login OK em {time.time()-t0:.0f}s — token capturado") if token else f"{ERRO} logou mas SEM token no localStorage")
    except Exception as e:
        print(f"{ERRO} login falhou: {str(e)[:160]}")
else:
    print(f"{ERRO} pulado (sem navegador ou sem TRILOGO_EMAIL/TRILOGO_SENHA)")

# ---------- [6] APIs + teste do Offset ----------
sec("[6] APIs DO TRÍLOGO + TESTE DO OFFSET (causa do loop)")
if page and token:
    def _lista(offset, limit=200):
        return page.evaluate("""async (a) => {
          const s=JSON.parse(localStorage.getItem('session')||'{}');
          try{
            const r=await fetch('https://web.api.trilogo.app/api/Ticket/ListTicketsByUser',
              {method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+s.accessToken},
               body:JSON.stringify({StatusActions:'1,2,3,4,5,6,7,8,9,10',OnlyUnread:false,Offset:a.off,Limit:a.lim})});
            let j=null; try{j=await r.json();}catch(e){}
            const t=(j&&Array.isArray(j.tickets))?j.tickets:[];
            return {status:r.status, n:t.length, ids:t.slice(0,5).map(x=>String(x.id))};
          }catch(e){return {status:-1, err:String(e).slice(0,80)}}
        }""", {"off": offset, "lim": limit})
    try:
        p0 = _lista(0); print(f"   ListTicketsByUser Offset=0  -> status {p0.get('status')}, {p0.get('n')} tickets, primeiros: {p0.get('ids')}")
        p1 = _lista(200); print(f"   ListTicketsByUser Offset=200-> status {p1.get('status')}, {p1.get('n')} tickets, primeiros: {p1.get('ids')}")
        if p0.get("status") == 200 and p1.get("status") == 200:
            if p0.get("ids") and p0.get("ids") == p1.get("ids") and (p1.get("n") or 0) >= 200:
                print(f"   {ERRO} A API IGNORA o Offset (páginas idênticas e cheias) -> o adicional-3 ENTRA EM LOOP aqui. Use o adicional-4.")
            else:
                print(f"   {OK} paginação com Offset funciona (páginas diferentes ou última página)")
        tk = next((str(x.get("ticket")) for x in itens if x.get("ticket")), None)
        if tk:
            c = page.evaluate("""async (tk) => {
              const s=JSON.parse(localStorage.getItem('session')||'{}');
              try{ const r=await fetch('https://web.api.trilogo.app/api/Ticket/GetTicketCosts?ticketId='+encodeURIComponent(tk),
                     {headers:{'Authorization':'Bearer '+s.accessToken}});
                   let j=null; try{j=await r.json();}catch(e){}
                   return {status:r.status, n:Array.isArray(j)?j.length:null}; }catch(e){return {status:-1}}
            }""", tk)
            print(f"   GetTicketCosts (ticket {tk}) -> status {c.get('status')}, {c.get('n')} custo(s)")
    except Exception as e:
        print(f"{ERRO} teste de API falhou: {str(e)[:160]}")
else:
    print(f"{ERRO} pulado (sem login/token)")

sec("FIM — me mande este log completo")
