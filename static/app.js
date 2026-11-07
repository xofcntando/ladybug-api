const $ = (s, r = document) => r.querySelector(s);
const el = (t, a = {}, ...kids) => { const e = document.createElement(t); for (const [k, v] of Object.entries(a)) k === 'class' ? e.className = v : k === 'html' ? e.innerHTML = v : k.startsWith('on') ? e.addEventListener(k.slice(2), v) : e.setAttribute(k, v); kids.flat().forEach(c => e.append(c?.nodeType ? c : document.createTextNode(c ?? ''))); return e; };
const getKey = () => localStorage.getItem('ladybug_key') || '';

let CALLS = [];
async function api(path, { method = 'GET', json, form, blob } = {}) {
  const call = { method, path, json, t0: performance.now() };
  CALLS.push(call);
  const h = { 'X-API-Key': getKey() };
  let body;
  if (json) { h['Content-Type'] = 'application/json'; body = JSON.stringify(json); }
  if (form) body = form;
  const r = await fetch(path, { method, headers: h, body });
  call.ms = Math.round(performance.now() - call.t0);
  call.status = r.status;
  if (!r.ok) {
    let d = ''; try { const j = await r.json(); d = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch { d = r.statusText; }
    const e = new Error(d || 'Request failed'); e.status = r.status; throw e;
  }
  const ct = r.headers.get('content-type') || '';
  if (blob || !ct.includes('json')) return { blob: await r.blob(), type: ct };
  return r.json();
}
const qs = o => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== '' && v != null)).toString();
const bytes = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
const mins = s => s ? `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}` : '';
function saveBlob(blob, name) { const u = URL.createObjectURL(blob); const a = el('a', { href: u, download: name }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 5000); }

/* ---------- renderers ---------- */
const R = {
  json: d => el('div', { class: 'card' }, copyBar('Response', JSON.stringify(d, null, 2)), el('pre', { class: 'json' }, JSON.stringify(d, null, 2))),
  text: d => el('div', { class: 'card' }, copyBar(`${d.provider || ''} ${d.model || ''}`.trim(), d.text), el('div', { class: 'text' }, d.text),
    d.seo ? el('pre', { class: 'json', style: 'margin-top:14px' }, JSON.stringify(d.seo, null, 2)) : ''),
  image: ({ blob, name }) => { const u = URL.createObjectURL(blob); return el('div', { class: 'card pic' }, el('div', { class: 'bar' }, el('span', {}, `${bytes(blob.size)}`), el('button', { class: 'sm', onclick: () => saveBlob(blob, name) }, 'Save image')), el('img', { src: u, alt: 'Result' })); },
  ascii: d => el('div', { class: 'card' }, copyBar('ASCII', d.ascii), el('pre', { class: 'json', style: 'line-height:1;font-size:8px' }, d.ascii)),
  audio: ({ blob, name }) => { const u = URL.createObjectURL(blob); return el('div', { class: 'card' }, el('audio', { src: u, controls: '', style: 'width:100%' }), el('div', { class: 'actions', style: 'margin-top:12px' }, el('button', { class: 'go', onclick: () => saveBlob(blob, name) }, 'Save MP3'))); },
  colors: d => el('div', { class: 'card' }, el('div', { class: 'swatches' }, d.colors.map(c => el('div', { class: 'sw' }, el('div', { style: `background:${c.hex}` }), el('div', {}, c.hex, el('br'), c.percent + '%'))))),
  audit: d => {
    const c = d.score >= 80 ? 'var(--ok)' : d.score >= 55 ? 'var(--warn)' : 'var(--red)';
    return el('div', { class: 'card' },
      el('div', { class: 'score' }, el('div', { class: 'ring', style: `--p:${d.score};--c:${c}` }, el('span', {}, d.score)), el('div', {}, el('b', {}, d.title || d.url), el('div', { class: 'meta' }, `${d.issues.length} to fix, ${d.passed.length} passed`))),
      d.issues.length ? [el('h4', {}, 'To fix'), el('ul', { class: 'list bad' }, d.issues.map(i => el('li', {}, i)))] : '',
      el('h4', {}, 'Passed'), el('ul', { class: 'list good' }, d.passed.map(i => el('li', {}, i))));
  },
  links: d => el('div', { class: 'card' }, el('p', {}, `Checked ${d.checked} links. `, el('b', {}, `${d.broken_count} broken.`)),
    el('table', {}, el('tr', {}, el('th', {}, 'Status'), el('th', {}, 'URL')), d.all.map(x => el('tr', {}, el('td', {}, el('span', { class: 'pill' + (x.status && x.status < 400 ? '' : ' bad') }, x.status ?? x.error)), el('td', { class: 'mono' }, x.url))))),
  music: d => {
    const items = d.results || [];
    const one = m => {
      const slot = el('div', { class: 'actions' });
      const btn = el('button', { class: 'sm', onclick: async () => {
        btn.disabled = true; btn.textContent = 'Fetching…';
        try {
          const j = await api('/v1/music/download?' + qs({ id: m.id }));
          const { blob } = await api(j.file.download_url, { blob: true });
          saveBlob(blob, j.file.filename); btn.textContent = 'Saved (' + bytes(j.file.size) + ')';
        } catch (e) { btn.textContent = 'Failed: ' + e.message; } btn.disabled = false;
      } }, 'Download');
      slot.append(btn, m.page_url ? el('a', { class: 'sm', href: m.page_url, target: '_blank', rel: 'noopener' }, 'Source page') : '');
      return el('div', { class: 'card media' }, m.thumbnail ? el('img', { src: m.thumbnail, alt: '', referrerpolicy: 'no-referrer' }) : el('div'),
        el('div', {}, el('h2', {}, m.title), el('div', { class: 'meta' }, [m.artist, mins(m.duration), m.source, m.license, m.commercial_use === false ? 'non-commercial' : ''].filter(Boolean).join('  ·  ')),
          m.preview_url && m.source !== 'archive' ? el('audio', { src: m.preview_url, controls: '', preload: 'none', style: 'width:100%;margin:8px 0' }) : '', slot,
          el('div', { class: 'meta mono', style: 'word-break:break-all' }, 'API: ' + location.origin + '/v1/music/download?id=' + m.id),
          el('div', { class: 'meta' }, m.attribution)));
    };
    return el('div', {}, d.errors ? el('div', { class: 'meta' }, 'Some sources failed: ' + JSON.stringify(d.errors)) : '', items.length ? items.map(one) : el('div', { class: 'empty card' }, 'Nothing found.'));
  },
  lyrics: d => el('div', { class: 'card' }, copyBar(`${d.title || ''} · ${d.artist || ''}`, d.lyrics || ''), el('div', { class: 'text', style: 'white-space:pre-wrap' }, d.lyrics || 'No lyrics found.')),
  musicfile: d => el('div', { class: 'card media' }, el('div'), el('div', {}, el('h2', {}, d.title), el('div', { class: 'meta' }, [d.artist, d.license].filter(Boolean).join('  ·  ')),
    (() => { const h = el('div', {}, 'Fetching file…'); api(d.file.download_url, { blob: true }).then(({ blob }) => h.replaceChildren(el('audio', { src: URL.createObjectURL(blob), controls: '' }), el('button', { class: 'go', onclick: () => saveBlob(blob, d.file.filename) }, `Save (${bytes(d.file.size)})`))).catch(e => h.replaceChildren(e.message)); return h; })(),
    el('div', { class: 'meta' }, d.attribution))),
  media: d => {
    const f = d.file, isA = d.type === 'audio';
    const holder = el('div', {}, el('div', { class: 'loading' }, el('i'), el('i'), el('i'), 'Fetching file…'));
    api(f.download_url, { blob: true }).then(({ blob }) => {
      const u = URL.createObjectURL(blob);
      holder.replaceChildren(el(isA ? 'audio' : 'video', { src: u, controls: '' }), el('button', { class: 'go', onclick: () => saveBlob(blob, f.filename) }, `Save ${isA ? 'MP3' : 'MP4'} (${bytes(f.size)})`));
    }).catch(e => holder.replaceChildren(el('div', { class: 'err card' }, e.message)));
    return el('div', { class: 'card media' }, d.thumbnail ? el('img', { src: d.thumbnail, alt: '' }) : el('div'), el('div', {}, el('h2', {}, d.title || f.filename), el('div', { class: 'meta' }, [d.uploader, mins(d.duration)].filter(Boolean).join('  ·  '), `  ·  link works for ${d.expires_in_minutes} min`), holder));
  },
  info: d => el('div', { class: 'card media' }, d.thumbnail ? el('img', { src: d.thumbnail, alt: '' }) : el('div'), el('div', {}, el('h2', {}, d.title), el('div', { class: 'meta' }, [d.uploader, mins(d.duration)].filter(Boolean).join('  ·  ')), d.heights?.length ? el('div', {}, 'Qualities: ' + d.heights.map(h => h + 'p').join(', ')) : '')),
  pic: d => el('div', { class: 'card pic' }, el('div', { class: 'bar' }, el('span', {}, [d.anime, d.artist && 'art by ' + d.artist, d.provider].filter(Boolean).join('  ·  ')), el('a', { class: 'sm', href: d.url, target: '_blank', rel: 'noopener' }, 'Open')), el('img', { src: d.url, alt: 'Result', referrerpolicy: 'no-referrer' })),
  anime: d => {
    const items = d.results || [d];
    const one = m => el('div', { class: 'card media' }, m.cover ? el('img', { src: m.cover, alt: '', referrerpolicy: 'no-referrer' }) : el('div'),
      el('div', {}, el('h2', {}, m.title || m.name), el('div', { class: 'meta' }, [m.format, m.year, m.episodes && m.episodes + ' eps', m.status, m.score && '★ ' + m.score + '/100', m.studio].filter(Boolean).join('  ·  ')),
        m.genres?.length ? el('div', { class: 'meta' }, m.genres.join(', ')) : '', m.description ? el('p', {}, m.description.length > 420 ? m.description.slice(0, 420) + '…' : m.description) : '',
        el('div', { class: 'actions' }, m.url ? el('a', { class: 'sm', href: m.url, target: '_blank', rel: 'noopener' }, 'AniList') : '', m.trailer ? el('a', { class: 'sm', href: m.trailer, target: '_blank', rel: 'noopener' }, 'Trailer') : '')));
    return el('div', {}, items.length ? items.map(one) : el('div', { class: 'empty card' }, 'Nothing found.'));
  },
  character: d => el('div', { class: 'card media' }, d.image ? el('img', { src: d.image, alt: '', referrerpolicy: 'no-referrer' }) : el('div'),
    el('div', {}, el('h2', {}, d.name), el('div', { class: 'meta' }, [d.native, d.gender, d.age && 'age ' + d.age].filter(Boolean).join('  ·  ')), el('p', {}, (d.description || '').slice(0, 500)),
      d.appears_in?.length ? el('div', { class: 'meta' }, 'Appears in: ' + d.appears_in.join(', ')) : '')),
  quote: d => el('div', { class: 'card' }, copyBar(`${d.character} · ${d.anime}`, d.quote), el('div', { class: 'text' }, '“' + d.quote + '”')),
  airing: d => el('div', { class: 'card' }, el('table', {}, el('tr', {}, ['Anime', 'Episode', 'Airs'].map(h => el('th', {}, h))), d.results.map(x => el('tr', {}, el('td', {}, el('a', { href: x.url, target: '_blank', rel: 'noopener' }, x.title)), el('td', {}, x.episode), el('td', {}, new Date(x.airs_at).toLocaleString()))))),
  trace: d => el('div', {}, d.results.length ? d.results.map(x => el('div', { class: 'card media' }, x.image ? el('img', { src: x.image, alt: '' }) : el('div'), el('div', {}, el('h2', {}, x.anime || 'Unknown'),
    el('div', { class: 'meta' }, `Episode ${x.episode ?? '?'}  ·  ${mins(x.from_sec)} - ${mins(x.to_sec)}  ·  ${(x.similarity * 100).toFixed(1)}% match`), x.preview ? el('video', { src: x.preview, controls: '', muted: '' }) : ''))) : el('div', { class: 'empty card' }, 'No match.')),
  dict: d => el('div', { class: 'card' },
    copyBar(d.word, (d.meanings || []).map(m => `${m.part_of_speech}: ` + m.definitions.map(x => x.definition).join(' / ')).join('\n')),
    el('h2', {}, d.word, ' ', el('span', { class: 'meta' }, d.phonetic || '')),
    d.audio ? el('audio', { src: d.audio, controls: '', style: 'width:100%;margin:6px 0 10px' }) : '',
    (d.meanings || []).map(m => el('div', { style: 'margin-bottom:12px' }, el('b', {}, m.part_of_speech || ''),
      el('ol', {}, m.definitions.slice(0, 6).map(x => el('li', {}, x.definition, x.example ? el('div', { class: 'meta' }, '“' + x.example + '”') : ''))),
      m.synonyms?.length ? el('div', { class: 'meta' }, 'Synonyms: ' + m.synonyms.slice(0, 10).join(', ')) : '')),
    d.related ? el('div', { class: 'meta' }, ['synonyms', 'antonyms', 'rhymes'].filter(k => d.related[k]?.length).map(k => `${k}: ${d.related[k].join(', ')}`).join('\n')) : ''),
  trivia: d => el('div', {}, d.questions.map((q, i) => el('div', { class: 'card' },
    el('div', { class: 'meta' }, `${i + 1}. ${q.category} · ${q.difficulty}`), el('div', { class: 'text' }, q.question),
    el('ul', {}, q.answers.map(a => el('li', {}, a))),
    el('details', {}, el('summary', {}, 'Show answer'), el('b', {}, q.correct_answer))))),
  song: d => el('div', {}, d.results.map(x => el('div', { class: 'card media' }, x.cover ? el('img', { src: x.cover, alt: '' }) : el('div'),
    el('div', {}, el('h2', {}, x.title), el('div', { class: 'meta' }, [x.artist, x.album, x.release_date, x.genre, mins(x.duration)].filter(Boolean).join('  ·  ')),
      x.preview ? el('audio', { src: x.preview, controls: '', style: 'width:100%;margin:8px 0' }) : '',
      el('div', {}, Object.entries(x.links || {}).filter(([, u]) => u).map(([n, u]) => el('a', { href: u, target: '_blank', rel: 'noopener', style: 'margin-right:12px' }, n)))))) ),
  time: d => el('div', { class: 'card' }, copyBar(d.timezone, d.local_time),
    el('h2', {}, d.time_12h, ' ', el('span', { class: 'meta' }, `${d.abbreviation} · ${d.utc_offset}${d.dst ? ' · daylight saving' : ''}`)),
    el('div', {}, [d.place?.name, d.place?.country].filter(Boolean).join(', ') || d.timezone), el('div', { class: 'meta' }, d.date),
    d.sun ? el('div', { class: 'meta', style: 'margin-top:8px' }, `Sunrise ${d.sun.sunrise.slice(11)} · Sunset ${d.sun.sunset.slice(11)} · Day ${d.sun.day_length}`) : ''),
  files: async () => {
    const d = await api('/v1/files');
    const box = el('div', { class: 'card' });
    const draw = items => { box.replaceChildren(items.length ? el('table', {}, el('tr', {}, ['Name', 'Size', 'Expires', ''].map(h => el('th', {}, h))), items.map(f => el('tr', {},
      el('td', {}, f.filename), el('td', {}, bytes(f.size)), el('td', {}, f.expires_at ? new Date(f.expires_at * 1000).toLocaleTimeString() : 'never'),
      el('td', {}, el('button', { class: 'sm', onclick: async () => { const { blob } = await api(f.download_url, { blob: true }); saveBlob(blob, f.filename); } }, 'Download'), ' ',
        el('button', { class: 'sm danger', onclick: async () => { await api('/v1/files/' + f.id, { method: 'DELETE' }); draw(items.filter(x => x.id !== f.id)); } }, 'Delete'))))) : el('div', { class: 'empty' }, 'No files yet. Upload one or run a download.')); };
    draw(d.files); return box;
  },
};
function copyBar(label, text) { return el('div', { class: 'bar' }, el('span', {}, label), el('button', { class: 'sm', onclick: e => { navigator.clipboard.writeText(text); e.target.textContent = 'Copied'; setTimeout(() => e.target.textContent = 'Copy', 1500); } }, 'Copy')); }

