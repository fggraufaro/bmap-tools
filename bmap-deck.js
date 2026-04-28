/**
 * BMAP Snapshot Deck — SAFE FULL VERSION
 * Full branded PPTX deck with graph-style visuals drawn as shapes.
 * Avoids native pptx.addChart(), which can create corrupt PowerPoint XML in browser builds.
 *
 * Requirements:
 * - PptxGenJS loaded in HTML before this file
 * - window.bankData set by context-generator.html:
 *   { rows, allBr, tgt, ik }
 * - Optional: window.AI_PROXY and window.SUPA_KEY
 */

// ===============================
// BRAND TOKENS
// ===============================
const C = {
  navy: '1A2332',
  navy2: '162436',
  teal: '1D9E75',
  amber: 'F5A623',
  white: 'FFFFFF',
  offwh: 'F5F5F2',
  gray1: 'F5F5F2',
  gray2: 'E8E8E5',
  gray3: '778899',
  gray4: '4A5568',
  invest: '27500A',
  investL: 'EAF3DE',
  analyze: '185FA5',
  analyzeL: 'E6F1FB',
  defend: '854F0B',
  defendL: 'FFF3E0',
  justify: 'A32D2D',
  justifyL: 'FCEBEB',
  red: 'DC2626',
  blue: '2563EB',
  green: '059669',
};

const ZONE_COLOR = {
  Invest: C.invest,
  Analyze: C.analyze,
  Defend: C.defend,
  Justify: C.justify,
};

const ZONE_LIGHT = {
  Invest: C.investL,
  Analyze: C.analyzeL,
  Defend: C.defendL,
  Justify: C.justifyL,
};

const LOGO_B64 = typeof window !== 'undefined' && window.BMAP_LOGO_B64 ? window.BMAP_LOGO_B64 : '';
const LOGO = LOGO_B64 ? 'image/png;base64,' + LOGO_B64 : null;

// ===============================
// SAFE HELPERS
// ===============================
function safe(v, fallback = '') {
  if (v === undefined || v === null || Number.isNaN(v)) return fallback;
  return String(v);
}

