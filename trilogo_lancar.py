# -*- coding: utf-8 -*-
"""
Robô de LANÇAMENTO de orçamentos no Trílogo (custo do ticket).
Pega a lista no motor do FrotaHub, loga na conta e, para cada orçamento das pastas
1 (normais) e 4 (rateio), cria o custo no ticket (Tipo=Materiais, Valor=TOTAL GERAL,
Nº do documento=ticket) e avisa o motor, que move o arquivo (1->2 / 4->5) e marca
como lançado. Roda 1 conta por execução (o workflow chama 2x: Instalações e Civil).

Segredos (GitHub):
  MOTOR_URL           ex.: https://frotahub-motor.onrender.com
  ROBOT_KEY           mesmo valor da variável ROBOT_KEY no Render
  TRILOGO_EMAIL, TRILOGO_SENHA, ABA (CIVIL|INSTALACOES)

DEDUP: o motor só devolve o que ainda está em 1/4; e só move/marca quando o custo
entra de fato. Se o sucesso não for confirmado, o arquivo fica onde está.
"""
import os, re, sys, json, tempfile, urllib.request, urllib.error, urllib.parse
from playwright.sync_api import sync_playwright

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
LOGIN_URL = "https://mercadinhossaoluiz.trilogo.app/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def _get(path):
    req = urllib.request.Request(f"{MOTOR}{path}", headers={"x-robot-key": RKEY})
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())

def _post(path, obj):
    req = urllib.request.Request(f"{MOTOR}{path}", data=json.dumps(obj).encode(), method="POST",
        headers={"x-robot-key": RKEY, "content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read().decode())

def _baixa_pdf(origem, nome):
    url = f"{MOTOR}/robot/lancar_pdf?key={urllib.parse.quote(RKEY)}&origem={origem}&nome={urllib.parse.quote(nome)}"
    data = urllib.request.urlopen(urllib.request.Request(url), timeout=120).read()
    f = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False); f.write(data); f.close()
    return f.name

def _fmt_valor(v):
    try: return f"{float(v):.2f}".replace(".", ",")
    except Exception: return str(v)

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
    print("  login: ok", flush=True)

def _click_texto(page, textos, timeout=8000):
    """Clica no 1º elemento cujo texto casa. Tenta role=button e texto puro."""
    for t in textos:
        try:
            page.get_by_role("button", name=re.compile(t, re.I)).first.click(timeout=timeout); return True
        except Exception: pass
        try:
            page.get_by_text(re.compile(t, re.I)).first.click(timeout=3000); return True
        except Exception: pass
    return False

def lancar_um(page, it):
    """Cria o custo no ticket. Devolve True só se confirmar o sucesso."""
    tk = it.get("ticket"); origem = it.get("origem"); nome = it.get("arquivo")
    valor = it.get("valor")
    if not tk:
        print(f"[skip] {nome}: sem ticket associado"); return False
    print(f"  ticket {tk}: abrindo…", flush=True)
    page.goto(f"https://mercadinhossaoluiz.trilogo.app/ticket/{tk}", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    # confirma que o ticket abriu nesta conta (empresa prestadora). Se não abrir, deixa p/ outra conta.
    if "ticket" not in page.url:
        print(f"[skip] ticket {tk} não abriu nesta conta"); return False
    # 2-3) Custos do ticket -> Novo custo
    _click_texto(page, [r"custos do ticket"])
    page.wait_for_timeout(500)
    if not _click_texto(page, [r"novo custo", r"\+ *novo custo", r"adicionar custo"]):
        print(f"[falha] ticket {tk}: não achei 'Novo custo'"); return False
    page.wait_for_timeout(1200)
    # 5-7) subir o PDF no input de arquivo do modal
    pdf = _baixa_pdf(origem, nome)
    try:
        page.wait_for_selector("input[type=file]", timeout=15000)
        page.locator("input[type=file]").first.set_input_files(pdf)
    except Exception as e:
        print(f"[falha] ticket {tk}: input de arquivo não encontrado ({e})"); return False
    page.wait_for_timeout(2500)   # a IA do Trílogo tenta ler; ignoramos e preenchemos
    # 8-9) Tipo de custo = Materiais (Ant Select)
    try:
        page.get_by_text(re.compile(r"tipo de custo", re.I)).first.click(timeout=4000)
        page.wait_for_timeout(300)
        page.get_by_text(re.compile(r"^\s*materiais\s*$", re.I)).first.click(timeout=4000)
    except Exception:
        print(f"[aviso] ticket {tk}: não consegui setar 'Materiais' pela tela")
    # 10) Valor
    try:
        campo_v = page.get_by_label(re.compile(r"valor", re.I)).first
        campo_v.click(); page.keyboard.press("Control+A"); campo_v.type(_fmt_valor(valor))
    except Exception:
        try:
            page.get_by_placeholder(re.compile(r"valor", re.I)).first.fill(_fmt_valor(valor))
        except Exception: print(f"[aviso] ticket {tk}: campo Valor não preenchido")
    # 11) Número do documento = ticket
    try:
        page.get_by_label(re.compile(r"n[úu]mero do documento|nº do documento|documento", re.I)).first.fill(str(tk))
    except Exception:
        try: page.get_by_placeholder(re.compile(r"documento", re.I)).first.fill(str(tk))
        except Exception: print(f"[aviso] ticket {tk}: campo Documento não preenchido")
    # 12) Concluir
    if not _click_texto(page, [r"concluir", r"salvar", r"confirmar"]):
        print(f"[falha] ticket {tk}: não achei 'Concluir'"); return False
    # confirma sucesso
    try:
        page.get_by_text(re.compile(r"custo inserido com sucesso|sucesso", re.I)).first.wait_for(timeout=8000)
        print(f"[ok] ticket {tk}: custo lançado ({_fmt_valor(valor)})"); return True
    except Exception:
        print(f"[incerto] ticket {tk}: não vi o toast de sucesso — NÃO vou mover"); return False

def main():
    try:
        itens = _get("/robot/lancar_worklist").get("itens", [])
    except Exception as e:
        print("Falha ao obter worklist:", e); sys.exit(1)
    # nesta conta: processa os da aba correspondente + os "?" (tenta; se não abrir, pula)
    def _cabe(a):
        a = (a or "").upper()
        return (a == ABA) or (a in ("", "?"))
    fila = [x for x in itens if _cabe(x.get("aba"))]
    print(f"conta {ABA}: {len(fila)} de {len(itens)} orçamento(s) na fila")
    if not fila: return
    feitos = 0
    with sync_playwright() as p:
        print("abrindo navegador…", flush=True)
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
                if lancar_um(page, it):
                    r = _post("/robot/lancar_ok", {"origem": it["origem"], "nome": it["arquivo"]})
                    if r.get("ok"): feitos += 1; print(f"       movido: {it['arquivo']}")
            except Exception as e:
                print(f"[erro] {it.get('arquivo')}: {str(e)[:160]}")
        br.close()
    print(f"conta {ABA}: {feitos} lançado(s) e movido(s).")

if __name__ == "__main__":
    main()
