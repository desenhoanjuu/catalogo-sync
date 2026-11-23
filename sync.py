import os
import re
import datetime
from urllib.parse import urljoin

import requests
from supabase import create_client

# ==========================================
# CONFIGURAÇÕES (vêm dos Secrets do GitHub Actions)
# ==========================================
API_URI = os.getenv("API_URI")
API_USER = os.getenv("API_USER")
API_PASS = os.getenv("API_PASS")
API_TIMEOUT = 180

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ENDPOINT_PRODUTOS_V5 = "/api/millenium!vdl_anjuu/produtos/Lista_Produtos_Sem_Box_v5?$format=json"

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERRO: credenciais do Supabase não encontradas.")
    raise SystemExit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# ==========================================
# LIMPEZA DOS DADOS
# ==========================================
def limpar_texto(valor) -> str | None:
    if not valor:
        return None
    texto = " ".join(str(valor).replace("\r", " ").replace("\n", " ").split()).strip()
    return texto or None


def limpar_descricao(desc_bruta) -> str | None:
    texto = limpar_texto(desc_bruta)
    return texto.split(" - ")[0].strip() if texto else None


def limpar_grade(grade_bruta) -> str | None:
    texto = limpar_texto(grade_bruta)
    return texto.replace("/", ".") if texto else None


PALAVRAS_INGLES = {
    "black", "white", "grey", "gray", "green", "blue", "red", "beige", "brown", "pink", "yellow",
    "navy", "deep", "light", "dark", "olive", "khaki", "cream", "ivory", "wine", "purple", "orange",
    "mint", "sand", "camel", "lilac", "silver", "gold", "rose", "sky", "royal", "burgundy", "mustard",
    "teal", "turquoise", "charcoal", "taupe", "tan", "coffee", "chocolate", "salmon", "peach", "lavender",
}


def limpar_nome_cor(nome_cor_bruto) -> str | None:
    """Tira só o código Pantone do fim ("VERDE-14-6011" -> "VERDE") e a tradução
    em inglês ("PRETO-BLACK" -> "PRETO"). Estampas como "PRINT:416-1" ficam inteiras."""
    texto = limpar_texto(nome_cor_bruto)
    if not texto:
        return None
    texto = re.sub(r"-\d{2}-\d{4}\w*$", "", texto).strip()
    # Tira a tradução em inglês do fim ("BEGE-BEIGE", "MARROM 2-DEEP-GREEN", "CINZA CLARO-LIGHT")
    partes = texto.split("-")
    while len(partes) > 1 and all(w.lower() in PALAVRAS_INGLES for w in partes[-1].split()):
        partes.pop()
    limpo = "-".join(partes).strip()
    if limpo.upper() in ("OFF", "NUDE"):  # "OFF-WHITE" fica inteiro
        limpo = texto
    return limpo or None


def caixa_valida(item: dict) -> bool:
    """Caixas de verdade são identificadas por letra (A, B, TA, TB...). Linhas com número no lugar
    da letra (ex.: caixas NP com caixa "1") são erro de cadastro e ficam fora do site."""
    letra = limpar_texto(item.get("caixa"))
    return bool(letra) and re.fullmatch(r"[A-Za-z]+", letra) is not None


def registrar_caixa(produto: dict, item: dict, is_lo: bool, estoque_item: int) -> None:
    """Cada linha da API é uma caixa (ex.: CX-22D), que pode ter uma ou mais cores.
    Caixas LO (sortidas) não entram."""
    if is_lo:
        return
    if not caixa_valida(item):
        return
    letra = limpar_texto(item.get("caixa"))
    cores = []
    for c in item.get("fotos_cor") or []:
        nome = limpar_nome_cor(c.get("cor")) if isinstance(c, dict) else None
        if nome and nome not in cores:
            cores.append(nome)
    programacao = bool(item.get("programacao"))
    produto["caixas"].append({
        "caixa": letra.upper(),
        "pecas": int(item.get("qtde_pcs_cx") or 0),
        "cores": sorted(cores),
        "estoque": estoque_item,
        "esgotada": bool(item.get("caixa_esgotada")) and estoque_item <= 0,
        # Programação: ainda sem estoque físico, com previsão de chegada
        "programacao": programacao,
        # Preço por peça desta caixa no Millennium (o site multiplica pelas peças da caixa)
        "preco_pc": round(float(item.get("preco_padrao_pc") or 0), 2),
        "previsao": limpar_texto(item.get("data_entregapc")) if programacao else None,
    })


