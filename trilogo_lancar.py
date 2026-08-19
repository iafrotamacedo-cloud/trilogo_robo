# -*- coding: utf-8 -*-
"""
Robô de LANÇAMENTO / CONFERÊNCIA de orçamentos no Trílogo (custo do ticket).

Pega a lista no motor do FrotaHub, loga na conta e trabalha os orçamentos das pastas
1 (normais) e 4 (rateio). Roda 1 conta por execução (o workflow chama 2x: Instalações e Civil).

>>> NOVIDADE (rev API): a existência de custo é lida pela API do Trílogo
    GET https://web.api.trilogo.app/api/Ticket/GetTicketCosts?ticketId={numero}
    (token vem do localStorage['session'].accessToken após o login).
    Resposta: []  -> nenhum custo
              [ {..} ] -> custos já lançados (valores usados na trava da SOMA).
    Cada custo traz: type, totalValue, documentNumber, invoiceFiles[].fileName.

>>> ORÇAMENTO ADICIONAL (rev adicional-1): um ticket que JÁ TEM custo PODE receber outro.
    "Já existe custo" só bloqueia quando o custo é DESTE orçamento (valor/PDF batem, ou o
    BD marca lançado) => reconcilia. Para lançar (1º ou adicional) valem DUAS TRAVAS:
      A) STATUS do ticket precisa ser Executado (7) ou Vistoriado (5) — senão pula com
         "ticket <N> não está Executado/Vistoriado";
      B) SOMA de todos os custos do ticket + o novo <= R$ 600,00 (TETO_SOMA) — senão pula
         com a mensagem exata "soma acima de R$600,00".

MODO=conferir : reverifica o PONTO DE FALHA de cada orçamento (existência do ticket, status
                atual — Aberto/Em execução/Arquivado — e valor) e REPORTA motivo + status_ticket.
                READ-ONLY: não move arquivo, não marca lançado, não lança.
MODO=lancar   : para cada orçamento, PRÉ-CHECA por API (existência, status, soma) e SEMPRE
                lança — a verificação de duplicidade foi REMOVIDA (não reconcilia/pula mais por
                custo já existente). Abre "Custos do ticket > Novo custo", sobe o PDF,
                Tipo=Materiais, Valor=TOTAL GERAL, Nº do documento=ticket, conclui.
                Continuam valendo: Trava A (status Executado/Vistoriado) e Trava B (teto da soma).

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
ROBOT_LANCAR_REV = ("adicional-6 (= adicional-5 + reporta MOTIVO e STATUS do ticket dos não "
                    "lançados; MODO=conferir agora reverifica o ponto de falha, não a duplicidade)")
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

# ---- REGRAS DO ORÇAMENTO ADICIONAL (mesmo ticket pode receber 2+ custos) ----
# Trava A: só lança se o status do ticket for Executado (7) ou Vistoriado (5).
# Trava B: soma de TODOS os custos já lançados no ticket + o novo <= TETO_SOMA.
STATUS_OK_LANCAR = {5, 7}                                  # 5=Vistoriado · 7=Executado (códigos da API)
STATUS_LABEL = {1: "Aberto", 6: "Em execução", 7: "Executado", 5: "Vistoriado", 3: "Arquivado"}
TETO_SOMA = float(os.environ.get("TETO_SOMA", "600"))      # R$ — teto da SOMA acumulada por ticket
BASE_URL  = "https://mercadinhossaoluiz.trilogo.app"
LOGIN_URL = BASE_URL + "/"
API_URL   = "https://web.api.trilogo.app"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# Espera após anexar o PDF (a IA do Trílogo processa e PODE sobrescrever campos se
# preenchermos cedo demais). Ajustável sem mexer no código: ROBO_IA_ESPERA_MS.
IA_ESPERA_MS = int(os.environ.get("ROBO_IA_ESPERA_MS", "7000"))

def _abre_navegador(p):
    """Abre o navegador SEM depender do download do Chromium do Playwright (~2-3 min por
       rodada no Actions): usa o CHROME que já vem instalado no runner (channel='chrome').
       Fallback: Chromium do Playwright (para rodar localmente onde ele já foi baixado)."""
    try:
        return p.chromium.launch(channel="chrome", headless=True)
    except Exception as e:
        print(f"  chrome do sistema indisponível ({str(e)[:80]}) — tentando o chromium do playwright", flush=True)
        return p.chromium.launch(headless=True)

def _get(path):
    req = urllib.request.Request(f"{MOTOR}{path}", headers={"x-robot-key": RKEY})
    return json.loads(urllib.request.urlopen(req, timeout=45).read().decode())

def _post(path, obj):
    req = urllib.request.Request(f"{MOTOR}{path}", data=json.dumps(obj).encode(), method="POST",
        headers={"x-robot-key": RKEY, "content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())

def _prog(arquivo, status, pct, motivo=None, status_ticket=None):
    """Reporta o andamento/RESULTADO de um orçamento para o motor (barra + lista de não lançados).
       motivo: categoria fixa do resultado — FORA_STATUS, TICKET_INEXISTENTE, VALOR_NAO_LIDO,
               NAO_VERIFICADO, SOMA_ACIMA, FALHA, SEM_TICKET, A_LANCAR, LANCADO.
       status_ticket: rótulo do status do ticket no Trílogo (Aberto/Em execução/Arquivado…)."""
    p={"arquivo": arquivo, "status": status, "pct": pct}
    if motivo is not None: p["motivo"]=motivo
    if status_ticket is not None: p["status_ticket"]=status_ticket
    try: _post("/robot/lancar_progresso", p)
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
    const t = arr.find(t => String(t.id) === String(tk));
    return { status: r.status, existe: !!t, tstatus: (t && t.status != null) ? t.status : null };
  } catch (e) { return { status: -1, existe: null, tstatus: null }; }
}
"""