function num(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function moneyM(v) {
  const n = num(v, 0);
  if (Math.abs(n) >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  return `$${(n / 1e6).toFixed(0)}M`;
}

function pct(v, decimals = 1) {
  const n = num(v, 0);
  return `${n > 0 ? '+' : ''}${n.toFixed(decimals)}%`;
}

function pp(v, decimals = 1) {
  const n = num(v, 0);
  return `${n > 0 ? '+' : ''}${n.toFixed(decimals)}pp`;
}

function cleanBranchName(s) {
  return safe(s, 'Branch').replace(/^\d+--/, '').trim();
}

function safeFileName(s) {
  return safe(s, 'BMAP').replace(/[^a-z0-9]+/gi, '_').replace(/^_+|_+$/g, '').slice(0, 80);
}

function addText(slide, text, opts) {
  slide.addText(safe(text), {
    fontFace: 'Calibri',
    margin: 0,
    fit: 'shrink',
    ...opts,
  });
}

function rect(slide, x, y, w, h, color, lineColor = color, extra = {}) {
  slide.addShape('rect', {
    x, y, w, h,
    fill: { color },
    line: { color: lineColor, pt: 0.5 },
    ...extra,
  });
}

function line(slide, x, y, w, color = C.teal, pt = 1.5) {
  slide.addShape('line', {
    x, y, w, h: 0,
    line: { color, pt },
  });
}

function addLogo(slide, x, y, w, h) {
  if (LOGO) {
    try {
      slide.addImage({ data: LOGO, x, y, w, h });
      return;
    } catch (e) {
      console.warn('Logo failed, using text fallback', e);
    }
  }

  addText(slide, 'BMAP', {
    x, y, w, h,
    fontSize: 13,
    bold: true,
    color: C.navy,
    align: 'left',
    valign: 'mid',
  });
}

function addChrome(pres, slide, pageNum, sectionLabel) {
  rect(slide, 0, 0, 0.28, 5.625, C.navy);
  rect(slide, 0.28, 0, 0.08, 5.625, C.teal);

  addLogo(slide, 0.45, 5.08, 1.25, 0.28);

  if (sectionLabel) {
    rect(slide, 8.05, 0.16, 1.75, 0.28, C.teal, C.teal);
    addText(slide, sectionLabel, {
      x: 8.05, y: 0.165, w: 1.75, h: 0.25,
      fontSize: 7.5,
      bold: true,
      color: C.white,
      align: 'center',
      valign: 'mid',
      charSpacing: 1,
    });
  }

  addText(slide, safe(pageNum), {
    x: 9.5, y: 5.28, w: 0.35, h: 0.18,
    fontSize: 8.5,
    color: C.gray3,
    align: 'right',
  });
}

function addKpi(slide, x, y, w, h, value, label, valueColor = C.navy, bg = C.gray1) {
  rect(slide, x, y, w, h, bg, C.gray2);

  addText(slide, value, {
    x, y: y + 0.07, w, h: h * 0.52,
    fontSize: 21,
    bold: true,
    color: valueColor,
    align: 'center',
    valign: 'mid',
  });

  addText(slide, label, {
    x: x + 0.05, y: y + h * 0.63, w: w - 0.1, h: h * 0.25,
    fontSize: 7,
    bold: true,
    color: C.gray3,
    align: 'center',
    charSpacing: 0.7,
  });
}

function addTitle(slide, headline, subtitle) {
  addText(slide, headline, {
    x: 0.72, y: 0.18, w: 5.8, h: 0.72,
    fontSize: 25,
    bold: true,
    color: C.navy,
    valign: 'bottom',
  });

  line(slide, 0.72, 1.02, 5.55, C.teal, 1.5);

  if (subtitle) {
    addText(slide, subtitle, {
      x: 0.72, y: 1.12, w: 5.6, h: 0.48,
      fontSize: 10,
      italic: true,
      color: C.gray4,
      valign: 'top',
    });
  }
}

function addBullets(slide, bullets, x, y, w, h, fontSize = 10) {
  const items = (bullets || []).filter(Boolean).slice(0, 4);
  const runs = items.map((b, i) => ({
    text: safe(b),
    options: {
      bullet: { type: 'bullet' },
      breakLine: i < items.length - 1,
      fontSize,
      color: C.navy,
      paraSpaceAfterPt: 6,
    },
  }));

  if (runs.length) {
    slide.addText(runs, {
      x, y, w, h,
      fontFace: 'Calibri',
      margin: 0,
      valign: 'top',
    });
  }
}

function addCloseBar(slide, text, x, y, w) {
  rect(slide, x, y, w, 0.46, C.navy, C.navy);

  addText(slide, text, {
    x: x + 0.1, y: y + 0.08, w: w - 0.2, h: 0.3,
    fontSize: 10,
    bold: true,
    color: C.white,
    valign: 'mid',
  });
}

// ===============================
// SHAPE-BASED GRAPHS
// ===============================
function addBarGraph(slide, items, opts) {
  const {
    x, y, w, h,
    labelKey = 'label',
    valueKey = 'value',
    max = 100,
    barColor = C.navy,
    title = '',
    valueSuffix = '',
    maxItems = 6,
  } = opts;

  if (title) {
    addText(slide, title, {
      x, y: y - 0.28, w, h: 0.2,
      fontSize: 9,
      bold: true,
      color: C.gray4,
      charSpacing: 0.8,
    });
  }

  const data = (items || []).slice(0, maxItems);
  const rowH = h / Math.max(data.length, 1);
  const labelW = w * 0.38;
  const barW = w * 0.48;
  const valueW = w * 0.12;

  data.forEach((it, i) => {
    const yy = y + i * rowH;
    const val = Math.max(0, Math.min(num(it[valueKey], 0), max));
    const pctWidth = max > 0 ? val / max : 0;
    const z = it.zone || it.opportunity_zone;
    const color = ZONE_COLOR[z] || it.color || barColor;

    addText(slide, safe(it[labelKey]).slice(0, 22), {
      x, y: yy + 0.05, w: labelW, h: rowH - 0.08,
      fontSize: 8.3,
      color: C.gray4,
      valign: 'mid',
    });

    rect(slide, x + labelW + 0.05, yy + 0.13, barW, 0.16, C.gray2, C.gray2);
    rect(slide, x + labelW + 0.05, yy + 0.13, Math.max(0.02, barW * pctWidth), 0.16, color, color);

    addText(slide, `${val.toFixed(0)}${valueSuffix}`, {
      x: x + labelW + barW + 0.12,
      y: yy + 0.04,
      w: valueW,
      h: rowH - 0.06,
      fontSize: 8.5,
      bold: true,
      color: C.navy,
      align: 'right',
      valign: 'mid',
    });
  });
}

function addColumnGraph(slide, items, opts) {
  const {
    x, y, w, h,
    labelKey = 'label',
    valueKey = 'value',
    min = 0,
    max = 100,
    title = '',
    valueSuffix = '',
  } = opts;

  if (title) {
    addText(slide, title, {
      x, y: y - 0.28, w, h: 0.2,
      fontSize: 9,
      bold: true,
      color: C.gray4,
      charSpacing: 0.8,
    });
  }

  line(slide, x, y + h, w, C.gray2, 0.8);
  slide.addShape('line', {
    x, y, w: 0, h,
    line: { color: C.gray2, pt: 0.8 },
  });

  const data = items || [];
  const colGap = 0.22;
  const colW = (w - colGap * (data.length + 1)) / Math.max(data.length, 1);

  data.forEach((it, i) => {
    const val = num(it[valueKey], 0);
    const norm = Math.max(0, Math.min(1, (val - min) / (max - min || 1)));
    const bh = h * norm;
    const cx = x + colGap + i * (colW + colGap);
    const cy = y + h - bh;
    const color = it.color || C.teal;

    rect(slide, cx, cy, colW, Math.max(0.02, bh), color, color);

    addText(slide, `${val > 0 ? '+' : ''}${val.toFixed(1)}${valueSuffix}`, {
      x: cx - 0.08,
      y: cy - 0.24,
      w: colW + 0.16,
      h: 0.2,
      fontSize: 8.5,
      bold: true,
      color,
      align: 'center',
    });

    addText(slide, safe(it[labelKey]), {
      x: cx - 0.15,
      y: y + h + 0.08,
      w: colW + 0.3,
      h: 0.25,
      fontSize: 8,
      color: C.gray4,
      align: 'center',
    });
  });
}

function addPieLikeGraph(slide, zones, x, y, w, h) {
  const total = Math.max(1, Object.values(zones).reduce((a, b) => a + num(b, 0), 0));
  const keys = ['Invest', 'Analyze', 'Defend', 'Justify'];

  addText(slide, 'ZONE MIX', {
    x, y: y - 0.28, w, h: 0.2,
    fontSize: 9,
    bold: true,
    color: C.gray4,
    charSpacing: 0.8,
  });

  let curX = x;
  keys.forEach(k => {
    const val = num(zones[k], 0);
    const segW = w * (val / total);
    if (segW > 0.01) rect(slide, curX, y, segW, 0.35, ZONE_COLOR[k], ZONE_COLOR[k]);
    curX += segW;
  });

  keys.forEach((k, i) => {
    const ly = y + 0.62 + i * 0.42;
    rect(slide, x, ly + 0.04, 0.16, 0.16, ZONE_COLOR[k], ZONE_COLOR[k]);

    addText(slide, k, {
      x: x + 0.24,
      y: ly,
      w: 1.1,
      h: 0.22,
      fontSize: 8.5,
      bold: true,
      color: ZONE_COLOR[k],
    });

    addText(slide, `${zones[k] || 0} branches`, {
      x: x + 1.35,
      y: ly,
      w: 1.0,
      h: 0.22,
      fontSize: 8.2,
      color: C.gray4,
      align: 'right',
    });
  });

  addText(slide, String(total), {
    x: x + w - 0.9,
    y: y + 1.38,
    w: 0.8,
    h: 0.38,
    fontSize: 19,
    bold: true,
    color: C.navy,
    align: 'center',
  });

  addText(slide, 'branches', {
    x: x + w - 0.9,
    y: y + 1.78,
    w: 0.8,
    h: 0.2,
    fontSize: 8,
    color: C.gray3,
    align: 'center',
  });
}

function addGauge(slide, x, y, w, h, value, label, color = C.teal) {
  const v = Math.max(0, Math.min(100, num(value, 0)));

  rect(slide, x, y, w, h, C.gray2, C.gray2);
  rect(slide, x, y, w * (v / 100), h, color, color);

  addText(slide, `${v.toFixed(0)}`, {
    x, y: y - 0.36, w, h: 0.3,
    fontSize: 20,
    bold: true,
    color,
    align: 'center',
  });

  addText(slide, label, {
    x, y: y + h + 0.08, w, h: 0.22,
    fontSize: 7.5,
    bold: true,
    color: C.gray4,
    align: 'center',
    charSpacing: 0.6,
  });
}

// ===============================
// DATA + AI NARRATIVES
// ===============================
function buildData() {
  const bd = window.bankData || {};
  const rows = Array.isArray(bd.rows) ? bd.rows : [];
  const allBr = Array.isArray(bd.allBr) ? bd.allBr : [];
  const tgt = bd.tgt || null;

  if (!rows.length) throw new Error('No bank data loaded. Search/select a bank first.');

  const bankName = rows[0]?.namefull || window.selBank?.institution_name || 'Selected Bank';
  const totalDeposits = rows.reduce((a, r) => a + num(r.latest_dep, 0), 0);
  const avg = col => rows.reduce((a, r) => a + num(r[col], 0), 0) / Math.max(rows.length, 1);

  const bankYoY = avg('yoy_deposits') * 100;
  const peerYoY = avg('avg_comp_yoy') * 100;
  const gap = bankYoY - peerYoY;
  const avgScore = avg('opportunity_score');

  const zones = {
    Invest: rows.filter(r => r.opportunity_zone === 'Invest').length,
    Analyze: rows.filter(r => r.opportunity_zone === 'Analyze').length,
    Defend: rows.filter(r => r.opportunity_zone === 'Defend').length,
    Justify: rows.filter(r => r.opportunity_zone === 'Justify').length,
  };

  const topBranches = [...allBr]
    .sort((a, b) => num(b.opportunity_score) - num(a.opportunity_score))
    .slice(0, 8)
    .map(b => ({
      label: cleanBranchName(b.namebr),
      name: cleanBranchName(b.namebr),
      city: `${safe(b.citybr)}, ${safe(b.stalpbr)}`,
      value: num(b.opportunity_score, 0),
      score: num(b.opportunity_score, 0),
      deposits: num(b.latest_dep, 0),
      depText: moneyM(num(b.latest_dep, 0)),
      yoy: num(b.yoy_deposits, 0) * 100,
      zone: safe(b.opportunity_zone, 'Analyze'),
      tier: safe(b.priority_tier),
    }));

  return {
    bankName,
    date: new Date().toLocaleDateString('en-US', { month: 'long', year: 'numeric' }),
    branchCount: rows.length,
    deposits: totalDeposits,
    depositsText: moneyM(totalDeposits),
    avgScore,
    bankYoY,
    peerYoY,
    gap,
    zones,
    topBranches,
    metrics: {
      marketGrowth: avg('market_growth_score'),
      relativeGrowth: bankYoY,
      invDensity: avg('inv_density_norm_winsor'),
    },
    competitor: tgt ? {
      name: safe(tgt.target_institution, 'Top competitor'),
      branches: num(tgt.branches_in_radius, 0),
      yoy: num(tgt.avg_yoy_pct, 0),
      vulnerability: num(tgt.avg_vuln_score, 0),
    } : null,
  };
}

async function getNarratives(d) {
  const fallback = {
    network: {
      headline: `${d.branchCount} branches with ${d.zones.Invest} Invest markets`,
      spoken: `${d.bankName} has ${d.depositsText} in deposits and an average Opportunity Score of ${d.avgScore.toFixed(0)}. The network has a ${pp(d.gap)} deposit growth gap versus peers.`,
      bullets: [
        `${d.zones.Invest} branches are in Invest zone and should receive priority campaign support.`,
        `${d.zones.Analyze} branches require focused diagnosis before major spend.`,
        `Peer deposit growth is ${pct(d.peerYoY)} versus bank growth of ${pct(d.bankYoY)}.`,
      ],
      close: 'The opportunity is to convert strong markets into measurable deposit capture.',
    },
    priority: {
      headline: 'Priority branches define the growth plan',
      spoken: `The strongest branches are ${d.topBranches.slice(0, 3).map(b => b.name).join(', ')}. These markets combine higher score, deposit scale, and campaign readiness.`,
      bullets: [
        'Invest branches should anchor acquisition and promotional campaigns.',
        'Analyze branches should be reviewed for leakage, pricing, and competitor pressure.',
        'Branch-level prioritisation prevents marketing budget from being spread too thin.',
      ],
      close: 'Fund the markets most likely to convert awareness into deposits.',
    },
    financial: {
      headline: 'Growth performance is the strategic pressure point',
      spoken: `The bank is growing deposits at ${pct(d.bankYoY)}, while peers are growing at ${pct(d.peerYoY)}. That creates a ${pp(d.gap)} performance gap.`,
      bullets: [
        'The performance gap should be treated as a campaign urgency signal.',
        'Markets with high opportunity scores need stronger activation.',
        'Competitor overlap should guide rate, offer, and message testing.',
      ],
      close: 'The next move is disciplined campaign execution, not generic awareness.',
    },
    nextsteps: {
      headline: 'Move from insight to campaign execution',
      spoken: 'The deck should trigger an action conversation: which branches, which audiences, which offer, and how success will be measured.',
      bullets: [
        'Approve priority branches for the next 8-week campaign window.',
        'Define deposit target, offer strategy, and conversion assumptions by branch.',
        'Track campaign performance against peer growth and market momentum.',
      ],
      close: 'BMAP becomes most valuable when it links market insight to measurable growth.',
    },
  };

  if (!window.AI_PROXY) return fallback;

  try {
    const ctx = `Bank: ${d.bankName}
Branches: ${d.branchCount}
Deposits: ${d.depositsText}
Avg Opportunity Score: ${d.avgScore.toFixed(1)}
Bank YoY: ${pct(d.bankYoY)}
Peer YoY: ${pct(d.peerYoY)}
Gap: ${pp(d.gap)}
Zones: Invest ${d.zones.Invest}, Analyze ${d.zones.Analyze}, Defend ${d.zones.Defend}, Justify ${d.zones.Justify}
Top branches: ${d.topBranches.slice(0, 5).map(b => `${b.name} (${b.zone}, score ${b.score.toFixed(0)}, ${b.depText}, ${pct(b.yoy)})`).join('; ')}
Top competitor: ${d.competitor ? `${d.competitor.name}, ${d.competitor.branches} overlap branches, ${pct(d.competitor.yoy)} YoY` : 'N/A'}`;

    const resp = await fetch(window.AI_PROXY, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'claude-sonnet-4-5',
        max_tokens: 1600,
        system: `You are BMAP Executive Strategist. Return ONLY valid JSON with keys network, priority, financial, nextsteps. Each key has: headline max 8 words, spoken 1-2 sentences, bullets array of 3 short bullets, close one punchy sentence.`,
        messages: [{ role: 'user', content: ctx }],
      }),
    });

    const json = await resp.json();
    const txt = (json.content?.find(b => b.type === 'text')?.text || '').replace(/```json|```/g, '').trim();
    const parsed = JSON.parse(txt);

    return { ...fallback, ...parsed };
  } catch (e) {
    console.warn('AI narratives failed, using fallback', e);
    return fallback;
  }
}

