"""Build the GitHub Pages (static) edition of the LFFD dashboard into docs/.

The FastAPI backend is not available on GitHub Pages, so this build:
  - copies prototype/index.html to docs/index.html
  - injects a small bootstrap that sets window.API_BASE and enables DEMO mode
  - ships a docs/demo.json with representative (fabricated) data

The live app served by the FastAPI backend (static/index.html) must NEVER
contain the demo bootstrap — only docs/ is the public read-only demo.

Run:  .venv/bin/python build_pages.py
"""
from __future__ import annotations

import json
import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
# Source of truth is the working prototype. static/ (the live app) is published
# from prototype/ separately and must stay demo-free.
SRC = os.path.join(HERE, "prototype", "index.html")
OUTDIR = os.path.join(HERE, "docs")
DEMO_JSON = os.path.join(HERE, "docs", "demo.json")

BOOTSTRAP = """
<!-- ===== GitHub Pages demo bootstrap (injected by build_pages.py) ===== -->
<script>
window.API_BASE = '';
window.DEMO = true;
(function(){
  if(!window.DEMO) return;
  let DATA = null;
  async function demoData(){ if(!DATA){ DATA = await (await fetch('demo.json')).json(); } return DATA; }
  // Crypto is live-data (CoinGecko/RSS). In demo mode we serve a small static
  // snapshot from demo.json so the tab is browsable offline.
  function demoCrypto(path, qs, d){
    const cx = d.crypto || {markets:[], favorites:[], holdings:[], news:[]};
    if(path.startsWith('/crypto/search')){
      const q = (qs.get('q') || '').toLowerCase();
      const res = (cx.markets || []).filter(c =>
        c.name.toLowerCase().includes(q) || c.symbol.toLowerCase().includes(q));
      return {results: res.slice(0,10).map(c=>({id:c.id,name:c.name,symbol:c.symbol,rank:c.rank,thumb:c.image}))};
    }
    if(path.startsWith('/crypto/markets')) return {coins: cx.markets || [], vs:'usd'};
    if(path.startsWith('/crypto/price')){
      const id = qs.get('id') || 'bitcoin';
      const mk = (cx.markets || []).find(c => c.id === id) || (cx.markets||[])[0] || {};
      return {id, vs:'usd', price: mk.price || 83475, chg_24h: mk.chg_24h || 0};
    }
    if(path.startsWith('/crypto/chart')){
      const id = qs.get('id') || 'bitcoin';
      const series = (cx.charts && cx.charts[id]) || [];
      return {id, days: qs.get('days')||'7', vs:'usd', prices: series};
    }
    if(path.startsWith('/crypto/ohlc')){
      const id = qs.get('id') || 'bitcoin';
      const series = (cx.charts && cx.charts[id]) || [];
      // synthesize candles from the price series for the offline demo
      const candles = series.map((p,i)=>{
        const o = i? series[i-1][1] : p[1];
        const c = p[1];
        const h = Math.max(o,c) * 1.004, l = Math.min(o,c) * 0.996;
        return [p[0], o, h, l, c];
      });
      return {id, days: qs.get('days')||'7', vs:'usd', candles};
    }
    if(path.startsWith('/crypto/news')){
      const cat = qs.get('category') || 'crypto';
      const items = (cx.news || []).filter(i => !i.category || i.category === cat);
      return {category: cat, items: items.length ? items : (cx.news || [])};
    }
    if(path.startsWith('/crypto/favorites')) return cx.favorites || [];
    if(path.startsWith('/crypto/holdings')) return cx.holdings || [];
    return {};
  }
  // HVAC payoff planner: reproduce the backend amortization so the demo planner
  // stays fully interactive (all figures fabricated).
  function amz(balance, payment, apr, extra){
    const rate = (apr||0)/100/12;
    const pay = (payment||0) + (extra||0);
    if(pay <= 0 || balance <= 0) return null;
    let b = balance, m = 0, interest = 0;
    while(b > 0.005 && m < 600){
      const i = b * rate;
      let p = pay - i;
      if(p <= 0) return null;
      if(p > b) p = b;
      interest += i; b = b - p; m++;
    }
    return {months:m, total_interest: Math.round(interest*100)/100};
  }
  function addMonths(iso, n){
    const p = (iso||'2026-10').split('-');
    const y = +p[0], mo = +p[1];
    const em = mo - 1 + n;
    return String(y + Math.floor(em/12)).padStart(4,'0')+'-'+String(em%12+1).padStart(2,'0');
  }
  function demoHvac(path, qs, d){
    if(path.includes('?')) path = path.split('?')[0];
    const hv = d.hvac || {loans:[], totals:{owed:0,monthly:0,count:0,with_balance:0}};
    if(path === '/hvac') return hv;
    if(path === '/hvac/combined/plan'){
      const extra = parseFloat(qs.get('extra')) || 0;
      const per = {};
      (qs.get('extras')||'').split(',').forEach(s=>{ const a=s.split(':'); if(a.length===2) per[+a[0]]=parseFloat(a[1])||0; });
      const active = (hv.loans||[]).filter(l=>(l.balance||0)>0 && (l.payment||0)>0);
      const total_payment = Math.round(active.reduce((a,l)=>a+(l.payment||0),0)*100)/100;
      const total_extra = Math.round(active.reduce((a,l)=>a+((l.id in per)?per[l.id]:extra),0)*100)/100;
      const proj = (we)=>{ let months=0, interest=0; const pl=[];
        for(const l of active){ const ex=we?((l.id in per)?per[l.id]:extra):0; const r=amz(l.balance,l.payment,l.apr,ex); if(!r) return null;
          months=Math.max(months,r.months); interest+=r.total_interest;
          pl.push({id:l.id,name:l.name,months:r.months,balance:l.balance,payment:l.payment,extra:Math.round(ex*100)/100,monthly:Math.round(((l.payment||0)+ex)*100)/100,total_interest:r.total_interest}); }
        return {months,total_interest:Math.round(interest*100)/100,per_loan:pl}; };
      const base=proj(false), wx=proj(true);
      if(!base) return {active:false,loans:[],total_balance:0,total_payment};
      return {active:true,loans:active,extra,total_balance:hv.totals.owed,total_payment,
        total_extra,total_payment_with_extra:Math.round((total_payment+total_extra)*100)/100,
        baseline:base,with_extra:wx,baseline_months:base.months,with_extra_months:wx.months,
        months_saved:base.months-wx.months,interest_saved:Math.round((base.total_interest-wx.total_interest)*100)/100,
        payoff_date_baseline:addMonths('2026-10',base.months),payoff_date_with_extra:addMonths('2026-10',wx.months)};
    }
    const mLoan = path.match(/^\/hvac\/(\d+)\/plan$/);
    if(mLoan){
      const l = (hv.loans||[]).find(x=>x.id===+mLoan[1]);
      if(!l) return {active:false};
      const extra = parseFloat(qs.get('extra')) || 0;
      const base = amz(l.balance,l.payment,l.apr,0), wx = amz(l.balance,l.payment,l.apr,extra);
      if(!base) return {active:false,loan:l};
      const out = {active:true,loan:l,extra,baseline:base,with_extra:wx,
        baseline_months:base.months,with_extra_months:wx.months,
        months_saved:base.months-wx.months,interest_saved:Math.round((base.total_interest-wx.total_interest)*100)/100,
        payoff_date_baseline:addMonths('2026-10',base.months),payoff_date_with_extra:addMonths('2026-10',wx.months)};
      if(l.deadline){ out.deadline=l.deadline; out.on_time = base.months<=0?false:(wx.months<=0?true:true); }
      return out;
    }
    return hv;
  }
  const origFetch = window.fetch;
  window.fetch = async function(url, opts){
    const u = String(url);
    if(!u.startsWith('/api')) return origFetch.apply(this, arguments);
    const d = await demoData();
    const path = u.slice(4); // strip '/api'
    const qs = path.includes('?') ? new URLSearchParams(path.split('?')[1]) : new URLSearchParams();
    let body;
    if(path.startsWith('/dashboard')) body = d.dashboard;
    else if(path.startsWith('/categories')) body = d.categories;
    else if(path.startsWith('/accounts')) body = d.accounts;
    else if(path.startsWith('/budgets')) body = d.budgets;
    else if(path.startsWith('/trends')) body = (function(t, qs){
      // demo mode: honor the category filter client-side so the prototype behaves
      const keys = (qs.get('categories')||'').split(',').map(s=>s.trim()).filter(Boolean);
      if(!keys.length || !t || !t.categories) return t;
      const cats = t.categories.filter(c=>keys.indexOf(c.key)>=0);
      const recent = (t.months||[]).slice(-3);
      let exp = 0; recent.forEach(m=>cats.forEach(c=>{ exp += (c.series&&c.series[m])||0; }));
      const inc = t.avg_3mo ? t.avg_3mo.income : 0;
      const avg3 = { income: inc, expense: Math.round(exp*100)/100, net: Math.round((inc-exp)*100)/100 };
      let expAll = 0; (t.months||[]).forEach(m=>cats.forEach(c=>{ expAll += (c.series&&c.series[m])||0; }));
      const incAll = t.avg_all ? t.avg_all.income : inc;
      const avgAll = { income: incAll, expense: Math.round(expAll*100)/100, net: Math.round((incAll-expAll)*100)/100, months: (t.months||[]).length };
      return Object.assign({}, t, { categories: cats, avg_3mo: avg3, avg_all: avgAll });
    })(d.trends, qs);
    else if(path.startsWith('/savings/history')) body = d.savingsHistory || {months:[], current:null, current_month:null};
    else if(path.startsWith('/paydown')) body = d.paydown || {cards:[], totals:{}};
    else if(path.startsWith('/savings')) body = d.savings || {accounts:[], totals:{}};
    else if(path.startsWith('/bills')) body = d.bills || {month:'', rows:[], counts:{}};
    else if(path.startsWith('/transactions')) body = (function(t, qs){
      t = t || {total:0, items:[]};
      const all = t.items || [];
      const lim = parseInt(qs.get('limit'), 10);
      const off = parseInt(qs.get('offset'), 10) || 0;
      let items = all;
      if(!isNaN(lim)) items = all.slice(off, off + lim);
      else if(off) items = all.slice(off);
      const total = (t.total != null) ? t.total : all.length;
      const sum = (t.sum != null) ? t.sum : all.reduce((a,x)=>a+(x.amount||0),0);
      return Object.assign({}, t, {items: items, total: total, sum: sum, count: items.length});
    })(d.transactions, qs);
    else if(path.startsWith('/rules')) body = d.rules;
    else if(path.startsWith('/imports')) body = d.imports;
    else if(path.startsWith('/economy/news')) body = (function(e, qs){
      e = e || {economy:[], bonds:[]};
      const cat = qs.get('category') || 'economy';
      let items = cat === 'all' ? [].concat(e.economy||[], e.bonds||[]) : (e[cat] || e.economy || []);
      return {category:cat, items:items};
    })(d.economyNews, qs);
    else if(path.startsWith('/economy/markets')) body = d.economyMarkets || {rates:{}};
    else if(path.startsWith('/ai-costs')) body = (function(t, qs){
      t = t || {total:0,count:0,daily:[],monthly:[],by_model:[],by_wallet:[],models:[]};
      const model = qs.get('model');
      if(!model || !t.daily) return t;
      // demo mode: filter the fabricated series to one model client-side
      const src = (t.daily_by_model || t.daily);   // rows with model info when available
      const daily = src.filter(r=>!r.model || r.model === model);
      const by_day = {};
      daily.forEach(r=>{ by_day[r.date] = (by_day[r.date]||0) + (r.cost||0); });
      const by_month = {};
      Object.keys(by_day).forEach(d=>{ const k=d.slice(0,7); by_month[k]=(by_month[k]||0)+by_day[d]; });
      const total = Object.values(by_day).reduce((a,b)=>a+b,0);
      const r6 = n => Math.round(n*1e6)/1e6;
      return Object.assign({}, t, {
        total: r6(total),
        count: Object.keys(by_day).length,
        daily: Object.keys(by_day).sort().map(d=>({date:d,cost:r6(by_day[d]),n:1})),
        monthly: Object.keys(by_month).sort().map(m=>({month:m,cost:r6(by_month[m]),n:1})),
        by_model: [{model:model, cost:r6(total), n:Object.keys(by_day).length}],
        by_wallet: [{wallet_type:'Paid', cost:r6(total), n:Object.keys(by_day).length}],
      });
    })(d.aiCosts, qs);
    else if(path.startsWith('/hvac')) body = demoHvac(path, qs, d);
    else if(path.startsWith('/crypto')) body = demoCrypto(path, qs, d);
    else body = {};
    return new Response(JSON.stringify(body), {status:200, headers:{'Content-Type':'application/json'}});
  };
  // Read-only demo: block writes with a friendly notice.
  const origPost = window.fetch;
  window.fetch = async function(url, opts){
    if(opts && opts.method && opts.method !== 'GET'){
      if(String(url).startsWith('/api')){
        if(String(url).indexOf('/chat') >= 0){
          return new Response(JSON.stringify({reply:
            "I'm only wired to the live app — this public demo is a read-only snapshot, "+
            "so there's no agent behind the curtain here. Open the real one and ask me again 🐾"}),
            {status:200, headers:{'Content-Type':'application/json'}});
        }
        if(window.toast) toast('Demo mode — changes are not saved');
        return new Response(JSON.stringify({ok:true}), {status:200, headers:{'Content-Type':'application/json'}});
      }
    }
    return origPost.apply(this, arguments);
  };
})();
</script>
<!-- ===== end demo bootstrap ===== -->
"""