_STATUS_MAP = {}   # {ticket: status} pré-carregado 1x por conta (lotes) — evita 1 chamada por item

_JS_LISTA = r"""
async (offset) => {
  const s = JSON.parse(localStorage.getItem('session') || '{}');
  const tkn = s.accessToken;
  if (!tkn) return { status: 0 };
  try {
    const r = await fetch('https://web.api.trilogo.app/api/Ticket/ListTicketsByUser',
      { method:'POST',
        headers:{'Content-Type':'application/json','Authorization':'Bearer '+tkn},
        body: JSON.stringify({ StatusActions:'1,2,3,4,5,6,7,8,9,10', OnlyUnread:false,
                               Offset: offset, Limit: 200 }) });
    let j = null; try { j = await r.json(); } catch(e) {}
    const arr = (j && Array.isArray(j.tickets)) ? j.tickets : [];
    return { status: r.status, tickets: arr.map(t => ({ id: t.id, st: t.status })) };
  } catch (e) { return { status: -1 }; }
}
"""

def _prefetch_status(page):
    """LOTE: 1 varredura paginada da ListTicketsByUser -> {ticket: status}, feita UMA vez
       por conta. Depois a Trava A (status) e a existência saem do mapa, sem API por item.
       BLINDADA contra loop: para se a página não trouxer ticket NOVO, e tem teto de
       páginas e de tempo — se a API ignorar o Offset, a varredura aborta e o robô segue
       com as consultas pontuais (mais lento, mas nunca trava)."""
    off = 0; t0 = time.time(); paginas = 0
    while paginas < 60 and (time.time() - t0) < 90:          # tetos de segurança
        try: r = page.evaluate(_JS_LISTA, off)
        except Exception: break
        if r.get("status") != 200: break
        tks = r.get("tickets") or []
        antes = len(_STATUS_MAP)
        for t in tks:
            if t.get("id") is not None: _STATUS_MAP[str(t["id"])] = t.get("st")
        paginas += 1
        if len(tks) < 200: break                              # última página
        if len(_STATUS_MAP) == antes:                         # página repetida (API ignorou o Offset)
            print("  [prefetch] API repetiu a página — abortando a varredura (sigo com consulta pontual)", flush=True)
            break
        off += 200
    print(f"  status pré-carregados 1x: {len(_STATUS_MAP)} tickets em {time.time()-t0:.0f}s ({paginas} página(s))", flush=True)

