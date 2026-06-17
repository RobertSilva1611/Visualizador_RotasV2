import streamlit as st
import pandas as pd
import numpy as np
from geopy.geocoders import ArcGIS
from datetime import datetime, date
import unicodedata
import re
import requests
import json
import time
import sqlite3
import urllib.parse

# ==========================================
# 1. CONFIGURAÇÕES GERAIS E CSS
# ==========================================
st.set_page_config(page_title="Visualizador Operacional", layout="wide")

# Chave do Google Maps
GOOGLE_MAPS_API_KEY = "AIzaSyCU46Uqvxnxkh5dF21jwUxdEtejMwstUC8"
DB_CACHE = 'memoria_geocoding.db'

# --- PALETA DE CORES ---
CORES_HEX = [
    '#e6194b', "#faf61c", "#1a1916", '#3cb44b', '#f58231', 
    "#666968", '#46f0f0', '#f032e6', '#bcf60c', "#0d5224", 
    '#008080', "#580e86", '#9a6324', "#4363d8", '#800000'
]

# --- ESTILO CSS PERSONALIZADO ---
st.markdown("""
<style>
    div.kpi-card {
        padding: 15px;
        border-radius: 10px;
        color: white;
        text-align: center;
        box-shadow: 2px 2px 5px rgba(0,0,0,0.2);
        margin-bottom: 10px;
    }
    div.kpi-title { font-size: 14px; font-weight: bold; margin-bottom: 5px; opacity: 0.9; }
    div.kpi-value { font-size: 28px; font-weight: bold; }
    
    .bg-blue { background-color: #2980b9; }
    .bg-green { background-color: #27ae60; }
    .bg-red { background-color: #c0392b; }
    .bg-orange { background-color: #d35400; }
    
    [data-testid="stDataFrame"] { width: 100%; }
</style>
""", unsafe_allow_html=True)


# ==========================================
# 2. FUNÇÕES AUXILIARES E DE MEMÓRIA (CACHE)
# ==========================================

def limpar_volume(val):
    if pd.isna(val) or val == '' or val is None: return 0.0
    try:
        if isinstance(val, str):
            v_str = re.sub(r'[^\d.,]', '', val).replace(',', '.')
            return float(v_str) if v_str else 0.0
        return float(val)
    except:
        return 0.0

