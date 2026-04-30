/**
 * bmap-deck.js — BMAP Snapshot Deck Builder
 * Critical fixes: String() conversion on all zone tiles, improved chart rendering
 */

const DC = {
  navy:   '1A2332', teal:   '1D9E75', amber:  'F5A623',
  white:  'FFFFFF', offwh:  'F5F5F2', gray1:  'F5F5F2',
  gray2:  'E8E8E5', gray3:  '778899', gray4:  '778899',
  invest: '27500A', investL:'EAF3DE',
  analyze:'185FA5', analyzeL:'E6F1FB',
  defend: '854F0B', defendL:'FFF3E0',
  justify:'A32D2D', justifyL:'FCEBEB',
};
const ZONE_C  = {Invest:DC.invest,  Analyze:DC.analyze,  Defend:DC.defend,  Justify:DC.justify};
const ZONE_L  = {Invest:DC.investL, Analyze:DC.analyzeL, Defend:DC.defendL, Justify:DC.justifyL};

const VLOGO = 'https://raw.githubusercontent.com/fggraufaro/bmap-tools/main/Verlocity-Logo.png';
const mkSh = () => ({type:'outer',blur:6,offset:2,angle:135,color:'000000',opacity:0.10});

function vChart(type, labels, datasets, opts={}) {
  return new Promise((resolve, reject) => {
    const c = document.createElement('canvas');
    c.width  = opts.w || 1000;
    c.height = opts.h || 500;
    c.style.cssText = 'position:fixed;left:-9999px;top:-9999px;';
    document.body.appendChild(c);
    
    const ctx = c.getContext('2d');
    
    const chartOpts = {
      animation: { duration: 0 },
      responsive: false,
      maintainAspectRatio: false,
      layout: { padding: { top:20, bottom:opts.pb||30, left:20, right:20 } },
      plugins: {
        legend: {
          display: opts.legend !== false,
          position: opts.lp || 'bottom',
          labels: { 
            font:{size:16, family:'Calibri', weight:'bold'}, 
            color:'#1A2332',
            boxWidth:28,
            boxHeight:18,
            padding:20,
            usePointStyle: false
          }
        },
        title: { display: false },
        tooltip: {
          enabled: opts.tooltip !== false,
          backgroundColor: '#1A2332',
          titleFont: { size: 14, weight: 'bold' },
          bodyFont: { size: 13 },
          padding: 14,
          borderRadius: 8,
          displayColors: true,
          callbacks: opts.tooltipCallback || {}
        }
      },
      scales: (type==='doughnut'||type==='pie') ? {} : {
        x: { 
          ticks:{color:'#1A2332', font:{size:14, weight:'600', family:'Calibri'}}, 
          grid:{display:false}, 
          border:{display:false} 
        },
        y: { 
          ticks:{color:'#778899', font:{size:13, family:'Calibri'}}, 
          grid:{color:'#E8E8E5', lineWidth:1.2}, 
          border:{display:false},
          beginAtZero: true
        }
      }
    };

    const ch = new Chart(ctx, {
      type,
      data: { labels, datasets },
      options: chartOpts
    });

    let attempts = 0;
    const tryCapture = () => {
      attempts++;
      if (attempts > 6) {
        try {
          const b64 = c.toDataURL('image/png').split(',')[1];
          if (!b64 || b64.length < 100) {
            throw new Error('Chart canvas empty');
          }
          ch.destroy();
          c.remove();
          resolve('image/png;base64,' + b64);
        } catch(e) {
          c.remove();
          reject(new Error('Chart rendering failed: ' + e.message));
        }
      } else {
        requestAnimationFrame(tryCapture);
      }
    };
    
    requestAnimationFrame(() => setTimeout(tryCapture, 200));
  });
}