def _ticket_info(page, tk):
    """(existe, status_code) do ticket. existe: True/False/None (não verificado);
       status_code: int da API (5=Vistoriado, 7=Executado, …) ou None.
       Usa o mapa pré-carregado quando houver; senão consulta pontual."""
    st = _STATUS_MAP.get(str(tk))
    if st is not None:
        return (True, st)
    try:
        r = page.evaluate(_JS_EXISTE, str(tk))
    except Exception:
        return (None, None)
    if r.get("status") == 200:
        return (bool(r.get("existe")), r.get("tstatus"))
    return (None, None)

def _ticket_existe(page, tk):
    """True/False se o ticket existe no Trílogo; None se não deu pra verificar."""
    return _ticket_info(page, tk)[0]

def _soma_custos(custos):
    """Soma os valores dos custos já lançados (lista normalizada de _custos_api
       ou lista {tipo,valor} do DOM). Valores inválidos contam como 0."""
    s = 0.0
    for c in (custos or []):
        try: s += float(c.get("valor") or 0)
        except Exception: pass
    return round(s, 2)

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
    page.wait_for_timeout(1500)   # o token é conferido logo abaixo; não precisa de 3s fixos
    # confirma que temos token (necessário para as chamadas de API)
    tk = _token(page)
    ci = _company(page)
    print("  login: ok" + ("" if tk else " (ATENÇÃO: token não encontrado no localStorage!)")
          + f" — pessoa={ci.get('pessoa')} · usuário={ci.get('email')} · companyGroup={ci.get('id')} ({ci.get('name')})", flush=True)
    if ci.get("name") and ("mercadinho" not in str(ci.get("name")).lower()):
        print(f"  ⚠️ ATENÇÃO: a sessão do robô NÃO está no Mercadinhos São Luiz e sim em '{ci.get('name')}' "
              f"(companyGroup {ci.get('id')}). As consultas de custo/existência vão sair ERRADAS.", flush=True)
    # AUTO-TESTE de diagnóstico (só quando SELFTEST=1) — enxuga a corrida no dia a dia.
    if os.environ.get("SELFTEST"):
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
  // ANEXA COMO NOTA FISCAL (invoice). A API do Trílogo EXIGE a nota fiscal quando há Nº do
  // documento ("Necessário anexar a nota fiscal referente ao número informado") — sem ela o
  // "Concluir" fica DESABILITADO. Por isso miramos o dropzone da NOTA FISCAL (não o de "outros
  // arquivos", que era o erro). É o 1º input do modal.
  let alvo = ins.find(e => /nota fiscal/i.test(((e.closest('div')||{}).textContent)||''));
  if(!alvo && ins.length) alvo = ins[0];   // fallback: o primeiro (dropzone principal)
  if(!alvo) return null;
  if(!alvo.id) alvo.id = 'robo_anexo_nf';
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

# ---- CONFERÊNCIA do PONTO DE FALHA (rápida, por API) — NÃO lança, NÃO move ----
def conferir_um(page, it):
    """Reverifica POR QUE cada orçamento não sobe e REPORTA o motivo + o status atual do
       ticket (Aberto/Em execução/Arquivado…). Não lança, não move, não mexe em custo.
       Reporta via _prog (motivo/status_ticket) — o motor persiste na lista de não lançados."""
    tk=it.get("ticket"); nome=it.get("arquivo"); valor=it.get("valor")
    if not tk:
        _prog(nome, "sem ticket associado", 0, motivo="SEM_TICKET"); return
    try: v=round(float(valor),2)
    except Exception: v=None
    if v is None or v<=0:
        _prog(nome, "valor do orçamento não lido", 0, motivo="VALOR_NAO_LIDO"); return
    existe, st_code = _ticket_info(page, tk)
    if existe is False:
        print(f"  ticket {tk}: NÃO EXISTE no Trílogo", flush=True)
        _prog(nome, "ticket inexistente no Trílogo", 0, motivo="TICKET_INEXISTENTE"); return
    if existe is None or st_code is None:
        print(f"  ticket {tk}: status NÃO verificado (erro na API)", flush=True)
        _prog(nome, "status não verificado — tentar de novo", 0, motivo="NAO_VERIFICADO"); return
    rot = STATUS_LABEL.get(st_code, f"status {st_code}")
    if st_code not in STATUS_OK_LANCAR:
        print(f"  ticket {tk}: fora de status (está: {rot})", flush=True)
        _prog(nome, f"não lançado — {rot}", 0, motivo="FORA_STATUS", status_ticket=rot); return
    # passou nas travas de status -> está pronto para lançar
    print(f"  ticket {tk}: pronto pra lançar (status: {rot})", flush=True)
    _prog(nome, f"pronto pra lançar ({rot})", 0, motivo="A_LANCAR", status_ticket=rot)
    time.sleep(0.05)   # gentileza com a API

