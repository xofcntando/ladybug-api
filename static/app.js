const $ = (s, r = document) => r.querySelector(s);
const el = (t, a = {}, ...kids) => { const e = document.createElement(t); for (const [k, v] of Object.entries(a)) k === 'class' ? e.className = v : k === 'html' ? e.innerHTML = v : k.startsWith('on') ? e.addEventListener(k.slice(2), v) : e.setAttribute(k, v); kids.flat().forEach(c => e.append(c?.nodeType ? c : document.createTextNode(c ?? ''))); return e; };
const getKey = () => localStorage.getItem('ladybug_key') || '';

async function api(path, { method = 'GET', json, form, blob } = {}) {
  const h = { 'X-API-Key': getKey() };
  let body;
  if (json) { h['Content-Type'] = 'application/json'; body = JSON.stringify(json); }
  if (form) body = form;
  const r = await fetch(path, { method, headers: h, body });
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

  CARD('welcome'), CARD('goodbye'),

  { g: 'Utilities', id: 'mail', name: 'Email check', desc: 'Checks format, mail server and disposable domains. Does not contact the mailbox.', fields: [T('email', 'Email address')], submit: 'Check', run: get('/v1/utils/email-validate') },
  { g: 'Utilities', id: 'dns', name: 'DNS lookup', desc: 'Records for a domain, plus whether SPF and DMARC are set.', fields: [T('domain', 'Domain', { ph: 'yourshop.com' })], submit: 'Look up', run: get('/v1/utils/dns') },
  { g: 'Utilities', id: 'cur', name: 'Currency', desc: 'Live exchange rates.', fields: [T('from', 'From', { def: 'USD' }), T('to', 'To', { def: 'EUR' }), { k: 'amount', label: 'Amount', type: 'number', def: 100 }], submit: 'Convert', run: get('/v1/utils/currency') },
  { g: 'Utilities', id: 'slug', name: 'Slug', desc: 'Turn a title into a URL slug.', fields: [T('text', 'Title')], submit: 'Make slug', run: post('/v1/utils/slug') },

  { g: 'Files', id: 'files', name: 'Stored files', desc: 'Files you uploaded or downloaded. Downloads delete themselves after an hour.', fields: [{ k: 'file', label: 'Upload a file (optional)', type: 'file' }], submit: 'Show files',
    run: async v => { if (v.file) { const fd = new FormData(); fd.append('file', v.file); await api('/v1/files', { method: 'POST', form: fd }); } return { kind: 'files', data: null }; } },
];

/* ---------- ui ---------- */
let current;
function buildNav() {
  const nav = $('#nav'); let g;
  TOOLS.forEach(t => { if (t.g !== g) { g = t.g; nav.append(el('h3', {}, g)); } nav.append(el('button', { 'data-id': t.id, onclick: () => open(t.id) }, t.name)); });
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
    btn.disabled = true; out.replaceChildren(el('div', { class: 'loading' }, el('i'), el('i'), el('i'), 'Working…'));
    try {
      const { kind, data } = await current.run({ ...vals });
      const node = kind === 'files' ? await R.files() : R[kind](data);
      out.replaceChildren(node);
    } catch (err) {
      out.replaceChildren(errBox(err.status === 401 ? 'API key rejected' : 'Something went wrong', err.status === 401 ? 'Check the key in the Render dashboard: your service > Environment > API_KEYS.' : err.message));
    }
    btn.disabled = false;
  });
  refresh();
  $('#tool').replaceChildren(el('div', { class: 'toolhead' }, el('h1', {}, current.name), el('p', {}, current.desc)), form, out);
}
const errBox = (t, m) => el('div', { class: 'card err' }, el('b', {}, t), m);

/* key handling */
async function checkKey(show) {
  const s = $('#keystate'); if (!getKey()) { s.textContent = ''; return; }
  try { await api('/v1/files'); s.textContent = 'Key works'; s.className = 'keystate ok'; }
  catch (e) { s.textContent = e.status === 401 ? 'Key rejected' : 'Server error'; s.className = 'keystate bad'; }
}
$('#apikey').value = getKey();
$('#keyform').addEventListener('submit', e => { e.preventDefault(); localStorage.setItem('ladybug_key', $('#apikey').value.trim()); checkKey(); });
buildNav(); open(location.hash.slice(1)); checkKey();