function addChrome(slide, pageNum, label) {
  slide.addShape('rect', {x:0,y:0,w:0.28,h:5.625, fill:{color:DC.navy}, line:{color:DC.navy}});
  slide.addShape('rect', {x:0.28,y:0,w:0.08,h:5.625, fill:{color:DC.teal}, line:{color:DC.teal}});
  slide.addImage({path:VLOGO, x:0.42,y:5.04,w:1.55,h:0.36});
  if (label) {
    slide.addShape('roundRect',{x:8.16,y:0.14,w:1.76,h:0.3,
      fill:{color:DC.teal},line:{color:DC.teal},rectRadius:0.04});
    slide.addText(label,{x:8.16,y:0.14,w:1.76,h:0.3,
      fontSize:7.5,fontFace:'Calibri',bold:true,color:DC.white,
      align:'center',valign:'middle',charSpacing:1.2,margin:0});
  }
  if (pageNum !== undefined) {
    slide.addText(String(pageNum),{x:9.5,y:5.28,w:0.38,h:0.2,
      fontSize:9,fontFace:'Calibri',color:DC.gray3,align:'right',margin:0});
  }
}

function addNarrative(slide, n, y0) {
  slide.addText(n.headline||'', {
    x:0.45,y:y0,w:5.6,h:0.78,
    fontSize:24,fontFace:'Calibri',bold:true,color:DC.navy,valign:'bottom',margin:0
  });
  slide.addShape('rect',{x:0.45,y:y0+0.84,w:5.6,h:0.04,fill:{color:DC.teal},line:{color:DC.teal}});
  slide.addText(n.spoken||'', {
    x:0.45,y:y0+0.96,w:5.6,h:0.6,
    fontSize:9.5,fontFace:'Calibri',color:DC.gray4,italic:true,valign:'top',margin:0
  });
  if (n.bullets && n.bullets.length) {
    const ba = n.bullets.map((b,i) => ({
      text:b,
      options:{bullet:{color:DC.teal},breakLine:i<n.bullets.length-1,
        fontSize:9.5,color:DC.navy,paraSpaceAfter:5}
    }));
    slide.addText(ba,{x:0.45,y:y0+1.62,w:5.6,h:1.28,fontFace:'Calibri',valign:'top'});
  }
  slide.addShape('rect',{x:0.45,y:y0+2.98,w:5.6,h:0.46,fill:{color:DC.navy},line:{color:DC.navy}});
  slide.addText(n.close||'',{
    x:0.56,y:y0+2.98,w:5.4,h:0.46,
    fontSize:9.5,fontFace:'Calibri',bold:true,color:DC.white,valign:'middle',margin:0
  });
}

function buildCover(pres, d) {
  const s = pres.addSlide('BLANK');
  s.background = {color:DC.white};
  s.addShape('rect',{x:0,y:0,w:0.28,h:5.625,fill:{color:DC.navy},line:{color:DC.navy}});
  s.addShape('rect',{x:0.28,y:0,w:0.08,h:5.625,fill:{color:DC.teal},line:{color:DC.teal}});
  s.addImage({path:VLOGO,x:1.2,y:0.28,w:2.45,h:0.54});
  s.addShape('rect',{x:1.2,y:0.96,w:8.6,h:0.05,fill:{color:DC.teal},line:{color:DC.teal}});
  s.addText(String(d.bankName),{x:1.2,y:1.1,w:8.5,h:0.88,
    fontSize:36,fontFace:'Calibri',bold:true,color:DC.navy,valign:'middle',margin:0});
  s.addText('BMAP Market Snapshot',{x:1.2,y:2.04,w:8.5,h:0.34,
    fontSize:16,fontFace:'Calibri',color:DC.navy,margin:0});
  s.addText(String(d.date),{x:1.2,y:2.42,w:8.5,h:0.26,
    fontSize:11,fontFace:'Calibri',color:DC.gray3,margin:0});
  
  const kpis=[
    {v:String(d.branches),l:'BRANCHES'},
    {v:String(d.deposits),l:'TOTAL DEPOSITS'},
    {v:String(d.avgScore),l:'AVG OPP SCORE'},
    {v:String(d.gap),     l:'VS PEER AVG', red:d.gapNeg},
  ];
  kpis.forEach((k,i)=>{
    const kx=1.2+i*2.14;
    s.addShape('rect',{x:kx,y:2.82,w:2.0,h:0.88,fill:{color:DC.gray1},line:{color:DC.gray2,pt:0.5}});
    s.addText(k.v,{x:kx,y:2.88,w:2.0,h:0.48,
      fontSize:22,fontFace:'Calibri',bold:true,
      color:k.red?DC.justify:DC.navy,align:'center',margin:0});
    s.addText(k.l,{x:kx,y:3.34,w:2.0,h:0.26,
      fontSize:7,fontFace:'Calibri',bold:true,color:DC.gray3,
      align:'center',charSpacing:0.8,margin:0});
  });
  
  const zones=[
    {v:String(d.invest), l:'INVEST', c:DC.invest, bg:DC.investL},
    {v:String(d.analyze),l:'ANALYZE',c:DC.analyze,bg:DC.analyzeL},
    {v:String(d.defend), l:'DEFEND', c:DC.defend, bg:DC.defendL},
    {v:String(d.justify),l:'JUSTIFY',c:DC.justify,bg:DC.justifyL},
  ];
  zones.forEach((z,i)=>{
    const zx=1.2+i*2.14;
    s.addShape('rect',{x:zx,y:3.82,w:2.0,h:0.72,fill:{color:z.bg},line:{color:z.c,pt:0.8}});
    s.addText(z.v,{x:zx,y:3.86,w:2.0,h:0.36,
      fontSize:20,fontFace:'Calibri',bold:true,color:z.c,align:'center',margin:0});
    s.addText(z.l,{x:zx,y:4.22,w:2.0,h:0.24,
      fontSize:8,fontFace:'Calibri',bold:true,color:z.c,
      align:'center',charSpacing:0.8,margin:0});
  });
  
  s.addImage({path:VLOGO,x:0.42,y:5.04,w:1.55,h:0.36});
  s.addText('Confidential  ·  Verlocity Princeton Partners Group  ·  '+new Date().getFullYear(),{
    x:1.2,y:5.3,w:8.5,h:0.2,fontSize:7.5,fontFace:'Calibri',
    color:DC.gray3,align:'center',margin:0});
}

