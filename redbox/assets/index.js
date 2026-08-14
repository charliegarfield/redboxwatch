
const q=document.getElementById('q'),fs=document.getElementById('f-status'),
  fst=document.getElementById('f-state'),tb=document.getElementById('rows'),
  pager=document.querySelector('.pager'),pageRows=[...tb.querySelectorAll('tr')];
let allRows=null,fetchStarted=false,shown=pageRows;
function loadAll(){
  // Full row set (every page) fetched once, on first filter use. If the fetch
  // fails (e.g. file:// preview), filtering quietly stays page-local.
  // ?v= is the build's content hash of the JSON (same cache-busting scheme as
  // styles.css): Pages caches assets for 4h, and a stale row set can show a
  // since-rejected candidate as a FINDING linking to a now-404 page.
  if(fetchStarted||!PAGED)return;fetchStarted=true;
  fetch('index-data.json?v='+DATA_V).then(r=>r.ok?r.json():Promise.reject())
    .then(d=>{const t=document.createElement('template');t.innerHTML=d.html;
      allRows=[...t.content.querySelectorAll('tr')];apply();})
    .catch(()=>{});}
function apply(){const t=q.value.trim().toLowerCase(),s=fs.value,st=fst.value,
    active=!!(t||s||st);
  if(active)loadAll();
  const src=active&&allRows?allRows:pageRows;
  if(shown!==src){shown=src;tb.replaceChildren(...src);}
  shown.forEach(r=>{const name=r.children[0].textContent.toLowerCase(),
    fec=(r.dataset.name||'').toLowerCase();
    const ok=(!t||name.includes(t)||fec.includes(t))&&(!s||r.dataset.status===s)&&(!st||r.dataset.state===st);
    r.style.display=ok?'':'none';});
  if(pager)pager.style.display=active&&allRows?'none':'';}
[q,fs,fst].forEach(e=>e.addEventListener('input',apply));
document.querySelectorAll('th[data-sort]').forEach(th=>th.addEventListener('click',()=>{
  const i=+th.dataset.sort,tb=document.getElementById('rows');
  const sorted=[...tb.querySelectorAll('tr')].sort((a,b)=>{
    // Candidate cells print the FEC "LAST, FIRST" form, so plain text
    // comparison already sorts by surname.
    const x=a.children[i].textContent.trim(),y=b.children[i].textContent.trim();
    const nx=parseFloat(x.replace(/[$,]/g,'')),ny=parseFloat(y.replace(/[$,]/g,''));
    if(!isNaN(nx)||!isNaN(ny)){return(isNaN(ny)?-Infinity:ny)-(isNaN(nx)?-Infinity:nx);}
    return x.localeCompare(y);});
  sorted.forEach(r=>tb.appendChild(r));}));
document.querySelectorAll('.st.has-pop').forEach(el=>el.addEventListener('click',e=>{
  if(e.target.closest('a'))return;
  fst.value=el.dataset.state;apply();
  document.querySelector('.agate').scrollIntoView({behavior:'smooth'});}));
