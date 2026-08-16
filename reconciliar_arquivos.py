# -*- coding: utf-8 -*-
"""
FrotaHub — Reconcilia o `arquivo_pdf` no banco lendo os montados das pastas 10/11.

Por que ler o PDF e não casar pelo nome? Porque os NOMES SE REPETEM nas pastas 10/11.
Então abrimos CADA PDF (pdfplumber), pegamos o TOTAL GERAL e casamos com a linha de
`notas_orcamento` por TICKET + VALOR — que é ÚNICO pela regra de ouro (nunca há dois
'gerado' com mesmo ticket + mesmo valor_orcamento). Aí preenchemos o `arquivo_pdf`
(no formato 'N/mês/loja/arquivo.pdf', igual ao resto do sistema).

MODOS (variáveis de ambiente):
  APLICAR=0  (default) -> só mostra o PLANO (não grava nada).   APLICAR=1 -> grava (PATCH).
  SO_NULOS=1 (default) -> só preenche onde arquivo_pdf está NULL.
  SO_NULOS=0           -> também corrige onde o arquivo_pdf gravado diverge do casado.

Segredos (iguais aos outros robôs):
  DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN,
  SUPABASE_URL, SUPABASE_SERVICE_KEY
NÃO apaga nada. NÃO mexe no Dropbox. Só LÊ os PDFs e (se APLICAR=1) grava o arquivo_pdf.
"""
import os, io, json, re, sys, urllib.parse, urllib.request, urllib.error
try: sys.stdout.reconfigure(line_buffering=True)
except Exception: pass
import pdfplumber

APP_KEY=os.environ.get("DROPBOX_APP_KEY",""); APP_SEC=os.environ.get("DROPBOX_APP_SECRET",""); REFRESH=os.environ.get("DROPBOX_REFRESH_TOKEN","")
BASE=(os.environ.get("DROPBOX_BASE") or "/FROTAHUB/2 - MANUTENCAO").rstrip("/")
SB_URL=os.environ.get("SUPABASE_URL","").rstrip("/"); SB_KEY=os.environ.get("SUPABASE_SERVICE_KEY","")
APLICAR=os.environ.get("APLICAR","0")=="1"
SO_NULOS=os.environ.get("SO_NULOS","1")!="0"

# (prefixo N no banco, nome da pasta no Dropbox, é rateio?)
PASTAS=[("10","10 - ORÇAMENTOS MONTADOS",False),("11","11 - ORÇAMENTOS MONTADOS RATEIO",True)]

