import io
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.storage.blob import BlobServiceClient

# ==========================================
# CONFIGURAÇÃO DO AZURE STORAGE
# ==========================================
CONNECTION_STRING = (
    "DefaultEndpointsProtocol=https;"
    ""
)
CONTAINER_NAME = "bronze"
CONTAINER_GOLD = "gold"
FUSO = ZoneInfo("")

# ==========================================
# CONFIGURAÇÃO DOS IDs AUTOMÁTICOS
# ==========================================
# (prefixo, primeiro número, quantidade de dígitos)
# Ajuste o "primeiro número" para o PRÓXIMO ID livre de cada entidade.
# Ex.: ("CLI", 137, 3) gera CLI137, CLI138, CLI139...
ID_CLIENTE = ("CLI", 137, 3)
ID_FORNECEDOR = ("FOR", 9, 2)
ID_PRODUTO = ("PROD", 26, 2)
ID_FUNCIONARIO = ("FNC", 9, 2)
ID_VENDA = ("VEN", 233, 4)

# st.set_page_config precisa ser o primeiro comando do Streamlit
st.set_page_config(
    page_title="Hardplay - Sistema de Cadastros e Ingestão",
    layout="centered",
)


def obter_connection_string() -> str | None:
    return CONNECTION_STRING


@st.cache_resource
def get_blob_service_client(conn_str: str) -> BlobServiceClient:
    """Cria o client uma única vez, em vez de a cada envio."""
    return BlobServiceClient.from_connection_string(conn_str)


# ==========================================
# ID AUTOMÁTICO (contador guardado no Azure)
# ==========================================
# O último número usado de cada entidade fica num arquivo em bronze/_controle/.
# Se ele não existir, a numeração começa no valor de "inicio".
# Para reiniciar a contagem de uma entidade, apague o arquivo dela.
def _blob_contador(prefixo: str):
    cliente = get_blob_service_client(obter_connection_string())
    return cliente.get_blob_client(
        container=CONTAINER_NAME, blob=f"_controle/contador_{prefixo.lower()}.txt"
    )


def _ler_contador(prefixo: str, inicio: int):
    """Retorna (último número usado, etag). Se não existe ainda, (inicio - 1, None)."""
    try:
        download = _blob_contador(prefixo).download_blob()
        texto = download.readall().decode("utf-8").strip()
        return int(texto), download.properties.etag
    except ResourceNotFoundError:
        return inicio - 1, None


def previsao_id(config: tuple) -> str:
    """Mostra qual será o próximo ID, sem reservar."""
    prefixo, inicio, largura = config
    try:
        ultimo, _ = _ler_contador(prefixo, inicio)
        return f"{prefixo}{ultimo + 1:0{largura}d}"
    except Exception:
        return "(automático)"


def reservar_id(config: tuple) -> str:
    """Reserva e retorna o próximo ID (ex.: CLI137), seguro contra envios simultâneos."""
    prefixo, inicio, largura = config
    blob = _blob_contador(prefixo)
    for _ in range(5):
        ultimo, etag = _ler_contador(prefixo, inicio)
        novo = ultimo + 1
        try:
            if etag:
                blob.upload_blob(
                    str(novo), overwrite=True, etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                )
            else:
                blob.upload_blob(str(novo), overwrite=False)
            return f"{prefixo}{novo:0{largura}d}"
        except (ResourceModifiedError, ResourceExistsError):
            continue  # outra pessoa reservou ao mesmo tempo; tenta de novo
    raise RuntimeError("Não foi possível reservar um novo ID. Tente novamente.")