/* ---------- tool definitions ---------- */
const URL_F = (ph = 'https://…') => ({ k: 'url', label: 'Page URL', ph });
const T = (k, label, o = {}) => ({ k, label, type: 'text', ...o });
const S = (k, label, opts, o = {}) => ({ k, label, type: 'select', opts, ...o });
const Q = [['360', '360p'], ['480', '480p'], ['720', '720p (HD)'], ['1080', '1080p (Full HD)']];
const IMG = f => async v => {
  const j = { url: v.url, format: v.format, quality: +v.quality || 85 };
  if (v.op === 'resize') Object.assign(j, { width: +v.width || null, height: +v.height || null, fit: v.fit });
  if (v.op === 'blur') j.level = +v.level || 10;
  if (v.op === 'circle') j.size = +v.size || 512;
  if (v.op === 'pixelate') j.size = +v.pix || 16;
  if (v.op === 'upscale') j.scale = +v.scale || 2;
  if (v.op === 'meme') Object.assign(j, { top: v.top, bottom: v.bottom });
  if (v.op === 'ascii') { const d = await api('/v1/image/ascii', { method: 'POST', json: { url: v.url, width: +v.awidth || 80 } }); return { kind: 'ascii', data: d }; }
  const r = await api(f(v), { method: 'POST', json: j, blob: true });
  return { kind: 'image', data: { blob: r.blob, name: 'ladybug-image.' + (v.format || 'png') } };
};
const post = (path, map = v => v, kind = 'json') => async v => ({ kind, data: await api(path, { method: 'POST', json: map(v) }) });
const get = (path, kind = 'json') => async v => ({ kind, data: await api(path + '?' + qs(v)) });

const CARD = kind => ({ g: 'Cards', id: kind, name: kind === 'welcome' ? 'Welcome card' : 'Goodbye card', desc: `A Ladybug Crafting ${kind} image for your group. Add the member’s photo link, name and group.`, submit: 'Create card',
  fields: [T('username', 'Username', { def: kind === 'welcome' ? 'New Member' : 'Old Friend' }), T('avatar', 'Profile picture URL (optional)', { ph: 'https://…/avatar.jpg' }), T('group', 'Group name', { ph: 'Ladybug Crafting HQ' }), { k: 'count', label: 'Member number', type: 'number' },
    T('message', 'Custom message (optional)'), T('background', 'Background image URL (optional)'), S('theme', 'Theme', ['ladybug', 'dark', 'light', 'sakura', 'ocean', 'forest'].map(x => [x, x])), S('format', 'Output', [['png', 'PNG'], ['jpeg', 'JPEG'], ['webp', 'WebP']]), S('brand', 'Ladybug Crafting footer', [['true', 'Show'], ['false', 'Hide']])],
  run: async v => { const j = { username: v.username || 'Member', avatar: v.avatar || null, group: v.group || null, count: +v.count || null, message: v.message || null, background: v.background || null, theme: v.theme, format: v.format, brand: v.brand !== 'false' };
    const r = await api('/v1/' + kind, { method: 'POST', json: j, blob: true }); return { kind: 'image', data: { blob: r.blob, name: `${kind}.${v.format}` } }; } });