async function buildNetwork(pres, d) {
  const s = pres.addSlide('BLANK');
  s.background = {color:DC.white};
  addChrome(s, 1, 'MARKET OVERVIEW');
  addNarrative(s, d.network, 0.14);

  const kpis=[
    {v:String(d.deposits),  l:'TOTAL DEPOSITS', bg:DC.gray1,   vc:DC.navy},
    {v:String(d.avgScore),  l:'AVG OPP SCORE',  bg:DC.gray1,   vc:DC.navy},
    {v:String(d.depositYoY),l:'DEPOSIT YoY',    bg:d.gapNeg?DC.justifyL:DC.investL, vc:d.gapNeg?DC.justify:DC.invest},
    {v:String(d.gap),       l:'GAP VS PEERS',   bg:d.gapNeg?DC.justifyL:DC.investL, vc:d.gapNeg?DC.justify:DC.invest},
  ];
  kpis.forEach((k,i)=>{
    const kx=6.46+(i%2)*1.76, ky=0.14+Math.floor(i/2)*0.9;
    s.addShape('rect',{x:kx,y:ky,w:1.62,h:0.78,fill:{color:k.bg},line:{color:DC.gray2,pt:0.4}});
    s.addText(k.v,{x:kx,y:ky+0.06,w:1.62,h:0.42,
      fontSize:19,fontFace:'Calibri',bold:true,color:k.vc,align:'center',margin:0});
    s.addText(k.l,{x:kx,y:ky+0.52,w:1.62,h:0.2,
      fontSize:6.5,fontFace:'Calibri',bold:true,color:DC.gray3,
      align:'center',charSpacing:0.6,margin:0});
  });

  try {
    const zoneData = [d.invest, d.analyze, d.defend, d.justify];
    const total = zoneData.reduce((a,b) => a+b, 0);
    
    const pieImg = await vChart('pie',
      ['Invest','Analyze','Defend','Justify'],
      [{
        data: zoneData,
        backgroundColor: ['#27500A', '#185FA5', '#854F0B', '#A32D2D'],
        borderColor: '#FFFFFF',
        borderWidth: 4,
        borderRadius: 5
      }],
      {
        w: 1100, h: 620, pb: 50,
        legend: true,
        lp: 'bottom',
        tooltipCallback: {
          label: (ctx) => {
            const val = ctx.parsed || 0;
            const pct = total ? ((val / total) * 100).toFixed(1) : '0';
            return `${ctx.label}: ${val} (${pct}%)`;
          }
        }
      });
    
    if (pieImg && pieImg.length > 100) {
      s.addImage({data: pieImg, x: 6.0, y: 1.8, w: 3.8, h: 3.1});
    }
  } catch(e) {
    console.warn('Pie chart failed:', e.message);
  }
}

