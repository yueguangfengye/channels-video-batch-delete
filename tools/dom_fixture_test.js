/* 离线验证 cleaner/dom.py 里 scanList 的核心算法。
 *
 * 不连浏览器、不需要真页面：用一套极简 DOM shim 造出「视频号动态列表」的
 * 结构，把注入脚本原样跑一遍，断言它能不能正确：
 *   1. 找出每条视频的发布时间
 *   2. 把时间节点正确地"向上归位"到卡片容器
 *   3. 提取标题（不能把"删除"、时间、"更多"当成标题）
 *   4. 找到卡片内的删除入口
 *
 * 用法： node tools/dom_fixture_test.js _jscheck.json
 */
'use strict';
const fs = require('fs');

const jsPath = process.argv[2] || '_jscheck.json';
const JS_LIB = JSON.parse(fs.readFileSync(jsPath, 'utf8')).lib;

// ---------------------------------------------------------------- DOM shim
const TEXT_NODE = 3;
const ELEMENT_NODE = 1;

class TextNode {
  constructor(value, parent) {
    this.nodeType = TEXT_NODE;
    this.nodeValue = value;
    this.parentElement = parent;
  }
}

class El {
  constructor(tag, opts = {}) {
    this.nodeType = ELEMENT_NODE;
    this.tagName = String(tag).toUpperCase();
    this.childNodes = [];
    this.parentElement = null;
    this.attrs = {};
    if (opts.cls) this.attrs.class = opts.cls;
    this._rect = opts.rect || null;
  }
  get children() {
    return this.childNodes.filter((c) => c.nodeType === ELEMENT_NODE);
  }
  get className() {
    return this.attrs.class || '';
  }
  append(...nodes) {
    for (const n of nodes) {
      if (typeof n === 'string') {
        this.childNodes.push(new TextNode(n, this));
      } else {
        n.parentElement = this;
        this.childNodes.push(n);
      }
    }
    return this;
  }
  get textContent() {
    let s = '';
    for (const c of this.childNodes) s += c.nodeType === TEXT_NODE ? c.nodeValue : c.textContent;
    return s;
  }
  get innerText() {
    return this.textContent;
  }
  getAttribute(n) {
    return Object.prototype.hasOwnProperty.call(this.attrs, n) ? this.attrs[n] : null;
  }
  setAttribute(n, v) {
    this.attrs[n] = String(v);
  }
  getBoundingClientRect() {
    let r = this._rect;
    let p = this.parentElement;
    while (!r && p) {
      r = p._rect;
      p = p.parentElement;
    }
    r = r || { left: 0, top: 0, width: 800, height: 100 };
    return {
      left: r.left, top: r.top, width: r.width, height: r.height,
      right: r.left + r.width, bottom: r.top + r.height, x: r.left, y: r.top,
    };
  }
  contains(other) {
    let p = other;
    while (p) {
      if (p === this) return true;
      p = p.parentElement;
    }
    return false;
  }
  querySelectorAll(selector) {
    return queryAll(this, selector);
  }
  querySelector(selector) {
    const r = queryAll(this, selector);
    return r.length ? r[0] : null;
  }
  scrollIntoView() {}
}

function matchSimple(el, part) {
  part = part.trim();
  if (!part) return false;
  if (part[0] === '#') return el.attrs.id === part.slice(1);
  if (part[0] === '.') {
    const want = part.slice(1);
    return (el.attrs.class || '').split(/\s+/).indexOf(want) >= 0;
  }
  const m = /^\[([\w-]+)="?([^"\]]*)"?\]$/.exec(part);
  if (m) return el.attrs[m[1]] === m[2];
  return el.tagName === part.toUpperCase();
}

function descendants(root) {
  const out = [];
  const stack = [...root.children];
  while (stack.length) {
    const el = stack.shift();
    out.push(el);
    stack.unshift(...el.children);
  }
  return out;
}

function queryAll(root, selector) {
  const parts = selector.split(',').map((s) => s.trim()).filter(Boolean);
  const all = root === document ? [...descendants(document.documentElement)] : descendants(root);
  return all.filter((el) => parts.some((p) => matchSimple(el, p)));
}

const document = {
  body: null,
  documentElement: null,
  scrollingElement: null,
  contains(el) {
    let p = el;
    while (p) {
      if (p === document.documentElement) return true;
      p = p.parentElement;
    }
    return false;
  },
  querySelector: (s) => document.documentElement.querySelector(s),
  querySelectorAll: (s) => queryAll(document.documentElement, s),
  createTreeWalker(root, whatToShow) {
    const items = [];
    (function walk(node) {
      for (const c of node.childNodes) {
        if (c.nodeType === TEXT_NODE) items.push(c);
        else walk(c);
      }
    })(root);
    let i = -1;
    return { nextNode: () => (++i < items.length ? items[i] : null) };
  },
};

