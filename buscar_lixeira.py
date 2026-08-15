# -*- coding: utf-8 -*-
"""
FrotaHub - Busca (e opcionalmente restaura) na LIXEIRA do Dropbox os orcamentos
nao-lancados que sumiram das pastas 1 e 4.

Usa os MESMOS segredos do robo:
  DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN
  DROPBOX_BASE  (default: /FROTAHUB/2 - MANUTENCAO)

Modo:
  RESTAURAR=0 (default) -> so LISTA o que achou na lixeira (nao mexe em nada)
  RESTAURAR=1           -> restaura os arquivos encontrados para o lugar original
"""
import os, json, urllib.parse, urllib.request, urllib.error

APP_KEY = os.environ.get("DROPBOX_APP_KEY", "")
APP_SEC = os.environ.get("DROPBOX_APP_SECRET", "")
REFRESH = os.environ.get("DROPBOX_REFRESH_TOKEN", "")
BASE    = (os.environ.get("DROPBOX_BASE") or "/FROTAHUB/2 - MANUTENCAO").rstrip("/")
RESTAURAR = os.environ.get("RESTAURAR", "0") == "1"

# pastas a varrer (nome exato no Dropbox)
PASTAS = [
    "1 - ORÇAMENTOS NÃO LANÇADOS",
    "4 - ORÇAMENTOS DE RATEIO NÃO LANÇADOS",
    "2 - ORÇAMENTOS LANÇADOS",
    "5 - ORÇAMENTOS DE RATEIO LANÇADOS",
    "10 - ORÇAMENTOS MONTADOS",
    "11 - ORÇAMENTOS MONTADOS RATEIO",
]

# os 19 tickets faltando (com o valor esperado, so p/ conferencia visual)
FALTANDO = {
 "117044":"53.64","120779":"29.40/35.88/181.15/191.88","121733":"207.12","124766":"32.04",
 "125408":"252.56","125605":"6.60","125610":"38.28","125619":"19.08","125691":"180.96",
 "126032":"100.80","126114":"239.88","126367":"36.96","126484":"149.88/161.76","126655":"27.48",
 "126721":"225.36","126747":"132.78","130240":"135.48","130320":"42.48","130503":"38.04",
}

def _api(access, url, arg):
    req = urllib.request.Request(url, data=json.dumps(arg).encode("utf-8"),
        headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))

def obter_token():
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": REFRESH,
        "client_id": APP_KEY, "client_secret": APP_SEC}).encode()
    req = urllib.request.Request("https://api.dropbox.com/oauth2/token", data=data)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())["access_token"]

def listar_apagados(access, pasta):
    """Lista entradas (inclusive apagadas) de uma pasta, recursivo."""
    path = f"{BASE}/{pasta}"
    out = []
    url = "https://api.dropboxapi.com/2/files/list_folder"
    arg = {"path": path, "recursive": True, "include_deleted": True, "limit": 2000}
    while True:
        try:
            r = _api(access, url, arg)
        except urllib.error.HTTPError as e:
            print(f"   ! erro em '{pasta}': {e.read().decode()[:200]}")
            return out
        for e in r.get("entries", []):
            if e.get(".tag") == "deleted":
                out.append(e)  # {name, path_lower, path_display}
        if not r.get("has_more"): break
        url = "https://api.dropboxapi.com/2/files/list_folder/continue"
        arg = {"cursor": r["cursor"]}
    return out

def ultima_rev(access, path):
    """Rev mais recente (nao-apagada) p/ restaurar."""
    try:
        r = _api(access, "https://api.dropboxapi.com/2/files/list_revisions",
                 {"path": path, "mode": "path", "limit": 10})
        revs = r.get("entries", [])
        return revs[0]["rev"] if revs else None
    except urllib.error.HTTPError:
        return None

def restaurar(access, path, rev):
    return _api(access, "https://api.dropboxapi.com/2/files/restore",
                {"path": path, "rev": rev})

def main():
    if not (APP_KEY and APP_SEC and REFRESH):
        print("!! Faltam os segredos DROPBOX_APP_KEY/APP_SECRET/REFRESH_TOKEN"); return
    access = obter_token()
    print(f"Base: {BASE}   Modo: {'RESTAURAR' if RESTAURAR else 'SO LISTAR'}\n")

    achados = {}  # ticket -> lista de (path_display, rev)
    for pasta in PASTAS:
        print(f">> varrendo lixeira de: {pasta}")
        for e in listar_apagados(access, pasta):
            nome = e.get("name", "")
            for tk in FALTANDO:
                if tk in nome:
                    achados.setdefault(tk, []).append(e.get("path_display") or e.get("path_lower"))

    print("\n================ RESULTADO ================")
    encontrados = sorted(achados)
    for tk in sorted(FALTANDO):
        if tk in achados:
            print(f"  [ACHOU]  {tk} (valor {FALTANDO[tk]})")
            for p in achados[tk]:
                print(f"           {p}")
        else:
            print(f"  [--- ]   {tk} (valor {FALTANDO[tk]})  -> nao esta na lixeira dessas pastas")
    print(f"\nAchados na lixeira: {len(encontrados)}/19   |   Sem rastro: {19-len(encontrados)}/19")

    if RESTAURAR and achados:
        print("\n================ RESTAURANDO ================")
        for tk, paths in achados.items():
            for p in paths:
                rev = ultima_rev(access, p)
                if not rev:
                    print(f"  ! {tk} {p}: sem revisao p/ restaurar"); continue
                try:
                    restaurar(access, p, rev); print(f"  ✓ restaurado: {p}")
                except urllib.error.HTTPError as ex:
                    print(f"  ! falha {p}: {ex.read().decode()[:160]}")
    elif achados:
        print("\n(Modo so-listar. Para restaurar, rode de novo com RESTAURAR=1.)")

if __name__ == "__main__":
    main()