const TOOLS = [
  { g: 'Media', id: 'dl', name: 'Download audio or video', desc: 'Paste a YouTube, TikTok, Instagram, Facebook, X, SoundCloud or Vimeo link. Get an MP3 or MP4 you can play and save.',
    fields: [T('url', 'Link', { ph: 'https://www.youtube.com/watch?v=…' }), S('type', 'Format', [['audio', 'MP3 audio'], ['video', 'MP4 video']]), S('quality', 'Video quality', Q, { def: '720', show: v => v.type === 'video' })], submit: 'Download',
    run: async v => ({ kind: 'media', data: await api('/v1/media/download?' + qs({ url: v.url, type: v.type, quality: v.quality })) }) },
  { g: 'Media', id: 'info', name: 'Video info', desc: 'Title, length, thumbnail and available qualities without downloading.', fields: [T('url', 'Link')], submit: 'Get info', run: get('/v1/media/info', 'info') },

  { g: 'Music', id: 'm-search', name: 'Free music search', desc: 'Search openly licensed music (Internet Archive, ccMixter, Openverse, Audius CC tracks). Preview, download, and copy the attribution line.',
    fields: [T('q', 'Song, artist or mood', { ph: 'ambient piano' }), S('source', 'Source', [['all', 'All sources'], ['archive', 'Internet Archive'], ['ccmixter', 'ccMixter'], ['openverse', 'Openverse'], ['audius', 'Audius (CC)']]), S('commercial_only', 'Licence', [['false', 'Any open licence'], ['true', 'Commercial use allowed']]), { k: 'limit', label: 'Results per source', type: 'number', def: 5 }], submit: 'Search', run: get('/v1/music/search', 'music') },
  { g: 'Music', id: 'm-trend', name: 'Trending free music', desc: 'Popular Creative Commons tracks on Audius right now.', fields: [{ k: 'limit', label: 'Results', type: 'number', def: 10 }], submit: 'Show', run: get('/v1/music/trending', 'music') },
  { g: 'Music', id: 'm-dl', name: 'Download by ID', desc: 'Use an ID from the search, like archive:confluence-1 or ccmixter:22364.', fields: [T('id', 'Track ID', { ph: 'ccmixter:22364' })], submit: 'Download',
    run: async v => ({ kind: 'musicfile', data: await api('/v1/music/download?' + qs({ id: v.id })) }) },
  { g: 'Music', id: 'm-sources', name: 'Music sources', desc: 'The free music catalogues that are searched and their licences.', fields: [], submit: 'Show sources', run: get('/v1/music/sources') },
  { g: 'Music', id: 'm-info', name: 'Track details', desc: 'Licence, attribution and files for one track ID from the search.', fields: [T('id', 'Track ID', { ph: 'ccmixter:22364' })], submit: 'Get details', run: get('/v1/music/info') },

  { g: 'Scrape', id: 'page', name: 'Scrape a page', desc: 'Title, meta tags, headings, links, images, structured data and clean text from any public page.', fields: [URL_F()], submit: 'Scrape', run: post('/v1/scrape/page') },
  { g: 'Scrape', id: 'product', name: 'Product data', desc: 'Name, price, currency, stock and rating from a product page.', fields: [URL_F('https://shop.com/products/…')], submit: 'Extract', run: post('/v1/scrape/product') },
  { g: 'Scrape', id: 'extract', name: 'CSS selectors', desc: 'One per line as name: selector. Add @attr to read an attribute, for example link: a.buy@href', fields: [URL_F(), { k: 'sel', label: 'Selectors', type: 'textarea', ph: 'title: h1\nprice: .price\nimage: img.main@src' }], submit: 'Extract',
    run: post('/v1/scrape/extract', v => ({ url: v.url, selectors: Object.fromEntries(v.sel.split('\n').map(l => l.split(/:(.*)/s).slice(0, 2).map(x => x.trim())).filter(p => p[0] && p[1])) })) },
  { g: 'Scrape', id: 'aiex', name: 'AI extract', desc: 'Name the fields you want. The AI reads the page and returns them as JSON.', fields: [URL_F(), T('fields', 'Fields (comma separated)', { ph: 'name, price, sku, brand' })], submit: 'Extract',
    run: post('/v1/scrape/ai-extract', v => ({ url: v.url, fields: v.fields.split(',').map(x => x.trim()).filter(Boolean) })) },
  { g: 'Scrape', id: 'sitemap', name: 'Sitemap URLs', desc: 'List every URL in a site’s sitemap.', fields: [T('url', 'Site or sitemap URL', { ph: 'https://shop.com' }), { k: 'limit', label: 'Max URLs', type: 'number', def: 200 }], submit: 'List URLs', run: get('/v1/scrape/sitemap') },

  { g: 'Images', id: 'img', name: 'Edit an image', desc: 'Resize, convert, compress, grayscale, blur or crop to a circle.', submit: 'Process',
    fields: [T('url', 'Image URL', { ph: 'https://…/photo.jpg' }), S('op', 'Action', [['resize', 'Resize'], ['convert', 'Convert / compress'], ['grayscale', 'Grayscale'], ['blur', 'Blur'], ['circle', 'Circle crop'], ['pixelate', 'Pixelate'], ['upscale', 'Upscale'], ['meme', 'Meme text'], ['ascii', 'ASCII art']]),
      { k: 'width', label: 'Width (px)', type: 'number', show: v => v.op === 'resize' }, { k: 'height', label: 'Height (px)', type: 'number', show: v => v.op === 'resize' },
      S('fit', 'Fit', [['contain', 'Keep whole image'], ['cover', 'Fill and crop'], ['stretch', 'Stretch']], { show: v => v.op === 'resize' }),
      { k: 'level', label: 'Blur level (1-100)', type: 'number', def: 20, show: v => v.op === 'blur' }, { k: 'size', label: 'Size (px)', type: 'number', def: 512, show: v => v.op === 'circle' },
      { k: 'pix', label: 'Pixel size', type: 'number', def: 16, show: v => v.op === 'pixelate' }, { k: 'scale', label: 'Scale (2-4x)', type: 'number', def: 2, show: v => v.op === 'upscale' },
      T('top', 'Top text', { show: v => v.op === 'meme' }), T('bottom', 'Bottom text', { show: v => v.op === 'meme' }), { k: 'awidth', label: 'Characters wide', type: 'number', def: 80, show: v => v.op === 'ascii' },
      S('format', 'Output', [['png', 'PNG'], ['jpeg', 'JPEG'], ['webp', 'WebP']]), { k: 'quality', label: 'Quality', type: 'number', def: 85, show: v => v.format !== 'png' }],
    run: v => IMG(x => '/v1/image/' + x.op)(v) },
  { g: 'Images', id: 'colors', name: 'Dominant colors', desc: 'Pull the main colors from an image.', fields: [T('url', 'Image URL'), { k: 'count', label: 'Colors', type: 'number', def: 5 }], submit: 'Find colors', run: post('/v1/image/colors', v => ({ url: v.url, count: +v.count }), 'colors') },
  { g: 'Images', id: 'qr', name: 'QR code', desc: 'Turn a link or text into a QR code.', fields: [T('data', 'Link or text', { ph: 'https://yourshop.com' }), { k: 'size', label: 'Size (px)', type: 'number', def: 512 }, T('fg', 'Code color', { def: 'black' }), T('bg', 'Background', { def: 'white' })], submit: 'Make QR',
    run: async v => { const r = await api('/v1/qr?' + qs(v), { blob: true }); return { kind: 'image', data: { blob: r.blob, name: 'qr.png' } }; } },

  { g: 'SEO', id: 'audit', name: 'Page audit', desc: 'Checks title, description, headings, canonical, alt text, social tags and structured data. Gives a score and a fix list.', fields: [URL_F()], submit: 'Run audit', run: post('/v1/seo/audit', v => v, 'audit') },
  { g: 'SEO', id: 'links', name: 'Broken links', desc: 'Checks the links on a page and lists the ones that fail.', fields: [URL_F(), { k: 'max_links', label: 'Links to check', type: 'number', def: 30 }], submit: 'Check links', run: post('/v1/seo/links-check', v => ({ url: v.url, max_links: +v.max_links }), 'links') },

  { g: 'AI', id: 'chat', name: 'Ask AI', desc: 'Send any prompt.', fields: [{ k: 'prompt', label: 'Prompt', type: 'textarea' }, T('system', 'Instructions (optional)', { ph: 'You are a helpful shop assistant' })], submit: 'Ask', run: post('/v1/ai/chat', v => v, 'text') },
  { g: 'AI', id: 'ask', name: 'Quick question', desc: 'Short one-shot question (800 characters max). Handy for bots.', fields: [T('q', 'Question'), T('system', 'Instructions (optional)')], submit: 'Ask', run: get('/v1/ai/ask', 'text') },
  { g: 'AI', id: 'aiimg', name: 'AI image', desc: 'Generate an image from a description.', fields: [{ k: 'prompt', label: 'Describe the image', type: 'textarea', ph: 'A ceramic mug on a sunlit wooden table, product photo' }, S('size', 'Shape', [['1024x1024', 'Square'], ['1536x1024', 'Landscape'], ['1024x1536', 'Portrait']])], submit: 'Generate',
    run: async v => { const r = await api('/v1/ai/image', { method: 'POST', json: { prompt: v.prompt, size: v.size }, blob: true }); return { kind: 'image', data: { blob: r.blob, name: 'ai-image.png' } }; } },
  { g: 'AI', id: 'tts', name: 'Text to speech', desc: 'Turn text into an MP3 voice-over.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }, S('voice', 'Voice', ['alloy', 'ash', 'coral', 'echo', 'fable', 'nova', 'onyx', 'sage', 'shimmer', 'hu-HU-NoemiNeural', 'hu-HU-TamasNeural', 'es-ES-ElviraNeural', 'fr-FR-DeniseNeural', 'de-DE-KatjaNeural', 'ja-JP-NanamiNeural', 'pt-BR-FranciscaNeural'].map(x => [x, x]))], submit: 'Speak',
    run: async v => { const r = await api('/v1/ai/tts', { method: 'POST', json: { text: v.text, voice: v.voice }, blob: true }); return { kind: 'audio', data: { blob: r.blob, name: 'speech.mp3' } }; } },
  { g: 'AI', id: 'mod', name: 'Safety check', desc: 'Flags unsafe or adult text and images. Use it on customer uploads.', fields: [{ k: 'text', label: 'Text (optional)', type: 'textarea' }, T('image_url', 'Image URL (optional)')], submit: 'Check', run: post('/v1/ai/moderate', v => ({ text: v.text || null, image_url: v.image_url || null })) },
  { g: 'AI', id: 'desc', name: 'Product description', desc: 'Write a description for a product.', fields: [T('product_name', 'Product'), { k: 'details', label: 'Details', type: 'textarea', ph: 'Material, size, who it is for…' }, T('tone', 'Tone', { def: 'friendly and persuasive' }), T('language', 'Language', { def: 'English' }), S('length', 'Length', [['short', 'Short'], ['medium', 'Medium'], ['long', 'Long']], { def: 'medium' })], submit: 'Write', run: post('/v1/ai/product-description', v => v, 'text') },
  { g: 'AI', id: 'seo', name: 'SEO title and description', desc: 'Meta title, meta description and keywords from page content.', fields: [{ k: 'content', label: 'Page or product text', type: 'textarea' }, T('language', 'Language', { def: 'English' })], submit: 'Generate', run: post('/v1/ai/seo-meta', v => v, 'text') },
  { g: 'AI', id: 'reply', name: 'Customer reply', desc: 'Draft an answer to a customer. Nothing is sent.', fields: [{ k: 'message', label: 'Customer message', type: 'textarea' }, { k: 'context', label: 'Facts to use', type: 'textarea', ph: 'Order status, return policy…' }, T('tone', 'Tone', { def: 'warm and professional' })], submit: 'Draft reply', run: post('/v1/ai/customer-reply', v => v, 'text') },
  { g: 'AI', id: 'trans', name: 'Translate', desc: 'Translate text into any language.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }, T('target_language', 'Translate to', { ph: 'Hungarian' })], submit: 'Translate', run: post('/v1/ai/translate', v => v, 'text') },
  { g: 'AI', id: 'sum', name: 'Summarize', desc: 'Shorten long text.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }], submit: 'Summarize', run: post('/v1/ai/summarize', v => v, 'text') },
  { g: 'AI', id: 'rewrite', name: 'Rewrite', desc: 'Rewrite text with your instruction.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }, T('instruction', 'Instruction', { def: 'Make it shorter and more persuasive' })], submit: 'Rewrite', run: post('/v1/ai/rewrite', v => v, 'text') },
  { g: 'AI', id: 'class', name: 'Classify', desc: 'Sort text into one of your labels.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }, T('labels', 'Labels (comma separated)', { ph: 'complaint, question, praise' })], submit: 'Classify', run: post('/v1/ai/classify', v => ({ text: v.text, labels: v.labels.split(',').map(x => x.trim()).filter(Boolean) })) },

  { g: 'Anime', id: 'a-search', name: 'Search anime or manga', desc: 'Find any title with score, episodes, studio, genres and trailer.', fields: [T('q', 'Title', { ph: 'Steins;Gate' }), S('type', 'Type', [['ANIME', 'Anime'], ['MANGA', 'Manga']]), { k: 'limit', label: 'Results', type: 'number', def: 5 }], submit: 'Search', run: get('/v1/anime/search', 'anime') },
  { g: 'Anime', id: 'a-info', name: 'Anime details', desc: 'Full details of the best match for a name.', fields: [T('q', 'Title', { ph: 'Cowboy Bebop' }), S('type', 'Type', [['ANIME', 'Anime'], ['MANGA', 'Manga']])], submit: 'Get details', run: get('/v1/anime/info', 'anime') },
  { g: 'Anime', id: 'a-top', name: 'Top charts', desc: 'Best rated, most popular or trending right now.', fields: [S('by', 'Chart', [['score', 'Top rated'], ['popularity', 'Most popular'], ['trending', 'Trending now']]), S('type', 'Type', [['ANIME', 'Anime'], ['MANGA', 'Manga']]), T('genre', 'Genre (optional)', { ph: 'Action' }), { k: 'limit', label: 'Results', type: 'number', def: 10 }], submit: 'Show chart', run: get('/v1/anime/top', 'anime') },
  { g: 'Anime', id: 'a-random', name: 'Random anime', desc: 'Can’t decide what to watch? Get a random popular title.', fields: [S('type', 'Type', [['ANIME', 'Anime'], ['MANGA', 'Manga']]), T('genre', 'Genre (optional)', { ph: 'Romance' })], submit: 'Surprise me', run: get('/v1/anime/random', 'anime') },
  { g: 'Anime', id: 'a-season', name: 'Seasonal anime', desc: 'What is airing this season, or any past one.', fields: [{ k: 'year', label: 'Year (blank = now)', type: 'number' }, S('season', 'Season', [['', 'Current'], ['WINTER', 'Winter'], ['SPRING', 'Spring'], ['SUMMER', 'Summer'], ['FALL', 'Fall']]), { k: 'limit', label: 'Results', type: 'number', def: 12 }], submit: 'Show season', run: get('/v1/anime/season', 'anime') },
  { g: 'Anime', id: 'a-airing', name: 'Airing schedule', desc: 'Episodes releasing in the next days, soonest first.', fields: [{ k: 'days', label: 'Next days', type: 'number', def: 3 }, { k: 'limit', label: 'Results', type: 'number', def: 20 }], submit: 'Show schedule', run: get('/v1/anime/airing', 'airing') },
  { g: 'Anime', id: 'a-char', name: 'Character lookup', desc: 'Profile, image and the shows a character appears in.', fields: [T('q', 'Character', { ph: 'Lelouch' })], submit: 'Look up', run: get('/v1/anime/character', 'character') },
  { g: 'Anime', id: 'a-quote', name: 'Random quote', desc: 'An anime quote, optionally from one show or character. The source allows about 5 per hour.', fields: [T('anime', 'Anime (optional)'), T('character', 'Character (optional)')], submit: 'Get quote', run: get('/v1/anime/quote', 'quote') },
  { g: 'Anime', id: 'a-image', name: 'Anime image', desc: 'A random safe-for-work anime picture.', fields: [S('category', 'Style', [['waifu', 'Waifu'], ['neko', 'Neko'], ['husbando', 'Husbando'], ['kitsune', 'Kitsune']])], submit: 'Get image', run: get('/v1/anime/image', 'pic') },
  { g: 'Anime', id: 'a-react', name: 'Reaction GIF', desc: 'Hug, pat, kiss, slap, dance and 35 more animated reactions.', fields: [S('action', 'Reaction', ['hug', 'pat', 'kiss', 'slap', 'cry', 'dance', 'laugh', 'happy', 'blush', 'wave', 'wink', 'smile', 'smug', 'poke', 'punch', 'kick', 'bite', 'cuddle', 'highfive', 'handhold', 'facepalm', 'shrug', 'nod', 'nope', 'pout', 'stare', 'sleep', 'yawn', 'bored', 'baka', 'think', 'thumbsup', 'tickle', 'feed', 'nom', 'peck', 'lurk', 'shoot', 'yeet', 'handshake'].map(x => [x, x]))], submit: 'Get GIF', run: get('/v1/anime/react', 'pic') },
  { g: 'Anime', id: 'a-trace', name: 'Find anime from a screenshot', desc: 'Paste a screenshot link. Returns the show, episode and timestamp.', fields: [T('url', 'Screenshot URL', { ph: 'https://…/screenshot.jpg' })], submit: 'Find it', run: get('/v1/anime/trace', 'trace') },
  { g: 'Anime', id: 'a-actions', name: 'Reaction list', desc: 'All reaction actions you can use with Reaction GIF.', fields: [], submit: 'Show actions', run: get('/v1/anime/actions') },

  CARD('welcome'), CARD('goodbye'),

  { g: 'More', id: 'x-lyrics', name: 'Lyrics', desc: 'Find song lyrics by title and artist.', fields: [T('query', 'Song', { ph: 'Blinding Lights The Weeknd' })], submit: 'Find lyrics', run: get('/v1/lyrics', 'lyrics') },
  { g: 'More', id: 'x-shot', name: 'Website screenshot', desc: 'PNG screenshot of any public page.', fields: [T('url', 'Page URL', { ph: 'https://yourshop.com' })], submit: 'Capture', run: async v => { const r = await api('/v1/screenshot?' + qs({ url: v.url }), { blob: true }); return { kind: 'image', data: { blob: r.blob, name: 'screenshot.png' } }; } },
  { g: 'More', id: 'x-rmbg', name: 'Remove background', desc: 'Cut the background out of a picture. Transparent PNG.', fields: [T('url', 'Image URL', { ph: 'https://…/photo.jpg' })], submit: 'Remove', run: async v => { const r = await api('/v1/image/removebg?' + qs({ url: v.url }), { blob: true }); return { kind: 'image', data: { blob: r.blob, name: 'no-background.png' } }; } },
  { g: 'More', id: 'x-rank', name: 'Rank card', desc: 'XP / level card for a member.', fields: [T('name', 'Name', { def: 'Sam' }), T('pic', 'Picture URL (optional)'), { k: 'level', label: 'Level', type: 'number', def: 12 }, { k: 'xp', label: 'XP', type: 'number', def: 740 }, { k: 'needed', label: 'XP needed', type: 'number', def: 1000 }, { k: 'rank', label: 'Rank #', type: 'number', def: 3 }, S('theme', 'Theme', [['dark', 'Dark'], ['aurora', 'Aurora']])], submit: 'Create card', run: async v => { const r = await api('/v1/cards/rank?' + qs(v), { blob: true }); return { kind: 'image', data: { blob: r.blob, name: 'rank.png' } }; } },
  { g: 'More', id: 'x-quote', name: 'Quote card', desc: 'A quote on a picture card.', fields: [{ k: 'text', label: 'Quote', type: 'textarea' }, T('author', 'Author'), T('pic', 'Picture URL (optional)'), S('theme', 'Theme', [['dark', 'Dark'], ['light', 'Light']])], submit: 'Create card', run: async v => { const r = await api('/v1/cards/quote?' + qs(v), { blob: true }); return { kind: 'image', data: { blob: r.blob, name: 'quote.png' } }; } },
  { g: 'More', id: 'x-wanted', name: 'Wanted poster', desc: 'Wanted poster with a reward.', fields: [T('name', 'Name', { def: 'Unknown' }), T('pic', 'Picture URL (optional)'), T('crime', 'Crime', { def: 'Too many puns' }), T('reward', 'Reward', { def: '$10,000' })], submit: 'Create poster', run: async v => { const r = await api('/v1/cards/wanted?' + qs(v), { blob: true }); return { kind: 'image', data: { blob: r.blob, name: 'wanted.png' } }; } },
  { g: 'More', id: 'x-jail', name: 'Jail card', desc: 'Mugshot behind bars.', fields: [T('name', 'Name', { def: 'Unknown' }), T('pic', 'Picture URL (optional)'), T('crime', 'Crime', { def: 'Too many puns' }), T('sentence', 'Sentence', { def: '30 DAYS' }), T('cell', 'Cell', { def: 'A-12' })], submit: 'Create card', run: async v => { const r = await api('/v1/cards/jail?' + qs(v), { blob: true }); return { kind: 'image', data: { blob: r.blob, name: 'jail.png' } }; } },
  { g: 'More', id: 'x-ytcard', name: 'YouTube card', desc: 'Now-playing style card from a YouTube link.', fields: [T('url', 'YouTube link', { ph: 'https://youtu.be/…' })], submit: 'Create card', run: async v => { const r = await api('/v1/cards/ytcard?' + qs({ url: v.url }), { blob: true }); return { kind: 'image', data: { blob: r.blob, name: 'ytcard.png' } }; } },
  { g: 'More', id: 'x-tweet', name: 'Tweet mock-up', desc: 'Tweet-style image for mock-ups and design drafts.', fields: [T('name', 'Name', { def: 'Sam' }), T('username', 'Username', { def: 'sam' }), { k: 'text', label: 'Text', type: 'textarea' }, T('pic', 'Picture URL (optional)'), S('verified', 'Verified', [['false', 'No'], ['true', 'Yes']]), { k: 'likes', label: 'Likes', type: 'number', def: 120 }, S('theme', 'Theme', [['dim', 'Dim'], ['dark', 'Dark'], ['light', 'Light']])], submit: 'Create image', run: async v => { const r = await api('/v1/fake/tweet?' + qs(v), { blob: true }); return { kind: 'image', data: { blob: r.blob, name: 'tweet.png' } }; } },
  { g: 'More', id: 'x-ytc', name: 'YouTube comment mock-up', desc: 'Comment-style image for mock-ups and design drafts.', fields: [T('name', 'Name', { def: 'Sam' }), { k: 'text', label: 'Comment', type: 'textarea' }, T('pic', 'Picture URL (optional)'), { k: 'likes', label: 'Likes', type: 'number', def: 42 }, T('time', 'Time', { def: '1 hour ago' })], submit: 'Create image', run: async v => { const r = await api('/v1/fake/ytcomment?' + qs(v), { blob: true }); return { kind: 'image', data: { blob: r.blob, name: 'ytcomment.png' } }; } },
  { g: 'More', id: 'x-fun', name: 'Fun lines', desc: 'Joke, meme, roast, insult, compliment or flirt line.', fields: [S('kind', 'Kind', ['joke', 'meme', 'roast', 'insult', 'compliment', 'flirt'].map(x => [x, x]))], submit: 'Get one', run: async v => ({ kind: 'json', data: await api('/v1/fun/' + v.kind) }) },
  { g: 'More', id: 'x-ytsearch', name: 'YouTube search', desc: 'Find a video by name. Copy the link into the downloader.', fields: [T('query', 'Search', { ph: 'shape of you' }), { k: 'limit', label: 'Results', type: 'number', def: 5 }], submit: 'Search', run: get('/v1/yt/search') },
  { g: 'More', id: 'x-play', name: 'Download by name', desc: 'Type a song or video name. Gets the first YouTube match as MP3 or MP4.', fields: [T('query', 'Name', { ph: 'shape of you' }), S('type', 'Format', [['audio', 'MP3 audio'], ['video', 'MP4 video']]), S('quality', 'Video quality', Q, { def: '480', show: v => v.type === 'video' })], submit: 'Download',
    run: async v => ({ kind: 'media', data: await api('/v1/yt/play?' + qs({ query: v.query, type: v.type, quality: v.quality })) }) },
  { g: 'More', id: 'x-ytvideo', name: 'YouTube video details', desc: 'Full metadata for a video: channel, stats, chapters, thumbnails and every format.', fields: [T('url', 'Video link', { ph: 'https://www.youtube.com/watch?v=…' })], submit: 'Get details', run: get('/v1/yt/video') },
  { g: 'More', id: 'x-ytformats', name: 'YouTube formats', desc: 'Just the audio and video formats and the best pick of each.', fields: [T('url', 'Video link', { ph: 'https://www.youtube.com/watch?v=…' })], submit: 'List formats', run: get('/v1/yt/formats') },

  { g: 'Utilities', id: 'mail', name: 'Email check', desc: 'Checks format, mail server and disposable domains. Does not contact the mailbox.', fields: [T('email', 'Email address')], submit: 'Check', run: get('/v1/utils/email-validate') },
  { g: 'Utilities', id: 'dns', name: 'DNS lookup', desc: 'Records for a domain, plus whether SPF and DMARC are set.', fields: [T('domain', 'Domain', { ph: 'yourshop.com' })], submit: 'Look up', run: get('/v1/utils/dns') },
  { g: 'Utilities', id: 'cur', name: 'Currency', desc: 'Live exchange rates.', fields: [T('from', 'From', { def: 'USD' }), T('to', 'To', { def: 'EUR' }), { k: 'amount', label: 'Amount', type: 'number', def: 100 }], submit: 'Convert', run: get('/v1/utils/currency') },
  { g: 'Utilities', id: 'slug', name: 'Slug', desc: 'Turn a title into a URL slug.', fields: [T('text', 'Title')], submit: 'Make slug', run: post('/v1/utils/slug') },
  { g: 'Utilities', id: 'hash', name: 'Hash text', desc: 'MD5, SHA-1, SHA-256 or SHA-512 of any text.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }, S('algo', 'Algorithm', ['sha256', 'sha512', 'sha1', 'md5'].map(x => [x, x]))], submit: 'Hash', run: async v => ({ kind: 'json', data: await api('/v1/utils/hash?' + qs({ algo: v.algo }), { method: 'POST', json: { text: v.text } }) }) },
  { g: 'Utilities', id: 'uuid', name: 'UUID generator', desc: 'Random v4 UUIDs.', fields: [{ k: 'count', label: 'How many', type: 'number', def: 5 }], submit: 'Generate', run: get('/v1/utils/uuid') },

  { g: 'Files', id: 'files', name: 'Stored files', desc: 'Files you uploaded or downloaded. Downloads delete themselves after an hour.', fields: [{ k: 'file', label: 'Upload a file (optional)', type: 'file' }], submit: 'Show files',
    run: async v => { if (v.file) { const fd = new FormData(); fd.append('file', v.file); await api('/v1/files', { method: 'POST', form: fd }); } return { kind: 'files', data: null }; } },
  { g: 'Files', id: 'f-fetch', name: 'Save a link to files', desc: 'The server downloads the link and keeps it in Stored files.', fields: [T('url', 'File URL', { ph: 'https://…/file.pdf' }), T('filename', 'Save as (optional)')], submit: 'Save', run: post('/v1/download/fetch', v => ({ url: v.url, filename: v.filename || null })) },
  { g: 'Files', id: 'f-proxy', name: 'Download a link', desc: 'Fetch a remote file through Ladybug and save it to your device (nothing is stored).', fields: [T('url', 'File URL', { ph: 'https://…/file.pdf' })], submit: 'Download',
    run: async v => { const r = await api('/v1/download/proxy?' + qs({ url: v.url }), { blob: true }); saveBlob(r.blob, v.url.split('/').pop().split('?')[0] || 'download'); return { kind: 'json', data: { saved: true, size: r.blob.size, type: r.type } }; } },
  { g: 'Files', id: 'f-csv', name: 'JSON to CSV', desc: 'Paste a JSON array of objects, get a CSV file.', fields: [{ k: 'rows', label: 'Rows (JSON array)', type: 'textarea', ph: '[{"name":"Mug","price":12},{"name":"Cup","price":8}]' }, T('filename', 'File name', { def: 'export.csv' })], submit: 'Make CSV',
    run: async v => { let rows; try { rows = JSON.parse(v.rows); } catch { throw new Error('Rows must be valid JSON, like [{"a":1}]'); } const r = await api('/v1/export/csv', { method: 'POST', json: { rows, filename: v.filename || 'export.csv' }, blob: true }); saveBlob(r.blob, v.filename || 'export.csv'); return { kind: 'json', data: { saved: true, rows: rows.length, size: r.blob.size } }; } },

  { g: 'Dev tools', id: 'd-b64e', name: 'Base64 encode', desc: 'Encode text as Base64. Choose URL-safe for tokens and query strings.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }, S('urlsafe', 'Alphabet', [['false', 'Standard'], ['true', 'URL-safe']])], submit: 'Encode', run: async v => ({ kind: 'json', data: await api('/v1/dev/base64/encode?' + qs({ urlsafe: v.urlsafe }), { method: 'POST', json: { text: v.text } }) }) },
  { g: 'Dev tools', id: 'd-b64d', name: 'Base64 decode', desc: 'Decode Base64 back to text.', fields: [{ k: 'text', label: 'Base64', type: 'textarea' }, S('urlsafe', 'Alphabet', [['false', 'Standard'], ['true', 'URL-safe']])], submit: 'Decode', run: async v => ({ kind: 'json', data: await api('/v1/dev/base64/decode?' + qs({ urlsafe: v.urlsafe }), { method: 'POST', json: { text: v.text } }) }) },
  { g: 'Dev tools', id: 'd-case', name: 'Case converter', desc: 'Convert text between camelCase, snake_case, kebab-case, CONSTANT and more.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }, S('style', 'Style', ['camel', 'pascal', 'snake', 'kebab', 'constant', 'dot', 'upper', 'lower', 'title', 'sentence'].map(x => [x, x]))], submit: 'Convert', run: async v => ({ kind: 'json', data: await api('/v1/dev/case?' + qs({ style: v.style }), { method: 'POST', json: { text: v.text } }) }) },
  { g: 'Dev tools', id: 'd-stats', name: 'Text statistics', desc: 'Word, character, sentence counts, reading time and top words.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }], submit: 'Analyse', run: post('/v1/dev/text-stats') },
  { g: 'Dev tools', id: 'd-json', name: 'JSON formatter', desc: 'Pretty-print or minify JSON, and see the parsed type.', fields: [{ k: 'text', label: 'JSON', type: 'textarea' }, { k: 'indent', label: 'Indent (0 = minify)', type: 'number', def: 2 }], submit: 'Format', run: async v => ({ kind: 'json', data: await api('/v1/dev/json/format?' + qs({ indent: v.indent }), { method: 'POST', json: { text: v.text } }) }) },
  { g: 'Dev tools', id: 'd-color', name: 'Colour converter', desc: 'HEX to RGB/HSL with luminance and contrast ratios.', fields: [T('hex', 'HEX colour', { def: '#ff8800' })], submit: 'Convert', run: get('/v1/dev/color') },
  { g: 'Dev tools', id: 'd-ts', name: 'Timestamp', desc: 'Current epoch, ISO-8601 and calendar details.', fields: [], submit: 'Now', run: get('/v1/dev/timestamp') },
  { g: 'Dev tools', id: 'd-pw', name: 'Password generator', desc: 'Strong random passwords using the secrets module.', fields: [{ k: 'length', label: 'Length', type: 'number', def: 16 }, { k: 'count', label: 'How many', type: 'number', def: 1 }, S('symbols', 'Symbols', [['true', 'Yes'], ['false', 'No']]), S('digits', 'Digits', [['true', 'Yes'], ['false', 'No']])], submit: 'Generate', run: get('/v1/dev/password') },
  { g: 'Dev tools', id: 'd-lorem', name: 'Lorem ipsum', desc: 'Placeholder paragraphs for mock-ups.', fields: [{ k: 'paragraphs', label: 'Paragraphs', type: 'number', def: 1 }, { k: 'sentences', label: 'Sentences each', type: 'number', def: 4 }], submit: 'Generate', run: get('/v1/dev/lorem') },
  { g: 'Dev tools', id: 'd-base', name: 'Number base', desc: 'Convert an integer between bases 2 and 36.', fields: [T('value', 'Value', { def: '255' }), { k: 'from_base', label: 'From base', type: 'number', def: 10 }, { k: 'to_base', label: 'To base', type: 'number', def: 16 }], submit: 'Convert', run: get('/v1/dev/number-base') },
  { g: 'Dev tools', id: 'd-regex', name: 'Regex tester', desc: 'Test a regular expression and list the matches.', fields: [T('pattern', 'Pattern', { ph: '\\d+' }), { k: 'text', label: 'Text', type: 'textarea' }, S('ignore_case', 'Ignore case', [['false', 'No'], ['true', 'Yes']])], submit: 'Test', run: async v => ({ kind: 'json', data: await api('/v1/dev/regex?' + qs({ pattern: v.pattern, ignore_case: v.ignore_case }), { method: 'POST', json: { text: v.text } }) }) },
  { g: 'Dev tools', id: 'd-jwt', name: 'JWT decoder', desc: 'Decode a JWT header and payload (signature is not verified).', fields: [{ k: 'token', label: 'Token', type: 'textarea' }], submit: 'Decode', run: get('/v1/dev/jwt/decode') },
  { g: 'Dev tools', id: 'd-rand', name: 'Random value', desc: 'Number, string, UUID, colour, coin, dice or a pick from a list.', fields: [S('type', 'Type', ['number', 'string', 'uuid', 'color', 'coin', 'dice', 'choice'].map(x => [x, x])), { k: 'minimum', label: 'Min (number)', type: 'number', def: 1 }, { k: 'maximum', label: 'Max (number)', type: 'number', def: 100 }, T('choices', 'Choices (comma separated)', { def: 'a,b,c' })], submit: 'Roll', run: get('/v1/dev/random') },
  { g: 'Dev tools', id: 'd-diff', name: 'Text diff', desc: 'Unified diff between two texts.', fields: [{ k: 'text', label: 'Original', type: 'textarea' }, { k: 'other', label: 'Changed', type: 'textarea' }, S('context', 'Context lines', [['3', '3'], ['1', '1'], ['5', '5'], ['0', '0']])], submit: 'Compare', run: async v => ({ kind: 'json', data: await api('/v1/dev/diff?' + qs({ other: v.other, context: v.context }), { method: 'POST', json: { text: v.text } }) }) },
  { g: 'Dev tools', id: 'd-urle', name: 'URL encode', desc: 'Percent-encode text. Component mode also encodes / ? & =.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }, S('component', 'Mode', [['true', 'Component'], ['false', 'Whole URL']])], submit: 'Encode', run: async v => ({ kind: 'json', data: await api('/v1/dev/url/encode?' + qs({ component: v.component }), { method: 'POST', json: { text: v.text } }) }) },
  { g: 'Dev tools', id: 'd-urld', name: 'URL decode', desc: 'Decode percent-encoded text.', fields: [{ k: 'text', label: 'Encoded text', type: 'textarea' }], submit: 'Decode', run: post('/v1/dev/url/decode', v => ({ text: v.text })) },
  { g: 'Dev tools', id: 'd-htmle', name: 'HTML escape', desc: 'Turn < > & and quotes into HTML entities.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }], submit: 'Escape', run: post('/v1/dev/html/escape', v => ({ text: v.text })) },
  { g: 'Dev tools', id: 'd-htmlu', name: 'HTML unescape', desc: 'Turn HTML entities back into characters.', fields: [{ k: 'text', label: 'Text', type: 'textarea' }], submit: 'Unescape', run: post('/v1/dev/html/unescape', v => ({ text: v.text })) },
  { g: 'Dev tools', id: 'd-jsonv', name: 'JSON validator', desc: 'Check whether text is valid JSON and see where it breaks.', fields: [{ k: 'text', label: 'JSON', type: 'textarea' }], submit: 'Validate', run: post('/v1/dev/json/validate', v => ({ text: v.text })) },
  { g: 'Dev tools', id: 'd-tsc', name: 'Timestamp converter', desc: 'Epoch (seconds or ms) or ISO date to every common format.', fields: [T('value', 'Value', { def: '1767225600' })], submit: 'Convert', run: async v => ({ kind: 'json', data: await api('/v1/dev/timestamp/convert?' + qs({ value: v.value }), { method: 'POST' }) }) },

  { g: 'Web check', id: 'w-status', name: 'HTTP status', desc: 'Status code, redirect chain and response time for any URL.', fields: [URL_F()], submit: 'Check', run: get('/v1/web/status') },
  { g: 'Web check', id: 'w-head', name: 'Response headers', desc: 'All response headers plus a security-header score.', fields: [URL_F()], submit: 'Fetch', run: get('/v1/web/headers') },
  { g: 'Web check', id: 'w-robots', name: 'robots.txt', desc: 'Fetch and parse a site\u2019s robots.txt: sitemaps and rule counts.', fields: [URL_F()], submit: 'Fetch', run: get('/v1/web/robots') },
  { g: 'Web check', id: 'w-ssl', name: 'TLS certificate', desc: 'Issuer, validity dates, days left, SANs and TLS version for a host.', fields: [T('host', 'Host', { def: 'github.com' })], submit: 'Inspect', run: get('/v1/web/ssl') },

  { g: 'Jobs', id: 'j-search', name: 'Job search', desc: 'Search Remotive, Arbeitnow, Remote OK, Jobicy, The Muse and Himalayas at once. Merged, de-duplicated, newest first.', fields: [T('q', 'Keywords', { def: 'python developer' }), S('source', 'Source', ['all', 'remotive', 'arbeitnow', 'remoteok', 'jobicy', 'themuse', 'himalayas'].map(x => [x, x])), T('location', 'Location', { ph: 'Germany, Berlin, usa...' }), S('remote', 'Remote', [['', 'Any'], ['true', 'Remote only'], ['false', 'On-site only']]), T('job_type', 'Job type', { ph: 'full time, contract, internship' }), { k: 'min_salary', label: 'Min yearly salary', type: 'number' }, { k: 'days', label: 'Posted in last N days', type: 'number' }, { k: 'limit', label: 'How many', type: 'number', def: 10 }], submit: 'Search jobs', run: get('/v1/jobs/search') },
  { g: 'Jobs', id: 'j-insights', name: 'Job market insights', desc: 'Openings, remote share, top skills, top hiring companies and published salary stats for a role.', fields: [T('q', 'Role or skill', { def: 'react developer' }), T('location', 'Location'), S('remote', 'Remote', [['', 'Any'], ['true', 'Remote only'], ['false', 'On-site only']])], submit: 'Analyse', run: get('/v1/jobs/insights') },
  { g: 'Jobs', id: 'j-hn', name: 'HN: Who is hiring?', desc: 'Posts from the latest Hacker News hiring thread.', fields: [T('q', 'Filter', { def: 'python' }), { k: 'limit', label: 'How many', type: 'number', def: 10 }], submit: 'Find posts', run: get('/v1/jobs/hn-hiring') },
  { g: 'Jobs', id: 'j-cat', name: 'Job categories', desc: 'Valid category values for a job board.', fields: [S('source', 'Source', ['remotive', 'jobicy', 'themuse'].map(x => [x, x]))], submit: 'List', run: get('/v1/jobs/categories') },
  { g: 'Jobs', id: 'j-src', name: 'Job sources', desc: 'Which job boards are searched and what each supports.', fields: [], submit: 'Show sources', run: get('/v1/jobs/sources') },
  { g: 'Info', id: 'i-weather', name: 'Weather', desc: 'Current weather and a daily forecast for any city.', fields: [T('city', 'City', { def: 'Cape Town' }), S('units', 'Units', [['metric', 'Metric'], ['imperial', 'Imperial']]), { k: 'days', label: 'Forecast days', type: 'number', def: 3 }], submit: 'Get weather', run: get('/v1/info/weather') },
  { g: 'Info', id: 'i-geo', name: 'Geocode', desc: 'Place name to coordinates, country, timezone and population.', fields: [T('q', 'Place', { def: 'Johannesburg' })], submit: 'Look up', run: get('/v1/info/geocode') },
  { g: 'Info', id: 'i-hol', name: 'Public holidays', desc: 'Public holidays for a country and year.', fields: [T('country', 'Country code', { def: 'ZA' }), { k: 'year', label: 'Year', type: 'number', def: 2026 }, S('upcoming', 'Only upcoming', [['false', 'No'], ['true', 'Yes']])], submit: 'List', run: get('/v1/info/holidays') },
  { g: 'Info', id: 'i-crypto', name: 'Crypto prices', desc: 'Spot prices with 24h change (CoinGecko ids, comma separated).', fields: [T('ids', 'Coins', { def: 'bitcoin,ethereum' }), T('vs', 'Currencies', { def: 'usd,eur' })], submit: 'Get prices', run: get('/v1/info/crypto') },
  { g: 'Info', id: 'i-wiki', name: 'Wikipedia', desc: 'Summary of a Wikipedia article.', fields: [T('q', 'Topic', { def: 'Nelson Mandela' }), T('lang', 'Language', { def: 'en' })], submit: 'Summarise', run: get('/v1/info/wiki') },
  { g: 'Info', id: 'i-books', name: 'Book search', desc: 'Search Open Library by title, author or ISBN.', fields: [T('q', 'Query', { def: 'dune' }), { k: 'limit', label: 'How many', type: 'number', def: 10 }], submit: 'Search', run: get('/v1/info/books') },
  { g: 'Info', id: 'i-hn', name: 'Hacker News', desc: 'Top, new, best, ask, show or job stories.', fields: [S('kind', 'List', ['top', 'new', 'best', 'ask', 'show', 'job'].map(x => [x, x])), { k: 'limit', label: 'How many', type: 'number', def: 15 }], submit: 'Fetch', run: get('/v1/info/hn') },
  { g: 'Info', id: 'i-ip', name: 'IP lookup', desc: 'Location, ISP and timezone of a public IP address.', fields: [T('ip', 'IP address', { def: '8.8.8.8' })], submit: 'Look up', run: get('/v1/info/ip') },
  { g: 'Info', id: 'i-dict', name: 'Dictionary', desc: 'Definitions, pronunciation, examples, synonyms, antonyms and rhymes for an English word.', fields: [T('word', 'Word', { def: 'serendipity' })], submit: 'Define', run: get('/v1/info/dictionary', 'dict') },
  { g: 'Info', id: 'i-trivia', name: 'Trivia', desc: 'Random quiz questions. Wait a few seconds between draws.', fields: [{ k: 'amount', label: 'Questions', type: 'number', def: 5 }, S('category', 'Category', [['', 'Any'], ...['general', 'books', 'film', 'music', 'tv', 'games', 'science', 'computers', 'math', 'mythology', 'sports', 'geography', 'history', 'politics', 'art', 'animals', 'vehicles', 'anime', 'cartoons'].map(x => [x, x])]), S('difficulty', 'Difficulty', [['', 'Any'], ['easy', 'Easy'], ['medium', 'Medium'], ['hard', 'Hard']]), S('type', 'Type', [['', 'Any'], ['multiple', 'Multiple choice'], ['boolean', 'True / false']])], submit: 'Get questions', run: get('/v1/info/trivia', 'trivia') },
  { g: 'Info', id: 'i-song', name: 'Song search', desc: 'Artist, album, cover art, 30-second preview, release date and store links.', fields: [T('q', 'Song or artist', { ph: 'blinding lights weeknd' }), { k: 'limit', label: 'Results', type: 'number', def: 5 }], submit: 'Search songs', run: get('/v1/info/song', 'song') },
  { g: 'Info', id: 'i-time', name: 'Local time & sun', desc: 'Current time, UTC offset, daylight saving, sunrise and sunset for a city.', fields: [T('city', 'City', { def: 'Johannesburg' })], submit: 'Get time', run: get('/v1/info/time', 'time') },
  { g: 'Account', id: 'acc-usage', name: 'My usage', desc: 'Requests used today and left on your key.', fields: [], submit: 'Check usage', run: get('/v1/account/usage') },
  { g: 'Account', id: 'acc-store', name: 'Storage status (admin)', desc: 'MongoDB connection and where the YouTube cookies come from. Needs the admin key in the key box at the top.', fields: [], submit: 'Check', run: get('/v1/admin/storage') },
  { g: 'Account', id: 'acc-cookies', name: 'Save YouTube cookies (admin)', desc: 'Replace the server\u2019s YouTube cookies without a redeploy. Needs the admin key in the key box at the top; stored in MongoDB.', fields: [{ k: 'text', label: 'cookies.txt contents', type: 'textarea' }], submit: 'Save cookies', run: async v => ({ kind: 'json', data: await api('/v1/admin/cookies', { method: 'PUT', json: { text: v.text } }) }) },
  { g: 'Account', id: 'acc-dl', name: 'Downloader status', desc: 'Is the media downloader healthy? Cookies, PO-token and backend state.', fields: [], submit: 'Check status', run: get('/v1/media/status') },
  { g: 'Account', id: 'acc-diag', name: 'Downloader diagnose', desc: 'Test a YouTube link end to end and see where it fails.', fields: [T('url', 'Test link', { def: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' })], submit: 'Diagnose', run: get('/v1/media/diagnose') },
  { g: 'Account', id: 'acc-probe', name: 'YouTube probe', desc: 'Raw request with and without cookies, with a verdict.', fields: [T('url', 'Test link', { def: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ' })], submit: 'Probe', run: get('/v1/media/probe') },
];

/* ---------- ui ---------- */
let current;
const COLLAPSED = new Set((() => { try { return JSON.parse(localStorage.getItem('lb_collapsed') || '[]'); } catch { return []; } })());
let NAVQ = '';
function buildNav() {
  const nav = $('#nav');
  nav.replaceChildren();
  const input = el('input', { id: 'navq', type: 'search', placeholder: `Search ${TOOLS.length} tools…`, 'aria-label': 'Search tools', autocomplete: 'off', spellcheck: 'false' });
  input.value = NAVQ;
  input.addEventListener('input', () => { NAVQ = input.value.trim().toLowerCase(); renderList(); });
  input.addEventListener('keydown', e => { if (e.key === 'Escape') { input.value = ''; NAVQ = ''; renderList(); } });
  const list = el('div', { class: 'navlist' });
  nav.append(el('div', { class: 'navsearch' }, input, el('span', { class: 'kbd' }, '/')), list);

  function renderList() {
    list.replaceChildren();
    let g = null, group = null, shown = 0;
    TOOLS.forEach(t => {
      const hay = (t.name + ' ' + t.g + ' ' + (t.desc || '')).toLowerCase();
      if (NAVQ && !hay.includes(NAVQ)) return;
      if (t.g !== g) {
        g = t.g;
        const gname = t.g;  // capture per group: `g` mutates on the next iteration
        const collapsed = !NAVQ && COLLAPSED.has(gname);
        const count = TOOLS.filter(x => x.g === gname).length;
        group = el('div', { class: 'navgroup' + (collapsed ? ' collapsed' : '') });
        group.append(el('button', {
          class: 'navgrouphead', type: 'button', 'aria-expanded': String(!collapsed),
          onclick: () => {
            if (COLLAPSED.has(gname)) COLLAPSED.delete(gname); else COLLAPSED.add(gname);
            try { localStorage.setItem('lb_collapsed', JSON.stringify([...COLLAPSED])); } catch {}
            renderList();
          },
        }, el('span', { class: 'chev' }, '▶'), el('span', {}, gname), el('span', { class: 'count' }, String(count))));
        list.append(group);
      }
      const b = el('button', { class: 'navitem', 'data-id': t.id, onclick: () => { open(t.id); closeNav(); } }, t.name);
      if (current && t.id === current.id) b.setAttribute('aria-current', 'true');
      group.append(b);
      shown++;
    });
    if (!shown) list.append(el('div', { class: 'empty', style: 'padding:12px 10px' }, `No tools match “${NAVQ}”.`));
  }
  renderList();
  nav._render = renderList;
}
function open(id) {
  current = TOOLS.find(t => t.id === id) || TOOLS[0];
  history.replaceState(null, '', '#' + current.id);
  document.querySelectorAll('nav button').forEach(b => b.setAttribute('aria-current', b.dataset.id === current.id));
  const vals = {}, wrap = {};
  const form = el('form', { class: 'tool', autocomplete: 'off' });
  const out = el('div', { id: 'out', 'aria-live': 'polite' });
  const refresh = () => current.fields.forEach(f => { if (f.show) wrap[f.k].style.display = f.show(vals) ? '' : 'none'; });
  const grid = el('div', { class: 'row' });
  current.fields.forEach(f => {
    vals[f.k] = f.def ?? (f.type === 'select' ? f.opts[0][0] : '');
    let inp;
    if (f.type === 'textarea') inp = el('textarea', { id: 'f_' + f.k, placeholder: f.ph || '' });
    else if (f.type === 'select') inp = el('select', { id: 'f_' + f.k }, f.opts.map(([v, l]) => el('option', { value: v }, l)));
    else inp = el('input', { id: 'f_' + f.k, type: f.type === 'number' ? 'number' : f.type === 'file' ? 'file' : 'text', placeholder: f.ph || '' });
    if (f.type !== 'file') inp.value = vals[f.k];
    inp.addEventListener(f.type === 'select' || f.type === 'file' ? 'change' : 'input', () => { vals[f.k] = f.type === 'file' ? inp.files[0] : inp.value; refresh(); });
    wrap[f.k] = el('div', { class: 'field', style: f.type === 'textarea' || f.k === 'url' || f.k === 'data' || f.k === 'prompt' ? 'grid-column:1/-1' : '' }, el('label', { for: 'f_' + f.k }, f.label), inp);
    grid.append(wrap[f.k]);
  });
  const btn = el('button', { class: 'go', type: 'submit' }, current.submit);
  form.append(grid, el('div', { class: 'actions' }, btn));
  form.addEventListener('submit', async e => {
    e.preventDefault();
    if (!getKey()) { out.replaceChildren(errBox('Add your API key', 'Paste it in the field at the top and press Save.')); $('#apikey').focus(); return; }
    CALLS = [];
    btn.disabled = true; out.replaceChildren(el('div', { class: 'loading' }, el('i'), el('i'), el('i'), 'Working…'));
    try {
      const { kind, data } = await current.run({ ...vals });
      const node = kind === 'files' ? await R.files() : R[kind](data);
      out.replaceChildren(apiBox(CALLS.find(c => c.path.startsWith('/v1/') && !c.path.includes('/files/'))), node);
    } catch (err) {
      out.replaceChildren(errBox(err.status === 401 ? 'API key rejected' : 'Something went wrong', err.status === 401 ? 'Check the key in the Render dashboard: your service > Environment > API_KEYS.' : err.message), apiBox(CALLS.find(c => c.path.startsWith('/v1/'))));
    }
    btn.disabled = false;
  });
  refresh();
  $('#tool').replaceChildren(el('div', { class: 'toolhead' }, el('span', { class: 'crumb' }, current.g), el('h1', {}, current.name), el('p', {}, current.desc)), form, out);
  if ($('#nav')._render) $('#nav')._render();
}
const fullKey = u => u + (u.includes('?') ? '&' : '?') + 'apikey=' + encodeURIComponent(getKey());
function apiBox(c) {
  if (!c) return '';
  const url = location.origin + c.path;
  const shown = c.method === 'GET' ? url + (url.includes('?') ? '&' : '?') + 'apikey=YOUR_KEY' : url;
  const curl = c.method === 'GET' ? `curl -H "X-API-Key: ${getKey() || 'YOUR_KEY'}" "${url}"`
    : `curl -X ${c.method} -H "X-API-Key: ${getKey() || 'YOUR_KEY'}" -H "Content-Type: application/json" -d '${JSON.stringify(c.json || {})}' "${url}"`;
  const cp = (label, text) => el('button', { class: 'sm', onclick: e => { navigator.clipboard.writeText(text); const t = e.target; t.textContent = 'Copied'; setTimeout(() => t.textContent = label, 1200); } }, label);
  const timing = c.ms != null
    ? el('span', { class: 'pill' + (c.status >= 400 ? ' bad' : ''), style: 'margin-left:8px;font-size:11px' }, `${c.status || 200} · ${c.ms} ms`)
    : '';
  return el('div', { class: 'card apibox' }, el('div', { class: 'bar' }, el('span', {}, 'API request', timing), el('span', { class: 'actions' }, c.method === 'GET' ? cp('Copy URL', fullKey(url)) : cp('Copy URL', url), cp('Copy cURL', curl))),
    el('pre', { class: 'json mono', style: 'white-space:pre-wrap;word-break:break-all;margin:0' }, `${c.method} ${shown}` + (c.json ? '\n' + JSON.stringify(c.json, null, 2) : '')));
}
const errBox = (t, m) => el('div', { class: 'card err' }, el('b', {}, t), m);

/* key handling */
async function checkKey(show) {
  const s = $('#keystate'); if (!getKey()) { s.textContent = ''; return; }
  try { await api('/v1/files'); s.textContent = 'Key works'; s.className = 'keystate ok'; }
  catch (e) { s.textContent = e.status === 401 ? 'Key rejected' : 'Server error'; s.className = 'keystate bad'; }
}
/* ---------- toasts ---------- */
function toast(msg, kind = '') {
  const t = el('div', { class: 'toast ' + kind }, msg);
  $('#toasts').append(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 220); }, 2600);
}

/* ---------- theme ---------- */
const THEME_KEY = 'lb_theme';
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem(THEME_KEY, t); } catch {}
  const b = $('#themebtn');
  if (b) { b.textContent = t === 'dark' ? '☀' : '☾'; b.setAttribute('aria-pressed', String(t === 'dark')); }
}
applyTheme(document.documentElement.dataset.theme || 'light');
$('#themebtn').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  toast(next === 'dark' ? 'Dark mode on' : 'Light mode on');
});

/* ---------- mobile nav ---------- */
function closeNav() { document.body.classList.remove('navopen'); $('#navtoggle').setAttribute('aria-expanded', 'false'); }
$('#navtoggle').addEventListener('click', () => {
  const openNow = document.body.classList.toggle('navopen');
  $('#navtoggle').setAttribute('aria-expanded', String(openNow));
  if (openNow) setTimeout(() => $('#navq')?.focus(), 60);
});
document.addEventListener('click', e => {
  if (document.body.classList.contains('navopen') && !e.target.closest('nav') && !e.target.closest('#navtoggle')) closeNav();
});

/* ---------- command palette ---------- */
const PAL = el('div', { class: 'palette', hidden: '', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Search tools' });
const palInput = el('input', { type: 'text', placeholder: 'Jump to a tool…', 'aria-label': 'Search tools', autocomplete: 'off', spellcheck: 'false' });
const palResults = el('div', { class: 'results', role: 'listbox' });
PAL.append(el('div', { class: 'box' }, palInput, palResults,
  el('div', { class: 'foot' }, el('span', {}, '↑↓ to move'), el('span', {}, '↵ to open'), el('span', {}, 'esc to close'))));
document.body.append(PAL);
let palHits = [], palSel = 0;

function palRender() {
  const q = palInput.value.trim().toLowerCase();
  palHits = TOOLS.filter(t => !q || (t.name + ' ' + t.g + ' ' + (t.desc || '')).toLowerCase().includes(q)).slice(0, 40);
  palSel = 0;
  palResults.replaceChildren();
  if (!palHits.length) { palResults.append(el('div', { class: 'empty', style: 'padding:14px' }, 'Nothing matches that.')); return; }
  palHits.forEach((t, i) => {
    const name = el('span', {});
    if (q) {
      const idx = t.name.toLowerCase().indexOf(q);
      if (idx >= 0) name.append(t.name.slice(0, idx), el('mark', {}, t.name.slice(idx, idx + q.length)), t.name.slice(idx + q.length));
      else name.append(t.name);
    } else name.append(t.name);
    const row = el('button', { class: 'res', role: 'option', 'aria-selected': String(i === palSel), onclick: () => palPick(i) }, name, el('span', { class: 'g' }, t.g));
    palResults.append(row);
  });
}
function palMove(d) {
  if (!palHits.length) return;
  palSel = (palSel + d + palHits.length) % palHits.length;
  [...palResults.children].forEach((c, i) => c.setAttribute('aria-selected', String(i === palSel)));
  palResults.children[palSel]?.scrollIntoView({ block: 'nearest' });
}
function palPick(i) {
  const t = palHits[i];
  if (!t) return;
  closePal();
  open(t.id);
  closeNav();
  toast(t.g + ' · ' + t.name);
}
function openPal() {
  PAL.hidden = false;
  palInput.value = '';
  palRender();
  setTimeout(() => palInput.focus(), 20);
}
function closePal() { PAL.hidden = true; }
$('#palettebtn').addEventListener('click', openPal);
PAL.addEventListener('click', e => { if (e.target === PAL) closePal(); });
palInput.addEventListener('input', palRender);
palInput.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { e.preventDefault(); palMove(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); palMove(-1); }
  else if (e.key === 'Enter') { e.preventDefault(); palPick(palSel); }
  else if (e.key === 'Escape') { e.preventDefault(); closePal(); }
});

/* ---------- keyboard shortcuts ---------- */
document.addEventListener('keydown', e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); PAL.hidden ? openPal() : closePal(); return; }
  if (e.key === 'Escape' && !PAL.hidden) { closePal(); return; }
  if (!typing && e.key === '/') { e.preventDefault(); $('#navq')?.focus(); return; }
  if (!typing && e.key === '?') { e.preventDefault(); toast('Ctrl K search · / filter · Esc close'); }
});

/* ---------- boot ---------- */
$('#apikey').value = getKey();
$('#keyform').addEventListener('submit', e => {
  e.preventDefault();
  const k = $('#apikey').value.trim();
  localStorage.setItem('ladybug_key', k);
  toast(k ? 'API key saved' : 'API key cleared', k ? 'ok' : '');
  checkKey();
});
buildNav(); open(location.hash.slice(1)); checkKey();
window.addEventListener('hashchange', () => open(location.hash.slice(1)));
