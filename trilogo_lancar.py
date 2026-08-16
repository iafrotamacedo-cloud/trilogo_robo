# -*- coding: utf-8 -*-
"""
Robô de LANÇAMENTO / CONFERÊNCIA de orçamentos no Trílogo (custo do ticket).

Pega a lista no motor do FrotaHub, loga na conta e trabalha os orçamentos das pastas
1 (normais) e 4 (rateio). Roda 1 conta por execução (o workflow chama 2x: Instalações e Civil).

>>> NOVIDADE (rev API): a existência de custo é lida pela API do Trílogo
    GET https://web.api.trilogo.app/api/Ticket/GetTicketCosts?ticketId={numero}
    (token vem do localStorage['session'].accessToken após o login).
    Resposta: []  -> nenhum custo (ainda não lançado)
              [ {..} ] -> já tem custo (Trílogo NÃO deixa inserir um 2º) => JÁ LANÇADO.
    Cada custo traz: type, totalValue, documentNumber, invoiceFiles[].fileName.

MODO=conferir : só LÊ os custos pela API (segundos) e REPORTA quais tickets já têm custo
                (duplicidade). READ-ONLY: não move arquivo, não marca lançado, não lança.
MODO=lancar   : para cada orçamento, PRÉ-CHECA por API; se já tem custo, só reconcilia
                (sem abrir a tela); se não tem, abre "Custos do ticket > Novo custo",
                sobe o PDF, Tipo=Materiais, Valor=TOTAL GERAL, Nº do documento=ticket, conclui.

Segredos (GitHub):
  MOTOR_URL   ex.: https://motor-orcamentos.onrender.com
  ROBOT_KEY   mesmo valor da variável ROBOT_KEY no Render
  TRILOGO_EMAIL, TRILOGO_SENHA, ABA (CIVIL|INSTALACOES)

DEDUP: o motor só devolve o que ainda está em 1/4; e só move/marca quando confirma.
       Nada é apagado (Dropbox move para a pasta "lançados").
"""
import sys, time
try: sys.stdout.reconfigure(line_buffering=True)   # GitHub faz buffer -> força linha a linha
except Exception: pass
print("BOOT 1/3: processo Python iniciou", flush=True)
import os, re, json, tempfile, functools, traceback, urllib.request, urllib.error, urllib.parse
print("BOOT 2/3: imports básicos ok — importando playwright (pode levar alguns segundos)…", flush=True)
from playwright.sync_api import sync_playwright
print("BOOT 3/3: playwright importado — robô pronto para iniciar", flush=True)
print = functools.partial(print, flush=True)
ROBOT_LANCAR_REV = "form-fix-3 (clique manual via JS + anexo no input do custo + tipo=Mão de obra)"
print(f"ROBO lançar rev: {ROBOT_LANCAR_REV}")

MOTOR = os.environ.get("MOTOR_URL", "").rstrip("/")
RKEY  = os.environ.get("ROBOT_KEY", "")
if not MOTOR or not RKEY:
    print("ERRO DE CONFIGURAÇÃO: defina os secrets do GitHub 'MOTOR_URL' (ex.: "
          "https://SEU-MOTOR.onrender.com) e 'ROBOT_KEY' (mesmo valor da variável "
          "ROBOT_KEY no Render). MOTOR_URL=%r ROBOT_KEY=%s" % (MOTOR, "vazio" if not RKEY else "ok"))
    sys.exit(1)
if not MOTOR.startswith("http"):
    print("ERRO: MOTOR_URL precisa começar com http(s):// — valor atual: %r" % MOTOR)
    sys.exit(1)
EMAIL = os.environ["TRILOGO_EMAIL"]
SENHA = os.environ["TRILOGO_SENHA"]
ABA   = os.environ.get("ABA", "").upper()
ALVO  = os.environ.get("ALVO", "").strip()   # "origem/arquivo" -> lança só esse; vazio = todos
MODO  = (os.environ.get("MODO", "") or "lancar").strip()   # "conferir" = só lê custos, não lança
BASE_URL  = "https://mercadinhossaoluiz.trilogo.app"
LOGIN_URL = BASE_URL + "/"
API_URL   = "https://web.api.trilogo.app"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def _get(path):
    req = urllib.request.Request(f"{MOTOR}{path}", headers={"x-robot-key": RKEY})
    return json.loads(urllib.request.urlopen(req, timeout=45).read().decode())

