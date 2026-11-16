import os
import requests
from datetime import datetime, timezone
from supabase import create_client, Client

# Busca as credenciais de forma segura na nuvem do GitHub
API_URI = os.getenv("API_URI")
API_USER = os.getenv("API_USER")
API_PASS = os.getenv("API_PASS")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

def sync_catalogo():
    print("Iniciando sincronização na nuvem...")
    
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Erro ao inicializar o Supabase: {e}")
        return
    
    url_login = f"http://{API_URI}/api/login?$format=json&$dateformat=iso"
    try:
        resp_login = requests.get(url_login, headers={"WTS-Authorization": f"{API_USER}/{API_PASS}"}, timeout=60)
        resp_login.raise_for_status()
        token = resp_login.json().get("session")
    except Exception as e:
        print(f"Falha ao conectar na API Millennium: {e}")
        return

    print("Buscando dados no Millennium...")
    url_dados = f"http://{API_URI}/api/millenium!vdl_anjuu/produtos/Lista_Produtos_Sem_Box_v5"
    try:
        resp_dados = requests.get(
            url_dados, 
            headers={"WTS-Session": token, "Content-Type": "application/json"},
            json={"estoque_maior_que": -9999},
            timeout=360
        )
        resp_dados.raise_for_status()
        raw_data = resp_dados.json().get("value", [])
    except Exception as e:
        print(f"Falha ao descarregar produtos: {e}")
        return
    finally:
        try:
            requests.get(f"http://{API_URI}/api/logout", headers={"WTS-Session": token}, timeout=5)
        except:
            pass

    produtos_agrupados = {}
    caixas_lista = []
    
    for item in raw_data:
        ref = str(item.get("referencia") or "").strip()
        cod = str(item.get("cod_produto") or "").strip()
        
        if ref == cod or "teste" in cod.lower(): continue
        if not (item.get("kit") or item.get("caixa") or item.get("lo")): continue
        
        pe = max(0, int(item.get("disp_pronta_entrega") or 0))
        prog = max(0, int(item.get("qtde_cxs_pedido_compra_aberto") or 0))
        campanhas_raw = item.get("campanhas") or []
        campanhas = [str(c.get("nome_campanha") or "").strip() for c in campanhas_raw if isinstance(c, dict)] if isinstance(campanhas_raw, list) else []

        if ref not in produtos_agrupados:
            produtos_agrupados[ref] = {
                "referencia": ref,
                "filial": str(item.get("marca") or "").strip(),
                "marca": str(item.get("brand") or "").strip(),
                "colecao": str(item.get("colecao") or "").strip(),
                "subcolecao": str(item.get("subcolecao") or "").strip(),
                "tipo": str(item.get("tipo") or "").strip(),
                "grupo": str(item.get("grupo") or "").strip(),
                "departamento": str(item.get("departamento") or "").strip(),
                "campanhas": campanhas,
                "preco": float(item.get("preco_padrao_pc") or 0),
                "estoque_pe": 0,
                "estoque_prog": 0,
                "ativo": True,
                "atualizado_em": datetime.now(timezone.utc).isoformat()
            }
        
        produtos_agrupados[ref]["estoque_pe"] += pe
        produtos_agrupados[ref]["estoque_prog"] += prog
        
        cores_lista = [str(cor.get("cor") or "") for cor in (item.get("fotos_cor") or [])]
        caixas_lista.append({
            "referencia": ref,
            "cx_code": cod,
            "cores": cores_lista,
            "estoque_pe": pe,
            "estoque_prog": prog,
            "qtde_pcs_cx": int(item.get("qtde_pcs_cx") or 0),
            "grade": str(item.get("grade") or ""),
            "composicao": str(item.get("composicao") or ""),
            "is_lo": bool(item.get("lo"))
        })

    for p in produtos_agrupados.values():
        if (p["estoque_pe"] + p["estoque_prog"]) <= 0:
            p["ativo"] = False

    produtos_final = list(produtos_agrupados.values())
    for i in range(0, len(produtos_final), 500):
        supabase.table("produtos").upsert(produtos_final[i:i+500]).execute()
    
    supabase.table("caixas").delete().neq("referencia", "0").execute()
    for i in range(0, len(caixas_lista), 500):
        supabase.table("caixas").insert(caixas_lista[i:i+500]).execute()
        
    referencias_atualizadas = list(produtos_agrupados.keys())
    if referencias_atualizadas:
        supabase.table("produtos").update({"ativo": False}).not_.in_("referencia", referencias_atualizadas).execute()
    
    print("Sincronização na nuvem concluída com sucesso!")

if __name__ == "__main__":
    sync_catalogo()