async function buildBranches(pres, d) {
  const s = pres.addSlide('BLANK');
  s.background = {color:DC.white};
  addChrome(s, 2, 'PRIORITY MARKETS');
  addNarrative(s, d.priority, 0.14);

  d.branches.slice(0,5).forEach((b,i)=>{
    const by=0.14+i*1.06;
    const zc  = ZONE_C[b.zone]||DC.analyze;
    const zbg = ZONE_L[b.zone]||DC.analyzeL;
    s.addShape('rect',{x:6.22,y:by,w:3.6,h:0.9,
      fill:{color:DC.gray1},line:{color:DC.gray2,pt:0.4},shadow:mkSh()});
    s.addShape('rect',{x:6.22,y:by,w:0.06,h:0.9,fill:{color:zc},line:{color:zc}});
    s.addShape('rect',{x:6.34,y:by+0.26,w:0.34,h:0.34,fill:{color:zc},line:{color:zc}});
    s.addText(String(i+1),{x:6.34,y:by+0.26,w:0.34,h:0.34,
      fontSize:11,fontFace:'Calibri',bold:true,color:DC.white,
      align:'center',valign:'middle',margin:0});
    s.addText(String(b.name),{x:6.76,y:by+0.07,w:2.18,h:0.26,
      fontSize:10,fontFace:'Calibri',bold:true,color:DC.navy,margin:0});
    s.addText(String(b.city),{x:6.76,y:by+0.33,w:2.18,h:0.18,
      fontSize:8,fontFace:'Calibri',color:DC.gray3,margin:0});
    s.addText(`${String(b.dep)}  ·  ${String(b.yoy)}% YoY`,{x:6.76,y:by+0.54,w:2.18,h:0.22,
      fontSize:9,fontFace:'Calibri',color:DC.navy,margin:0});
    s.addShape('roundRect',{x:9.0,y:by+0.3,w:0.72,h:0.24,
      fill:{color:zbg},line:{color:zc,pt:0.5},rectRadius:0.04});
    s.addText(String(b.zone),{x:9.0,y:by+0.3,w:0.72,h:0.24,
      fontSize:7,fontFace:'Calibri',bold:true,color:zc,
      align:'center',valign:'middle',margin:0});
  });
}

function buildFinancial(pres, d) {
  const s = pres.addSlide('BLANK');
  s.background = {color:DC.white};
  addChrome(s, 3, 'FINANCIAL HEALTH');
  addNarrative(s, d.financial, 0.14);

  const cols = [{x:6.22,w:1.12,label:'',align:'left'},{x:7.38,w:1.26,label:'VALUE'},{x:8.68,w:1.0,label:'BENCHMARK'}];
  cols.forEach(col=>{
    s.addShape('rect',{x:col.x,y:0.14,w:col.w,h:0.3,fill:{color:DC.navy},line:{color:DC.navy}});
    s.addText(col.label,{x:col.x,y:0.14,w:col.w,h:0.3,
      fontSize:7.5,fontFace:'Calibri',bold:true,color:DC.white,
      align:col.align||'center',valign:'middle',margin:col.align==='left'?[0,0,0,8]:0});
  });

  d.metrics.forEach((m,i)=>{
    const my=0.48+i*0.64, bg=i%2===0?DC.gray1:DC.white;
    s.addShape('rect',{x:6.22,y:my,w:3.56,h:0.58,fill:{color:bg},line:{color:DC.gray2,pt:0.3}});
    s.addText(String(m.label),{x:6.3,y:my+0.13,w:1.0,h:0.28,fontSize:9.5,fontFace:'Calibri',color:DC.navy,margin:0});
    s.addText(String(m.value),{x:7.38,y:my+0.1,w:1.26,h:0.32,
      fontSize:13,fontFace:'Calibri',bold:true,color:DC.navy,align:'center',margin:0});
    s.addText(String(m.bench),{x:8.68,y:my+0.13,w:1.0,h:0.28,
      fontSize:9,fontFace:'Calibri',color:DC.gray3,align:'center',italic:true,margin:0});
    const sc = m.ok ? DC.teal : DC.amber;
    s.addShape('roundRect',{x:9.74,y:my+0.13,w:0.3,h:0.3,fill:{color:sc},line:{color:sc},rectRadius:0.04});
    s.addText(m.ok?'✓':'!',{x:9.74,y:my+0.13,w:0.3,h:0.3,
      fontSize:9,fontFace:'Calibri',bold:true,color:DC.white,
      align:'center',valign:'middle',margin:0});
  });

  if (d.competitor) {
    s.addShape('rect',{x:6.22,y:4.88,w:3.56,h:0.34,
      fill:{color:DC.justifyL},line:{color:DC.justify,pt:0.5}});
    s.addText(`⚠  Key Competitor  ·  ${String(d.competitor.branches)} branch overlap  ·  Peer avg YoY ${String(d.competitor.yoy)}%`,{
      x:6.32,y:4.9,w:3.36,h:0.28,
      fontSize:8.5,fontFace:'Calibri',bold:true,color:DC.justify,valign:'middle',margin:0});
  }
}