def obter_token():
    data=urllib.parse.urlencode({"grant_type":"refresh_token","refresh_token":REFRESH,
        "client_id":APP_KEY,"client_secret":APP_SEC}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://api.dropbox.com/oauth2/token",data=data)) as r:
        return json.loads(r.read().decode())["access_token"]

def listar(access, pasta):
    """Lista recursivo os PDFs de uma pasta. Devolve path_display de cada arquivo."""
    path=f"{BASE}/{pasta}"; out=[]; url="https://api.dropboxapi.com/2/files/list_folder"
    arg={"path":path,"recursive":True,"include_deleted":False,"limit":2000}
    while True:
        req=urllib.request.Request(url,data=json.dumps(arg).encode(),
            headers={"Authorization":f"Bearer {access}","Content-Type":"application/json"})
        try: r=json.loads(urllib.request.urlopen(req).read().decode())
        except urllib.error.HTTPError as e: print(f"   ! erro em '{pasta}': {e.read().decode()[:150]}"); return out
        for e in r.get("entries",[]):
            if e.get(".tag")=="file" and e.get("name","").lower().endswith(".pdf"):
                out.append(e.get("path_display") or e.get("path_lower"))
        if not r.get("has_more"): break
        url="https://api.dropboxapi.com/2/files/list_folder/continue"; arg={"cursor":r["cursor"]}
    return out

def baixar(access, path):
    req=urllib.request.Request("https://content.dropboxapi.com/2/files/download",
        headers={"Authorization":f"Bearer {access}","Dropbox-API-Arg":json.dumps({"path":path})})
    with urllib.request.urlopen(req) as r: return r.read()

def _num(s):
    s=re.sub(r"[^\d,.\-]","",str(s or "").strip())
    if not s: return None
    if "," in s: s=s.replace(".","").replace(",",".")
    try: return round(float(s),2)
    except Exception: return None

def _ticket_do_nome(nome):
    m=re.search(r'_(\d{4,7})(?:_NOTA_.*)?\.pdf$', nome, re.I) or re.search(r'(\d{4,7})', nome or "")
    return m.group(1) if m else ""

_HDR_OK=lambda j: ("descri" in j) and ("valor" in j) and ("total" in j)
def _soma_itens(pdf):
    """Fallback: soma o 'total' dos itens da tabela do orçamento."""
    soma=0.0; achou=False
    for page in pdf.pages:
        for tbl in (page.extract_tables() or []):
            if not tbl or len(tbl)<2: continue
            j0=" ".join((c or "").lower() for c in tbl[0])
            hi=0 if _HDR_OK(j0) else next((k for k,row in enumerate(tbl) if _HDR_OK(" ".join((c or "").lower() for c in row))), None)
            if hi is None: continue
            for row in tbl[hi+1:]:
                cells=[(c or "").strip() for c in row]
                if "total geral" in " ".join(cells).lower(): continue
                if len(cells)<6: continue
                t=_num(cells[5])
                if t is not None: soma+=t; achou=True
    return round(soma,2) if achou else None

def valor_do_pdf(pdf_bytes):
    """Total do orçamento: prefere a linha 'TOTAL GERAL'; senão soma os itens."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        txt="\n".join((pg.extract_text() or "") for pg in pdf.pages)
        m=re.search(r'total\s+geral.*?([\d]{1,3}(?:\.\d{3})*,\d{2})', txt, re.I|re.S)
        if m:
            v=_num(m.group(1))
            if v is not None: return v
        return _soma_itens(pdf)

def rel_do_path(pref, folder, path_display):
    """'/FROTAHUB/2 - MANUTENCAO/10 - ...MONTADOS/MÊS/LOJA/x.pdf' -> '10/MÊS/LOJA/x.pdf'."""
    marca=f"{BASE}/{folder}/"
    if not path_display.startswith(marca): return None
    return f"{pref}/{path_display[len(marca):]}"

def sb_get(qs):
    url=f"{SB_URL}/rest/v1/notas_orcamento?{qs}"
    req=urllib.request.Request(url, headers={"apikey":SB_KEY,"authorization":f"Bearer {SB_KEY}"})
    return json.loads(urllib.request.urlopen(req,timeout=60).read().decode())

def sb_patch(oid, rel):
    url=f"{SB_URL}/rest/v1/notas_orcamento?id=eq.{urllib.parse.quote(str(oid))}"
    body=json.dumps({"arquivo_pdf":rel}).encode()
    req=urllib.request.Request(url,data=body,method="PATCH",headers={
        "apikey":SB_KEY,"authorization":f"Bearer {SB_KEY}","content-type":"application/json","prefer":"return=minimal"})
    urllib.request.urlopen(req,timeout=60).read()

def main():
    faltam=[k for k,v in {"DROPBOX":APP_KEY and APP_SEC and REFRESH,"SUPABASE":SB_URL and SB_KEY}.items() if not v]
    if faltam: print("!! Faltam segredos:", ", ".join(faltam)); sys.exit(1)
    print(f"Base: {BASE}   Modo: {'APLICAR (grava no banco)' if APLICAR else 'SÓ PLANO'}   {'só NULOS' if SO_NULOS else 'NULOS + divergentes'}\n")
    access=obter_token()

    # 1) índice dos PDFs montados por (ticket, valor)
    print(">> lendo os montados das pastas 10/11 (abre PDF por PDF)…")
    pdf_idx={}   # (ticket, valor2) -> list de {rel, pref, nome}
    lidos=0; sem_valor=[]
    for pref,folder,_rat in PASTAS:
        paths=listar(access, folder)
        print(f"   {folder}: {len(paths)} PDF(s)")
        for i,pd in enumerate(paths,1):
            nome=pd.split("/")[-1]; tk=_ticket_do_nome(nome)
            try: val=valor_do_pdf(baixar(access,pd))
            except Exception as e: sem_valor.append((nome,str(e)[:70])); continue
            if not tk or val is None: sem_valor.append((nome,"sem ticket/valor")); continue
            rel=rel_do_path(pref,folder,pd)
            if not rel: continue
            pdf_idx.setdefault((tk,round(val,2)),[]).append({"rel":rel,"pref":pref,"nome":nome})
            lidos+=1
            if i%25==0: print(f"      …{i}/{len(paths)}")
    print(f"   montados lidos com valor: {lidos}  ·  sem valor/ticket: {len(sem_valor)}\n")

    # 2) linhas do banco a reconciliar
    rows=sb_get("status=eq.gerado&select=id,ticket,valor_orcamento,rateio,arquivo_pdf&limit=8000")
    print(f">> notas_orcamento (gerado): {len(rows)}")

    plano=[]; ambiguos=[]; sem_match=[]; ja_ok=0
    for r in rows:
        tk=str(r.get("ticket") or ""); val=_num(r.get("valor_orcamento")); ap=r.get("arquivo_pdf")
        precisa = (ap in (None,"")) if SO_NULOS else True
        if not precisa: continue
        if not tk or val is None: continue
        cands=pdf_idx.get((tk,round(val,2)),[])
        if not cands:
            # tolerância de 1 centavo (arredondamento)
            for dv in (0.01,-0.01,0.02,-0.02):
                cands=pdf_idx.get((tk,round(val+dv,2)),[])
                if cands: break
        if not cands: sem_match.append((tk,val)); continue
        # prefere a pasta que casa com rateio (11 se rateio, senão 10)
        alvo_pref="11" if r.get("rateio") else "10"
        pref_cands=[c for c in cands if c["pref"]==alvo_pref] or cands
        escolha=None
        if len(pref_cands)==1: escolha=pref_cands[0]
        else:
            nomes=sorted({c["nome"] for c in pref_cands})
            if len(nomes)==1: escolha=pref_cands[0]     # mesmo arquivo repetido em subpastas
        if not escolha:
            ambiguos.append((tk,val,[c["rel"] for c in pref_cands])); continue
        if ap and ap==escolha["rel"]: ja_ok+=1; continue
        plano.append({"id":r["id"],"ticket":tk,"valor":val,"de":ap,"para":escolha["rel"]})

    print(f"\n================ PLANO ================")
    for p in plano[:60]:
        print(f"  {p['ticket']} R$ {p['valor']:.2f}  ->  {p['para']}" + (f"   (era: {p['de']})" if p['de'] else ""))
    if len(plano)>60: print(f"  … (+{len(plano)-60})")
    print(f"\n  a preencher/corrigir: {len(plano)}")
    print(f"  já corretos (nada a fazer): {ja_ok}")
    print(f"  SEM montado casando (ticket+valor): {len(sem_match)}")
    if sem_match[:20]: print("    ex.: " + ", ".join(f"{t}/{v}" for t,v in sem_match[:20]))
    print(f"  AMBÍGUOS (2+ montados p/ mesmo ticket+valor): {len(ambiguos)}")
    for tk,val,rels in ambiguos[:15]: print(f"    {tk} R$ {val:.2f}: {rels}")

    if APLICAR and plano:
        print("\n================ GRAVANDO ================")
        ok=0
        for p in plano:
            try: sb_patch(p["id"], p["para"]); ok+=1
            except Exception as e: print(f"  ! {p['ticket']}: falha {str(e)[:90]}")
        print(f"\nGravados: {ok}/{len(plano)}")
    elif plano:
        print("\n(Modo só-plano. Confira acima e rode com APLICAR=1 para gravar.)")

if __name__=="__main__":
    main()