# ==========================================
# ENVIO PARA A CAMADA BRONZE
# ==========================================
def enviar_para_bronze(df: pd.DataFrame, pasta_destino: str) -> str | None:
    """Converte o DataFrame em CSV e envia para a Bronze, com timestamp no nome do arquivo.

    Retorna o nome do arquivo gravado, ou None se falhar."""
    conn_str = obter_connection_string()
    if not conn_str:
        st.error("❌ Connection string do Azure não configurada no código.")
        return None

    try:
        # Timestamp no fuso do Brasil + sufixo único para evitar sobrescrever
        # arquivos enviados no mesmo segundo.
        timestamp_str = datetime.now(FUSO).strftime("%Y%m%d_%H%M%S")
        sufixo = uuid.uuid4().hex[:6]
        file_name = f"{pasta_destino}/{pasta_destino}_{timestamp_str}_{sufixo}.csv"

        csv_bytes = df.to_csv(index=False).encode("utf-8")

        blob_client = get_blob_service_client(conn_str).get_blob_client(
            container=CONTAINER_NAME, blob=file_name
        )
        blob_client.upload_blob(csv_bytes, overwrite=False)
        return file_name
    except Exception as e:
        st.error(f"❌ Erro ao enviar para o Azure Blob Storage: {e}")
        return None


# ==========================================
# CONSULTA DE CADASTROS EXISTENTES
# ==========================================
# entidade -> (pasta na Bronze, coluna do ID)
ENTIDADES = {
    "Clientes": ("clients", "id_cliente"),
    "Fornecedores": ("fornecedores", "id_fornecedor"),
    "Produtos": ("products", "id_produto"),
    "Trabalhadores": ("workers", "id_funcionario"),
    "Vendas": ("sales", "id_venda"),
}


@st.cache_data(ttl=60, show_spinner="Carregando cadastros do Azure...")
def carregar_cadastros(pasta: str, coluna_id: str) -> pd.DataFrame:
    """Lê todos os CSVs de uma pasta da Bronze e junta num único DataFrame.

    O resultado fica em cache por 60 segundos (botão "Atualizar" limpa o cache)."""
    container = get_blob_service_client(obter_connection_string()).get_container_client(
        CONTAINER_NAME
    )
    blobs = [
        b for b in container.list_blobs(name_starts_with=f"{pasta}/")
        if b.name.lower().endswith(".csv")
    ]
    if not blobs:
        return pd.DataFrame()

    def ler(blob) -> pd.DataFrame:
        dados = container.download_blob(blob.name).readall()
        df = pd.read_csv(io.BytesIO(dados), dtype=str, keep_default_na=False)
        df["enviado_em"] = blob.last_modified.astimezone(FUSO).strftime("%Y-%m-%d %H:%M:%S")
        return df

    with ThreadPoolExecutor(max_workers=8) as executor:
        partes = list(executor.map(ler, blobs))

    df = pd.concat(partes, ignore_index=True).sort_values("enviado_em")
    if coluna_id in df.columns:
        # Se o mesmo ID aparecer em mais de um arquivo, mantém o envio mais recente
        df = df.drop_duplicates(subset=coluna_id, keep="last")
    return df.sort_values("enviado_em", ascending=False).reset_index(drop=True)


def cliente_existe(id_cliente: str) -> bool:
    """Confere se o cliente já tem cadastro na Bronze (leitura imediata, sem esperar o ADF)."""
    carregar_cadastros.clear()  # garante que um cliente recém-criado seja encontrado
    try:
        df = carregar_cadastros("clients", "id_cliente")
    except Exception as e:
        st.error(f"❌ Não foi possível consultar os clientes no Azure: {e}")
        return False
    return (
        not df.empty
        and "id_cliente" in df.columns
        and id_cliente.strip().upper() in set(df["id_cliente"].str.strip().str.upper())
    )


# Pastas da camada Gold (atenção às maiúsculas: o Azure diferencia)
GOLD_PASTAS = {
    "Clientes": "Clients",
    "Fornecedores": "Fornecedores",
    "Produtos": "Products",
    "Trabalhadores": "Worker",
    "Vendas": "Sales",
}


def _credenciais_azure() -> tuple[str, str]:
    """Extrai nome da conta e chave da connection string."""
    partes = dict(p.split("=", 1) for p in CONNECTION_STRING.split(";") if "=" in p)
    return partes["AccountName"], partes["AccountKey"]


