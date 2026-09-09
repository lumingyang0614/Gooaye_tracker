// Real headless Chrome, actual frontend assets, deterministic local API fixtures.
// Run: node tests/browser_te.cjs
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const out = path.join(root, 'test_output/te_20260908_resume');
fs.mkdirSync(out, {recursive:true});
let mode = 'normal', requests = [], segmentFailures = 0;
const ep = {episode_number:700,title:'測試集數',sentiment:'bullish',importance:0,
  summary:'摘要',transcript_source:'whisper',transcript_text:'逐字稿測試全文',
  comparison_notes:'比對測試說明',tickers:['2330.TW'],source_url:'https://example.com/transcript'};
const company = {symbol:'2330.TW',company_name:'台積電',mention_count:1,stance:'bearish',confidence:0,
  thesis:'測試觀點',evidence_urls:['https://example.com/transcript']};
const segment = {episode_number:700,chunk_index:0,sentiment:'bearish',importance:0,
  summary:'<img src=x onerror="window.injected=true">',reason:'分段理由',source_url:'https://example.com/transcript'};
const server = http.createServer((req,res)=>{
  const u=new URL(req.url,'http://localhost'); requests.push(u.pathname+u.search);
  if(u.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type','application/json');
    if(mode==='offline' && u.pathname!=='/api/health') {res.statusCode=503;res.end(JSON.stringify({detail:'PostgreSQL unavailable'}));return;}
    let data;
    if(u.pathname==='/api/health') data={postgres:mode!=='offline',llm:true,llm_provider:'fixture'};
    else if(u.pathname==='/api/dashboard') data={total_episodes:mode==='empty'?0:13,sentiment_summary:{bullish:1},latest:mode==='empty'?[]:[ep],companies:mode==='empty'?[]:[company]};
    else if(u.pathname==='/api/episodes') {const p=Number(u.searchParams.get('page')||1);data={page:p,pages:mode==='empty'?0:2,total:mode==='empty'?0:13,has_previous:p>1,has_next:mode!=='empty'&&p<2,items:mode==='empty'?[]:[{...ep,episode_number:p===1?700:699}]};}
    else if(u.pathname.endsWith('/segments')) {
      if(segmentFailures>0){segmentFailures--;res.statusCode=503;data={detail:'offline'};}
      else data={symbol:'2330.TW',segments:mode==='emptySegments'?[]:[segment]};
    } else {res.statusCode=404;data={detail:'unknown'};}
    res.end(JSON.stringify(data));return;
  }
  const assets={'/':'index.html','/static/app.js':'app.js','/static/styles.css':'styles.css'};
  if(!assets[u.pathname]){res.statusCode=404;res.end();return;}
  res.setHeader('Content-Type',u.pathname.endsWith('.js')?'text/javascript; charset=utf-8':u.pathname.endsWith('.css')?'text/css':'text/html; charset=utf-8');
  res.end(fs.readFileSync(path.join(root,'frontend',assets[u.pathname])));
});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let chrome, ws, seq=0; const pending=new Map(), results=[], errors=[];
function call(method,params={}){return new Promise((resolve,reject)=>{const id=++seq;const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout '+method));},10000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression){const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;}
async function until(expression){for(let i=0;i<100;i++){if(await evaluate(expression))return;await sleep(50);}throw Error('Condition timed out: '+expression);}
async function check(name,expression){const actual=await evaluate(expression);results.push({name,passed:actual===true,actual});}
async function click(selector){await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);}
async function navigate(){await call('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/`});await until(`document.querySelector('#statusText') && !['載入中'].includes(document.querySelector('#statusText').textContent)`);await until(`document.querySelector('#episodesPagination').textContent.trim() !== '' || document.querySelector('#statusText').textContent === 'API 連線失敗'`);}
async function screenshot(name){const r=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,name+'.png'),Buffer.from(r.data,'base64'));}
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const profile=fs.mkdtempSync(path.join(out,'chrome-profile-'));
  chrome=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
    '--remote-debugging-port=19333',`--user-data-dir=${profile}`,'about:blank'],{windowsHide:true,stdio:'ignore'});
  chrome.on('error',e=>errors.push(String(e)));
  let targets;
  for(let i=0;i<100;i++){try{targets=await fetch('http://127.0.0.1:19333/json/list').then(r=>r.json());if(targets.length)break;}catch{}await sleep(100);}
  if(!targets)throw Error('Chrome debugging endpoint unavailable');
  ws=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
  ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(Error(JSON.stringify(m.error))):p.resolve(m.result);}if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails.text);};
  await call('Runtime.enable');await call('Page.enable');
  await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await navigate();
  await check('latest tab and zero importance',`document.querySelector('#latest').classList.contains('active') && document.querySelector('#latestGrid .importance').textContent.includes('0%')`);
  results.push({name:'segments lazy loading',passed:!requests.some(p=>p.endsWith('/segments'))});
  await click('[data-tab="companies"]');await check('company tab and zero importance',`document.querySelector('#companies').classList.contains('active') && document.querySelector('.row small:not(.market)').textContent.trim()==='重要度 0%'`);
  await check('market filter and market label',`document.querySelector('#companyFilters button.active').textContent.startsWith('全部') && document.querySelector('.row .symbol small.market') !== null`);
  await click('.segments-toggle');await until(`document.querySelector('.segment-item') !== null`);
  await check('segment fields, zero and escaped markup',`document.querySelector('.segment-meta').textContent.includes('第 1 段') && document.querySelector('.segment-item').textContent.includes('0%') && document.querySelector('.segment-item').textContent.includes('分段理由') && !document.querySelector('.segment-item img') && !window.injected`);
  await check('source link',`document.querySelector('.segment-item .source').href==='https://example.com/transcript'`);
  await screenshot('companies_desktop');
  const fetched=requests.filter(p=>p.endsWith('/segments')).length;
  await click('.segments-toggle');await check('collapse segment panel',`document.querySelector('.segments-panel').hidden`);
  await click('.segments-toggle');results.push({name:'reopen uses cached segments',passed:requests.filter(p=>p.endsWith('/segments')).length===fetched});
  await click('[data-tab="episodes"]');await click('#episodesGrid details summary');await check('transcript expanded',`document.querySelector('#episodesGrid details').open && document.querySelector('#episodesGrid .original-text').textContent.includes('逐字稿測試全文')`);
  await click('#episodesGrid details summary');await check('transcript collapsed',`!document.querySelector('#episodesGrid details').open`);
  await click('#episodesPagination button[data-page="2"]');await until(`document.querySelector('#episodesPagination').textContent.includes('第 2 / 2 頁')`);await check('pagination next',`document.querySelector('#episodesGrid h3').textContent.includes('699') && document.querySelector('#episodesPagination button:last-child').disabled`);
  await click('#episodesPagination button[data-page="1"]');await until(`document.querySelector('#episodesPagination').textContent.includes('第 1 / 2 頁')`);
  await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await click('[data-tab="companies"]');
  await check('mobile page has no document overflow',`document.documentElement.scrollWidth<=window.innerWidth`);await screenshot('companies_mobile');
  segmentFailures=1;await navigate();await click('[data-tab="companies"]');await click('.segments-toggle');await until(`document.querySelector('.segments-panel').textContent.includes('載入失敗')`);
  await check('segment failure shown',`!document.querySelector('.segments-panel').dataset.loaded`);await click('.segments-toggle');await click('.segments-toggle');await until(`document.querySelector('.segment-item') !== null`);await check('segment retry succeeds',`document.querySelector('.segments-panel').dataset.loaded==='1'`);
  mode='emptySegments';await navigate();await click('[data-tab="companies"]');await click('.segments-toggle');await until(`document.querySelector('.segments-panel').textContent.includes('尚無分段資料')`);await check('empty segments message',`document.querySelector('.segments-panel').textContent.includes('尚無分段資料')`);
  mode='empty';await navigate();await check('empty latest, companies and archive messages',`document.querySelector('#latestGrid').textContent.includes('尚未同步') && document.querySelector('#companyTable').textContent.includes('尚無公司觀點') && document.querySelector('#episodesGrid').textContent.includes('尚未同步')`);
  mode='offline';await navigate();await screenshot('database_offline');await check('offline does not present empty data as successful load',`document.querySelector('#statusText').textContent==='API 連線失敗'`);
})().catch(e=>{errors.push(String(e));process.exitCode=1;}).finally(async()=>{
  fs.writeFileSync(path.join(out,'browser_results.json'),JSON.stringify({environment:'Headless Chrome; actual frontend; local fixture HTTP API',results,errors,requests},null,2));
  console.log(JSON.stringify({checks:results.length,passed:results.filter(r=>r.passed).length,failed:results.filter(r=>!r.passed),errors},null,2));
  if(ws){try{await call('Browser.close');}catch{}ws.close();}if(chrome)chrome.kill();server.close();
  if(results.some(r=>!r.passed)||errors.length)process.exitCode=1;
});