// ===============================
// SLIDE BUILDERS
// ===============================
function slideCover(pres, d) {
  const s = pres.addSlide();
  s.background = { color: C.white };

  rect(s, 0, 0, 0.28, 5.625, C.navy);
  rect(s, 0.28, 0, 0.08, 5.625, C.teal);

  addLogo(s, 1.1, 0.3, 1.7, 0.38);
  line(s, 1.1, 0.92, 8.55, C.teal, 2);

  addText(s, d.bankName, {
    x: 1.1, y: 1.18, w: 8.4, h: 0.72,
    fontSize: 34,
    bold: true,
    color: C.navy,
  });

  addText(s, 'BMAP Market Snapshot', {
    x: 1.1, y: 2.02, w: 8.4, h: 0.32,
    fontSize: 16,
    color: C.navy,
  });

  addText(s, d.date, {
    x: 1.1, y: 2.42, w: 8.4, h: 0.22,
    fontSize: 10.5,
    color: C.gray3,
  });

  const kpis = [
    [String(d.branchCount), 'BRANCHES', C.navy],
    [d.depositsText, 'TOTAL DEPOSITS', C.navy],
    [d.avgScore.toFixed(0), 'AVG OPP SCORE', C.navy],
    [pp(d.gap), 'VS PEERS', d.gap < 0 ? C.justify : C.teal],
  ];

  kpis.forEach((k, i) => {
    addKpi(s, 1.1 + i * 2.15, 2.88, 2.0, 0.86, k[0], k[1], k[2]);
  });

  const zoneList = ['Invest', 'Analyze', 'Defend', 'Justify'];
  zoneList.forEach((z, i) => {
    const x = 1.1 + i * 2.15;

    rect(s, x, 3.88, 2.0, 0.72, ZONE_LIGHT[z], ZONE_COLOR[z]);

    addText(s, String(d.zones[z]), {
      x, y: 3.94, w: 2.0, h: 0.32,
      fontSize: 20,
      bold: true,
      color: ZONE_COLOR[z],
      align: 'center',
    });

    addText(s, z.toUpperCase(), {
      x, y: 4.28, w: 2.0, h: 0.22,
      fontSize: 8,
      bold: true,
      color: ZONE_COLOR[z],
      align: 'center',
      charSpacing: 0.8,
    });
  });

  addText(s, 'Confidential · Verlocity Princeton Partners Group · BMAP Intelligence', {
    x: 1.1, y: 5.28, w: 8.4, h: 0.2,
    fontSize: 7.5,
    color: C.gray3,
    align: 'center',
  });
}

