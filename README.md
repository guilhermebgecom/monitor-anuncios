# Monitor de anúncios — Meta Ad Library

Coleta todo dia os **150 anúncios de maior impressão** de cada loja e de cada **palavra-chave** listada em `lojas.json` (via API da ScrapeCreators), agrupa por produto e publica uma **planilha interativa** no GitHub Pages:

- **Produtos anunciados:** criativo, produto, loja, anúncios ativos, variação, anúncio mais antigo (dias), monitorando desde, sinal de escala e o botão **Anúncios** (abre os links da Biblioteca de Anúncios daquele produto, ranqueados por posição em impressões ou por duplicações).
- **Evolução:** só os produtos em aumento rápido (possível início de escala), com gráfico por produto: linha verde quando sobe, vermelha quando cai; passe o mouse para ver os valores de cada dia.

Roda sozinho no **GitHub Actions** (todo dia às 07:23 de Brasília) e salva tudo neste repositório.

## O que fica no repositório

| Caminho | O que é |
|---|---|
| `lojas.json` | Lista de lojas monitoradas (edite aqui) |
| Planilha interativa | `https://SEU_USUARIO.github.io/monitor-anuncios/` (atualizada a cada coleta) |
| `relatorios/ultimo.md` | Resumo curto da coleta mais recente (com o link da planilha) |
| `relatorios/AAAA-MM-DD.md` | Relatório de cada dia |
| `relatorios/csv/` | `produtos.csv` (situação atual), `historico.csv` (produto × dia), `anuncios.csv` (anúncios ativos) |
| `criativos/<loja>/<id>.jpg` | Miniatura da capa de cada anúncio (imagem ou quadro inicial do vídeo), salva no dia em que o anúncio aparece |
| `dados/monitor.db` | Histórico completo (SQLite) — não apague |
| `scripts/` | Código do monitor |
| `.github/workflows/monitor.yml` | Agendamento diário |

## Configuração (uma vez)

1. **Créditos da ScrapeCreators:** em https://app.scrapecreators.com compre o pacote de créditos (US$ 47 = 25 mil créditos, não expiram) e copie sua API key.
2. **Crie um repositório privado** no GitHub (ex.: `monitor-anuncios`) e suba todos os arquivos desta pasta, **incluindo a pasta oculta `.github`**.
   - Pelo terminal, dentro desta pasta:
     ```bash
     git init -b main
     git add -A
     git commit -m "Monitor de anúncios"
     git remote add origin https://github.com/SEU_USUARIO/monitor-anuncios.git
     git push -u origin main
     ```
   - Pelo site: "Add file → Upload files" e arraste o conteúdo da pasta. Se a pasta `.github` não subir (alguns sistemas escondem pastas que começam com ponto), use "Add file → Create new file", digite o nome `.github/workflows/monitor.yml` e cole o conteúdo do arquivo.
3. **Guarde a chave como secret:** no repositório, *Settings → Secrets and variables → Actions → New repository secret*.
   - Name: `SCRAPECREATORS_API_KEY`
   - Secret: sua API key
4. **Permissão de escrita:** *Settings → Actions → General → Workflow permissions* → marque **Read and write permissions** → *Save*. (Necessário para o robô salvar o histórico.)
5. **Ativar o GitHub Pages (para a planilha):** *Settings → Pages → Build and deployment → Source* → escolha **GitHub Actions**. O GitHub Pages exige repositório **público** ou plano **GitHub Pro**. Atenção: o link da planilha é público (qualquer pessoa com o link vê), mas não aparece em buscadores.
6. **Primeira coleta:** aba *Actions* → **Monitor de anúncios** → **Run workflow**. Em poucos minutos aparece um commit "Coleta AAAA-MM-DD" e o relatório em `relatorios/ultimo.md`. O resumo também aparece na própria página da execução.

A partir daí roda sozinho todo dia. As comparações (escalando, novos, pausados) começam na segunda coleta.

## Adicionar ou remover lojas

Edite `lojas.json` no próprio GitHub (ícone de lápis) e salve:

```json
[
  {"nome": "Alessntials", "site": "m.alessntials.com", "pais": "US"},
  {"nome": "Nova Loja", "site": "novaloja.com", "pais": "US"}
]
```

- `site`: o domínio para onde os anúncios levam (com ou sem `m.`/`shop.`). Todo anúncio que leva para esse site entra, venha da página que vier.
- `pais`: código de 2 letras do mercado (`US`, `BR`, `GB`...) ou `ALL`.

**Palavras-chave** (anúncios de qualquer anunciante que usem o termo no texto):

```json
{"nome": "Car gadget free shipping", "palavra_chave": "car gadget free shipping", "pais": "US"}
```

- Por padrão as palavras podem aparecer em qualquer ordem. Para exigir a frase exata, acrescente `"frase_exata": true`.
- Na planilha, a palavra-chave aparece no filtro (grupo "Palavras-chave") e a coluna **Loja** mostra o site de quem está anunciando cada produto.
- Termos genéricos trazem anúncios sem relação; nomes de produto ou nicho funcionam melhor.
- **Mesmo produto em lojas diferentes vira uma linha só.** O monitor junta produtos de sites diferentes quando o criativo é praticamente idêntico (mesma imagem/quadro do vídeo, comparando forma e cor) ou quando os nomes no link têm 60%+ das palavras em comum. A coluna Loja mostra "N lojas" (passe o mouse para ver quais) e o botão Anúncios lista os anúncios de todas, com a loja de cada um.
- Remover uma loja do arquivo **não** apaga o histórico dela, mas ela continua sendo coletada até ser removida do banco: peça ao Claude ("pare de monitorar a loja X") ou rode `python scripts/monitor.py remove --name "Loja X"` com `ADS_MONITOR_DIR=dados`.

