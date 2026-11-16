import os
import time
import datetime
from urllib.parse import urljoin
import requests
from supabase import create_client

# ==========================================
# CONFIGURAÇÕES E VARIÁVEIS DE AMBIENTE
# ==========================================
# O GitHub Actions irá injetar essas variáveis através dos secrets
API_URI = os.getenv("API_URI")
API_USER = os.getenv("API_USER")
API_PASS = os.getenv("API_PASS")
API_TIMEOUT = 180

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ENDPOINT_PRODUTOS_V5 = "/api/millenium!vdl_anjuu/produtos/Lista_Produtos_Sem_Box_v5?$format=json"

# Inicializa cliente Supabase
if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERRO: Credenciais do Supabase não encontradas nas variáveis de ambiente.")
    exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==========================================
# FUNÇÕES DE LIMPEZA E FORMATAÇÃO
# ==========================================
def limpar_texto(valor) -> str | None:
    if not valor:
        return None
    texto = " ".join(str(valor).replace('\r', ' ').replace('\n', ' ').split()).strip()
    return texto if texto else None

def limpar_descricao(desc_bruta) -> str | None:
    texto = limpar_texto(desc_bruta)
    if not texto:
        return None
    return texto.split(" - ")[0].strip()

def limpar_grade(grade_bruta) -> str | None:
    texto = limpar_texto(grade_bruta)
    if not texto:
        return None
    return texto.replace('/', '.')

def mapear_marca(item: dict) -> str | None:
    bruta = limpar_texto(item.get("marca"))
    if not bruta:
        return None
    upper = bruta.upper()
    
    if "ERRO" in upper or "CANCELADO" in upper:
        return "IGNORAR"
        
    if "ANJUU" in upper:
        return "AISHTY"
    if "ALIRIO" in upper:
        return "WE'RE THE BASIC"
    if "VASPAC" in upper:
        return "VASPAC"
    return upper

def extrair_campanha(item: dict) -> str | None:
    lista_str = limpar_texto(item.get("lista_campanhas"))
    if lista_str:
        unicas = list(dict.fromkeys([c.strip() for c in lista_str.split(",") if c.strip()]))
        if unicas:
            return ", ".join(unicas)
    campanhas = item.get("campanhas")
    if isinstance(campanhas, list) and campanhas:
        nomes = [limpar_texto(c.get("nome_campanha")) for c in campanhas if isinstance(c, dict)]
        nomes = [n for n in nomes if n]
        if nomes:
            return ", ".join(dict.fromkeys(nomes))
    return None

# ==========================================
# COMUNICAÇÃO COM API MILLENNIUM
# ==========================================
def obter_base_url() -> str:
    if not API_URI:
        raise ValueError("Variável API_URI não encontrada.")
    base = API_URI.strip().rstrip('/')
    if not base.startswith(('http://', 'https://')):
        base = f"http://{base}"
    return base

def obter_raiz() -> str:
    return obter_base_url().split('/api/')[0]

