const $ = id => document.getElementById(id);
const j = async (url, opt = {}) => {
  const r = await fetch(url, { credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, ...opt });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
  return d;
};
const msg = (t, bad) => { $('msg').textContent = t; $('msg').className = bad ? 'pr-err' : 'pr-muted'; };

function row(p = {}) {
  const d = document.createElement('div'); d.className = 'pr-row';
  const f = (k, label, type, val, w) => {
    const l = document.createElement('label'); l.textContent = label + ' ';
    const i = document.createElement('input'); i.type = type; i.dataset.k = k; i.value = val ?? ''; if (w) i.style.width = w;
    if (type === 'number') { i.min = 0; i.step = 'any'; }
    l.append(i); return l;
  };
  d.append(f('id', 'Id', 'text', p.id, '7rem'), f('name', 'Name', 'text', p.name, '8rem'),
    f('price_monthly', 'Price / month', 'number', p.price_monthly ?? 0, '7rem'),
    f('requests_per_month', 'Requests / month', 'number', p.requests_per_month ?? 0, '9rem'));
  const hl = document.createElement('label'); const c = document.createElement('input'); c.type = 'checkbox'; c.dataset.k = 'highlight'; c.checked = !!p.highlight;
  hl.append(c, ' Highlight'); d.append(hl);
  const ft = document.createElement('label'); ft.className = 'pr-wide'; ft.textContent = 'Features (one per line) ';
  const ta = document.createElement('textarea'); ta.dataset.k = 'features'; ta.rows = 3; ta.value = (p.features || []).join('\n'); ft.append(ta); d.append(ft);
  const rm = document.createElement('button'); rm.type = 'button'; rm.className = 'pr-btn pr-ghost'; rm.textContent = 'Remove'; rm.onclick = () => d.remove(); d.append(rm);
  return d;
}

function collect() {
  return { currency: ($('cur').value || 'USD').toUpperCase(), plans: [...$('rows').children].map(d => {
    const v = k => d.querySelector(`[data-k=${k}]`);
    return { id: v('id').value.trim(), name: v('name').value.trim(), price_monthly: +v('price_monthly').value,
      requests_per_month: Math.round(+v('requests_per_month').value), highlight: v('highlight').checked,
      features: v('features').value.split('\n').map(s => s.trim()).filter(Boolean) };
  }) };
}

async function openEditor(u) {
  $('login').hidden = true; $('editor').hidden = false; $('who').textContent = 'Signed in as ' + u;
  const d = await fetch('/v1/pricing').then(r => r.json());
  $('cur').value = d.currency; $('rows').textContent = ''; d.plans.forEach(p => $('rows').append(row(p)));
}

$('login').onsubmit = async e => {
  e.preventDefault(); $('lerr').textContent = '';
  try { const r = await j('/v1/pricing/admin/login', { method: 'POST', body: JSON.stringify({ username: $('u').value, password: $('p').value }) }); $('p').value = ''; openEditor(r.user); }
  catch (err) { $('lerr').textContent = err.message; }
};

$('out').onclick = async () => { await j('/v1/pricing/admin/logout', { method: 'POST' }); location.reload(); };
$('add').onclick = () => $('rows').append(row());
$('save').onclick = async () => {
  try { const r = await j('/v1/pricing/admin/plans', { method: 'PUT', body: JSON.stringify(collect()) }); msg('Saved ' + r.plans + ' plans. Public page updates within a minute.'); }
  catch (err) { msg(err.message, true); }
};

const genBtn = $('genAdminKeyBtn');
if (genBtn) {
  genBtn.onclick = async () => {
    try {
      const r = await j('/v1/pricing/admin/key', { method: 'POST' });
      $('adminKeyCode').textContent = r.key;
      navigator.clipboard.writeText(r.key);
      msg('Generated Unlimited Admin Key and copied to clipboard!', false);
    } catch (err) {
      msg(err.message, true);
    }
  };
}

const copyBtn = $('copyAdminKeyBtn');
if (copyBtn) {
  copyBtn.onclick = () => {
    const k = $('adminKeyCode').textContent;
    if (k && k !== 'Click Generate') {
      navigator.clipboard.writeText(k);
      msg('Copied admin key to clipboard!', false);
    }
  };
}

j('/v1/pricing/admin/me').then(r => openEditor(r.user)).catch(() => { $('login').hidden = false; });
