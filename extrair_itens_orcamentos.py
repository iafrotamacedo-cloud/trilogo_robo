# -*- coding: utf-8 -*-
"""
FrotaHub - Extrai TODOS os itens de TODOS os orçamentos montados (pastas 10 e 11)
lendo os PDFs no Dropbox. Gera 'itens_orcamentos.csv' (1 linha por item) para auditoria.
NÃO altera nada no Dropbox nem no banco — só lê e gera o CSV.

Segredos (iguais ao robô): DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN
"""
import os, io, csv, json, re, urllib.parse, urllib.request, urllib.error
import pdfplumber

APP_KEY = os.environ.get("DROPBOX_APP_KEY", "")
APP_SEC = os.environ.get("DROPBOX_APP_SECRET", "")
REFRESH = os.environ.get("DROPBOX_REFRESH_TOKEN", "")
BASE    = (os.environ.get("DROPBOX_BASE") or "/FROTAHUB/2 - MANUTENCAO").rstrip("/")
PASTAS  = ["10 - ORÇAMENTOS MONTADOS", "11 - ORÇAMENTOS MONTADOS RATEIO"]

def obter_token():
    data=urllib.parse.urlencode({"grant_type":"refresh_token","refresh_token":REFRESH,
        "client_id":APP_KEY,"client_secret":APP_SEC}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://api.dropbox.com/oauth2/token",data=data)) as r:
        return json.loads(r.read().decode())["access_token"]

def listar(access, pasta):
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
    except: return None

def _meta(path):
    """ticket, loja e mês a partir do caminho/nome do arquivo."""
    parts=path.split("/"); nome=parts[-1]; mes=parts[-3] if len(parts)>=3 else ""; loja_dir=parts[-2] if len(parts)>=2 else ""
    m=re.search(r'_(\d{4,7})(?:_NOTA_.*)?\.pdf$', nome, re.I) or re.search(r'(\d{4,7})', nome)
    ticket=m.group(1) if m else ""
    loja=re.sub(r'^\d+[_ ]*',"",loja_dir).replace("_"," ").strip() or loja_dir
    return ticket, loja, mes, nome

_HDR = ("descri","valor unit","total","quant")
def _eh_header(row):
    j=" ".join((c or "").lower() for c in row)
    return ("descri" in j) and ("valor" in j) and ("total" in j)

def extrair_itens(pdf_bytes):
    """Retorna lista de itens [{n,descricao,quant,unid,valor_unit,total}]. [] se não achou tabela."""
    itens=[]
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for tbl in (page.extract_tables() or []):
                if not tbl or len(tbl)<2: continue
                hi=next((k for k,row in enumerate(tbl) if _eh_header(row)), None)
                if hi is None: continue
                for row in tbl[hi+1:]:
                    cells=[(c or "").strip() for c in row]
                    linha=" ".join(cells).lower()
                    if "total geral" in linha: continue
                    # espera: [n, descricao, quant, unid, valor_unit, total]
                    if len(cells)<6:
                        cells=cells+[""]*(6-len(cells))
                    n,desc,quant,unid,vu,tot=cells[0],cells[1],cells[2],cells[3],cells[4],cells[5]
                    if not desc or not (_num(vu) or _num(tot)): continue
                    itens.append({"n":n,"descricao":desc.replace("\n"," ").strip(),
                        "quant":_num(quant),"unid":(unid or "UN")[:8],
                        "valor_unit":_num(vu),"total":_num(tot)})
    return itens

def main():
    if not (APP_KEY and APP_SEC and REFRESH):
        print("!! Faltam os segredos DROPBOX_APP_KEY/APP_SECRET/REFRESH_TOKEN"); return
    access=obter_token()
    arquivos=[]
    for p in PASTAS:
        print(f">> listando {p}"); arquivos += [(p,x) for x in listar(access,p)]
    print(f"Total de PDFs: {len(arquivos)}\n")

    linhas=[]; ok=0; falhou=[]
    for i,(pasta,path) in enumerate(arquivos,1):
        ticket,loja,mes,nome=_meta(path)
        try:
            its=extrair_itens(baixar(access,path))
        except Exception as e:
            falhou.append((nome,str(e)[:80])); continue
        if not its: falhou.append((nome,"tabela não reconhecida")); continue
        ok+=1
        for it in its:
            linhas.append({"pasta":pasta.split(" ")[0],"mes":mes,"loja":loja,"ticket":ticket,"arquivo":nome,
                "item":it["descricao"],"quantidade":it["quant"],"unidade":it["unid"],
                "valor_unit_orcamento":it["valor_unit"],"valor_total_item":it["total"]})
        if i%25==0: print(f"  ...{i}/{len(arquivos)} PDFs")

    with open("itens_orcamentos.csv","w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=["pasta","mes","loja","ticket","arquivo","item","quantidade","unidade","valor_unit_orcamento","valor_total_item"])
        w.writeheader()
        for r in linhas: w.writerow(r)

    print(f"\n=== RESUMO ===")
    print(f"PDFs lidos com itens: {ok}/{len(arquivos)}")
    print(f"PDFs sem tabela reconhecida: {len(falhou)}")
    print(f"Total de itens extraídos: {len(linhas)}")
    print("CSV gerado: itens_orcamentos.csv")
    if falhou:
        print("\n--- não extraídos (até 30) ---")
        for n,mot in falhou[:30]: print(f"  {n} · {mot}")

if __name__=="__main__":
    main()