def finalizar_caixas(produto: dict) -> None:
    """Ordena as caixas (A, B, C...) e mantém as colunas antigas por cor preenchidas."""
    caixas = sorted(produto["caixas"], key=lambda c: (c["caixa"], c["pecas"]))
    produto["caixas"] = caixas
    caixas_por_cor, estoque_por_cor = {}, {}
    # Caixas de uma cor só e com estoque têm preferência na coluna antiga
    for cx in sorted(caixas, key=lambda c: (len(c["cores"]) != 1, c["estoque"] <= 0)):
        for cor in cx["cores"]:
            estoque_por_cor[cor] = estoque_por_cor.get(cor, 0) + (cx["estoque"] if len(cx["cores"]) == 1 else 0)
            caixas_por_cor.setdefault(cor, f"{cx['pecas']}{cx['caixa']}")
    produto["caixas_por_cor"] = caixas_por_cor
    produto["estoque_por_cor"] = estoque_por_cor

    # Programação: referência com alguma caixa em programação e a previsão mais próxima
    def _data(texto):
        try:
            return datetime.datetime.strptime(str(texto)[:10], "%d/%m/%Y")
        except (TypeError, ValueError):
            return None

    previsoes = [cx["previsao"] for cx in caixas if cx.get("programacao") and _data(cx.get("previsao"))]
    produto["programacao"] = any(cx.get("programacao") for cx in caixas)

    # Preço da referência = preço por peça da 1ª caixa válida (mesma regra do gerador de catálogo)
    produto["preco_peca"] = next((cx["preco_pc"] for cx in caixas if cx.get("preco_pc")), None)
    produto["previsao_entrega"] = min(previsoes, key=_data) if previsoes else None

    # Todas as caixas esgotadas (e nenhuma em programação): a referência sai do site sozinha
    sem_caixa_disponivel = bool(caixas) and not any(
        cx["estoque"] > 0 or cx.get("programacao") for cx in caixas
    )
    produto["esgotado_erp"] = bool(produto.get("esgotado_erp")) or sem_caixa_disponivel


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
        unicas = list(dict.fromkeys(c.strip() for c in lista_str.split(",") if c.strip()))
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
# API MILLENNIUM
# ==========================================
def obter_raiz() -> str:
    base = (API_URI or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    return base.split("/api/")[0]


def login_millennium() -> str:
    url = f"{obter_raiz()}/api/login?$format=json&$dateformat=iso"
    resp = requests.get(url, headers={"WTS-Authorization": f"{API_USER}/{API_PASS}"}, timeout=30)
    resp.raise_for_status()
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

    print("🔑 Autenticando no Millennium...")
    token = login_millennium()
    headers = {"WTS-Session": token, "Content-Type": "application/json"}
    try:
        for param in ["data_atualizacao_inicial", "data_atualizacao", "nenhum"]:
            corpo = corpo_base.copy()
            if param != "nenhum":
                corpo[param] = data_str
            resposta = requests.get(url_atual, headers=headers, json=corpo, timeout=API_TIMEOUT)
            if resposta.status_code == 400 and param != "nenhum":
                continue
            resposta.raise_for_status()
            dados = resposta.json()
            todos_itens.extend(dados.get("value", []))

            proximo = dados.get("odata.nextLink") or dados.get("@odata.nextLink")
            url_proxima = urljoin(url_atual, proximo) if proximo else None
            while url_proxima:
                resp = requests.get(url_proxima, headers=headers, json=corpo, timeout=API_TIMEOUT)
                resp.raise_for_status()
                dados = resp.json()
                todos_itens.extend(dados.get("value", []))
                proximo = dados.get("odata.nextLink") or dados.get("@odata.nextLink")
                url_proxima = urljoin(url_proxima, proximo) if proximo else None
            print(f"📡 {len(todos_itens)} registro(s) recebidos (filtro: {param}).")
            break
    finally:
        logout_millennium(token)
    return todos_itens


def obter_referencias_ocultas_manualmente() -> set[str]:
    try:
        resp = supabase.table("produtos").select("referencia").eq("oculto_manual", True).execute()
        return {str(r["referencia"]).strip() for r in (resp.data or [])}
    except Exception as e:
        print(f"Aviso: não consegui ler os ocultos manuais ({e})")
        return set()


# ==========================================
# SINCRONIZAÇÃO
# ==========================================
def rodar_sincronizacao():
    print(f"🚀 Sincronização iniciada em {datetime.datetime.now()}")
    itens_api = buscar_dados_api(dias_recentes=7)
    if not itens_api:
        print("⚠️ Nenhum item retornado pela API.")
        return

    agrupados: dict[str, dict] = {}

    for item in itens_api:
        marca = mapear_marca(item)
        if marca == "IGNORAR":
            continue
        ref = limpar_texto(item.get("referencia"))
        if not ref:
            continue
        # Linha "mestre" da referência (sem caixa): não é uma caixa vendável
        if item.get("kit") is False and not item.get("caixa"):
            continue

        is_lo = bool(item.get("lo"))
        caixa_errada = bool(limpar_texto(item.get("caixa"))) and not caixa_valida(item)
        estoque_item = 0 if (is_lo or caixa_errada) else int(item.get("disp_pronta_entrega") or 0)
        esgotada = bool(item.get("referencia_esgotada"))
        id_sem = item.get("id_semelhante")
        id_sem_int = int(id_sem) if id_sem is not None and str(id_sem).strip().isdigit() and int(id_sem) > 0 else None

        if ref not in agrupados:
            agrupados[ref] = {
                "referencia": ref,
                "descricao": limpar_descricao(item.get("descricao")),
                "marca": marca,
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
                "esgotado_erp": esgotada,
                "caixas": [],
                "caixas_por_cor": {},
                "estoque_por_cor": {},
            }
        else:
            p = agrupados[ref]
            p["estoque_pe"] += estoque_item
            if not p["id_semelhante"] and id_sem_int:
                p["id_semelhante"] = id_sem_int
            for campo, valor in [
                ("marca", marca), ("descricao", limpar_descricao(item.get("descricao"))),
                ("tipo_peca", limpar_texto(item.get("tipo"))), ("grade", limpar_grade(item.get("grade"))),
                ("composicao", limpar_texto(item.get("composicao"))), ("departamento", limpar_texto(item.get("departamento"))),
                ("grupo", limpar_texto(item.get("grupo"))), ("colecao", limpar_texto(item.get("colecao"))),
                ("subcolecao", limpar_texto(item.get("subcolecao"))), ("campanha", extrair_campanha(item)),
            ]:
                if not p[campo] and valor:
                    p[campo] = valor

        registrar_caixa(agrupados[ref], item, is_lo, estoque_item)

    for produto in agrupados.values():
        finalizar_caixas(produto)

    lista = list(agrupados.values())
    print(f"📦 {len(lista)} referência(s) para atualizar.")
    for i in range(0, len(lista), 200):
        supabase.table("produtos").upsert(lista[i : i + 200], on_conflict="referencia").execute()
        print(f"   lote {i // 200 + 1} enviado")
    print("🎉 Sincronização concluída.")


if __name__ == "__main__":
    try:
        rodar_sincronizacao()
    except Exception as e:
        print(f"❌ Erro: {e}")
        raise SystemExit(1)
