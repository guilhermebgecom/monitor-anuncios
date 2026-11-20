# Monitor de anúncios — Meta Ad Library

Coleta todo dia os **300 anúncios de maior impressão** de cada loja listada em `lojas.json` (via API da ScrapeCreators), agrupa por produto e gera um relatório com o que mudou: produtos novos, escalando, reduzindo, saindo, anúncios novos e os que estão há mais tempo no ar.

Roda sozinho no **GitHub Actions** (todo dia às 07:23 de Brasília) e salva tudo neste repositório.

## O que fica no repositório

| Caminho | O que é |
|---|---|
| `lojas.json` | Lista de lojas monitoradas (edite aqui) |
| `relatorios/ultimo.md` | Relatório da coleta mais recente |
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
5. **Primeira coleta:** aba *Actions* → **Monitor de anúncios** → **Run workflow**. Em poucos minutos aparece um commit "Coleta AAAA-MM-DD" e o relatório em `relatorios/ultimo.md`. O resumo também aparece na própria página da execução.

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
- Remover uma loja do arquivo **não** apaga o histórico dela, mas ela continua sendo coletada até ser removida do banco: peça ao Claude ("pare de monitorar a loja X") ou rode `python scripts/monitor.py remove --name "Loja X"` com `ADS_MONITOR_DIR=dados`.

## Criativos

- Para cada anúncio novo, o robô salva a **imagem de capa** em miniatura (até 320 px, ~15–30 KB) na pasta `criativos/`. O relatório mostra essas miniaturas.
- O texto, título, botão, formato e link de destino de cada anúncio ficam no banco e em `relatorios/csv/anuncios.csv`.
- Para ver o anúncio completo (vídeo, carrossel), use o link **"ver anúncio"** do relatório: ele abre a Biblioteca de Anúncios e é permanente.
- Os links diretos de imagem/vídeo do Facebook **não** são guardados: eles expiram em poucos dias e contêm códigos longos que a proteção do GitHub confunde com senhas.
- Crescimento estimado da pasta: ~15–25 MB na primeira coleta e ~1–3 MB por dia depois. Se ficar grande, dá para reduzir o tamanho das miniaturas.

## Custos

- **ScrapeCreators:** ~30 créditos por loja por dia (300 anúncios ≈ 30 chamadas). Com 3 lojas: ~90/dia, ~2.700/mês, **~US$ 5/mês** (o pacote de US$ 47 dura ~9 meses). Cada loja nova soma ~US$ 1,70/mês.
- **GitHub Actions:** cada coleta leva poucos minutos (a primeira com capas, um pouco mais); cabe com folga nos 2.000 minutos/mês gratuitos de repositórios privados.
- O consumo de créditos de cada dia aparece no log da execução ("Créditos ScrapeCreators usados nesta coleta").

## Mudar horário ou tamanho da amostra

Em `.github/workflows/monitor.yml`:
- Horário: linha `cron: "23 10 * * *"` (horário UTC; 10:23 UTC = 07:23 em Brasília). O GitHub pode atrasar execuções agendadas em alguns minutos.
- Amostra: `--max-ads 300` (use sempre o mesmo valor; mudar distorce a comparação entre dias).

## Analisar com o Claude

Abra uma sessão do Claude com este repositório e pergunte, por exemplo: "o que mudou nos anúncios das lojas esta semana?", "quais produtos estão escalando na Sakerplus?", "quais produtos aparecem em mais de uma loja?". O Claude lê `relatorios/` e `dados/monitor.db`.

## Problemas comuns

- **Execução falhou em "Verificar chave da API":** o secret `SCRAPECREATORS_API_KEY` não foi criado (passo 3).
- **Falhou em "Salvar no repositório" (permission denied / 403):** falta a permissão de escrita (passo 4).
- **"Créditos da ScrapeCreators esgotados":** recarregue os créditos; o histórico não se perde, só aquele dia fica sem coleta.
- **Falhou em "Salvar no repositório" com "Push cannot contain secrets":** a proteção do GitHub achou algo parecido com uma senha. Abra o link que aparece no log, veja o trecho apontado e decida: se for um falso alarme, marque como tal no próprio GitHub; se tiver dúvida, peça ajuda ao Claude.
- **Uma loja veio com 0 anúncios:** a coleta é marcada como vazia e ignorada nos relatórios; confira se o domínio em `lojas.json` está certo.