def login_millennium() -> str:
    if not (API_USER and API_PASS):
        raise ValueError("API_USER / API_PASS não encontrados.")

    url = f"{obter_raiz()}/api/login?$format=json&$dateformat=iso"
    resp = requests.get(
        url,
        headers={"WTS-Authorization": f"{API_USER}/{API_PASS}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Falha no login do Millennium (HTTP {resp.status_code}).")

    token = resp.json().get("session", "")
    if not token:
        raise RuntimeError("A API não devolveu uma sessão válida.")
    return token

def logout_millennium(token: str) -> None:
    try:
        requests.get(f"{obter_raiz()}/api/logout", headers={"WTS-Session": token}, timeout=5)
    except Exception:
        pass

def buscar_dados_api(dias_recentes: int = 7) -> list[dict]:
    todos_itens = []
    url_atual = f"{obter_raiz()}{ENDPOINT_PRODUTOS_V5}"
    corpo_base: dict = {"estoque_maior_que": -9999}
    
    data_str = (datetime.datetime.now() - datetime.timedelta(days=dias_recentes)).strftime("%Y-%m-%d")
    parametros_data = ["data_atualizacao_inicial", "data_atualizacao", "nenhum"]

    print("🔑 Autenticando no Millennium...")
    token = login_millennium()
    headers = {"WTS-Session": token, "Content-Type": "application/json"}

    try:
        for param_teste in parametros_data:
            corpo = corpo_base.copy()
            if param_teste != "nenhum":
                corpo[param_teste] = data_str
                
            print(f"📡 Buscando dados com parâmetro '{param_teste}' (Data: {data_str})...")
            resposta = requests.get(url_atual, headers=headers, json=corpo, timeout=API_TIMEOUT)

            if resposta.status_code == 400 and param_teste != "nenhum":
                continue

            resposta.raise_for_status()
            dados = resposta.json()
            
            itens_pagina = dados.get("value", [])
            todos_itens.extend(itens_pagina)
            print(f"✅ Recebidos {len(itens_pagina)} itens na primeira página.")

            proximo_link = dados.get("odata.nextLink") or dados.get("@odata.nextLink")
            url_proxima = urljoin(url_atual, proximo_link) if proximo_link else None

            while url_proxima:
                print("⏳ Buscando próxima página...")
                resp_prox = requests.get(url_proxima, headers=headers, json=corpo, timeout=API_TIMEOUT)
                resp_prox.raise_for_status()
                dados_prox = resp_prox.json()
                itens_prox = dados_prox.get("value", [])
                todos_itens.extend(itens_prox)
                print(f"✅ Recebidos {len(itens_prox)} itens...")
                
                link = dados_prox.get("odata.nextLink") or dados_prox.get("@odata.nextLink")
                url_proxima = urljoin(url_proxima, link) if link else None
            
            break # Se funcionou com este parâmetro, interrompe o loop
    finally:
        print("🔒 Encerrando sessão do Millennium...")
        logout_millennium(token)

    return todos_itens

# ==========================================
# ATUALIZAÇÃO DO SUPABASE
# ==========================================
def obter_referencias_ocultas_manualmente() -> set[str]:
    print("📋 Consultando itens ocultos manualmente no Supabase...")
    try:
        resp = supabase.table("produtos").select("referencia").eq("oculto_manual", True).execute()
        return {str(r["referencia"]).strip() for r in (resp.data or [])}
    except Exception as e:
        print(f"Aviso: Erro ao buscar ocultos manuais - {e}")
        return set()

def rodar_sincronizacao():
    print(f"🚀 Iniciando sincronização (Data/Hora atual: {datetime.datetime.now()})")
    
    # Busca 7 dias para garantir que pegamos baixas de estoque
    itens_api = buscar_dados_api(dias_recentes=7)
    
    if not itens_api:
        print("⚠️ Nenhum item retornado pela API. Encerrando.")
        return

    refs_ocultas = obter_referencias_ocultas_manualmente()
    agrupados_por_ref: dict = {}

    print("⚙️ Processando e agrupando referências...")
    for item in itens_api:
        marca_mapeada = mapear_marca(item)
        if marca_mapeada == "IGNORAR":
            continue

        ref = limpar_texto(item.get("referencia"))
        if not ref:
            continue

        estoque_item = int(item.get("disp_pronta_entrega") or 0)
        esgotada = bool(item.get("referencia_esgotada"))
        id_sem = item.get("id_semelhante")
        id_sem_int = int(id_sem) if id_sem is not None and str(id_sem).strip().isdigit() and int(id_sem) > 0 else None

        esta_ativo = False if ref in refs_ocultas else (not esgotada)

        if ref not in agrupados_por_ref:
            agrupados_por_ref[ref] = {
                "referencia": ref,
                "descricao": limpar_descricao(item.get("descricao")),
                "marca": marca_mapeada,
                "departamento": limpar_texto(item.get("departamento")),
                "grupo": limpar_texto(item.get("grupo")),
                "colecao": limpar_texto(item.get("colecao")),
                "subcolecao": limpar_texto(item.get("subcolecao")),
                "campanha": extrair_campanha(item),
                "id_semelhante": id_sem_int,
                "tipo_peca": limpar_texto(item.get("tipo")),
                "grade": limpar_grade(item.get("grade")),
                "composicao": limpar_texto(item.get("composicao")),
                "preco": float(item.get("preco_padrao_pc") or 0),
                "estoque_pe": estoque_item,
                "ativo": esta_ativo,
            }
        else:
            agrupados_por_ref[ref]["estoque_pe"] += estoque_item
            if not agrupados_por_ref[ref]["id_semelhante"] and id_sem_int:
                agrupados_por_ref[ref]["id_semelhante"] = id_sem_int

            # Atualiza os dados de texto caso venham em outras cores/variantes
            for campo, valor_novo in [
                ("marca", marca_mapeada),
                ("descricao", limpar_descricao(item.get("descricao"))),
                ("tipo_peca", limpar_texto(item.get("tipo"))),
                ("grade", limpar_grade(item.get("grade"))),
                ("composicao", limpar_texto(item.get("composicao"))),
                ("departamento", limpar_texto(item.get("departamento"))),
                ("grupo", limpar_texto(item.get("grupo"))),
                ("colecao", limpar_texto(item.get("colecao"))),
                ("subcolecao", limpar_texto(item.get("subcolecao"))),
                ("campanha", extrair_campanha(item)),
            ]:
                if not agrupados_por_ref[ref][campo] and valor_novo:
                    agrupados_por_ref[ref][campo] = valor_novo

    lista_final = list(agrupados_por_ref.values())
    total_refs = len(lista_final)
    print(f"📦 Total de referências únicas processadas para envio: {total_refs}")

    if lista_final:
        print("☁️ Enviando lotes para o Supabase...")
        tamanho_lote = 200
        lotes_enviados = 0
        for i in range(0, total_refs, tamanho_lote):
            lote = lista_final[i : i + tamanho_lote]
            supabase.table("produtos").upsert(lote, on_conflict="referencia").execute()
            lotes_enviados += 1
            print(f"   -> Lote {lotes_enviados} enviado (Itens {i+1} a {i+len(lote)})")

    print("🎉 Sincronização concluída com sucesso!")

if __name__ == "__main__":
    try:
        rodar_sincronizacao()
    except Exception as e:
        print(f"❌ Erro crítico durante a execução: {e}")
        exit(1)