# ---- LANÇAMENTO real (com pré-checagem por API) ----------------------------------
def lancar_um(page, it):
    """Cria o custo no ticket. Devolve True só se confirmar o sucesso (ou se já estava lançado)."""
    tk = it.get("ticket"); origem = it.get("origem"); nome = it.get("arquivo")
    valor = it.get("valor")
    def fail(msg, marca=True, motivo="FALHA"):
        print(f"[falha] ticket {tk}: {msg}", flush=True)
        if marca: _prog(nome, "falha", 0, motivo=motivo)
        return False
    if not tk: return fail("sem ticket associado", motivo="SEM_TICKET")
    lancado_bd = bool(it.get("lancado"))

    # VALOR do novo orçamento (precisa existir para a trava da soma e para o formulário)
    try: v_novo = round(float(valor), 2)
    except Exception: v_novo = None
    if v_novo is None or v_novo <= 0:
        print(f"[skip] ticket {tk}: valor do orçamento não lido — não lanço", flush=True)
        _prog(nome, "valor do orçamento não lido", 0, motivo="VALOR_NAO_LIDO")
        return False

    # EXISTÊNCIA + TRAVA A (STATUS, obrigatória): lê pela API (ListTicketsByUser).
    # Orçamento (1º OU adicional) só entra em ticket Executado (7) ou Vistoriado (5).
    existe, st_code = _ticket_info(page, tk)
    if existe is False:
        print(f"[skip] ticket {tk}: NÃO EXISTE no Trílogo — número errado, não lanço", flush=True)
        _prog(nome, "ticket inexistente no Trílogo", 0, motivo="TICKET_INEXISTENTE")
        return False
    if existe and st_code is not None and st_code not in STATUS_OK_LANCAR:
        rot = STATUS_LABEL.get(st_code, f"status {st_code}")
        print(f"[skip] ticket {tk} não está Executado/Vistoriado (está: {rot}) — não lanço", flush=True)
        _prog(nome, f"não lançado — {rot}", 0, motivo="FORA_STATUS", status_ticket=rot)
        return False
    if existe is None or st_code is None:
        # a trava de status é PRÉ-CONDIÇÃO: sem status confirmado, não lança (rode de novo depois)
        print(f"[skip] ticket {tk}: status não verificado pela API — não lanço (trava de status é obrigatória)", flush=True)
        _prog(nome, "status não verificado — tentar de novo", 0, motivo="NAO_VERIFICADO")
        return False

    # PRÉ-CHECAGEM POR API dos custos existentes.
    ok_conta, custos_api = _custos_api(page, tk)
    # >>> VERIFICAÇÃO DE DUPLICIDADE REMOVIDA (a pedido): o robô NÃO reconcilia/pula mais quando
    #     o custo já é deste orçamento — sempre segue para LANÇAR. Continuam valendo apenas a
    #     Trava A (status Executado/Vistoriado, acima) e a Trava B (teto da soma, abaixo).

    # TRAVA B (SOMA <= teto), 1ª camada, pela API: soma TODOS os custos já lançados + o novo.
    if ok_conta:
        soma_previa = _soma_custos(custos_api)
        if soma_previa + v_novo > TETO_SOMA + 0.005:
            print(f"[skip] ticket {tk}: custos existentes R$ {_fmt_valor(soma_previa)} + novo "
                  f"R$ {_fmt_valor(v_novo)} = R$ {_fmt_valor(soma_previa+v_novo)} — passa do teto "
                  f"de R$ {_fmt_valor(TETO_SOMA)}", flush=True)
            _prog(nome, "soma acima de R$600,00", 0, motivo="SOMA_ACIMA")
            return False
        if custos_api:
            print(f"  ticket {tk}: {len(custos_api)} custo(s) já lançado(s) (R$ {_fmt_valor(soma_previa)}) — "
                  f"orçamento ADICIONAL permitido (soma final R$ {_fmt_valor(soma_previa+v_novo)})", flush=True)
    # (ok_conta False/None: a trava B roda de novo pela TELA, adiante)

    _prog(nome, "abrindo ticket", 15)
    print(f"  ticket {tk}: abrindo…", flush=True)
    page.goto(f"{BASE_URL}/ticket/{tk}", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)   # o _click_js abaixo já espera a seção aparecer (poll)
    if "ticket" not in page.url:
        print(f"[skip] ticket {tk} não abriu nesta conta"); return False   # não marca falha (outra conta)
    # "Custos do ticket" é um <span>, NÃO um botão -> clique por JS (bubbla e expande)
    if not _click_js(page, r"^\s*custos do ticket\s*$"): return fail("não achei a seção 'Custos do ticket'")
    page.wait_for_timeout(600)
    # 2ª CAMADA (pela TELA): revalida com o que está na seção "Custos do ticket" agora
    # (pega custo adicionado no meio-tempo e cobre o caso em que a API não respondeu).
    try: custos = page.evaluate(_JS_CUSTOS_DOM) or []
    except Exception: custos = []
    # (verificação de duplicidade pela TELA também removida — segue direto para a Trava B)
    # TRAVA B (2ª camada): soma dos custos na tela + o novo <= teto
    soma_tela = _soma_custos(custos)
    if soma_tela + v_novo > TETO_SOMA + 0.005:
        print(f"[skip] ticket {tk}: custos na tela R$ {_fmt_valor(soma_tela)} + novo "
              f"R$ {_fmt_valor(v_novo)} = R$ {_fmt_valor(soma_tela+v_novo)} — passa do teto "
              f"de R$ {_fmt_valor(TETO_SOMA)}", flush=True)
        _prog(nome, "soma acima de R$600,00", 0, motivo="SOMA_ACIMA")
        return False
    if custos:
        print(f"  ticket {tk}: lançando orçamento ADICIONAL — o ticket vai ficar com {len(custos)+1} custo(s), "
              f"soma R$ {_fmt_valor(soma_tela+v_novo)}", flush=True)
    if not _click_js(page, r"^\s*\+?\s*novo custo\s*$"): return fail("não apareceu 'Novo custo'")
    page.wait_for_timeout(800)
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
    # anexa o orçamento como NOTA FISCAL (OBRIGATÓRIO — sem ela o Concluir não habilita quando há
    # Nº do documento). Vai no dropzone da nota fiscal; isso dispara o leitor de IA, então DEPOIS
    # esperamos a IA assentar e, se ela trocar a tela, reabrimos o form manual. Preencher os campos
    # vem DEPOIS do anexo, pra sobrescrever qualquer chute da IA.
    pdf = _baixa_pdf(origem, nome)
    try:
        sel = page.evaluate(_JS_MARK_ANEXO)
        if not sel:
            return fail("não achei o dropzone da nota fiscal para anexar")
        page.set_input_files(sel, pdf)
        print(f"  ticket {tk}: nota fiscal anexada — aguardando o leitor de IA assentar…", flush=True)
        page.wait_for_timeout(IA_ESPERA_MS)   # dá tempo da IA processar o PDF (ROBO_IA_ESPERA_MS)
    except Exception as e:
        return fail(f"não anexei a nota fiscal ({str(e)[:80]})")
    # a IA pode ter trocado a tela; garante que o form manual está presente (reabre se sumiu)
    if page.locator("#serviceCost").count() == 0 or not page.locator("#serviceCost").first.is_visible():
        _click_js(page, r"preencher.*manual", tries=8, gap=500)
        try: page.wait_for_selector("#serviceCost", state="visible", timeout=10000)
        except Exception: return fail("form manual não voltou após anexar a nota fiscal")
        page.wait_for_timeout(400)
    # Tipo de custo = Mão de obra (ant-select #costType). ATENÇÃO: o Trílogo EXIBE "Mão de obra"
    # por padrão, mas NÃO grava esse valor no formulário até você ESCOLHER a opção ATIVAMENTE —
    # sem isso o campo conta como vazio e o "Concluir" fica DESABILITADO (era a causa do
    # "não vi confirmação de sucesso"). Então SEMPRE abrimos e escolhemos. O input fica atrás do
    # <span> do valor, então abrimos pelo CONTAINER (.ant-select-selector), não pelo input.
    ALVO_TIPO = r"^\s*m[aã]o de obra\s*$"
    try:
        # abre o dropdown clicando no CONTAINER do ant-select do #costType. CSS :has() (o xpath
        # ancestor casava também com div.ant-select-SELECTOR e dava timeout).
        sel = page.locator(".ant-select:has(#costType) .ant-select-selector").first
        try: sel.click(timeout=5000)
        except Exception: sel.click(timeout=5000, force=True)
        page.wait_for_timeout(600)
        # COMMIT REAL: só CLICAR na opção NÃO grava o valor no formulário — o Trílogo continuava
        # exibindo "Mão de obra" com o campo VAZIO por dentro, e o "Concluir" ficava DESABILITADO
        # (era exatamente isso: log '[pré-concluir] ... concluir_disabled=True'). O que grava é
        # ESCOLHER a opção destacada com ENTER (verificado ao vivo). "Mão de obra" é a 1ª opção e
        # já vem destacada por padrão, então abrir + Enter seleciona ela.
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)
        # segurança: confere se ficou "Mão de obra"; se não, reabre e escolhe pela opção.
        _tnow = page.evaluate(r"""() => {
          const fis=[...document.querySelectorAll('.ant-form-item')];
          const fi=fis.find(f=>{const l=f.querySelector('label'); return l && l.textContent && l.textContent.includes('Tipo de custo');});
          return (fi&&fi.querySelector('.ant-select-selection-item')||{}).textContent||'';
        }""") or ""
        if not re.match(ALVO_TIPO, _tnow.strip(), re.I):
            try: sel.click(timeout=3000)
            except Exception: pass
            page.wait_for_timeout(400)
            try: page.get_by_role("option", name=re.compile(ALVO_TIPO, re.I)).first.click(timeout=3000)
            except Exception: page.keyboard.press("Enter")
            page.wait_for_timeout(300)
    except Exception as e:
        return fail(f"não setei 'Mão de obra' ({e})")
    # Valor = #serviceCost. A máscara NÃO preenche por centavos: digitar "6096" vira R$ 6.096
    # (cem vezes maior!). O certo é digitar com VÍRGULA decimal -> "60,96" vira R$ 60,96.
    try:
        val_str = f"{float(valor):.2f}".replace(".", ",")   # 60.96 -> "60,96"
        v = page.locator("#serviceCost"); v.click(); page.keyboard.press("Control+A"); page.keyboard.press("Delete")
        v.type(val_str, delay=60)
    except Exception as e:
        return fail(f"campo Valor ({e})")
    # Número do documento = ticket  (#documentNumber)
    try:
        d = page.locator("#documentNumber"); d.click(); page.keyboard.press("Control+A"); page.keyboard.press("Delete")
        d.type(str(tk), delay=20)
    except Exception as e:
        return fail(f"campo Documento ({e})")
    _prog(nome, "concluindo", 90)
    page.wait_for_timeout(500)
    # DIAGNÓSTICO: estado real do form antes de concluir (pra achar exatamente o que trava)
    try:
        _st = page.evaluate(r"""() => {
          const fis=[...document.querySelectorAll('.ant-form-item')];
          const fi=fis.find(f=>{const l=f.querySelector('label'); return l && l.textContent && l.textContent.includes('Tipo de custo');});
          const tipo=(fi&&fi.querySelector('.ant-select-selection-item')||{}).textContent||'';
          const sc=document.querySelector('#serviceCost'), dn=document.querySelector('#documentNumber');
          const btns=[...document.querySelectorAll('button')].filter(b=>b.offsetParent!==null);
          const c=btns.find(b=>/^\s*concluir\s*$/i.test((b.textContent||'').trim()));
          return {tipo:tipo.trim(), valor:(sc?sc.value:''), doc:(dn?dn.value:''), concluir_disabled:(c? !!c.disabled : null)};
        }""")
        print(f"  ticket {tk}: [pré-concluir] tipo='{_st.get('tipo')}' valor='{_st.get('valor')}' "
              f"doc='{_st.get('doc')}' concluir_disabled={_st.get('concluir_disabled')}", flush=True)
        # FAIL-FAST: se o Concluir está desabilitado, não adianta clicar (falha em segundos, não em minutos)
        if _st.get("concluir_disabled") is True:
            return fail("Concluir desabilitado (provável nota fiscal faltando ou campo não commitado) — não insisto")
    except Exception as _e:
        print(f"  ticket {tk}: [pré-concluir] não li estado ({str(_e)[:60]})", flush=True)
    # clica Concluir de VERDADE (Playwright), esperando o botão habilitar (o form valida tipo+valor)
    try:
        btn = page.get_by_role("button", name=re.compile(r"^\s*concluir\s*$", re.I)).first
        for _ in range(10):
            try:
                if btn.is_enabled(): break
            except Exception: pass
            page.wait_for_timeout(300)
        btn.click(timeout=6000)
    except Exception as e:
        if not _click_js(page, r"^\s*concluir\s*$", tries=6): return fail(f"não cliquei 'Concluir' ({str(e)[:60]})")
    # confirma sucesso: aceita o toast OU o fechamento do modal (o #serviceCost some).
    try:
        page.get_by_text(re.compile(r"custo inserido com sucesso|sucesso", re.I)).first.wait_for(timeout=12000)
        print(f"[ok] ticket {tk}: custo lançado (R$ {_fmt_valor(valor)})"); return True
    except Exception:
        try:
            page.wait_for_selector("#serviceCost", state="detached", timeout=6000)
            print(f"[ok] ticket {tk}: custo lançado — modal fechou (R$ {_fmt_valor(valor)})"); return True
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
        br = _abre_navegador(p)
        ctx = br.new_context(user_agent=UA)
        ctx.set_default_navigation_timeout(30000)
        page = ctx.new_page(); page.set_default_timeout(12000)   # falha rápido em vez de travar
        # MODO CAPTURA (CAPTURA=1): loga as chamadas de escrita à API do Trílogo durante o
        # lançamento — serve para MAPEAR o endpoint de criação de custo e, na próxima revisão,
        # lançar DIRETO pela API (segundos por item, sem abrir a tela). Só loga, não muda nada.
        if os.environ.get("CAPTURA"):
            def _cap(resp):
                try:
                    rq = resp.request
                    if "web.api.trilogo.app" in rq.url and rq.method in ("POST", "PUT", "PATCH"):
                        pd = ""
                        try: pd = (rq.post_data or "")[:500]
                        except Exception: pass
                        print(f"  [captura] {rq.method} {rq.url} -> {resp.status} | ct={rq.headers.get('content-type','')[:60]} | payload[:500]={pd}", flush=True)
                except Exception: pass
            page.on("response", _cap)
            print("  MODO CAPTURA ligado: vou logar os POSTs da API durante o lançamento", flush=True)
        try:
            login(page)
        except Exception as e:
            print("Falha no login:", e); br.close(); sys.exit(1)
        # LOTE: com 3+ itens, vale pré-carregar os status de TODOS os tickets numa varredura
        # só (poucas chamadas paginadas) em vez de 1 consulta por item. Com 1-2 itens, a
        # consulta pontual continua mais barata.
        if len(fila) >= 3:
            _prefetch_status(page)
        for idx, it in enumerate(fila, 1):
            t1 = time.time()
            print(f"[{idx}/{len(fila)}] ticket {it.get('ticket')} · {it.get('arquivo')}", flush=True)
            try:
                if MODO == "conferir":
                    conferir_um(page, it); feitos += 1
                elif lancar_um(page, it):
                    r = _post("/robot/lancar_ok", {"origem": it["origem"], "nome": it["arquivo"]})
                    if r.get("ok"): feitos += 1; print(f"       movido: {it['arquivo']}")
            except Exception as e:
                print(f"[erro] {it.get('arquivo')}: {str(e)[:160]}")
            print(f"       ⏱ item em {time.time()-t1:.0f}s", flush=True)
        br.close()
    print(f"conta {ABA} [MODO={MODO}]: {feitos} processado(s). tempo total {time.time()-t0:.0f}s")

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        print("ERRO NÃO TRATADO:"); traceback.print_exc(); sys.exit(1)