@st.cache_data(ttl=60, show_spinner="Carregando dados da Gold...")
def carregar_gold(pasta: str) -> pd.DataFrame:
    """Lê uma tabela da camada Gold. Detecta sozinho se é Delta, Parquet ou CSV."""
    conta, chave = _credenciais_azure()
    container = get_blob_service_client(obter_connection_string()).get_container_client(
        CONTAINER_GOLD
    )
    nomes = [b.name for b in container.list_blobs(name_starts_with=f"{pasta}/")]
    if not nomes:
        return pd.DataFrame()

    # 1) Tabela Delta (tem a pasta _delta_log)
    if any("/_delta_log/" in n for n in nomes):
        try:
            from deltalake import DeltaTable
        except ImportError:
            raise RuntimeError(
                "A Gold está em Delta. Instale a biblioteca: pip install deltalake"
            )
        opcoes = {"account_name": conta, "account_key": chave}
        ultimo_erro = None
        for uri in (
            f"abfss://{CONTAINER_GOLD}@{conta}.dfs.core.windows.net/{pasta}",
            f"az://{CONTAINER_GOLD}/{pasta}",
        ):
            try:
                return DeltaTable(uri, storage_options=opcoes).to_pandas()
            except Exception as e:  # tenta o próximo formato de endereço
                ultimo_erro = e
        raise RuntimeError(f"Não foi possível abrir a tabela Delta: {ultimo_erro}")

    # 2) Parquet ou 3) CSV (ignora arquivos auxiliares como _SUCCESS e .crc)
    for extensao, leitor in (
        (".parquet", lambda dados: pd.read_parquet(io.BytesIO(dados))),
        (".csv", lambda dados: pd.read_csv(io.BytesIO(dados), dtype=str, keep_default_na=False)),
    ):
        arquivos = [n for n in nomes if n.lower().endswith(extensao)]
        if arquivos:
            def ler(nome, leitor=leitor):
                return leitor(container.download_blob(nome).readall())

            with ThreadPoolExecutor(max_workers=8) as executor:
                partes = list(executor.map(ler, arquivos))
            return pd.concat(partes, ignore_index=True)

    raise RuntimeError(
        f"Não encontrei tabela Delta, .parquet nem .csv em `{CONTAINER_GOLD}/{pasta}/`."
    )