const globalObj = {
  document,
  NodeFilter: { SHOW_TEXT: 4, SHOW_ELEMENT: 1 },
  innerWidth: 1280,
  innerHeight: 800,
  scrollY: 0,
  getComputedStyle: () => ({
    position: 'static', display: 'block', visibility: 'visible',
    opacity: '1', overflowY: 'visible', cursor: 'pointer',
  }),
  Set,
  Map,
  console,
};
globalObj.window = globalObj;
Object.assign(globalThis, globalObj);

// ---------------------------------------------------------------- fixture
const ROW_H = 110;
const listRoot = new El('div', { cls: 'post-list', rect: { left: 0, top: 0, width: 800, height: 900 } });

function makeCard(i, title, timeText, withDeleteText) {
  const top = i * ROW_H;
  const card = new El('div', { cls: 'post-item', rect: { left: 0, top, width: 800, height: ROW_H - 10 } });
  const cover = new El('div', { cls: 'cover', rect: { left: 0, top, width: 160, height: 90 } }).append(' ');
  const meta = new El('div', { cls: 'meta', rect: { left: 170, top, width: 620, height: 90 } });
  const titleEl = new El('div', { cls: 'title', rect: { left: 170, top, width: 500, height: 24 } }).append(title);
  const timeEl = new El('div', { cls: 'time', rect: { left: 170, top: top + 30, width: 220, height: 20 } }).append(timeText);
  const ops = new El('div', { cls: 'ops', rect: { left: 700, top: top + 30, width: 90, height: 20 } });
  if (withDeleteText) {
    ops.append(new El('span', { cls: 'op', rect: { left: 700, top: top + 30, width: 30, height: 20 } }).append('删除'));
  }
  ops.append(new El('span', { cls: 'op', rect: { left: 745, top: top + 30, width: 40, height: 20 } }).append('更多'));
  meta.append(titleEl, timeEl, ops);
  card.append(cover, meta);
  return card;
}

listRoot.append(
  makeCard(0, '2023年春节带货复盘：单品破万单的三个动作', '2023年2月18日 20:15', true),
  makeCard(1, '实体店老板必看：客流下滑的真正原因', '2022-11-03 09:40', true),
  makeCard(2, '昨天的直播回放片段', '昨天 18:20', false),
  makeCard(3, '2026年新号起盘思路', '2026-01-08 10:00', false)
);

const app = new El('div', {});
app.append(listRoot);
document.body = new El('body', {});
document.body.append(app);
document.documentElement = document.body;

// ---------------------------------------------------------------- run
(new Function(JS_LIB))();

const res = window.__WB.scanList({
  roots: ['.post-list', '#app', ''],
  maxUp: 8,
  minW: 240,
  minH: 56,
  maxH: 420,
  deleteTexts: ['删除'],
  moreTexts: ['更多', '…', '...'],
});

// ---------------------------------------------------------------- assert
let bad = 0;
function chk(name, cond, detail) {
  if (cond) {
    console.log('  PASS  ' + name + (detail ? '  ' + detail : ''));
  } else {
    bad++;
    console.log('  FAIL  ' + name + '  ' + (detail || ''));
  }
}

console.log('scanList 返回 ' + res.count + ' 条：');
for (const it of res.items) {
  console.log(`   time=${JSON.stringify(it.timeText)}  title=${JSON.stringify((it.title || '').slice(0, 34))}  action=${JSON.stringify(it.actionText)}  rect=${it.rect.top}x${it.rect.h}`);
}

chk('识别出 4 个条目', res.count === 4, '实际 ' + res.count);
chk('时间文本保真', res.items[1] && res.items[1].timeText === '2022-11-03 09:40', res.items[1] && res.items[1].timeText);
chk('相对时间也能识别', res.items.some((i) => /昨天|天前|刚刚/.test(i.timeText)), JSON.stringify(res.items.map((i) => i.timeText)));
chk(
  '标题不含按钮词/纯日期',
  res.items.every((i) => {
    const t = (i.title || '').trim();
    if (!t || t === '删除' || t === '更多') return false;
    if (/^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}/.test(t)) return false;
    return true;
  }),
  JSON.stringify(res.items.map((i) => i.title))
);
chk('标题提取正确', (res.items[0].title || '').indexOf('2023年春节带货复盘') === 0, res.items[0].title);
chk('卡片向上归位成功（rect 是整张卡）', res.items.every((i) => i.rect.h >= 56 && i.rect.w >= 240), JSON.stringify(res.items.map((i) => i.rect.h)));
chk('能定位到删除入口', res.items[0].actionText === '删除' && res.items[1].actionText === '删除');
chk('无删除按钮时退化为"更多"', res.items[2].actionText === '更多', res.items[2].actionText);

// 中心点应当落在卡片内部
const c = window.__WB.center(res.items[0].cardId, false);
chk('取到可点击中心点', c && c.x > 0 && c.y > 0 && c.inView, c && `${Math.round(c.x)},${Math.round(c.y)}`);

console.log(bad === 0 ? '\nDOM FIXTURE TEST: ALL PASS' : `\nDOM FIXTURE TEST: ${bad} FAILED`);
process.exit(bad === 0 ? 0 : 1);