async function buildGap(pres, d) {
  const s = pres.addSlide('BLANK');
  s.background = {color:DC.navy};
  s.addShape('rect',{x:0,y:0,w:0.12,h:5.625,fill:{color:DC.teal},line:{color:DC.teal}});

  s.addText(String(d.gap),{x:0.28,y:0.16,w:5.2,h:1.86,
    fontSize:96,fontFace:'Calibri',bold:true,color:DC.teal,valign:'middle',margin:0});
  s.addText('GAP VS PEER AVERAGE',{x:0.28,y:2.08,w:5.2,h:0.34,
    fontSize:13,fontFace:'Calibri',bold:true,color:DC.white,charSpacing:3,margin:0});
  s.addText(String(d.gapSubtitle),{x:0.28,y:2.5,w:5.2,h:0.26,
    fontSize:9.5,fontFace:'Calibri',color:'4A6A8A',italic:true,margin:0});

  const tiles=[
    {v:String(d.bankYoY)+'%',  l:'THIS BANK YoY', c:d.gapNeg?'F87171':DC.teal},
    {v:String(d.peerYoY)+'%',  l:'PEER AVG',      c:DC.gray3},
    {v:String(d.gap),          l:'GAP',           c:DC.amber},
  ];
  tiles.forEach((t,i)=>{
    const tx=0.28+i*1.78;
    s.addShape('rect',{x:tx,y:2.96,w:1.62,h:1.12,fill:{color:'162436'},line:{color:'1E3A5F',pt:0.5}});
    s.addShape('rect',{x:tx,y:2.96,w:1.62,h:0.06,fill:{color:t.c},line:{color:t.c}});
    s.addText(t.v,{x:tx,y:3.06,w:1.62,h:0.58,
      fontSize:20,fontFace:'Calibri',bold:true,color:t.c,align:'center',valign:'middle',margin:0});
    s.addText(t.l,{x:tx,y:3.7,w:1.62,h:0.26,
      fontSize:7,fontFace:'Calibri',bold:true,color:'3A5A7A',align:'center',charSpacing:0.8,margin:0});
  });

  try {
    const gapImg = await vChart('bar',
      ['This Bank','Peer Avg'],
      [{
        label: 'Deposit YoY %',
        data: [parseFloat(d.bankYoY), parseFloat(d.peerYoY)],
        backgroundColor: [d.gapNeg ? '#A32D2D' : '#1D9E75', '#778899'],
        borderWidth: 0,
        borderRadius: 8,
        barThickness: 100
      }],
      {
        w: 1150, h: 580, pb: 50,
        legend: false,
        tooltipCallback: {
          label: (ctx) => `${ctx.parsed.y.toFixed(1)}%`
        }
      });
    
    if (gapImg && gapImg.length > 100) {
      s.addImage({data: gapImg, x: 5.4, y: 0.15, w: 4.4, h: 5.0});
    }
  } catch(e) {
    console.warn('Gap chart failed:', e.message);
  }

  s.addText('Verlocity Princeton Partners Group   ·   BMAP Intelligence   ·   '+String(d.bankName),{
    x:0.28,y:5.3,w:9.5,h:0.22,fontSize:7.5,fontFace:'Calibri',color:'2A4060',margin:0});
  s.addText('5',{x:9.5,y:5.3,w:0.38,h:0.22,
    fontSize:9,fontFace:'Calibri',color:'2A4060',align:'right',margin:0});
}