function slideNetwork(pres, d, n) {
  const s = pres.addSlide();
  s.background = { color: C.white };

  addChrome(pres, s, 1, 'MARKET OVERVIEW');
  addTitle(s, n.network.headline, n.network.spoken);

  addBullets(s, n.network.bullets, 0.72, 1.82, 5.55, 1.1, 10);
  addCloseBar(s, n.network.close, 0.72, 3.15, 5.55);

  addKpi(s, 6.55, 0.28, 1.45, 0.75, d.depositsText, 'DEPOSITS', C.navy);
  addKpi(s, 8.15, 0.28, 1.45, 0.75, d.avgScore.toFixed(0), 'OPP SCORE', C.navy);
  addKpi(s, 6.55, 1.18, 1.45, 0.75, pct(d.bankYoY), 'BANK YOY', d.bankYoY < 0 ? C.justify : C.teal);
  addKpi(s, 8.15, 1.18, 1.45, 0.75, pct(d.peerYoY), 'PEER YOY', C.gray4);

  addPieLikeGraph(s, d.zones, 6.55, 2.35, 3.05, 2.25);

  addGauge(s, 0.72, 4.35, 1.55, 0.18, d.metrics.marketGrowth, 'MARKET GROWTH', C.teal);
  addGauge(s, 2.65, 4.35, 1.55, 0.18, Math.max(0, Math.min(100, d.avgScore)), 'OPPORTUNITY', C.navy);
  addGauge(s, 4.58, 4.35, 1.55, 0.18, d.metrics.invDensity, 'INV. DENSITY', C.amber);
}

