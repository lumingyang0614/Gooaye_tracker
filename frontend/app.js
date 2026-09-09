const byId = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const stanceName = (value) => ({bullish:'多頭', bearish:'空頭', neutral:'中立'}[value] || value || '中立');
const sourceLabel = (value) => ({whatmkreallysaid:'非官方逐字稿站', vocus_free:'vocus.cc 免費集數', whisper:'本機 Whisper 轉譯'}[value] || value || '尚未取得逐字稿');
const sourceTag = (value) => value === 'whisper' ? 'tag whisper' : 'tag';

document.querySelectorAll('nav button').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('nav button,.panel').forEach((el) => el.classList.remove('active'));
  button.classList.add('active');
  byId(button.dataset.tab).classList.add('active');
}));

function episodeCard(episode) {
  const tickers = episode.tickers || [];
  const comparisonNote = episode.comparison_notes
    ? `<p class="reason"><strong>音檔比對：</strong>${esc(episode.comparison_notes)}</p>` : '';
  const transcript = episode.transcript_text
    ? `<details class="original-post"><summary>展開完整逐字稿（${Number(episode.transcript_length || episode.transcript_text.length).toLocaleString()} 字）</summary><div class="original-text">${esc(episode.transcript_text)}</div></details>`
    : '';
  return `<article class="card">
    <div class="meta">
      <span class="tag ${esc(episode.sentiment)}">${stanceName(episode.sentiment)}</span>
      <span class="${sourceTag(episode.transcript_source)}">${sourceLabel(episode.transcript_source)}</span>
      <span class="importance" title="這一集分析結果的重要度，不是對立場的把握程度">重要度 ${Math.round(Number(episode.importance ?? 0.5) * 100)}%</span>
    </div>
    <h3>EP${esc(episode.episode_number)} · ${esc(episode.title)}</h3>
    <p>${esc(episode.summary || episode.description || '尚未完成分析')}</p>
    ${tickers.length ? `<div class="tickers">${tickers.map((t) => `<span>$${esc(t)}</span>`).join('')}</div>` : ''}
    ${episode.analysis_reason ? `<p class="reason"><strong>分析理由：</strong>${esc(episode.analysis_reason)}</p>` : ''}
    ${comparisonNote}
    ${transcript}
    <a class="source" href="${esc(episode.source_url || episode.vocus_url || '#')}" target="_blank" rel="noreferrer">查看逐字稿來源 ↗</a>
  </article>`;
}

function cards(target, items, empty = '目前沒有資料') {
  target.innerHTML = items.length ? items.map(episodeCard).join('') : `<article class="card"><h3>${esc(empty)}</h3></article>`;
}

function sentiment(counts) {
  byId('sentimentSummary').innerHTML = `
    <div class="sentiment-box bullish"><span>多頭</span><strong>${counts.bullish || 0}</strong></div>
    <div class="sentiment-box bearish"><span>空頭</span><strong>${counts.bearish || 0}</strong></div>
    <div class="sentiment-box neutral"><span>中立</span><strong>${counts.neutral || 0}</strong></div>`;
}

function segmentItem(segment) {
  return `<div class="segment-item">
    <div class="segment-meta">
      <span class="tag ${esc(segment.sentiment)}">${stanceName(segment.sentiment)}</span>
      <span>EP${esc(segment.episode_number)} · 第 ${esc(segment.chunk_index + 1)} 段</span>
      <span class="importance" title="這個片段的重要度，不是對立場的把握程度">重要度 ${Math.round(Number(segment.importance ?? 0.5) * 100)}%</span>
    </div>
    <p>${esc(segment.summary || '（無摘要）')}</p>
    ${segment.reason ? `<p class="reason"><strong>分析理由：</strong>${esc(segment.reason)}</p>` : ''}
    ${segment.source_url ? `<a class="source" href="${esc(segment.source_url)}" target="_blank" rel="noreferrer">查看逐字稿來源 ↗</a>` : ''}
  </div>`;
}

const MARKET_FILTERS = [
  {key: 'ALL', label: '全部'},
  {key: 'TW', label: '台股'},
  {key: 'US', label: '美股'},
  {key: 'OTHER', label: '其他市場'},
  {key: 'UNKNOWN', label: '待確認'},
];

let companyRows = [];
let activeMarket = 'ALL';

function pointList(title, items, kind) {
  if (!items.length) return '';
  return `<div class="${kind}"><h4>${title}</h4><ul>${items.map((item) => `<li>${esc(item)}</li>`).join('')}</ul></div>`;
}

function companyThesis(company) {
  const strengths = company.strengths || [];
  const weaknesses = company.weaknesses || [];
  const thesis = company.thesis
    ? `<p class="thesis">${esc(company.thesis)}</p>`
    : '<p class="thesis muted">尚未產生論點，請執行 scripts/rebuild_company_insights。</p>';
  const points = pointList('優勢', strengths, 'pros') + pointList('劣勢', weaknesses, 'cons');
  // Both lists empty is a legitimate model answer, not evidence of a failure:
  // COMPANY_INSTRUCTIONS tells the model to return empty arrays rather than
  // invent reasons it has no basis for. Claiming the batch failed here would
  // report an outage every time the host merely mentioned a company in
  // passing. Whether the narrative pass actually ran is an operational fact,
  // reported by the sync/rebuild commands as narratives_completed/attempted.
  const note = points
    ? `<div class="pros-cons">${points}</div>`
    : '<p class="section-note">目前沒有可列出的優勢或劣勢。</p>';
  return thesis + note;
}