function buildNextSteps(pres, d) {
  const s = pres.addSlide('BLANK');
  s.background = {color:DC.white};
  addChrome(s, 5, 'STRATEGIC PRIORITIES');
  addNarrative(s, d.nextsteps, 0.14);

  const acColors=[DC.teal,DC.analyze,DC.amber,DC.navy];
  d.actions.forEach((a,i)=>{
    const ay=0.14+i*1.3, ac=acColors[i];
    s.addShape('rect',{x:6.22,y:ay,w:3.6,h:1.14,
      fill:{color:DC.gray1},line:{color:DC.gray2,pt:0.4},shadow:mkSh()});
    s.addShape('rect',{x:6.22,y:ay,w:0.06,h:1.14,fill:{color:ac},line:{color:ac}});
    s.addShape('rect',{x:6.34,y:ay+0.36,w:0.34,h:0.34,fill:{color:ac},line:{color:ac}});
    s.addText(String(i+1).padStart(2,'0'),{x:6.34,y:ay+0.36,w:0.34,h:0.34,
      fontSize:9,fontFace:'Calibri',bold:true,color:DC.white,
      align:'center',valign:'middle',margin:0});
    s.addText(String(a.title),{x:6.76,y:ay+0.1,w:2.98,h:0.28,
      fontSize:10.5,fontFace:'Calibri',bold:true,color:DC.navy,margin:0});
    s.addText(String(a.body),{x:6.76,y:ay+0.42,w:2.98,h:0.62,
      fontSize:8.5,fontFace:'Calibri',color:DC.gray4,valign:'top',margin:0});
  });
}