# ==========================================
# VALIDAÇÕES
# ==========================================
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validar_cnpj(cnpj: str) -> bool:
    """Valida CNPJ pelos dígitos verificadores."""
    numeros = re.sub(r"\D", "", cnpj)
    if len(numeros) != 14 or numeros == numeros[0] * 14:
        return False

    def digito(base: str, pesos: list[int]) -> str:
        soma = sum(int(d) * p for d, p in zip(base, pesos))
        resto = soma % 11
        return "0" if resto < 2 else str(11 - resto)

    d1 = digito(numeros[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    d2 = digito(numeros[:12] + d1, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return numeros[-2:] == d1 + d2


# ==========================================
# AUXILIARES DE FORMULÁRIO
# ==========================================
def preparar_formulario(chave: str, campos: list[str]):
    """Limpa os campos após um cadastro e mostra a mensagem do último salvamento."""
    if st.session_state.pop(f"limpar_{chave}", False):
        for campo in campos:
            st.session_state.pop(campo, None)

    salvo = st.session_state.pop(f"salvo_{chave}", None)
    if salvo:
        st.success(
            f"✅ {salvo['rotulo']} **{salvo['id']}** salvo no Azure: "
            f"`{CONTAINER_NAME}/{salvo['arquivo']}`"
        )
        if salvo.get("aviso"):
            st.warning(f"⚠️ {salvo['aviso']}")
        st.info(
            "O Azure Data Factory detectará este arquivo automaticamente para "
            "processar as camadas Silver e Gold e atualizar o Power BI."
        )


def campo_id(rotulo: str, config: tuple):
    """Campo de ID bloqueado, mostrando o próximo número que será usado."""
    st.text_input(
        f"{rotulo} (gerado automaticamente)",
        value=previsao_id(config),
        disabled=True,
    )


def salvar_com_id(chave: str, rotulo: str, config: tuple, pasta: str,
                  coluna_id: str, dados: dict, aviso: str | None = None,
                  apos_salvar=None):
    """Reserva o próximo ID, envia o CSV e recarrega a página com a mensagem de sucesso.

    apos_salvar: função opcional executada depois que o arquivo foi gravado
    (usada na venda para baixar o estoque do produto)."""
    try:
        novo_id = reservar_id(config)
    except Exception as e:
        st.error(f"❌ Erro ao gerar o ID: {e}")
        return

    df_novo = pd.DataFrame([{coluna_id: novo_id, **dados}])
    arquivo = enviar_para_bronze(df_novo, pasta)
    if arquivo:
        if apos_salvar:
            try:
                apos_salvar()
            except Exception as e:
                extra = f"A venda foi salva, mas o estoque NÃO foi atualizado: {e}"
                aviso = f"{aviso} {extra}" if aviso else extra
        st.session_state[f"salvo_{chave}"] = {
            "id": novo_id, "arquivo": arquivo, "rotulo": rotulo, "aviso": aviso,
        }
        st.session_state[f"limpar_{chave}"] = True
        st.rerun()


def tem_erros(erros: list[str]) -> bool:
    """Mostra os erros de validação (se houver) e informa se o envio deve parar."""
    for erro in erros:
        st.warning(f"⚠️ {erro}")
    return bool(erros)


# ==========================================
# INTERFACE
# ==========================================
st.title("🎮 Hardplay - Portal de Cadastros (Camada Bronze)")
st.markdown(
    "Insira novos dados nos formulários abaixo. O arquivo será salvo no Azure "
    "com o timestamp no nome."
)

(
    tab_clientes,
    tab_fornecedores,
    tab_produtos,
    tab_trabalhadores,
    tab_vendas,
    tab_consulta,
) = st.tabs([
    "👤 Clientes",
    "🏭 Fornecedores",
    "📦 Produtos",
    "👔 Trabalhadores",
    "🛒 Vendas",
    "🔎 Consultar",
])

# ==========================================
# 6. CONSULTAR CADASTROS
# ==========================================
with tab_consulta:
    st.subheader("🔎 Consultar Cadastros Existentes")
    entidade = st.selectbox("Entidade", list(ENTIDADES.keys()))
    pasta_consulta, coluna_id_consulta = ENTIDADES[entidade]
    fonte = st.radio(
        "Fonte dos dados",
        ["Gold (consolidado)", "Bronze (envios recentes)"],
        horizontal=True,
    )
    usar_gold = fonte.startswith("Gold")
    if usar_gold:
        st.caption(
            "A Gold só inclui cadastros já processados pelo pipeline do Data Factory. "
            "Para ver um cadastro recém-enviado, use a fonte Bronze."
        )

    col_busca, col_atualizar = st.columns([4, 1])
    termo = col_busca.text_input(
        "Buscar (ID, nome, e-mail, cidade, categoria...)", key="busca_termo"
    ).strip()
    col_atualizar.markdown("<div style='height: 1.9rem'></div>", unsafe_allow_html=True)
    if col_atualizar.button("🔄 Atualizar"):
        carregar_cadastros.clear()
        carregar_gold.clear()

    try:
        if usar_gold:
            origem = f"{CONTAINER_GOLD}/{GOLD_PASTAS[entidade]}"
            df_consulta = carregar_gold(GOLD_PASTAS[entidade])
        else:
            origem = f"{CONTAINER_NAME}/{pasta_consulta}"
            df_consulta = carregar_cadastros(pasta_consulta, coluna_id_consulta)
    except Exception as e:
        df_consulta = None
        st.error(f"❌ Erro ao ler os cadastros do Azure: {e}")

    if df_consulta is not None:
        if df_consulta.empty:
            st.info(f"Nenhum cadastro encontrado em `{origem}/`.")
        else:
            if termo:
                mascara = (
                    df_consulta.astype(str)
                    .apply(lambda c: c.str.contains(termo, case=False, na=False, regex=False))
                    .any(axis=1)
                )
                df_consulta = df_consulta[mascara]
            st.caption(f"{len(df_consulta)} registro(s) encontrado(s).")
            st.dataframe(df_consulta, hide_index=True)
            st.download_button(
                "⬇️ Baixar resultado em CSV",
                df_consulta.to_csv(index=False).encode("utf-8"),
                file_name=f"{pasta_consulta}_consulta.csv",
                mime="text/csv",
            )

# ==========================================
# 1. CLIENTES
# ==========================================
with tab_clientes:
    st.subheader("👤 Cadastro de Novo Cliente")
    preparar_formulario("cliente", ["cli_nome", "cli_email", "cli_telefone", "cli_cidade"])

    with st.form("form_clients"):
        campo_id("ID do Cliente", ID_CLIENTE)
        nome_cliente = st.text_input("Nome Completo do Cliente", key="cli_nome").strip()
        email = st.text_input("E-mail", key="cli_email").strip()
        telefone = st.text_input("Telefone", key="cli_telefone").strip()
        cidade = st.text_input("Cidade", key="cli_cidade").strip()
        submitted = st.form_submit_button("Salvar Cliente na Bronze")

    if submitted:
        erros = []
        if not nome_cliente:
            erros.append("O nome do cliente é obrigatório.")
        if email and not EMAIL_RE.match(email):
            erros.append("E-mail inválido.")

        if not tem_erros(erros):
            salvar_com_id(
                "cliente", "Cliente", ID_CLIENTE, "clients", "id_cliente",
                {
                    "nome_cliente": nome_cliente,
                    "email": email,
                    "telefone": telefone,
                    "cidade": cidade,
                },
            )

# ==========================================
# 2. FORNECEDORES
# ==========================================
with tab_fornecedores:
    st.subheader("🏭 Cadastro de Novo Fornecedor")
    preparar_formulario(
        "fornecedor", ["for_nome", "for_cnpj", "for_prazo", "for_produtos"]
    )

    with st.form("form_fornecedores"):
        campo_id("ID do Fornecedor", ID_FORNECEDOR)
        nome_fornecedor = st.text_input("Nome da Empresa Fornecedora", key="for_nome").strip()
        cnpj = st.text_input("CNPJ (Ex: 00.000.000/0001-00)", key="for_cnpj").strip()
        prazo_entrega_dias = st.number_input(
            "Prazo de Entrega (Dias)", min_value=1, value=5, step=1, key="for_prazo"
        )
        cod_produtos_fornecidos = st.text_input(
            "Códigos dos Produtos Fornecidos (Ex: PROD01, PROD11)", key="for_produtos"
        ).strip().upper()
        submitted = st.form_submit_button("Salvar Fornecedor na Bronze")

    if submitted:
        erros = []
        if not nome_fornecedor:
            erros.append("O nome do fornecedor é obrigatório.")
        if cnpj and not validar_cnpj(cnpj):
            erros.append("CNPJ inválido.")

        if not tem_erros(erros):
            salvar_com_id(
                "fornecedor", "Fornecedor", ID_FORNECEDOR, "fornecedores", "id_fornecedor",
                {
                    "nome_fornecedor": nome_fornecedor,
                    "cnpj": cnpj,
                    "prazo_entrega_dias": int(prazo_entrega_dias),
                    "cod_produtos_fornecidos": cod_produtos_fornecidos,
                },
            )

# ==========================================
# 3. PRODUTOS
# ==========================================
with tab_produtos:
    st.subheader("📦 Cadastro de Novo Produto / Hardware")
    preparar_formulario(
        "produto",
        ["prod_nome", "prod_categoria", "prod_fornecedor",
         "prod_compra", "prod_venda", "prod_qtd"],
    )

    with st.form("form_products"):
        campo_id("ID do Produto", ID_PRODUTO)
        nome_produto = st.text_input("Nome do Produto", key="prod_nome").strip()
        categoria = st.text_input(
            "Categoria (Ex: HARDWARE, PERIFERICOS)", key="prod_categoria"
        ).strip().upper()
        id_fornecedor = st.text_input(
            "ID do Fornecedor Responsável (Ex: FOR01)", key="prod_fornecedor"
        ).strip().upper()
        preco_compra = st.number_input(
            "Preço de Compra (R$)", min_value=0.0, step=0.01, format="%.2f", key="prod_compra"
        )
        preco_venda = st.number_input(
            "Preço de Venda (R$)", min_value=0.0, step=0.01, format="%.2f", key="prod_venda"
        )
        quantidade_atual = st.number_input(
            "Quantidade Atual em Estoque", min_value=0, value=10, step=1, key="prod_qtd"
        )
        submitted = st.form_submit_button("Salvar Produto na Bronze")

    if submitted:
        erros = []
        aviso = None
        if not nome_produto:
            erros.append("O nome do produto é obrigatório.")
        if not id_fornecedor:
            erros.append("O ID do fornecedor é obrigatório.")
        if preco_venda <= 0:
            erros.append("O preço de venda deve ser maior que zero.")
        if preco_compra <= 0:
            erros.append("O preço de compra deve ser maior que zero.")
        elif 0 < preco_venda < preco_compra:
            aviso = "O preço de venda está abaixo do preço de compra (margem negativa)."

        if not tem_erros(erros):
            salvar_com_id(
                "produto", "Produto", ID_PRODUTO, "products", "id_produto",
                {
                    "nome_produto": nome_produto,
                    "categoria": categoria,
                    "id_fornecedor": id_fornecedor,
                    "preco_compra": float(preco_compra),
                    "preco_venda": float(preco_venda),
                    "quantidade_atual": int(quantidade_atual),
                },
                aviso=aviso,
            )

# ==========================================
# 4. TRABALHADORES / WORKERS
# ==========================================
with tab_trabalhadores:
    st.subheader("👔 Cadastro de Novo Trabalhador (Worker)")
    preparar_formulario(
        "funcionario", ["fnc_nome", "fnc_cargo", "fnc_depto", "fnc_admissao"]
    )

    with st.form("form_workers"):
        campo_id("ID do Funcionário", ID_FUNCIONARIO)
        nome_funcionario = st.text_input("Nome Completo do Colaborador", key="fnc_nome").strip()
        cargo = st.text_input("Cargo", key="fnc_cargo").strip()
        departamento = st.text_input(
            "Departamento (Ex: VENDAS, SUPORTE)", key="fnc_depto"
        ).strip().upper()
        data_admissao = st.date_input("Data de Admissão", value=date.today(), key="fnc_admissao")
        submitted = st.form_submit_button("Salvar Trabalhador na Bronze")

    if submitted:
        erros = []
        if not nome_funcionario:
            erros.append("O nome do funcionário é obrigatório.")
        if data_admissao > date.today():
            erros.append("A data de admissão não pode ser futura.")

        if not tem_erros(erros):
            salvar_com_id(
                "funcionario", "Funcionário", ID_FUNCIONARIO, "workers", "id_funcionario",
                {
                    "nome_funcionario": nome_funcionario,
                    "cargo": cargo,
                    "departamento": departamento,
                    "data_admissao": data_admissao.isoformat(),
                },
            )

# ==========================================
# 5. VENDAS / SALES
# ==========================================
with tab_vendas:
    st.subheader("🛒 Registro de Nova Venda")
    preparar_formulario(
        "venda",
        ["ven_data", "ven_cliente", "ven_produto", "ven_funcionario", "ven_qtd"],
    )

    def _num(valor) -> float:
        """Converte texto do CSV ('1234.50' ou '1.234,50') em número."""
        try:
            texto = str(valor).strip()
            if "," in texto:
                texto = texto.replace(".", "").replace(",", ".")
            return float(texto)
        except ValueError:
            return 0.0

    def _brl(valor: float) -> str:
        return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    # Produtos já cadastrados (último registro de cada ID), com preço e estoque
    try:
        df_prod = carregar_cadastros("products", "id_produto")
    except Exception as e:
        df_prod = pd.DataFrame()
        st.error(f"❌ Erro ao ler os produtos do Azure: {e}")

    produtos = {}
    colunas_ok = {"id_produto", "preco_venda", "quantidade_atual"}
    if not df_prod.empty and colunas_ok <= set(df_prod.columns):
        for _, linha in df_prod.iterrows():
            produtos[linha["id_produto"].strip().upper()] = linha
    else:
        st.warning(
            "⚠️ Nenhum produto encontrado em `bronze/products/`. Cadastre o produto na aba "
            "📦 Produtos (ou copie o arquivo histórico de produtos para essa pasta)."
        )

    campo_id("ID da Venda", ID_VENDA)
    data_venda = st.date_input("Data da Venda", value=date.today(), key="ven_data")
    id_cliente = st.text_input("ID do Cliente (Ex: CLI001)", key="ven_cliente").strip().upper()
    id_produto = st.selectbox(
        "Produto",
        options=list(produtos),
        index=None,
        placeholder="Selecione o produto",
        format_func=lambda pid: (
            f"{pid} — {produtos[pid].get('nome_produto', '')} "
            f"({_brl(_num(produtos[pid]['preco_venda']))} | "
            f"estoque: {int(_num(produtos[pid]['quantidade_atual']))})"
        ),
        key="ven_produto",
    )
    id_funcionario = st.text_input(
        "ID do Funcionário / Vendedor (Ex: FNC01)", key="ven_funcionario"
    ).strip().upper()
    quantidade = st.number_input("Quantidade Vendida", min_value=1, value=1, step=1, key="ven_qtd")

    produto = produtos.get(id_produto) if id_produto else None
    preco_unit = _num(produto["preco_venda"]) if produto is not None else 0.0
    estoque = int(_num(produto["quantidade_atual"])) if produto is not None else 0
    valor_total = round(preco_unit * int(quantidade), 2)

    col_preco, col_estoque, col_total = st.columns(3)
    col_preco.metric("Preço unitário", _brl(preco_unit))
    col_estoque.metric("Estoque disponível", estoque)
    col_total.metric("Valor total", _brl(valor_total))

    if st.button("Registrar Venda na Bronze", type="primary"):
        erros = []
        if not id_cliente:
            erros.append(
                "Informe o ID do cliente. Se ele ainda não tem cadastro, "
                "crie o registro de cliente na aba 👤 Clientes."
            )
        elif not cliente_existe(id_cliente):
            erros.append(
                f"Cliente {id_cliente} não encontrado. "
                "Crie o registro de cliente na aba 👤 Clientes antes de registrar a venda."
            )
        if produto is None:
            erros.append("Selecione o produto.")
        elif int(quantidade) > estoque:
            erros.append(
                f"Estoque insuficiente: há {estoque} unidade(s) de {id_produto} "
                f"e a venda pede {int(quantidade)}."
            )
        elif valor_total <= 0:
            erros.append("O produto está com preço de venda zerado; corrija o cadastro dele.")
        if not id_funcionario:
            erros.append("O ID do funcionário é obrigatório.")
        if data_venda > date.today():
            erros.append("A data da venda não pode ser futura.")

        if not tem_erros(erros):

            def baixar_estoque():
                """Grava uma nova versão do produto com o estoque reduzido."""
                nova = {k: v for k, v in produto.to_dict().items() if k != "enviado_em"}
                nova["quantidade_atual"] = int(estoque - int(quantidade))
                if not enviar_para_bronze(pd.DataFrame([nova]), "products"):
                    raise RuntimeError("não foi possível gravar o produto no Azure.")
                carregar_cadastros.clear()

            salvar_com_id(
                "venda", "Venda", ID_VENDA, "sales", "id_venda",
                {
                    "data_venda": data_venda.isoformat(),
                    "id_cliente": id_cliente,
                    "id_produto": id_produto,
                    "id_funcionario": id_funcionario,
                    "quantidade": int(quantidade),
                    "valor_total": float(valor_total),
                },
                apos_salvar=baixar_estoque,
            )