def build():
    os.makedirs(OUTDIR, exist_ok=True)
    with open(SRC, encoding="utf-8") as f:
        html = f.read()
    # inject bootstrap right before </head> (falls back to top of file)
    if "</head>" in html:
        html = html.replace("</head>", BOOTSTRAP + "</head>", 1)
    else:
        html = BOOTSTRAP + html
    # Scrub real-world identifiers so the PUBLIC demo shows only synthetic names.
    # (Keeps the live app in static/ untouched.)
    SCRUB = [
        ("Wells Fargo", "SynthCard Issuer"),
        ("Capital One", "SynthCard"),
        ("M&amp;T Bank", "SynthBank"),
        ("M&amp;T", "SynthBank"),
        ("M&T", "SynthBank"),
        ("Citizens", "SynthBank"),
        ("Chase", "SynthBank"),
        ("Gemini", "SynthCard"),
        ("$430.99 accrued", "a growing accrued balance"),
    ]
    for a, b in SCRUB:
        html = html.replace(a, b)
    with open(os.path.join(OUTDIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    # demo data
    if os.path.exists(DEMO_JSON):
        print(f"docs/index.html written ({len(html)} bytes); demo.json present")
    else:
        print(f"docs/index.html written ({len(html)} bytes); WARNING: demo.json missing")
    # Pages needs a .nojekyll so files starting with _ aren't stripped
    open(os.path.join(OUTDIR, ".nojekyll"), "w").close()
    # copy static assets referenced by the page (favicon)
    for asset in ("favicon.svg", "favicon.ico"):
        src_asset = os.path.join(HERE, "static", asset)
        if os.path.exists(src_asset):
            shutil.copy2(src_asset, os.path.join(OUTDIR, asset))


if __name__ == "__main__":
    build()