## Criativos

- Para cada anúncio novo, o robô salva a **imagem de capa** em miniatura (até 320 px, ~15–30 KB) na pasta `criativos/`. O relatório mostra essas miniaturas.
- O texto, título, botão, formato e link de destino de cada anúncio ficam no banco e em `relatorios/csv/anuncios.csv`.
- Para ver o anúncio completo (vídeo, carrossel), use o link **"ver anúncio"** do relatório: ele abre a Biblioteca de Anúncios e é permanente.
- Os links diretos de imagem/vídeo do Facebook **não** são guardados: eles expiram em poucos dias e contêm códigos longos que a proteção do GitHub confunde com senhas.
- Crescimento estimado da pasta: ~15–25 MB na primeira coleta e ~1–3 MB por dia depois. Se ficar grande, dá para reduzir o tamanho das miniaturas.

## Sinais de escala na planilha

Além do 🚀 (aumento rápido, aba Evolução), cada produto pode ter etiquetas na coluna **Sinal** (passe o mouse ou toque para ver o detalhe):

- **🔁 Duplicado** — 10+ duplicações somadas nos anúncios do produto (criativos replicados em vários conjuntos).
- **📄 Várias páginas** — anunciado por 3+ páginas diferentes.
- **⏳ Consolidado** — 3+ anúncios no ar há 30+ dias que continuam entre os de maior impressão (vencedor estável).

Esses sinais também aparecem nos cartões da Evolução, no topo da janela Anúncios, pesam na ordem das Sugestões e o resumo diário lista os ⏳ consolidados de cada loja.

## Sugestões de lojas

A aba **Sugestões** da planilha mostra as **10 lojas** encontradas nas buscas por palavra-chave que ainda não são monitoradas, ordenadas por sinais de escala (produtos escalando, crescimento de anúncios, presença em várias palavras-chave, volume na amostra). Marketplaces e redes sociais ficam de fora. Para cada sugerida, o robô consulta o **total de anúncios ativos na Biblioteca** (1 crédito por loja por dia). Para passar a monitorar uma loja: **Copiar para lojas.json** → **Editar lojas.json** → cole a linha e salve.

## Custos

- **ScrapeCreators:** ~14 créditos por loja ou palavra-chave por dia (150 anúncios). Com 6 entradas (4 lojas + 2 palavras-chave): ~84/dia, ~2.500/mês, **~US$ 4,70/mês** (o pacote de US$ 47 dura ~10 meses). Cada entrada nova soma ~US$ 0,80/mês. A verificação das 10 sugestões soma ~10 créditos/dia (~US$ 0,55/mês).
- **GitHub Actions:** cada coleta leva poucos minutos (a primeira com capas, um pouco mais); cabe com folga nos 2.000 minutos/mês gratuitos de repositórios privados.
- O consumo de créditos de cada dia aparece no log da execução ("Créditos ScrapeCreators usados nesta coleta").

## Atualizar só a planilha (sem gastar créditos)

Em *Actions → Monitor de anúncios → Run workflow*, marque **"Só atualizar a planilha"** antes de clicar no botão verde. O robô republica a planilha com os dados já coletados, sem chamar a API (útil depois de mudanças visuais).

## Mudar horário ou tamanho da amostra

Em `.github/workflows/monitor.yml`:
- Horário: linha `cron: "23 10 * * *"` (horário UTC; 10:23 UTC = 07:23 em Brasília). O GitHub pode atrasar execuções agendadas em alguns minutos.
- Amostra: `--max-ads 150` (use sempre o mesmo valor; mudar distorce a comparação entre dias).

## Analisar com o Claude

Abra uma sessão do Claude com este repositório e pergunte, por exemplo: "o que mudou nos anúncios das lojas esta semana?", "quais produtos estão escalando na Sakerplus?", "quais produtos aparecem em mais de uma loja?". O Claude lê `relatorios/` e `dados/monitor.db`.

## Problemas comuns

- **Execução falhou em "Verificar chave da API":** o secret `SCRAPECREATORS_API_KEY` não foi criado (passo 3).
- **Falhou em "Salvar no repositório" (permission denied / 403):** falta a permissão de escrita (passo 4).
- **"Créditos da ScrapeCreators esgotados":** recarregue os créditos; o histórico não se perde, só aquele dia fica sem coleta.
- **Falhou em "Salvar no repositório" com "Push cannot contain secrets":** a proteção do GitHub achou algo parecido com uma senha. Abra o link que aparece no log, veja o trecho apontado e decida: se for um falso alarme, marque como tal no próprio GitHub; se tiver dúvida, peça ajuda ao Claude.
- **Falhou em "Publicar planilha no GitHub Pages":** o Pages não está ativado com a fonte *GitHub Actions* (passo 5) ou o repositório é privado sem GitHub Pro. A coleta e o histórico são salvos mesmo assim.
- **Uma loja veio com 0 anúncios:** a coleta é marcada como vazia e ignorada nos relatórios; confira se o domínio em `lojas.json` está certo.
