# Hardplay – Documentação do Projeto

## Visão geral

O Hardplay é um sistema de cadastros que grava clientes, fornecedores, produtos, trabalhadores e vendas no Azure e leva esses dados, sem intervenção manual, até um relatório no Power BI.

- **Aplicação (Streamlit):** formulários em abas para cadastrar registros, mais uma tela de consulta.
- **Armazenamento:** conta `deltalake07`, com os contêineres `bronze`, `silver` e `gold`.
- **Processamento (Azure Data Factory):** um arquivo novo na Bronze dispara o pipeline da entidade, que atualiza a Silver e a Gold.
- **Relatório (Power BI):** lê as tabelas da Gold.

## Arquitetura

Os dados passam por três camadas de armazenamento, e um pipeline do Azure Data Factory move cada entidade de uma para a outra.

```
App Streamlit
   -> bronze (CSV por cadastro)
   -> trigger de eventos (Blob created)
   -> Data Flow 1: Bronze -> Silver
   -> Data Flow 2: Silver -> Gold (Parquet)
   -> Power BI (leitura da Gold)
```

O app grava na Bronze, e o trigger aciona o pipeline da entidade, que roda dois Data Flows em sequência: Bronze para Silver e Silver para Gold. O Power BI lê a Gold.

## Aplicação de cadastros (Streamlit)

O arquivo `app.py` tem seis abas. Cada cadastro vira um CSV de uma linha na pasta da entidade, dentro do contêiner `bronze`.

| Aba | Pasta na Bronze | ID automático |
| --- | --- | --- |
| Clientes | `clients/` | CLI137 em diante (3 dígitos) |
| Fornecedores | `fornecedores/` | FOR09 em diante (2 dígitos) |
| Produtos | `products/` | PROD26 em diante (2 dígitos) |
| Trabalhadores | `workers/` | FNC09 em diante (2 dígitos) |
| Vendas | `sales/` | VEN0233 em diante (4 dígitos) |
| Consultar | lê Gold ou Bronze | não se aplica |

Os números iniciais de fornecedor, produto, funcionário e venda foram estimados pelos exemplos do código original. Ajuste-os no bloco de configuração dos IDs, no topo do `app.py`, para o próximo número livre de cada tabela.

### IDs automáticos

O último número usado de cada entidade fica em `bronze/_controle/contador_<prefixo>.txt`. O app lê o contador, grava o novo valor com controle de versão do arquivo (ETag) e só então usa o ID. Se duas pessoas salvarem ao mesmo tempo, uma delas tenta de novo. Um upload que falha deixa um número sem uso. Para reiniciar a contagem, apague o arquivo do contador.

### Validações

- Nome obrigatório em clientes, fornecedores, produtos e trabalhadores.
- Formato de e-mail e CNPJ (pelos dígitos verificadores).
- Preços maiores que zero; aviso quando o preço de venda fica abaixo do de compra.
- Datas de admissão e de venda não podem ser futuras.

### Vendas

1. O produto é escolhido numa lista com ID, nome, preço e estoque, lida da pasta `bronze/products/`.
2. O valor total é calculado: preço de venda × quantidade.
3. O ID do cliente precisa ter cadastro em `bronze/clients/`. Se não tiver, a venda é bloqueada com a orientação de criar o registro na aba Clientes.
4. Se a quantidade for maior que o estoque, a venda é bloqueada.
5. Depois de gravar a venda, o app grava uma nova versão do produto em `bronze/products/` com o estoque reduzido. Se essa etapa falhar, a venda fica salva e o app avisa que o estoque não foi atualizado.

### Consulta

A aba Consultar lê por padrão a Gold (arquivos Delta, Parquet ou CSV em `gold/Clients`, `gold/Fornecedores`, `gold/Products`, `gold/Worker`, `gold/Sales`). A fonte Bronze mostra envios ainda não processados. Há busca em todas as colunas e exportação do resultado em CSV.

## Azure Storage

Tudo fica na conta `deltalake07`, em três contêineres, um por camada. O Azure diferencia maiúsculas de minúsculas nos nomes de pastas.

| Contêiner | Conteúdo | Quem grava |
| --- | --- | --- |
| `bronze` | CSVs brutos em `clients/`, `fornecedores/`, `products/`, `workers/`, `sales/`, e os contadores em `_controle/` | App (Streamlit) |
| `silver` | Dados tratados, um arquivo CSV por entidade (ex.: `Clients/clients_cleaned.csv`) | Data Flows `BS_...` |
| `gold` | Tabelas finais: `Clients`, `Fornecedores`, `Products`, `Worker`, `Sales` (ex.: `Clients/clients.parquet`) | Data Flows `SG_...` |

