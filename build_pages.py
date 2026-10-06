"""Build the GitHub Pages (static) edition of the LFFD dashboard into docs/.

The FastAPI backend is not available on GitHub Pages, so this build:
  - copies static/index.html to docs/index.html
  - injects a small bootstrap that sets window.API_BASE and enables DEMO mode
  - ships a docs/demo.json with representative (fabricated) data

DEMO mode intercepts the api() layer and serves sample data, so the UI is fully
browsable on the road. Point window.API_BASE at a real tunnel later and DEMO
can be turned off by removing the injected <script> block.

Run:  .venv/bin/python build_pages.py
"""
from __future__ import annotations

import json
import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "static", "index.html")
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
    else if(path.startsWith('/trends')) body = d.trends;
    else if(path.startsWith('/savings')) body = d.savings || {accounts:[], totals:{}};
    else if(path.startsWith('/transactions')) body = d.transactions;
    else if(path.startsWith('/rules')) body = d.rules;
    else if(path.startsWith('/imports')) body = d.imports;
    else if(path.startsWith('/crypto')) body = demoCrypto(path, qs, d);
    else body = {};
    return new Response(JSON.stringify(body), {status:200, headers:{'Content-Type':'application/json'}});
  };
  // Read-only demo: block writes with a friendly notice.
  const origPost = window.fetch;
  window.fetch = async function(url, opts){
    if(opts && opts.method && opts.method !== 'GET'){
      if(String(url).startsWith('/api')){
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
