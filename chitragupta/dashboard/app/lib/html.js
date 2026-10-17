// html`<div class=${c}>${children}</div>` -> Preact vnodes. Our own small tagged-template parser (no htm: its licence is
// outside the project's MIT/BSD/OFL rule). Each template is parsed once and cached; values only fill holes, so a value
// can never become markup. Supports components (<${Comp} …>, closed by </${Comp}> or <//>), quoted/unquoted/hole
// attribute values, boolean attributes, ...${props} spreads, self-closing tags and <!-- comments -->.
import { h, Fragment } from '/vendor/preact.module.js';

const HOLE = '\u0001';
const cache = new WeakMap();
const ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', '#39': "'", middot: '·', hellip: '…', ndash: '–', mdash: '—', times: '×' };

function decode(s) {
  return s.indexOf('&') < 0 ? s : s.replace(/&(#39|[a-z]+);/g, (m, n) => (n in ENT ? ENT[n] : m));
}

function cleanText(s) {
  if (!s.trim() && s.indexOf('\n') >= 0) return '';
  return s.replace(/^\s*\n\s*|\s*\n\s*$/g, '').replace(/\s*\n\s*/g, ' ');
}

function parse(statics) {
  const src = statics.join(HOLE);
  let i = 0, field = 0;
  const root = { children: [] };
  const stack = [root];
  let text = '';
  const top = () => stack[stack.length - 1];
  const flushText = () => {
    const t = cleanText(text);
    if (t) top().children.push(decode(t));
    text = '';
  };
  const ws = () => { while (i < src.length && /\s/.test(src[i])) i++; };
  while (i < src.length) {
    const c = src[i];
    if (c === HOLE) { flushText(); top().children.push({ f: field++ }); i++; continue; }
    if (c !== '<') { text += c; i++; continue; }
    if (src.startsWith('<!--', i)) { flushText(); const e = src.indexOf('-->', i); i = e < 0 ? src.length : e + 3; continue; }
    flushText();
    if (src[i + 1] === '/') {  // closing tag: </div>, </${C}> or <//>
      const e = src.indexOf('>', i);
      for (let k = i; k < e; k++) if (src[k] === HOLE) field++;
      i = e + 1;
      if (stack.length > 1) stack.pop();
      continue;
    }
    i++;
    const node = { props: [], children: [] };
    if (src[i] === HOLE) { node.tag = { f: field++ }; i++; }
    else { const m = /^[A-Za-z][\w.:-]*/.exec(src.slice(i)); node.tag = m ? m[0] : 'div'; i += m ? m[0].length : 0; }
    let selfClose = false;
    for (;;) {
      ws();
      if (i >= src.length) break;
      if (src[i] === '>') { i++; break; }
      if (src[i] === '/' && src[i + 1] === '>') { selfClose = true; i += 2; break; }
      if (src.startsWith('...', i) && src[i + 3] === HOLE) { node.props.push({ spread: field++ }); i += 4; continue; }
      const m = /^[^\s=/>\u0001]+/.exec(src.slice(i));
      if (!m) { i++; continue; }
      const name = m[0];
      i += name.length;
      ws();
      if (src[i] !== '=') { node.props.push({ name, v: true }); continue; }
      i++;
      ws();
      if (src[i] === HOLE) { node.props.push({ name, v: { f: field++ } }); i++; continue; }
      const q = src[i] === '"' || src[i] === "'" ? src[i] : null;
      if (q) i++;
      const parts = [];
      let buf = '';
      while (i < src.length) {
        const d = src[i];
        if (q ? d === q : /[\s>]/.test(d) || (d === '/' && src[i + 1] === '>')) break;
        if (d === HOLE) { if (buf) parts.push(decode(buf)); buf = ''; parts.push({ f: field++ }); i++; continue; }
        buf += d; i++;
      }
      if (q) i++;
      if (buf) parts.push(decode(buf));
      node.props.push({ name, v: parts.length === 1 && typeof parts[0] === 'object' ? parts[0] : { parts } });
    }
    top().children.push(node);
    if (!selfClose) stack.push(node);
  }
  flushText();
  return root.children;
}

function build(node, vals) {
  if (typeof node === 'string') return node;
  if (node.f !== undefined) return vals[node.f];
  const props = {};
  for (const p of node.props) {
    if (p.spread !== undefined) { Object.assign(props, vals[p.spread]); continue; }
    const v = p.v;
    if (v === true) props[p.name] = true;
    else if (v.f !== undefined) props[p.name] = vals[v.f];
    else props[p.name] = v.parts.map((x) => (typeof x === 'string' ? x : (vals[x.f] == null ? '' : String(vals[x.f])))).join('');
  }
  const tag = typeof node.tag === 'string' ? node.tag : vals[node.tag.f];
  return h(tag, props, ...node.children.map((c) => build(c, vals)));
}

export function html(statics, ...vals) {
  let tree = cache.get(statics);
  if (!tree) { tree = parse(statics); cache.set(statics, tree); }
  const out = tree.map((n) => build(n, vals));
  return out.length === 1 ? out[0] : h(Fragment, null, ...out);
}