### Nome dos arquivos da Bronze

Cada cadastro gera `<pasta>/<pasta>_<AAAAMMDD>_<HHMMSS>_<6 caracteres>.csv`, com horário de São Paulo. O sufixo evita que dois envios no mesmo segundo se sobrescrevam. O arquivo histórico de cada entidade fica na mesma pasta (por exemplo `clients_historico.csv` e `products.csv`).

### Colunas gravadas pelo app

| Entidade | Colunas |
| --- | --- |
| Clientes | `id_cliente`, `nome_cliente`, `email`, `telefone`, `cidade` |
| Fornecedores | `id_fornecedor`, `nome_fornecedor`, `cnpj`, `prazo_entrega_dias`, `cod_produtos_fornecidos` |
| Produtos | `id_produto`, `nome_produto`, `categoria`, `id_fornecedor`, `preco_compra`, `preco_venda`, `quantidade_atual` |
| Trabalhadores | `id_funcionario`, `nome_funcionario`, `cargo`, `departamento`, `data_admissao` |
| Vendas | `id_venda`, `data_venda`, `id_cliente`, `id_produto`, `id_funcionario`, `quantidade`, `valor_total` |

## Azure Data Factory

Cada entidade tem um trigger de eventos de armazenamento, um pipeline e dois Data Flows. O trigger só dispara quando um arquivo novo aparece na pasta; sem cadastro novo, nada executa.

### Componentes por entidade

| Entidade | Trigger | Pipeline | Data Flows | Pasta da Bronze |
| --- | --- | --- | --- | --- |
| Clientes | `trg_clients` | `PL_CLIENTS` | `BS_Clients` → `SG_Clients` | `clients/` |
| Fornecedores | `trg_fornecedores` | `PL_FORNECEDORES` | `BS_Fornecedores` → data flow da Gold | `fornecedores/` |
| Produtos | `trg_products` | `PL_PRODUCTS` | `BS_Products` → data flow da Gold | `products/` |
| Trabalhadores | `trg_workers` | `PL_WORKERS` | `BS_Workers` → `SG_Worker` | `workers/` |
| Vendas | `trg_sales` | `PL_SALES` | `BS_Sales` → data flow da Gold | `sales/` |

Na data desta documentação, o fluxo de Clientes, Fornecedores e Vendas já executou com sucesso. O de Trabalhadores ainda precisa de um pipeline associado ao trigger, e o de Produtos precisa do tratamento de versões descrito abaixo.

### Triggers

- Tipo: eventos de armazenamento (`BlobEventsTrigger`), evento **Blob created**.
- Conta `deltalake07`, contêiner `bronze`, caminho que começa com `<pasta>/` e termina com `.csv`.
- O provedor `Microsoft.EventGrid` precisa estar registrado na assinatura.
- O arquivo do contador, em `_controle/`, fica fora dos filtros e não dispara nada.
- O trigger precisa estar associado a um pipeline e publicado (**Publish all**). Eventos só valem para arquivos criados depois da ativação.

### Pipelines

- Duas atividades de Data Flow ligadas por sucesso: Bronze → Silver, depois Silver → Gold.
- **Concurrency = 1**, para duas execuções seguidas não regravarem a mesma pasta ao mesmo tempo.

### Datasets e caminhos

Os datasets da Bronze (`B_...`) apontam para a **pasta** da entidade, sem nome de arquivo. Um nome fixo faz o Data Flow ignorar os CSVs novos.

| Etapa | Onde configurar | Valor |
| --- | --- | --- |
| Leitura da Bronze | Source options → Wildcard paths | `clients/*.csv` (ou a pasta da entidade) |
| Leitura da Silver | Source options → Wildcard paths | `Clients/*.csv` (nome real da pasta, com as mesmas maiúsculas) |
| Saída da Silver | Sink → Settings | Clear the folder, arquivo único (ex.: `clients_cleaned.csv`), Single partition |
| Saída da Gold | Sink → Settings | Clear the folder, arquivo único (ex.: `clients.parquet`), Single partition |

No Wildcard paths, o caminho é lido a partir da raiz do contêiner, então `*.csv` sozinho procura na raiz e não encontra nada.

### Produtos: versões do mesmo item

Cada baixa de estoque cria um novo arquivo do mesmo `id_produto`. O Data Flow `BS_Products` precisa ficar só com a versão mais recente:

1. No source, em **Column to store file name**, usar `arquivo`.
2. **Window** sobre `id_produto`, ordenado por `arquivo` decrescente, com a coluna `rank` = `rowNumber()`.
3. **Filter** com `rank == 1`.
4. Remover `rank` e `arquivo` antes do sink.