async function snapshotExport() {
  if (!window.selBank || !window.bankData) return;

  const btn = document.getElementById('snapBtn');
  const ld  = document.getElementById('snapLd');
  if (btn) { btn.disabled=true; btn.textContent='Step 1/4 — Pulling data...'; }
  if (ld)  ld.classList.add('on');

  try {
    const {rows, allBr:br, tgt, ik} = window.bankData;
    const bankName = rows[0]?.namefull || '—';

    const tot      = rows.reduce((a,r)=>a+(+r.latest_dep||0),0);
    const avg      = v => rows.reduce((a,r)=>a+(+r[v]||0),0)/rows.length;
    const invest   = rows.filter(r=>r.opportunity_zone==='Invest').length;
    const analyze  = rows.filter(r=>r.opportunity_zone==='Analyze').length;
    const defend   = rows.filter(r=>r.opportunity_zone==='Defend').length;
    const justify  = rows.filter(r=>r.opportunity_zone==='Justify').length;
    const bankYoY  = avg('yoy_deposits')*100;
    const compYoY  = avg('avg_comp_yoy')*100;
    const gap      = bankYoY - compYoY;
    const avgScore = avg('opportunity_score');

    if (btn) btn.textContent='Step 1/4 — Pulling financials...';
    const SUPA = 'https://tuiiywphoynbmkxpoyps.supabase.co';
    const KEY  = window.SUPA_KEY || '';
    const finRes = await fetch(
      `${SUPA}/rest/v1/bank_financial_snapshot_latest?inst_key=eq.${ik}&select=*&limit=1`,
      {headers:{apikey:KEY,'Authorization':'Bearer '+KEY}}
    );
    const fin = (await finRes.json())[0] || {};

    if (btn) btn.textContent='Step 2/4 — Generating narratives...';
    const topBr  = [...br].sort((a,b)=>b.opportunity_score-a.opportunity_score);
    const justTop= br.filter(b=>b.opportunity_zone==='Justify').sort((a,b)=>b.latest_dep-a.latest_dep);
    const tier1  = br.filter(b=>b.priority_tier?.startsWith('1')).slice(0,2);

    const aiCtx = `Bank: ${bankName} | ${rows.length} branches | $${(tot/1e9).toFixed(2)}B deposits
Deposit YoY: +${bankYoY.toFixed(1)}% | Peer avg: +${compYoY.toFixed(1)}% | Gap: ${gap>0?'+':''}${gap.toFixed(1)}pp
Avg opp score: ${avgScore.toFixed(1)}/100 | Zones: Invest ${invest} | Analyze ${analyze} | Defend ${defend} | Justify ${justify}
ROA: ${fin.roa||'—'}% | NIM: ${fin.nim||'—'}% | Efficiency: ${fin.efficiency_ratio||'—'}%
Net income YoY: ${fin.net_income_yoy_pct||'—'}% | Tier 1: ${fin.tier1_capital_pct||'—'}%
Top competitor: ${tgt?tgt.target_institution+' — '+tgt.branches_in_radius+' overlap branches':'N/A'}
Top 3 branches: ${topBr.slice(0,3).map(b=>`${b.namebr.replace(/^\d+--/,'').trim()} ($${((+b.latest_dep)/1e6).toFixed(0)}M, ${((+b.yoy_deposits)*100).toFixed(1)}% YoY, score ${(+b.opportunity_score).toFixed(0)})`).join(', ')}`;

    const aiResp = await fetch(window.AI_PROXY||'', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        model:'claude-sonnet-4-5', max_tokens:2400,
        system:`You are BMAP Executive Strategist at Verlocity. Write boardroom-quality slide narratives grounded in the exact numbers provided.
Return ONLY valid JSON — no markdown, no explanation:
{"slides":[
  {"id":"network","headline":"strong claim max 9 words","spoken":"2 sentences Tom says walking in. Specific numbers.","bullets":["data point with number","competitive insight","risk or opportunity"],"close":"forward action with metric"},
  {"id":"priority","headline":"...","spoken":"...","bullets":[...],"close":"..."},
  {"id":"financial","headline":"...","spoken":"...","bullets":[...],"close":"..."},
  {"id":"nextsteps","headline":"...","spoken":"...","bullets":[...],"close":"..."}
]}`,
        messages:[{role:'user',content:`Generate 4-slide narratives for:\n\n${aiCtx}`}]
      })
    });
    const aiJson = await aiResp.json();
    let aiTxt = (aiJson.content?.find(b=>b.type==='text')?.text||'{"slides":[]}')
      .replace(/```json|```/g,'').trim();
    const narr = {};
    try { JSON.parse(aiTxt).slides.forEach(s=>narr[s.id]=s); } catch(e) {}

    const N = id => narr[id] || {headline:'',spoken:'',bullets:[],close:''};

    if (btn) btn.textContent='Step 3/4 — Drawing charts...';
    const DATA = {
      bankName,
      date: new Date().toLocaleDateString('en-US',{month:'long',year:'numeric'}),
      branches:   String(rows.length),
      deposits:   `$${(tot/1e9).toFixed(1)}B`,
      avgScore:   avgScore.toFixed(0),
      depositYoY: `${bankYoY>=0?'+':''}${bankYoY.toFixed(1)}%`,
      gap:        `${gap>=0?'+':''}${gap.toFixed(1)}pp`,
      bankYoY:    bankYoY.toFixed(1),
      peerYoY:    compYoY.toFixed(1),
      gapNeg:     gap < 0,
      gapSubtitle:`Deposit growth vs. peer average — ${fin.period||'Q4 2025'}`,
      invest, analyze, defend, justify,
      network:   N('network'),
      priority:  N('priority'),
      financial: N('financial'),
      nextsteps: N('nextsteps'),
      branches: topBr.slice(0,8).map(b=>({
        name:  b.namebr.replace(/^\d+--/,'').trim().slice(0,16),
        city:  `${b.citybr}, ${b.stalpbr}`,
        score: (+b.opportunity_score).toFixed(0),
        dep:   `$${((+b.latest_dep)/1e6).toFixed(0)}M`,
        yoy:   `${((+b.yoy_deposits)*100)>=0?'+':''}${((+b.yoy_deposits)*100).toFixed(1)}`,
        zone:  b.opportunity_zone,
      })),
      metrics:[
        {label:'ROA',           value:`${fin.roa||'—'}%`,              bench:'>1.0%',    ok:+fin.roa>=1},
        {label:'NIM',           value:`${fin.nim||'—'}%`,              bench:'2.5–3.5%', ok:+fin.nim>=2.5&&+fin.nim<=4},
        {label:'Efficiency',    value:`${fin.efficiency_ratio||'—'}%`, bench:'<60%',     ok:+fin.efficiency_ratio<60},
        {label:'Net Income YoY',value:`${+fin.net_income_yoy_pct>0?'+':''}${fin.net_income_yoy_pct||'—'}%`,bench:'>0%',ok:+fin.net_income_yoy_pct>0},
        {label:'Deposit YoY',   value:`${bankYoY>=0?'+':''}${bankYoY.toFixed(1)}%`,bench:'>2%',ok:bankYoY>=2},
        {label:'Cost of Funds', value:`${fin.cost_of_funds_pct||'—'}%`,bench:'<2%',     ok:+fin.cost_of_funds_pct<2},
        {label:'Tier 1 Capital',value:`${fin.tier1_capital_pct||'—'}%`,bench:'>8%',     ok:+fin.tier1_capital_pct>=8},
      ],
      competitor: tgt?{branches:tgt.branches_in_radius, yoy:(+tgt.avg_yoy_pct).toFixed(1)}:null,
      actions:[
        {title: tier1.length?`Activate: ${tier1.map(b=>b.namebr.replace(/^\d+--/,'').trim().split(' ').slice(0,3).join(' ')).join(' + ')}`:'Activate Top Invest Branches',
         body:  tier1.length?tier1.map(b=>`${b.namebr.replace(/^\d+--/,'').trim()} — Score ${(+b.opportunity_score).toFixed(0)} | $${((+b.latest_dep)/1e6).toFixed(0)}M`).join('\n'):`${invest} Invest-zone branches ready for campaign launch.`},
        {title:'Launch Targeted Audience Campaigns',
         body:`${invest} Invest zone branches with active growth. Rate-sensitive depositors + digital big-bank leavers. Deploy via AudienceFinder.`},
        {title:`Justify Zone — ${justify} Branches Under Review`,
         body: justTop.slice(0,2).map(b=>`${b.namebr.replace(/^\d+--/,'').trim()}: $${((+b.latest_dep)/1e6).toFixed(0)}M — assess marketing ROI`).join('\n')||`${justify} branches in Justify zone. Monitor for consolidation.`},
        {title: tgt?'Protect Against Key Competitor':'Protect Market Position',
         body:  tgt?`${tgt.branches_in_radius} shared geographies. Competitor at ${(+tgt.avg_yoy_pct).toFixed(1)}% deposit growth. Deploy defensive rate messaging in overlap zones.`:'Identify top competitors in overlapping markets and monitor rate activity.'},
      ],
    };

    if (btn) btn.textContent='Step 4/4 — Building deck...';
    const pres = new PptxGenJS();
    pres.layout = 'LAYOUT_16x9';
    pres.author = 'Verlocity Princeton Partners Group';
    pres.title  = `BMAP Snapshot — ${bankName}`;
    pres.defineLayout({name:'BLANK',width:10,height:5.625});

    buildCover(pres, DATA);
    await buildNetwork(pres, DATA);
    buildBranches(pres, DATA);
    buildFinancial(pres, DATA);
    await buildGap(pres, DATA);
    buildNextSteps(pres, DATA);

    const blob = await pres.write('blob');
    const safeName = bankName.replace(/[^a-z0-9]/gi,'_');
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href=url; 
    a.download=`BMAP_Snapshot_${safeName}.pptx`;
    document.body.appendChild(a); 
    a.click();
    setTimeout(()=>{URL.revokeObjectURL(url); a.remove();},1000);

    if (btn){btn.textContent='✓ Downloaded';btn.style.background='#1D9E75';}
    setTimeout(()=>{
      if(btn){btn.textContent='⬇ Generate & Export BMAP Snapshot';btn.style.background='';btn.disabled=false;}
    },3500);

  } catch(err) {
    console.error('snapshotExport error:',err);
    if(btn){btn.textContent='⬇ Generate & Export BMAP Snapshot';btn.style.background='';btn.disabled=false;}
    alert('Export failed: '+err.message);
  }
  if(ld) ld.classList.remove('on');
}