function marketCounts(rows) {
  const counts = {ALL: rows.length};
  rows.forEach((company) => {
    const key = company.market || 'UNKNOWN';
    counts[key] = (counts[key] || 0) + 1;
  });
  return counts;
}

function renderMarketFilters() {
  const counts = marketCounts(companyRows);
  byId('companyFilters').innerHTML = MARKET_FILTERS
    .filter((filter) => filter.key === 'ALL' || counts[filter.key])
    .map((filter) => `<button type="button" data-market="${filter.key}"${filter.key === activeMarket ? ' class="active"' : ''}>${filter.label} ${counts[filter.key] || 0}</button>`)
    .join('');
  byId('companyFilters').querySelectorAll('button').forEach((button) =>
    button.addEventListener('click', () => {
      activeMarket = button.dataset.market;
      renderMarketFilters();
      companies();
    }));
}

function companies() {
  const target = byId('companyTable');
  const rows = activeMarket === 'ALL'
    ? companyRows
    : companyRows.filter((company) => (company.market || 'UNKNOWN') === activeMarket);
  if (!rows.length) {
    target.innerHTML = companyRows.length
      ? '<article class="card"><h3>這個市場還沒有公司</h3><p>換一個分類，或先同步台股證券主檔讓代號能被解析。</p></article>'
      : '<article class="card"><h3>尚無公司觀點資料</h3><p>請先執行一次同步。</p></article>';
    return;
  }
  target.innerHTML = `<div class="market-table">
    <div class="row head"><span>代號</span><span>公司</span><span>提及</span><span>立場／重要度</span><span>論點與優劣勢</span><span>原文</span><span>分段</span></div>
    ${rows.map((company) => `
    <div class="row" data-symbol="${esc(company.symbol)}">
      <span class="symbol">${esc(company.symbol)}<small class="market">${esc(company.market || 'UNKNOWN')}</small></span>
      <strong>${esc(company.company_name || company.symbol)}</strong>
      <span>${company.mention_count} 集</span>
      <span><b class="stance ${esc(company.stance)}">${stanceName(company.stance)}</b><small title="提及此公司的片段平均重要度，不是對立場的把握程度">重要度 ${Math.round(Number(company.confidence ?? 0.5) * 100)}%</small></span>
      <span class="market-signal">${companyThesis(company)}</span>
      <span>${(company.evidence_urls || []).slice(0, 3).map((url, i) => `<a href="${esc(url)}" target="_blank" rel="noreferrer">${i + 1}</a>`).join(' ') || '—'}</span>
      <span><button type="button" class="segments-toggle" data-symbol="${esc(company.symbol)}">分段</button></span>
    </div>
    <div class="segments-panel" data-symbol-panel="${esc(company.symbol)}" hidden></div>`).join('')}
  </div>`;

  target.querySelectorAll('.segments-toggle').forEach((button) => button.addEventListener('click', async () => {
    const symbol = button.dataset.symbol;
    const panel = target.querySelector(`[data-symbol-panel="${CSS.escape(symbol)}"]`);
    if (!panel.hidden) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    if (panel.dataset.loaded) return;
    panel.innerHTML = '<p class="section-note">載入中…</p>';
    try {
      const data = await fetch(`/api/companies/${encodeURIComponent(symbol)}/segments`).then((r) => {
        if (!r.ok) throw new Error('segments unavailable');
        return r.json();
      });
      const segments = data.segments || [];
      panel.innerHTML = segments.length
        ? segments.map(segmentItem).join('')
        : '<p class="section-note">尚無分段資料</p>';
      panel.dataset.loaded = '1';
    } catch (e) {
      panel.innerHTML = '<p class="section-note">分段資料載入失敗</p>';
    }
  }));
}

let episodesPageNumber = 1;
async function loadEpisodes(page = 1) {
  const data = await fetch(`/api/episodes?page=${page}&page_size=12`).then((r) => {
    if (!r.ok) throw new Error('episodes unavailable');
    return r.json();
  });
  episodesPageNumber = data.page;
  cards(byId('episodesGrid'), data.items || [], '尚未同步任何集數');
  byId('episodesPagination').innerHTML = `
    <button type="button" data-page="${data.page - 1}" ${data.has_previous ? '' : 'disabled'}>上一頁</button>
    <span>第 ${data.page} / ${data.pages} 頁 · 共 ${data.total} 集</span>
    <button type="button" data-page="${data.page + 1}" ${data.has_next ? '' : 'disabled'}>下一頁</button>`;
  byId('episodesPagination').querySelectorAll('button:not(:disabled)').forEach((button) =>
    button.addEventListener('click', () => {
      loadEpisodes(Number(button.dataset.page));
      byId('episodes').scrollIntoView({behavior: 'smooth'});
    }));
}

async function load() {
  try {
    const [data, health] = await Promise.all([
      fetch('/api/dashboard').then((r) => r.json()),
      fetch('/api/health').then((r) => r.json()),
    ]);
    const ready = health.postgres && health.llm;
    document.querySelector('.status').classList.toggle('ok', ready);
    byId('statusText').textContent = ready ? `服務正常 · ${health.llm_provider}` : '部分服務未連線';
    byId('totalEpisodes').textContent = `共收錄 ${data.total_episodes || 0} 集`;
    sentiment(data.sentiment_summary || {});
    cards(byId('latestGrid'), data.latest || [], '尚未同步任何集數，請先執行 scripts/sync_gooaye.py');
    companyRows = data.companies || [];
    renderMarketFilters();
    companies();
    await loadEpisodes();
  } catch (e) {
    byId('statusText').textContent = 'API 連線失敗';
  }
}
load();