A ordem funciona porque `products.csv` vem antes de `products_2026...csv`. Para não depender disso, o arquivo histórico pode se chamar `products_00_historico.csv`.

## Power BI

O Power BI se conecta ao Blob Storage da conta `deltalake07` e lê os arquivos Parquet da Gold. As consultas ficam em seis itens: `BaseGold` (a fonte, com a lista de arquivos), `Clients`, `Fornecedores`, `Products`, `Workers` e `Sales`.

### Leitura de uma tabela

Como o Parquet guarda o nome e o tipo das colunas dentro do arquivo, não há linha de cabeçalho repetida. Cada consulta filtra os arquivos da pasta da entidade em `BaseGold` e combina os resultados:

```
let
    Fonte = BaseGold,
    Arquivos = Table.SelectRows(
        Fonte,
        each Text.StartsWith([Name], "Clients/") and Text.EndsWith([Name], ".parquet")
    ),
    Tabelas = Table.AddColumn(Arquivos, "Dados", each Parquet.Document([Content])),
    Resultado = Table.Combine(Tabelas[Dados])
in
    Resultado
```

Cada consulta deve apontar para a pasta da sua entidade. Os nomes das pastas da Gold são `Clients`, `Fornecedores`, `Products`, `Worker` e `Sales`.


## Guia de operação

### Rodar o app

1. Instale as bibliotecas: `pip install streamlit pandas azure-storage-blob deltalake pyarrow`.
2. No terminal, na pasta do `app.py`, rode `streamlit run app.py`.
3. Ao atualizar o código, pare o Streamlit com `Ctrl+C` e rode de novo. Apertar só "Rerun" no navegador pode manter o arquivo antigo.

### Testar o fluxo de ponta a ponta

1. No app, cadastre um registro (por exemplo, um cliente chamado "Teste ADF").
2. No portal do Azure, confira o CSV novo em `bronze/<pasta>/`.
3. No ADF, abra **Monitor → Trigger runs** e veja o trigger da entidade como *Succeeded*, em até dois minutos.
4. Em **Monitor → Pipeline runs**, acompanhe o pipeline. Cada Data Flow leva alguns minutos, porque sobe um cluster Spark; a primeira execução pode passar de 10 minutos.
5. No portal, confira a data de modificação dos arquivos na `silver` e na `gold`.
6. No Power BI, use **Atualizar visualização** e procure o registro. Na aba Consultar do app, escolha a fonte Gold.


### Custos

- O toggle **Data flow debug** mantém um cluster ligado e consome crédito. Deixe desligado fora dos testes; ele se desliga sozinho após 60 minutos sem uso.
- Os triggers não têm custo parado, e cada pipeline sobe e desliga o próprio cluster.
- Em Manage → Integration runtimes, deixe o **Time to live** do Data Flow em 0 para o cluster desligar após cada execução.


### Limitações da arquitetura

- **Estoque como arquivos:** o estoque é regravado como nova versão do produto. Duas vendas do mesmo produto no mesmo instante podem ler o mesmo estoque, e uma baixa sobrescreve a outra. Para uso mais pesado, o estoque deveria ficar num banco transacional, como o Azure SQL.
- **Falha na baixa de estoque:** a venda é gravada antes da baixa. Se a segunda etapa falhar, o app avisa, mas o estoque precisa ser corrigido manualmente.
- **Bronze lida em cada consulta:** a aba Consultar com fonte Bronze e a validação do cliente baixam todos os CSVs da pasta. Com milhares de arquivos fica lenta; a Gold não tem esse problema.
- **Atraso entre cadastro e Gold:** um cadastro novo só aparece na Gold depois que o pipeline termina, o que leva alguns minutos.
- **Sem edição ou exclusão:** o app só insere. Corrigir um cadastro exige gravar uma nova versão do mesmo ID (e a Silver precisa tratar versões, como em produtos) ou editar os arquivos no portal.
- **IDs de relacionamento:** fornecedor, produto e funcionário digitados nos formulários não são validados contra os cadastros. Apenas o ID do cliente na venda e o produto (lista) são conferidos.
- **Números iniciais dos IDs:** os de fornecedor, produto, funcionário e venda foram estimados. Confira o próximo número livre de cada tabela para não duplicar IDs.

## Próximos passos

- [ ] Agendar a atualização do Power BI no Service, ou disparar o refresh ao fim do pipeline.
- [ ] Configurar alertas de falha em Monitor → Alerts & metrics.
- [ ] Avaliar um banco transacional para o estoque, se o uso crescer.

## Documentação do código

Veja [docs/codigo.md](docs/codigo.md) para o detalhamento do `app.py`.
