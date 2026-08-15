# -*- coding: utf-8 -*-
"""
FrotaHub - Recupera os orcamentos nao-lancados que sumiram das pastas 1/4,
COPIANDO o PDF montado que esta guardado na pasta 10/11 de volta pra pasta 1.
NAO apaga nada. NAO mexe no montado (so copia). NAO toca no banco.

Segredos (iguais ao robo): DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN
Modo:  APLICAR=0 (default) -> so mostra o plano   |   APLICAR=1 -> copia de verdade
"""
import os, json, urllib.parse, urllib.request, urllib.error

APP_KEY = os.environ.get("DROPBOX_APP_KEY", "")
APP_SEC = os.environ.get("DROPBOX_APP_SECRET", "")
REFRESH = os.environ.get("DROPBOX_REFRESH_TOKEN", "")
BASE    = (os.environ.get("DROPBOX_BASE") or "/FROTAHUB/2 - MANUTENCAO").rstrip("/")
APLICAR = os.environ.get("APLICAR", "0") == "1"

PASTA_DESTINO   = "1 - ORÇAMENTOS NÃO LANÇADOS"
PASTAS_MONTADOS = ["10 - ORÇAMENTOS MONTADOS", "11 - ORÇAMENTOS MONTADOS RATEIO"]

# 14 tickets com copia montada LIMPA na pasta 10 (LOJA_TICKET.pdf, 1 orcamento so).
# Fora daqui de proposito: 120779 e 126484 (multi-valor, conferir no backup) e
# 125605/126367/130503 (sem copia em lugar nenhum).
TICKETS = ["117044","121733","124766","125408","125610","125619","125691",
           "126032","126114","126655","126721","126747","130240","130320"]

def _api(access, url, arg):
    req = urllib.request.Request(url, data=json.dumps(arg).encode("utf-8"),
        headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))

def obter_token():
    data = urllib.parse.urlencode({"grant_type":"refresh_token","refresh_token":REFRESH,
        "client_id":APP_KEY,"client_secret":APP_SEC}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://api.dropbox.com/oauth2/token", data=data)) as r:
        return json.loads(r.read().decode())["access_token"]

def listar(access, pasta, include_deleted=False):
    path = f"{BASE}/{pasta}"
    out=[]; url="https://api.dropboxapi.com/2/files/list_folder"
    arg={"path":path,"recursive":True,"include_deleted":include_deleted,"limit":2000}
    while True:
        try: r=_api(access,url,arg)
        except urllib.error.HTTPError as e:
            print(f"   ! erro em '{pasta}': {e.read().decode()[:160]}"); return out
        for e in r.get("entries",[]):
            if e.get(".tag")=="file":
                out.append(e)  # name, path_display
        if not r.get("has_more"): break
        url="https://api.dropboxapi.com/2/files/list_folder/continue"; arg={"cursor":r["cursor"]}
    return out

def copiar(access, de, para):
    return _api(access,"https://api.dropboxapi.com/2/files/copy_v2",
                {"from_path":de,"to_path":para,"autorename":True})

def main():
    if not (APP_KEY and APP_SEC and REFRESH):
        print("!! Faltam os segredos DROPBOX_APP_KEY/APP_SECRET/REFRESH_TOKEN"); return
    access=obter_token()
    print(f"Base: {BASE}   Modo: {'APLICAR (copiar)' if APLICAR else 'SO PLANO'}\n")

    # o que ja existe na pasta destino (p/ nao duplicar)
    destino_nomes={e["name"] for e in listar(access, PASTA_DESTINO)}

    # candidatos montados
    montados=[]
    for p in PASTAS_MONTADOS:
        print(f">> lendo montados: {p}")
        montados += listar(access, p)

    plano=[]  # (ticket, from_path, to_path)
    for tk in TICKETS:
        cands=[e for e in montados
               if tk in e["name"] and e["name"].lower().endswith(".pdf")]
        # prefere o montado "puro" (LOJA_TICKET.pdf, sem _NOTA_)
        puros=[e for e in cands if "_NOTA_" not in e["name"].upper()]
        escolha = puros if puros else cands
        if not escolha:
            print(f"  [--- ] {tk}: nenhum PDF montado encontrado"); continue
        for e in escolha:
            if e["name"] in destino_nomes:
                continue  # ja esta na pasta 1
            to = f"{BASE}/{PASTA_DESTINO}/{e['name']}"
            plano.append((tk, e["path_display"], to))

    print("\n================ PLANO DE CÓPIA (10/11 -> pasta 1) ================")
    for tk, de, para in plano:
        print(f"  {tk}: {de}")
    print(f"\nTotal a copiar: {len(plano)}")

    if APLICAR and plano:
        print("\n================ COPIANDO ================")
        ok=0
        for tk, de, para in plano:
            try: copiar(access, de, para); ok+=1; print(f"  ✓ {tk}: {de.split('/')[-1]}")
            except urllib.error.HTTPError as ex:
                print(f"  ! {tk}: falha {ex.read().decode()[:140]}")
        print(f"\nCopiados: {ok}/{len(plano)}")
    elif plano:
        print("\n(Modo só-plano. Para copiar de verdade, rode com APLICAR=1.)")

if __name__ == "__main__":
    main()