function slideBranches(pres, d, n) {
  const s = pres.addSlide();
  s.background = { color: C.white };

  addChrome(pres, s, 2, 'PRIORITY MARKETS');
  addTitle(s, n.priority.headline, n.priority.spoken);

  addBarGraph(s, d.topBranches, {
    x: 0.72, y: 1.82, w: 5.5, h: 2.15,
    labelKey: 'name',
    valueKey: 'score',
    max: 100,
    title: 'OPPORTUNITY SCORE BY BRANCH',
    maxItems: 7,
  });

  addCloseBar(s, n.priority.close, 0.72, 4.15, 5.55);

  d.topBranches.slice(0, 5).forEach((b, i) => {
    const y = 0.24 + i * 1.02;

    rect(s, 6.48, y, 3.35, 0.82, C.gray1, C.gray2);
    rect(s, 6.48, y, 0.07, 0.82, ZONE_COLOR[b.zone] || C.analyze, ZONE_COLOR[b.zone] || C.analyze);

    rect(s, 6.66, y + 0.25, 0.28, 0.28, ZONE_COLOR[b.zone] || C.analyze, ZONE_COLOR[b.zone] || C.analyze);
    addText(s, String(i + 1), {
      x: 6.66, y: y + 0.31, w: 0.28, h: 0.14,
      fontSize: 8,
      bold: true,
      color: C.white,
      align: 'center',
    });

    addText(s, b.name, {
      x: 7.05, y: y + 0.08, w: 1.75, h: 0.22,
      fontSize: 10,
      bold: true,
      color: C.navy,
    });

    addText(s, b.city, {
      x: 7.05, y: y + 0.33, w: 1.75, h: 0.18,
      fontSize: 8,
      color: C.gray3,
    });

    addText(s, `${b.depText} · ${pct(b.yoy)}`, {
      x: 7.05, y: y + 0.55, w: 1.75, h: 0.18,
      fontSize: 8.5,
      color: C.navy,
    });

    rect(s, 8.95, y + 0.28, 0.7, 0.24, ZONE_LIGHT[b.zone] || C.analyzeL, ZONE_COLOR[b.zone] || C.analyze);
    addText(s, b.zone, {
      x: 8.95, y: y + 0.31, w: 0.7, h: 0.16,
      fontSize: 6.8,
      bold: true,
      color: ZONE_COLOR[b.zone] || C.analyze,
      align: 'center',
    });
  });
}