def _post(path, obj):
    req = urllib.request.Request(f"{MOTOR}{path}", data=json.dumps(obj).encode(), method="POST",
        headers={"x-robot-key": RKEY, "content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())

def _prog(arquivo, status, pct):
    """Reporta o andamento de um orçamento para o motor (barra da tela)."""
    try: _post("/robot/lancar_progresso", {"arquivo": arquivo, "status": status, "pct": pct})
    except Exception: pass

def _baixa_pdf(origem, nome):
    url = f"{MOTOR}/robot/lancar_pdf?key={urllib.parse.quote(RKEY)}&origem={origem}&nome={urllib.parse.quote(nome)}"
    data = urllib.request.urlopen(urllib.request.Request(url), timeout=120).read()
    f = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False); f.write(data); f.close()
    return f.name

def _fmt_valor(v):
    try: return f"{float(v):.2f}".replace(".", ",")
    except Exception: return str(v)

def _tipo_nome(t):
    return {1: "Mão de obra", 2: "Materiais", 3: "Mão de obra e Materiais"}.get(t, f"tipo {t}")

def _custo_casa(custos, valor, arquivo):
    """Retorna 'valor'/'arquivo' se algum custo do ticket corresponde a ESTE orçamento
    (mesmo valor ou mesmo PDF) — sinal de que foi lançado (inclusive MANUALMENTE).
    Retorna None se nenhum custo bate (custo é de outro orçamento => conflito real)."""
    try: v = round(float(valor), 2) if valor is not None else None
    except Exception: v = None
    base_arq = os.path.splitext((arquivo or "").rsplit("/", 1)[-1].lower())[0]
    for c in (custos or []):
        cv = c.get("valor")
        try: cv = round(float(cv), 2) if cv is not None else None
        except Exception: cv = None
        if v is not None and cv is not None and abs(cv - v) < 0.01:
            return "valor"
        pdf = os.path.splitext((c.get("pdf") or "").lower())[0]
        if pdf and base_arq and pdf == base_arq:
            return "arquivo"
    return None

def _custo_do_ticket(custos, tk):
    """Só os custos cujo 'documento' (documentNumber) é o PRÓPRIO ticket — descarta o
    custo FANTASMA que a API às vezes devolve para ticket inexistente/errado."""
    tks = str(tk).strip()
    return [c for c in (custos or []) if str(c.get("doc") or "").strip() == tks]

def _tem_custo_real(custos, tk, valor, nome):
    """Custo conta como REAL se pertence ao ticket (documento == ticket) OU casa com
    ESTE orçamento (valor/arquivo). Ignora fantasma."""
    return bool(_custo_do_ticket(custos, tk)) or bool(_custo_casa(custos, valor, nome))

# ---- Existência do ticket (a API de custos devolve fantasma p/ ticket inexistente) ----
_JS_EXISTE = r"""
async (tk) => {
  const s = JSON.parse(localStorage.getItem('session') || '{}');
  const tkn = s.accessToken;
  if (!tkn) return { status: 0, existe: null };
  try {
    const r = await fetch('https://web.api.trilogo.app/api/Ticket/ListTicketsByUser',
      { method:'POST',
        headers:{'Content-Type':'application/json','Authorization':'Bearer '+tkn},
        body: JSON.stringify({ searchTerm: String(tk), page:1, pageSize:20 }) });
    let j = null; try { j = await r.json(); } catch(e) {}
    const arr = (j && Array.isArray(j.tickets)) ? j.tickets : [];
    const existe = arr.some(t => String(t.id) === String(tk));
    return { status: r.status, existe: existe };
  } catch (e) { return { status: -1, existe: null }; }
}
"""

def _ticket_existe(page, tk):
    """True/False se o ticket existe no Trílogo; None se não deu pra verificar."""
    try:
        r = page.evaluate(_JS_EXISTE, str(tk))
    except Exception:
        return None
    if r.get("status") == 200:
        return bool(r.get("existe"))
    return None

def login(page):
    print("  login: abrindo tela…", flush=True)
    page.goto(LOGIN_URL, wait_until="domcontentloaded")   # networkidle trava em SPA
    EMAIL_SEL = ("input[type=email], input[placeholder*='mail' i], input[name*='mail' i], "
                 "input[id*='mail' i], input[type=text]")
    page.wait_for_selector(EMAIL_SEL, timeout=25000)
    page.locator(EMAIL_SEL).first.fill(EMAIL)
    try: page.get_by_role("button", name=re.compile("continuar|prosseguir|avan|próximo|proximo|entrar|login", re.I)).click(timeout=5000)
    except Exception: page.keyboard.press("Enter")
    page.wait_for_selector("input[type=password]", timeout=25000)
    page.locator("input[type=password]").first.fill(SENHA)
    try: page.get_by_role("button", name=re.compile("entrar|continuar|acessar|login", re.I)).click(timeout=5000)
    except Exception: page.keyboard.press("Enter")
    page.wait_for_timeout(3000)
    # confirma que temos token (necessário para as chamadas de API)
    tk = _token(page)
    ci = _company(page)
    print("  login: ok" + ("" if tk else " (ATENÇÃO: token não encontrado no localStorage!)")
          + f" — pessoa={ci.get('pessoa')} · usuário={ci.get('email')} · companyGroup={ci.get('id')} ({ci.get('name')})", flush=True)
    if ci.get("name") and ("mercadinho" not in str(ci.get("name")).lower()):
        print(f"  ⚠️ ATENÇÃO: a sessão do robô NÃO está no Mercadinhos São Luiz e sim em '{ci.get('name')}' "
              f"(companyGroup {ci.get('id')}). As consultas de custo/existência vão sair ERRADAS.", flush=True)
    # AUTO-TESTE de diagnóstico: o que ESTA sessão enxerga p/ tickets-chave (comparar com a verdade)
    for _tk in ("126713","126039","126400","126670","125973","126454"):
        try:
            _ex = _ticket_existe(page, _tk)
            _ok, _cs = _custos_api(page, _tk)
            print(f"  [selftest] ticket {_tk}: existe={_ex} custos={len(_cs)} docs={[c.get('doc') for c in _cs]}", flush=True)
        except Exception as _e:
            print(f"  [selftest] ticket {_tk}: erro {str(_e)[:60]}", flush=True)

def _company(page):
    """Lê companyGroupId/Name/email do token da sessão (pra conferir QUEM/QUAL cliente o robô usa)."""
    try:
        return page.evaluate("""() => {
          try {
            const t = JSON.parse(localStorage.getItem('session')||'{}').accessToken;
            const c = JSON.parse(atob(t.split('.')[1].replace(/-/g,'+').replace(/_/g,'/')));
            return { id: c['custom:companyGroupId'] || null, name: c['custom:companyGroupName'] || null,
                     email: c['email'] || c['custom:unique_name'] || null,
                     pessoa: c['custom:given_name'] || c['name'] || c['cognito:username'] || null };
          } catch(e) { return { id:null, name:null, email:null, pessoa:null }; }
        }""")
    except Exception:
        return {"id": None, "name": None, "email": None}

def _token(page):
    try:
        return page.evaluate("() => { const s = JSON.parse(localStorage.getItem('session')||'{}'); return s.accessToken || null; }")
    except Exception:
        return None

# ---- Leitura de custos pela API do Trílogo (rápida) ------------------------------
_JS_COSTS = r"""
async (tk) => {
  const s = JSON.parse(localStorage.getItem('session') || '{}');
  const tkn = s.accessToken;
  if (!tkn) return { status: 0, err: 'sem token' };
  try {
    const r = await fetch('https://web.api.trilogo.app/api/Ticket/GetTicketCosts?ticketId=' + encodeURIComponent(tk),
                          { headers: { 'Authorization': 'Bearer ' + tkn } });
    let j = null; try { j = await r.json(); } catch (e) {}
    return { status: r.status, arr: Array.isArray(j) ? j : null };
  } catch (e) {
    return { status: -1, err: String(e) };
  }
}
"""

def _custos_api(page, tk):
    """Lê os custos do ticket pela API. Retorna (ok_conta, custos).
       ok_conta=True  -> 200: 'custos' é a lista normalizada (pode ser []).
       ok_conta=False -> 401/403/404: ticket não é desta conta / sem acesso.
       ok_conta=None  -> erro/indefinido (deixa o chamador decidir)."""
    try:
        r = page.evaluate(_JS_COSTS, str(tk))
    except Exception as e:
        print(f"    (api) erro evaluate ticket {tk}: {str(e)[:80]}"); return (None, [])
    st = r.get("status"); arr = r.get("arr")
    if st == 200 and isinstance(arr, list):
        custos = []
        for c in arr:
            files = c.get("invoiceFiles") or []
            custos.append({
                "tipo":  _tipo_nome(c.get("type")),
                "valor": c.get("totalValue"),
                "doc":   c.get("documentNumber"),
                "pdf":   (files[0].get("fileName") if files else None),
            })
        return (True, custos)
    if st in (401, 403, 404):
        return (False, [])
    print(f"    (api) ticket {tk}: status inesperado {st}"); return (None, [])

# ---- Helpers de UI (ainda usados no fluxo de LANÇAMENTO real) --------------------
_JS_CLICK = r"""
(rx) => {
  const re = new RegExp(rx, 'i');
  const cand = [...document.querySelectorAll('button,[role=button],a,span,div')]
     .find(e => e.offsetParent!==null && e.querySelectorAll('*').length<=3 && re.test((e.textContent||'').trim()));
  if(!cand) return false;
  cand.scrollIntoView({block:'center'});
  cand.click();               // bubbla até o handler (ex.: cabeçalho ant-collapse)
  return true;
}
"""
def _click_js(page, pattern, tries=24, gap=500):
    """Espera aparecer um elemento cujo texto casa 'pattern' e clica nele (via JS)."""
    for _ in range(tries):
        try:
            if page.evaluate(_JS_CLICK, pattern): return True
        except Exception: pass
        page.wait_for_timeout(gap)
    return False

_JS_MARK_ANEXO = r"""
() => {
  const ins=[...document.querySelectorAll('input[type=file]')];
  // preferir o input de anexo DO CUSTO manual (evita o leitor de nota fiscal por IA)
  let alvo = ins.find(e => /relacionados a este custo|outros arquivos/i.test(((e.closest('div')||{}).textContent)||''));
  if(!alvo && ins.length) alvo = ins[ins.length-1];   // fallback: o último
  if(!alvo) return null;
  if(!alvo.id) alvo.id = 'robo_anexo_custo';
  return '#'+alvo.id;
}
"""

_JS_CUSTOS_DOM = r"""
() => {
  const brl = s => { const m=(s||'').match(/R\$\s*([\d.]+),(\d{2})/); return m? parseFloat(m[1].replace(/\./g,'')+'.'+m[2]) : null; };
  const cards = [...document.querySelectorAll('div')].filter(d=>{
    const t=d.innerText||''; return /(m[aã]o de obra|materiais)/i.test(t) && /R\$\s*[\d.]+,\d{2}/.test(t) && t.length<250 && d.querySelectorAll('div').length<10;
  });
  const uniq = cards.filter((c,i)=> !cards.some((o,j)=>j<i && o.contains(c)));
  return uniq.map(c=>({tipo:(c.innerText.match(/m[aã]o de obra|materiais/i)||[''])[0], valor: brl(c.innerText)})).filter(x=>x.valor!=null);
}
"""

# ---- CONFERÊNCIA (rápida, por API) -----------------------------------------------
def conferir_um(page, it):
    """Regra CERTA (usa a marca 'lancado' do BD como árbitro):
       - ticket SEM custo no Trílogo            -> a lançar (não é duplicata), não move.
       - ticket COM custo e orçamento lancado=1 -> foi ESTE que lançaram -> move (limpa 1->2/4->5).
       - ticket COM custo e orçamento lancado=0 -> o custo é de OUTRO orçamento -> CONFLITO,
                                                    NÃO move, deixa a nota onde está.
       Nunca apaga."""
    tk=it.get("ticket"); origem=it.get("origem"); nome=it.get("arquivo")
    valor=it.get("valor"); lancado_bd=bool(it.get("lancado"))
    if not tk:
        _post("/robot/conferir_resultado", {"arquivo":nome,"origem":origem,"ticket":tk,"valor":valor,
              "custos":[],"veredito":"sem_ticket","aberto":False}); return
    ok_conta, custos = (None, [])
    for _t in range(3):                       # retry: erro transitório NÃO pode virar "sem custo"
        ok_conta, custos = _custos_api(page, tk)
        if ok_conta is not None: break
        time.sleep(0.6)
    if ok_conta is None:    # NÃO deu pra verificar -> não afirma que está livre pra lançar
        print(f"  ticket {tk}: NÃO VERIFICADO (erro na API)", flush=True)
        _post("/robot/conferir_resultado", {"arquivo":nome,"origem":origem,"ticket":tk,"valor":valor,
              "custos":[],"veredito":"nao_verificado","duplicata":False,"incerto":True,"aberto":True}); return
    if ok_conta is False:   # ticket de outra conta — a outra conta confere
        print(f"  ticket {tk}: fora desta conta (pula)")
        _post("/robot/conferir_resultado", {"arquivo":nome,"origem":origem,"ticket":tk,"valor":valor,
              "custos":[],"veredito":"outra_conta","aberto":False,"outra_conta":True}); return
    # EXISTÊNCIA: a API de custos devolve custo FANTASMA p/ ticket inexistente -> valida antes
    existe = _ticket_existe(page, tk)
    if existe is False:
        print(f"  ticket {tk}: NÃO EXISTE no Trílogo — número errado (NÃO é duplicata)", flush=True)
        _post("/robot/conferir_resultado", {"arquivo":nome,"origem":origem,"ticket":tk,"valor":valor,
              "custos":[],"veredito":"ticket_inexistente","duplicata":False,"inexistente":True,"aberto":True}); return
    # custo REAL do ticket (descarta fantasma: só conta documento==ticket ou casa com este orçamento)
    tem_custo = _tem_custo_real(custos, tk, valor, nome)
    veredito=""
    # READ-ONLY: a conferência NÃO move nem marca nada — só detecta e reporta a duplicidade.
    if not tem_custo:
        veredito="a_lancar"                                     # sem custo REAL do ticket -> pode lançar
    elif lancado_bd:
        veredito="ja_lancado"                                   # sistema já lançou este
    elif _custo_casa(custos, valor, nome):
        veredito="lancado_manual"                               # lançado NA MÃO (valor/PDF batem)
    else:
        veredito="conflito"                                     # custo do ticket é de OUTRO orçamento
    print(f"  ticket {tk}: custos_api={len(custos)} real={'sim' if tem_custo else 'nao'} lancado_bd={lancado_bd} -> {veredito} (read-only)", flush=True)
    _post("/robot/conferir_resultado", {"arquivo":nome,"origem":origem,"ticket":tk,"valor":valor,
          "custos":custos,"veredito":veredito,
          "duplicata":(veredito in ("ja_lancado","lancado_manual","conflito")),
          "reconciliado":False,"conflito":(veredito=="conflito"),"aberto":True})
    time.sleep(0.06)   # gentileza com a API

# ---- LANÇAMENTO real (com pré-checagem por API) ----------------------------------
def lancar_um(page, it):
    """Cria o custo no ticket. Devolve True só se confirmar o sucesso (ou se já estava lançado)."""
    tk = it.get("ticket"); origem = it.get("origem"); nome = it.get("arquivo")
    valor = it.get("valor")
    def fail(msg, marca=True):
        print(f"[falha] ticket {tk}: {msg}", flush=True)
        if marca: _prog(nome, "falha", 0)
        return False
    if not tk: return fail("sem ticket associado")
    lancado_bd = bool(it.get("lancado"))

    # NÃO lançar em ticket que não existe (número errado na origem)
    if _ticket_existe(page, tk) is False:
        print(f"[skip] ticket {tk}: NÃO EXISTE no Trílogo — número errado, não lanço", flush=True)
        _prog(nome, "ticket inexistente no Trílogo — corrigir o número", 0)
        return False
    # PRÉ-CHECAGEM POR API: só conta custo REAL do ticket (ignora fantasma).
    ok_conta, custos_api = _custos_api(page, tk)
    if ok_conta and _tem_custo_real(custos_api, tk, valor, nome):
        if lancado_bd or _custo_casa(custos_api, valor, nome):
            print(f"[ja-lancado] ticket {tk}: custo é deste orçamento (sistema ou manual) — reconcilia, não relança", flush=True)
            _prog(nome, "já lançado", 100)
            return True                      # main move (1->2 / 4->5)
        print(f"[conflito] ticket {tk}: já tem custo de OUTRO orçamento — NÃO lanço, deixo a nota onde está", flush=True)
        _prog(nome, "conflito: ticket já tem custo de outro orçamento", 0)
        return False                         # NÃO move (main só move se retornar True)
    # (ok_conta False/None cai no fluxo normal: a própria tela confirma se é desta conta)

    _prog(nome, "abrindo ticket", 15)
    print(f"  ticket {tk}: abrindo…", flush=True)
    page.goto(f"{BASE_URL}/ticket/{tk}", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if "ticket" not in page.url:
        print(f"[skip] ticket {tk} não abriu nesta conta"); return False   # não marca falha (outra conta)
    # "Custos do ticket" é um <span>, NÃO um botão -> clique por JS (bubbla e expande)
    if not _click_js(page, r"^\s*custos do ticket\s*$"): return fail("não achei a seção 'Custos do ticket'")
    page.wait_for_timeout(1000)
    # TRAVA ANTI-DUPLICAÇÃO (2ª camada, DOM): confirma que ninguém adicionou custo no meio-tempo.
    try: custos = page.evaluate(_JS_CUSTOS_DOM) or []
    except Exception: custos = []
    if custos:
        if lancado_bd or _custo_casa(custos, valor, nome):
            print(f"[ja-lancado] ticket {tk}: custo (tela) é deste orçamento — reconcilia", flush=True)
            _prog(nome, "já lançado", 100)
            return True
        print(f"[conflito] ticket {tk}: já tem custo (tela) de OUTRO orçamento — não lanço, deixo onde está", flush=True)
        _prog(nome, "conflito: ticket já tem custo de outro orçamento", 0)
        return False
    if not _click_js(page, r"^\s*\+?\s*novo custo\s*$"): return fail("não apareceu 'Novo custo'")
    page.wait_for_timeout(1400)
    _prog(nome, "preenchendo", 45)
    # O "Novo custo" abre LIDERANDO com a IA de leitura de nota fiscal; é preciso clicar
    # "Preencher informações manualmente" pra revelar o form manual (#costType, #serviceCost,
    # #documentNumber). ISSO É UM LINK, não um <button> -> get_by_role("button") NÃO pega.
    # Uso o clique por JS (mesmo método de "Custos do ticket"/"Novo custo"): enxerga o link,
    # rola até ele e bubbla o clique. (Foi o que falhava e derrubava todos os lançamentos.)
    manual_ok = _click_js(page, r"preencher.*manual", tries=16, gap=500)
    if not manual_ok:
        print(f"[aviso] ticket {tk}: não cliquei 'Preencher informações manualmente' — tentando seguir", flush=True)
    # confirma o FORM MANUAL pela ASSINATURA dele: #serviceCost VISÍVEL. (Os input[type=file]
    # são ocultos e existem também no leitor de IA, então não servem de sinal de "abriu".)
    try:
        page.wait_for_selector("#serviceCost", state="visible", timeout=15000)
    except Exception:
        return fail("form manual de 'Novo custo' não abriu (a tela do Trílogo mudou)")
    page.wait_for_timeout(600)
    # anexa o orçamento NO input de anexo DO CUSTO ("outros arquivos relacionados a este custo"),
    # NUNCA no dropzone da nota fiscal (esse dispararia o leitor de IA). O input é oculto ->
    # marco por JS e uso set_input_files (funciona em input file oculto). Anexo é best-effort.
    pdf = _baixa_pdf(origem, nome)
    try:
        sel = page.evaluate(_JS_MARK_ANEXO)
        if sel:
            page.set_input_files(sel, pdf)
            page.wait_for_timeout(2500)
        else:
            print(f"[aviso] ticket {tk}: não achei o campo de anexo do custo — sigo sem anexar", flush=True)
    except Exception as e:
        print(f"[aviso] ticket {tk}: não anexei o PDF ({str(e)[:80]}) — sigo preenchendo", flush=True)
    # Tipo de custo = Mão de obra  (ant-select #costType). O '$' evita casar com a opção
    # combinada "Mão de obra e Materiais".
    try:
        page.locator("#costType").click(timeout=5000); page.wait_for_timeout(400)
        try: page.get_by_role("option", name=re.compile(r"^\s*m[aã]o de obra\s*$", re.I)).first.click(timeout=4000)
        except Exception: page.locator(".ant-select-item-option", has_text=re.compile(r"^\s*m[aã]o de obra\s*$", re.I)).first.click(timeout=4000)
    except Exception as e:
        return fail(f"não setei 'Mão de obra' ({e})")
    # Valor = #serviceCost  (máscara de moeda -> digita os centavos)
    try:
        cents = str(int(round(float(valor) * 100)))
        v = page.locator("#serviceCost"); v.click(); page.keyboard.press("Control+A"); page.keyboard.press("Delete")
        v.type(cents, delay=40)
    except Exception as e:
        return fail(f"campo Valor ({e})")
    # Número do documento = ticket  (#documentNumber)
    try:
        d = page.locator("#documentNumber"); d.click(); page.keyboard.press("Control+A"); page.keyboard.press("Delete")
        d.type(str(tk), delay=20)
    except Exception as e:
        return fail(f"campo Documento ({e})")
    _prog(nome, "concluindo", 90)
    page.wait_for_timeout(400)
    if not _click_js(page, r"^\s*concluir\s*$", tries=6): return fail("não achei 'Concluir'")
    # confirma sucesso (toast)
    try:
        page.get_by_text(re.compile(r"custo inserido com sucesso|sucesso", re.I)).first.wait_for(timeout=12000)
        print(f"[ok] ticket {tk}: custo lançado (R$ {_fmt_valor(valor)})"); return True
    except Exception:
        return fail("não vi confirmação de sucesso — NÃO vou mover")

def main():
    print(f"PASSO A: buscando lista no motor ({MOTOR}/robot/lancar_worklist) …")
    t0 = time.time()
    try:
        itens = _get("/robot/lancar_worklist").get("itens", [])
    except Exception as e:
        print(f"PASSO A FALHOU após {time.time()-t0:.0f}s: {e}"); sys.exit(1)
    print(f"PASSO A OK em {time.time()-t0:.0f}s: {len(itens)} orçamento(s) no total (pastas 1 e 4)")
    # nesta conta: processa os da aba correspondente + os "?" (tenta; se não for desta conta, pula)
    def _cabe(a):
        a = (a or "").upper()
        return (a == ABA) or (a in ("", "?"))
    fila = [x for x in itens if _cabe(x.get("aba"))]
    if MODO != "conferir":   # nunca LANÇA um arquivo sem registro no BD (não rastreado)
        _sr=[x for x in fila if x.get("sem_registro")]
        if _sr: print(f"pulando {len(_sr)} sem registro no BD: "+", ".join(x['arquivo'] for x in _sr[:5]))
        fila=[x for x in fila if not x.get("sem_registro")]
    if ALVO:   # lançar só um orçamento específico (botão 'Lançar' da linha)
        fila = [x for x in fila if f"{x['origem']}/{x['arquivo']}" == ALVO or x["arquivo"] == ALVO]
        print(f"ALVO único: {ALVO} -> {len(fila)} item(ns)")
    lim = int(os.environ.get("LIMITE") or "0")   # LIMITE=1 -> testa com 1; 0/vazio -> todos
    if lim > 0:
        fila = fila[:lim]; print(f"MODO TESTE: LIMITE={lim} -> processando só {len(fila)}")
    if MODO != "conferir":
        for x in fila: _prog(x["arquivo"], "aguardando", 0)   # popula a tela com a fila
    print(f"conta {ABA} [MODO={MODO}]: {len(fila)} de {len(itens)} orçamento(s) na fila")
    if not fila: print("nada a fazer nesta conta."); return
    feitos = 0
    with sync_playwright() as p:
        print("PASSO B: abrindo navegador (Chromium)…", flush=True)
        br = p.chromium.launch(headless=True)
        ctx = br.new_context(user_agent=UA)
        ctx.set_default_navigation_timeout(30000)
        page = ctx.new_page(); page.set_default_timeout(12000)   # falha rápido em vez de travar
        try:
            login(page)
        except Exception as e:
            print("Falha no login:", e); br.close(); sys.exit(1)
        for idx, it in enumerate(fila, 1):
            print(f"[{idx}/{len(fila)}] ticket {it.get('ticket')} · {it.get('arquivo')}", flush=True)
            try:
                if MODO == "conferir":
                    conferir_um(page, it); feitos += 1
                elif lancar_um(page, it):
                    r = _post("/robot/lancar_ok", {"origem": it["origem"], "nome": it["arquivo"]})
                    if r.get("ok"): feitos += 1; print(f"       movido: {it['arquivo']}")
            except Exception as e:
                print(f"[erro] {it.get('arquivo')}: {str(e)[:160]}")
        br.close()
    print(f"conta {ABA} [MODO={MODO}]: {feitos} processado(s). tempo total {time.time()-t0:.0f}s")

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        print("ERRO NÃO TRATADO:"); traceback.print_exc(); sys.exit(1)