def iniciar_banco_cache():
    conn = sqlite3.connect(DB_CACHE)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cache_geo (
            Chave TEXT PRIMARY KEY,
            Latitude TEXT,
            Longitude TEXT,
            Score TEXT,
            ID_Cliente TEXT,
            End_Correto TEXT,
            Bairro_Correto TEXT
        )
    ''')
    conn.commit()
    conn.close()

def carregar_memoria():
    iniciar_banco_cache()
    conn = sqlite3.connect(DB_CACHE)
    df = pd.read_sql("SELECT * FROM cache_geo", conn)
    conn.close()
    return df

def salvar_memoria(novo_df_cache):
    iniciar_banco_cache()
    colunas_banco = ['Chave', 'Latitude', 'Longitude', 'Score', 'ID_Cliente', 'End_Correto', 'Bairro_Correto']
    for col in colunas_banco:
        if col not in novo_df_cache.columns:
            novo_df_cache[col] = ""
            
    conn = sqlite3.connect(DB_CACHE)
    novo_df_cache.to_sql('temp_geo', conn, if_exists='replace', index=False)
    conn.execute('''
        INSERT OR REPLACE INTO cache_geo (Chave, Latitude, Longitude, Score, ID_Cliente, End_Correto, Bairro_Correto)
        SELECT Chave, Latitude, Longitude, Score, ID_Cliente, End_Correto, Bairro_Correto FROM temp_geo
    ''')
    conn.commit()
    conn.close()

def gerar_chave_cache(endereco, cidade, cep):
    e = str(endereco).strip().lower()
    c = str(cidade).strip().lower()
    z = str(cep).replace('-', '').replace('.', '').strip()
    return f"{e}|{c}|{z}"

def corrigir_texto_erp(texto):
    if not isinstance(texto, str): return ""
    texto = str(texto)
    correcoes = {
        'sÃ£o': 'são', 'SÃ£o': 'São', 'Ã£': 'ã', 'Ã©': 'é', 
        'Ã³': 'ó', 'Ã­': 'í', 'Ã§': 'ç', 'Ãª': 'ê', 
        'Ã¢': 'â', 'Ãµ': 'õ', 'Ãº': 'ú', 'Ã¡': 'á',
        'Ã‰': 'É', 'Ã': 'Á', 'Ã“': 'Ó'
    }
    for errado, certo in correcoes.items():
        texto = texto.replace(errado, certo)
    try: 
        texto = texto.encode('cp1252').decode('utf-8')
    except: 
        pass
    return texto.strip()

def limpar_endereco_para_geocoding(endereco):
    endereco = corrigir_texto_erp(endereco)
    if " - " in endereco:
        partes = endereco.split(" - ")
        if any(char.isdigit() for char in partes[0]): 
            endereco = partes[0]
            
    endereco = re.sub(r'[.,\- ]+$', '', endereco)
    if not isinstance(endereco, str): return ""
    
    try:
        endereco = endereco.encode('cp1252').decode('utf-8')
    except:
        pass 

    padrao_inutil = r'(?i)\b(ap|apt|apto|bl|bloco|sl|sala|cj|conjunto|casa|loja|térreo|fundos|km|frente|lado)\b.*'
    endereco = re.sub(padrao_inutil, '', endereco)
    
    nfkd_form = unicodedata.normalize('NFKD', endereco)
    endereco = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    endereco = re.sub(r'[^a-zA-Z0-9\s,-]', '', endereco)
    
    if ',' not in endereco:
        endereco = re.sub(r'([a-zA-Z])\s+(\d+)', r'\1, \2', endereco)
        
    return endereco.strip().strip(",.-")

def is_valid_sp_coord(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
        return (-25.5 <= lat <= -19.0) and (-53.5 <= lon <= -44.0)
    except:
        return False

@st.cache_data(ttl=3600)
def get_rota_detalhada_rua_otimizada(lista_pontos):
    if len(lista_pontos) < 2: return []
    caminho_completo = []
    chunk_size = 20
    for i in range(0, len(lista_pontos) - 1, chunk_size - 1):
        chunk = lista_pontos[i : i + chunk_size]
        if len(chunk) < 2: continue
        coords_str = ";".join([f"{p[1]},{p[0]}" for p in chunk])
        url = f"http://router.project-osrm.org/route/v1/driving/{coords_str}?overview=full&geometries=geojson"
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if 'routes' in data and len(data['routes']) > 0:
                    geometry = data['routes'][0]['geometry']['coordinates']
                    caminho_completo.extend([[p[1], p[0]] for p in geometry])
                    continue
        except: pass
        caminho_completo.extend(chunk)
    return caminho_completo

def limpar_nome_cidade(cidade):
    """
    Trata a string de município: MAIÚSCULO, sem acentos, sem caracteres 
    especiais e sem espaços duplos, garantindo agrupamento correto.
    """
    if pd.isna(cidade) or str(cidade).strip() == "": 
        return ""
    
    c = str(cidade).upper().strip()
    c = unicodedata.normalize('NFKD', c).encode('ASCII', 'ignore').decode('utf-8')
    c = re.sub(r'[^A-Z0-9\s]', '', c)
    c = re.sub(r'\s+', ' ', c).strip()
    return c

# ==========================================
# 3. LÓGICA DO VISUALIZADOR OPERACIONAL
# ==========================================

def mostrar_visualizador_operacional():
    st.title("👁️ Visualizador Operacional de Rotas")
    
    if 'config_concluida' not in st.session_state:
        st.session_state['config_concluida'] = False

    st.markdown("""
        <div style="background-color: #fff3cd; padding: 20px; border-radius: 10px; border-left: 6px solid #ffc107; margin-bottom: 20px;">
            <h4 style="color: #856404; margin-top: 0;">⚠️ Atenção: Regra de Importação</h4>
            <p style="color: #856404; font-size: 15px;">
                Para garantir a precisão do mapa, suba <b>apenas clientes que possuam data de visita programada</b>.<br>
                Linhas com datas vazias ou textos como 'Fechamento' serão ignoradas pelo visualizador (indo para Sem Rota).
            </p>
        </div>
    """, unsafe_allow_html=True)

    upload_view = st.file_uploader("📂 Subir Arquivo de Rota (CSV/Excel)", type=['csv', 'xlsx'], key="up_visualizador")

    if 'arquivo_atual_vis' not in st.session_state or st.session_state.get('arquivo_atual_vis') != (upload_view.name if upload_view else None):
        st.session_state['arquivo_atual_vis'] = upload_view.name if upload_view else None
        st.session_state['config_concluida'] = False
        if 'mapeamento_colunas' in st.session_state:
            del st.session_state['mapeamento_colunas']

    if upload_view:
        if upload_view.name.endswith('.csv'):
            df_view_raw = pd.read_csv(upload_view)
        else:
            df_view_raw = pd.read_excel(upload_view)

        # =========================================================
        # SEÇÃO 1: MAPEAMENTO DE COLUNAS
        # =========================================================
        if not st.session_state['config_concluida']:
            st.write("### 📊 Dados Carregados")
            st.dataframe(df_view_raw.head(), use_container_width=True)

            st.write("### ⚙️ Mapeamento de Colunas")
            colunas = df_view_raw.columns.tolist()
            
            def tentar_achar_coluna(palavras_chave):
                for i, col in enumerate(colunas):
                    if any(p.lower() in str(col).lower() for p in palavras_chave):
                        return i
                return 0

            c1, c2, c3 = st.columns(3)
            col_vendedor = c1.selectbox("Vendedor:", colunas, index=tentar_achar_coluna(['vendedor', 'vend']))
            col_data_orig = c2.selectbox("Data da Visita:", colunas, index=tentar_achar_coluna(['data', 'roteiro']))
            col_id_orig = c3.selectbox("Código do Cliente:", colunas, index=tentar_achar_coluna(['cód', 'cod', 'id', 'sap', 'cnpj']))
            
            c4, c5, c6 = st.columns(3)
            col_end_orig = c4.selectbox("Endereço:", colunas, index=tentar_achar_coluna(['endereço', 'endereco', 'rua']))
            col_bairro_orig = c5.selectbox("Bairro:", colunas, index=tentar_achar_coluna(['bairro']))
            col_nome_orig = c6.selectbox("Nome do Cliente:", colunas, index=tentar_achar_coluna(['cliente', 'razão', 'razao', 'nome', 'social']))

            c7, c8, c9 = st.columns(3)
            col_cidade_orig = c7.selectbox("Cidade/Município:", colunas, index=tentar_achar_coluna(['cidade', 'municipio', 'mun']))
            col_cep_orig = c8.selectbox("CEP (Opcional):", colunas, index=tentar_achar_coluna(['cep']))
            
            opcoes_vol = ["(Nenhum)"] + colunas
            idx_vol_achado = tentar_achar_coluna(['vol', 'peso', 'qtde', 'quantidade', 'volume', 'litros'])
            idx_vol_padrao = idx_vol_achado + 1 if any(p in colunas[idx_vol_achado].lower() for p in ['vol', 'peso', 'qtde', 'quantidade', 'volume', 'litros']) else 0
            col_vol_orig = c9.selectbox("Volume/Peso (Opcional):", opcoes_vol, index=idx_vol_padrao)

            # --- RESTAURADO: ORGANIZAÇÃO SEMANAL (ROTA MATRIZ) ---
            st.write("### 📅 Organização Semanal (Opcional)")
            opcoes_rota = ["(Nenhuma)"] + colunas
            default_rota_idx = 0
            if "Rota Matriz" in colunas:
                default_rota_idx = opcoes_rota.index("Rota Matriz")
            else:
                for i, col in enumerate(opcoes_rota):
                    if any(p in str(col).lower() for p in ['rota matriz', 'semana', 'matriz']):
                        default_rota_idx = i
                        break
            
            col_rota_matriz = st.selectbox("Coluna de Rota/Semana (Ex: 'Rota Matriz' com dados '1ªSem.1-Segunda'):", opcoes_rota, index=default_rota_idx)

            st.write("")
            if st.button("🚀 Gerar Dashboard", type="primary", use_container_width=True):
                st.session_state['mapeamento_colunas'] = {
                    'vendedor': col_vendedor,
                    'data': col_data_orig,
                    'id': col_id_orig,
                    'endereco': col_end_orig,
                    'bairro': col_bairro_orig,
                    'nome': col_nome_orig,
                    'cidade': col_cidade_orig,
                    'cep': col_cep_orig,
                    'vol': col_vol_orig,
                    'rota': col_rota_matriz
                }
                st.session_state['config_concluida'] = True
                st.rerun()

        # =========================================================
        # SEÇÃO 2: DASHBOARD
        # =========================================================
        if st.session_state['config_concluida']:
            
            if st.button("⚙️ Alterar Mapeamento de Colunas"):
                st.session_state['config_concluida'] = False
                st.rerun()
            
            st.divider()

            mapa = st.session_state['mapeamento_colunas']
            col_vendedor = mapa['vendedor']
            col_data_orig = mapa['data']
            col_id_orig = mapa['id']
            col_end_orig = mapa['endereco']
            col_bairro_orig = mapa['bairro']
            col_nome_orig = mapa['nome']
            col_cidade_orig = mapa['cidade']
            col_cep_orig = mapa['cep']
            col_vol_orig = mapa['vol']
            col_rota_matriz = mapa['rota']

            vendedores = sorted(df_view_raw[col_vendedor].dropna().astype(str).unique())
            sel_v = st.selectbox("👤 Vendedor", vendedores)
            df_filtro = df_view_raw[df_view_raw[col_vendedor].astype(str) == str(sel_v)].copy()

            # =====================================================
            # TRATAMENTO DE DATA (LENDO COMO TEXTO PURO)
            # =====================================================
            def limpar_data(val):
                v = str(val).strip()
                if pd.isna(val) or v.lower() in ["nan", "none", "", "nat", "fechamento"]: 
                    return "⚠️ Sem Data"
                # Remove o horário fantasma que o Pandas/Excel injeta
                return v.replace(" 00:00:00", "") 
            
            df_filtro["Roteiro_Data"] = df_filtro[col_data_orig].apply(limpar_data)

            # =====================================================
            # TRATAMENTO DA ROTA MATRIZ E DADOS INVÁLIDOS
            # =====================================================
            if col_rota_matriz != "(Nenhuma)":
                def extrair_semana(texto):
                    texto = str(texto).strip()
                    if texto.startswith("1ªSem"): return "1ª Semana"
                    if texto.startswith("2ªSem"): return "2ª Semana"
                    if texto.startswith("3ªSem"): return "3ª Semana"
                    if texto.startswith("4ªSem"): return "4ª Semana"
                    return "⚠️ Clientes Sem Rota" # Fora do padrão

                def extrair_dia(texto):
                    texto = str(texto).upper()
                    if "SEGUNDA" in texto: return "SEGUNDA"
                    if "TERÇA" in texto or "TERCA" in texto: return "TERÇA"
                    if "QUARTA" in texto: return "QUARTA"
                    if "QUINTA" in texto: return "QUINTA"
                    if "SEXTA" in texto: return "SEXTA"
                    if "SABADO" in texto or "SÁBADO" in texto: return "SÁBADO"
                    if "DOMINGO" in texto: return "DOMINGO"
                    return "SEM DIA"

                df_filtro["Semana_Visita"] = df_filtro[col_rota_matriz].apply(extrair_semana)
                df_filtro["Dia_Visita"] = df_filtro[col_rota_matriz].apply(extrair_dia)
            else:
                df_filtro["Semana_Visita"] = "Única"
                df_filtro["Dia_Visita"] = df_filtro["Roteiro_Data"]

            # =====================================================
            # COLUNAS PADRÃO
            # =====================================================
            df_filtro["Cidade_Ref"] = df_filtro[col_cidade_orig].apply(limpar_nome_cidade)
            df_filtro["Cliente"] = df_filtro[col_nome_orig]
            df_filtro["Codigo_Cliente"] = df_filtro[col_id_orig]
            df_filtro["Endereço_Limpo"] = df_filtro[col_end_orig]
            df_filtro["Bairro_Ref"] = df_filtro[col_bairro_orig]

            if col_cep_orig in df_filtro.columns:
                df_filtro["CEP_Ref"] = df_filtro[col_cep_orig]
            else:
                df_filtro["CEP_Ref"] = ""

            tot_clientes = len(df_filtro)
            tot_cidades = df_filtro["Cidade_Ref"].replace("", pd.NA).dropna().nunique()

            st.markdown(f"### 📋 Resumo Estratégico: {sel_v}")

            html_kpi = f"""
    <div style="display:flex;gap:20px;margin-bottom:20px;">
        <div style="flex:1; background:white; padding:20px; border-radius:10px; border-top:4px solid #1a73e8; text-align:center; box-shadow:1px 1px 5px rgba(0,0,0,0.15);">
            <h2 style="color:#333; margin-top:0; margin-bottom:5px;">{tot_clientes}</h2>
            <span style="color:#666; font-weight:bold; font-size:14px;">TOTAL CLIENTES</span>
        </div>
        <div style="flex:1; background:white; padding:20px; border-radius:10px; border-top:4px solid #1a73e8; text-align:center; box-shadow:1px 1px 5px rgba(0,0,0,0.15);">
            <h2 style="color:#333; margin-top:0; margin-bottom:5px;">{tot_cidades}</h2>
            <span style="color:#666; font-weight:bold; font-size:14px;">TOTAL CIDADES</span>
        </div>
    </div>
    """
            st.markdown(html_kpi, unsafe_allow_html=True)

            # =====================================================
            # ABAS DAS SEMANAS (COM ABA "TUDO")
            # =====================================================
            
            # Adicionamos "Tudo" no início da ordem
            ordem_semanas = [
                "Tudo", "1ª Semana", "2ª Semana", "3ª Semana", "4ª Semana", "Única", "⚠️ Clientes Sem Rota"
            ]

            # Filtra quais abas realmente existem (e mantém o "Tudo" se houver dados)
            semanas_unicas = ["Tudo"] + [s for s in ordem_semanas if s in df_filtro["Semana_Visita"].unique() and s != "Tudo"]

            if semanas_unicas:
                tabs = st.tabs(semanas_unicas)

                for idx, tab in enumerate(tabs):
                    with tab:
                        semana = semanas_unicas[idx]
                        
                        # Se for a aba "Tudo", pega o dataframe inteiro, senão filtra pela semana
                        if semana == "Tudo":
                            df_sem = df_filtro.copy()
                            coluna_agrupamento = "Roteiro_Data" # Agrupa cronologicamente
                        else:
                            df_sem = df_filtro[df_filtro["Semana_Visita"] == semana].copy()
                            # Se tem Rota Matriz mapeada, agrupa por Dia (Segunda, Terça...). Se não, agrupa por Data
                            coluna_agrupamento = "Dia_Visita" if col_rota_matriz != "(Nenhuma)" else "Roteiro_Data"

                        busca = st.text_input("🔍 Buscar cliente ou cidade...", key=f"busca_{idx}")

                        if busca:
                            filtro = (
                                df_sem["Cliente"].astype(str).str.contains(busca, case=False, na=False)
                            ) | (
                                df_sem["Cidade_Ref"].astype(str).str.contains(busca, case=False, na=False)
                            )
                            df_sem = df_sem[filtro]

                        # Lógica de Ordenação dos Dias
                        if coluna_agrupamento == "Dia_Visita" and col_rota_matriz != "(Nenhuma)":
                            ordem_dias = {"SEGUNDA": 1, "TERÇA": 2, "QUARTA": 3, "QUINTA": 4, "SEXTA": 5, "SÁBADO": 6, "DOMINGO": 7, "SEM DIA": 8, "PENDENTE": 9}
                            dias = sorted(df_sem[coluna_agrupamento].unique(), key=lambda x: ordem_dias.get(x, 99))
                        else:
                            # Se for a aba Tudo ou se não tiver matriz, agrupa pela Data pura (Roteiro_Data) ordenada alfabeticamente
                            dias = sorted(df_sem[coluna_agrupamento].unique())

                        for dia in dias:
                            df_dia = df_sem[df_sem[coluna_agrupamento] == dia].copy()

                            qtd_visitas = len(df_dia)
                            cidades = df_dia["Cidade_Ref"].dropna().astype(str).unique()
                            cidades = [c for c in cidades if c.strip() != ""] 
                            qtd_cidades = len(cidades)
                            texto_cidades = ", ".join(cidades[:3])
                            if qtd_cidades > 3: texto_cidades += "..."

                            # Define se o título mostrará "Dia:" ou "Data:" dependendo da aba
                            label_prefix = "Data" if coluna_agrupamento == "Roteiro_Data" else "Dia"
                            titulo = f"📅 {label_prefix}: {dia} | {qtd_visitas} visitas | 📍 {qtd_cidades} cid. ({texto_cidades})"

                            with st.expander(titulo):
                                
                                # =====================================================
                                # GERADOR DE LINK DO GOOGLE MAPS (OTIMIZADO)
                                # =====================================================
                                enderecos_maps = []
                                for _, row in df_dia.iterrows():
                                    if pd.notna(row['Endereço_Limpo']) and str(row['Endereço_Limpo']).strip() != "":
                                        
                                        end_str = f"{row['Endereço_Limpo']}, {row['Bairro_Ref']}, {row['Cidade_Ref']} - SP, Brasil"
                                        
                                        cep_val = str(row['CEP_Ref']).replace(".", "").replace("-", "").strip()
                                        if cep_val and cep_val.lower() not in ['nan', 'none', '']:
                                            end_str = f"{row['Endereço_Limpo']}, {row['Bairro_Ref']}, {row['Cidade_Ref']} - SP, CEP {cep_val}, Brasil"
                                            
                                        enderecos_maps.append(urllib.parse.quote(end_str))
                                

                                st.dataframe(
                                    df_dia[
                                        [
                                            "Codigo_Cliente",
                                            "Cliente",
                                            "Endereço_Limpo",
                                            "Bairro_Ref",
                                            "Cidade_Ref",
                                            "CEP_Ref",
                                            "Roteiro_Data"
                                        ]
                                    ],
                                    use_container_width=True,
                                    hide_index=True
                                )

# ==========================================
# 4. EXECUÇÃO PRINCIPAL
# ==========================================

if __name__ == "__main__":
    mostrar_visualizador_operacional()