function slideFinancial(pres, d, n) {
  const s = pres.addSlide();
  s.background = { color: C.white };

  addChrome(pres, s, 3, 'GROWTH GAP');
  addTitle(s, n.financial.headline, n.financial.spoken);

  addBullets(s, n.financial.bullets, 0.72, 1.82, 5.55, 1.1, 10);
  addCloseBar(s, n.financial.close, 0.72, 3.15, 5.55);

  const maxAbs = Math.max(5, Math.abs(d.bankYoY), Math.abs(d.peerYoY), Math.abs(d.gap)) + 3;

  addColumnGraph(s, [
    { label: 'Bank', value: d.bankYoY, color: d.bankYoY < 0 ? C.justify : C.teal },
    { label: 'Peer', value: d.peerYoY, color: C.gray3 },
    { label: 'Gap', value: d.gap, color: d.gap < 0 ? C.justify : C.amber },
  ], {
    x: 6.55, y: 1.0, w: 3.0, h: 2.55,
    min: Math.min(0, -maxAbs),
    max: maxAbs,
    valueSuffix: '%',
    title: 'DEPOSIT GROWTH COMPARISON',
  });

  const items = [
    { label: 'Market Growth', value: d.metrics.marketGrowth, color: C.teal },
    { label: 'Opportunity', value: d.avgScore, color: C.navy },
    { label: 'Density', value: d.metrics.invDensity, color: C.amber },
  ];

  items.forEach((it, i) => {
    addGauge(s, 6.55, 4.12 + i * 0.34, 3.0, 0.12, it.value, it.label.toUpperCase(), it.color);
  });

  if (d.competitor) {
    rect(s, 0.72, 4.12, 5.55, 0.58, C.justifyL, C.justify);

    addText(s, `Key competitor: ${d.competitor.name} · ${d.competitor.branches} overlap branches · ${pct(d.competitor.yoy)} YoY`, {
      x: 0.86, y: 4.28, w: 5.25, h: 0.25,
      fontSize: 9.2,
      bold: true,
      color: C.justify,
    });
  }
}

