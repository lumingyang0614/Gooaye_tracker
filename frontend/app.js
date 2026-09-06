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
      <span class="importance">信心 ${Math.round(Number(episode.importance || 0.5) * 100)}%</span>
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

function companies(rows) {
  const target = byId('companyTable');
  if (!rows.length) {
    target.innerHTML = '<article class="card"><h3>尚無公司觀點資料</h3><p>請先執行一次同步。</p></article>';
    return;
  }
  target.innerHTML = `<div class="market-table">
    <div class="row head"><span>代號</span><span>公司</span><span>提及</span><span>立場／信心</span><span>主要觀點</span><span>原文</span></div>
    ${rows.map((company) => `<div class="row">
      <span class="symbol">${esc(company.symbol)}</span>
      <strong>${esc(company.company_name || company.symbol)}</strong>
      <span>${company.mention_count} 次</span>
      <span><b class="stance ${esc(company.stance)}">${stanceName(company.stance)}</b><small>${Math.round(Number(company.confidence || 0.5) * 100)}%</small></span>
      <span class="market-signal">${esc(company.thesis || '等待分析')}</span>
      <span>${(company.evidence_urls || []).slice(0, 3).map((url, i) => `<a href="${esc(url)}" target="_blank" rel="noreferrer">${i + 1}</a>`).join(' ') || '—'}</span>
    </div>`).join('')}
  </div>`;
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
    companies(data.companies || []);
    await loadEpisodes();
  } catch (e) {
    byId('statusText').textContent = 'API 連線失敗';
  }
}
load();