function slideZoneStrategy(pres, d) {
  const s = pres.addSlide();
  s.background = { color: C.navy };

  rect(s, 0, 0, 0.12, 5.625, C.teal);

  addText(s, pp(d.gap), {
    x: 0.38, y: 0.35, w: 4.7, h: 1.1,
    fontSize: 76,
    bold: true,
    color: d.gap < 0 ? 'F87171' : C.teal,
  });

  addText(s, 'DEPOSIT GROWTH GAP VS PEERS', {
    x: 0.42, y: 1.62, w: 4.8, h: 0.3,
    fontSize: 12,
    bold: true,
    color: C.white,
    charSpacing: 2.2,
  });

  addText(s, `${d.bankName} is growing deposits at ${pct(d.bankYoY)} versus peers at ${pct(d.peerYoY)}. The core question is where marketing can close the gap fastest.`, {
    x: 0.42, y: 2.15, w: 4.9, h: 0.75,
    fontSize: 14,
    color: 'B8C3D1',
    italic: true,
  });

  const actionRows = [
    ['Invest', `${d.zones.Invest} branches`, 'Acquire and grow balances'],
    ['Analyze', `${d.zones.Analyze} branches`, 'Diagnose leakage before spend'],
    ['Defend', `${d.zones.Defend} branches`, 'Protect high-value households'],
    ['Justify', `${d.zones.Justify} branches`, 'Limit spend unless strategic'],
  ];

  actionRows.forEach((r, i) => {
    const y = 3.18 + i * 0.45;

    rect(s, 0.42, y, 4.7, 0.32, C.navy2, '1E3A5F');
    rect(s, 0.42, y, 0.06, 0.32, ZONE_COLOR[r[0]], ZONE_COLOR[r[0]]);

    addText(s, r[0], {
      x: 0.58, y: y + 0.07, w: 0.8, h: 0.18,
      fontSize: 8.5,
      bold: true,
      color: ZONE_COLOR[r[0]],
    });

    addText(s, r[1], {
      x: 1.55, y: y + 0.07, w: 1.0, h: 0.18,
      fontSize: 8.5,
      color: C.white,
    });

    addText(s, r[2], {
      x: 2.7, y: y + 0.07, w: 2.2, h: 0.18,
      fontSize: 8.5,
      color: 'B8C3D1',
    });
  });

  addBarGraph(s, d.topBranches, {
    x: 5.65, y: 0.85, w: 3.9, h: 3.55,
    labelKey: 'name',
    valueKey: 'score',
    max: 100,
    title: 'TOP PRIORITY SCORES',
    maxItems: 7,
  });

  addText(s, 'Verlocity · BMAP Intelligence', {
    x: 0.42, y: 5.25, w: 4.8, h: 0.2,
    fontSize: 7.5,
    color: '52657A',
  });

  addText(s, '4', {
    x: 9.45, y: 5.25, w: 0.3, h: 0.2,
    fontSize: 8.5,
    color: '52657A',
    align: 'right',
  });
}

function slideNextSteps(pres, d, n) {
  const s = pres.addSlide();
  s.background = { color: C.white };

  addChrome(pres, s, 5, 'NEXT STEPS');
  addTitle(s, n.nextsteps.headline, n.nextsteps.spoken);

  addBullets(s, n.nextsteps.bullets, 0.72, 1.82, 5.55, 1.1, 10);
  addCloseBar(s, n.nextsteps.close, 0.72, 3.15, 5.55);

  const actions = [
    {
      title: 'Prioritise branch campaigns',
      body: `Start with ${d.topBranches.slice(0, 3).map(b => b.name).join(', ')}.`,
      color: C.teal,
    },
    {
      title: 'Define offer strategy',
      body: 'Align rate, messaging, and channel by branch zone and competitor pressure.',
      color: C.blue,
    },
    {
      title: 'Measure conversion',
      body: 'Track campaign response, deposit lift, and peer growth gap every 2 weeks.',
      color: C.amber,
    },
    {
      title: 'Build BMAP growth loop',
      body: 'Use results to refine scores, personas, and next campaign investment.',
      color: C.navy,
    },
  ];

  actions.forEach((a, i) => {
    const y = 0.28 + i * 1.22;

    rect(s, 6.48, y, 3.35, 1.0, C.gray1, C.gray2);
    rect(s, 6.48, y, 0.07, 1.0, a.color, a.color);
    rect(s, 6.68, y + 0.34, 0.34, 0.34, a.color, a.color);

    addText(s, String(i + 1), {
      x: 6.68, y: y + 0.41, w: 0.34, h: 0.16,
      fontSize: 8.5,
      bold: true,
      color: C.white,
      align: 'center',
    });

    addText(s, a.title, {
      x: 7.18, y: y + 0.15, w: 2.45, h: 0.24,
      fontSize: 11,
      bold: true,
      color: C.navy,
    });

    addText(s, a.body, {
      x: 7.18, y: y + 0.45, w: 2.45, h: 0.38,
      fontSize: 8.5,
      color: C.gray4,
      valign: 'top',
    });
  });
}

// ===============================
// MAIN EXPORT
// ===============================
async function snapshotExport() {
  const btn = document.getElementById('snapBtn');
  const ld = document.getElementById('snapLd');
  const oldText = btn ? btn.textContent : '';

  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Building BMAP deck...';
    }

    if (ld) ld.classList.add('on');

    const data = buildData();
    const narratives = await getNarratives(data);

    const pres = new PptxGenJS();
    pres.layout = 'LAYOUT_16x9';
    pres.author = 'Verlocity Princeton Partners Group';
    pres.subject = 'BMAP Market Snapshot';
    pres.title = `BMAP Snapshot — ${data.bankName}`;
    pres.company = 'Verlocity Princeton Partners Group';
    pres.lang = 'en-US';

    slideCover(pres, data);
    slideNetwork(pres, data, narratives);
    slideBranches(pres, data, narratives);
    slideFinancial(pres, data, narratives);
    slideZoneStrategy(pres, data);
    slideNextSteps(pres, data, narratives);

    const fileName = `BMAP_Snapshot_${safeFileName(data.bankName)}_${Date.now()}.pptx`;

    await pres.writeFile({ fileName });

    if (btn) {
      btn.textContent = '✓ Downloaded';
      btn.style.background = '#1D9E75';

      setTimeout(() => {
        btn.textContent = oldText || '⬇ Generate & Export BMAP Snapshot';
        btn.style.background = '';
        btn.disabled = false;
      }, 2500);
    }
  } catch (err) {
    console.error('snapshotExport failed:', err);
    alert('PowerPoint export failed: ' + (err?.message || err));

    if (btn) {
      btn.textContent = oldText || '⬇ Generate & Export BMAP Snapshot';
      btn.style.background = '';
      btn.disabled = false;
    }
  } finally {
    if (ld) ld.classList.remove('on');
  }
}

window.snapshotExport = snapshotExport;
