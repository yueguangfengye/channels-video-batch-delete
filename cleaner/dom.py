"""页面探针（注入到 Edge 页面里执行的 JS）。

⚠️ 视频号后台用的是「无界（wujie）」微前端：真正的页面内容在
`<wujie-app>` 自定义元素的 **Shadow DOM** 里，`document.querySelectorAll`
完全穿不过去。所以本模块所有查询都走 `W.root()`，它优先返回 shadowRoot，
没有 shadow 时才退回 document。

坐标：Shadow DOM 不是独立视口，元素 `getBoundingClientRect()` 返回的就是
主文档视口坐标，可以直接交给 `Input.dispatchMouseEvent`，不需要任何偏移换算。
"""
from __future__ import annotations

JS_LIB = r"""
var W = window.__WB || (window.__WB = {});
W.__v = 4;
if (typeof W.seq !== 'number') W.seq = 0;
if (!W.map) W.map = new Map();

/* 内容根：wujie 微前端 -> shadowRoot；普通页面 -> document */
W.root = function () {
  var host = null;
  try { host = document.querySelector('wujie-app'); } catch (e) {}
  if (!host) { try { host = document.querySelector('.wujie_iframe'); } catch (e) {} }
  if (host && host.shadowRoot) return host.shadowRoot;
  return document;
};

W.rootKind = function () {
  var r = W.root();
  return (r === document) ? 'document' : 'shadow';
};

/* 查询：同时覆盖主文档和 shadow。
   列表在 shadow 里，但弹窗 / toast 这类组件常被 teleport 到 body 下，
   只查一边必然漏。 */
W.qsa = function (sel) {
  var out = [], seen = new Set();
  function push(list) {
    if (!list) return;
    for (var i = 0; i < list.length; i++) {
      if (!seen.has(list[i])) { seen.add(list[i]); out.push(list[i]); }
    }
  }
  try { push(document.querySelectorAll(sel)); } catch (e) {}
  var r = W.root();
  if (r !== document) {
    try { push(r.querySelectorAll(sel)); } catch (e) {}
  }
  return out;
};

W.stamp = function (el) {
  if (!el) return null;
  var old = el.getAttribute && el.getAttribute('data-wb-id');
  if (old && W.map.get(old) === el) return old;
  var id = 'wb' + (++W.seq);
  try { el.setAttribute('data-wb-id', id); } catch (e) {}
  W.map.set(id, el);
  return id;
};

W.get = function (id) {
  if (!id) return null;
  var el = W.map.get(id);
  if (el) {
    try { if (document.contains(el) || (el.getRootNode && el.getRootNode().contains(el))) return el; } catch (e) {}
  }
  try { return document.querySelector('[data-wb-id="' + id + '"]'); } catch (e) { return null; }
};

W.txt = function (el) {
  if (!el) return '';
  return (el.textContent || '').replace(/\s+/g, ' ').trim();
};

W.visible = function (el) {
  if (!el || !el.getBoundingClientRect) return false;
  var r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false;
  var cs = window.getComputedStyle(el);
  if (cs.visibility === 'hidden' || cs.display === 'none') return false;
  if (parseFloat(cs.opacity || '1') < 0.05) return false;
  return true;
};

W.rectOf = function (el) {
  var r = el.getBoundingClientRect();
  return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
           left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right), bottom: Math.round(r.bottom),
           cx: Math.round(r.left + r.width / 2), cy: Math.round(r.top + r.height / 2) };
};

W.center = function (id, doScroll) {
  var el = W.get(id);
  if (!el) return null;
  if (doScroll !== false) {
    try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) { try { el.scrollIntoView(); } catch (e2) {} }
  }
  var r = el.getBoundingClientRect();
  var vw = window.innerWidth, vh = window.innerHeight;
  return {
    x: r.left + r.width / 2, y: r.top + r.height / 2,
    w: r.width, h: r.height,
    inView: r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw,
    rect: W.rectOf(el)
  };
};

W.cardOf = function (node, minW, minH, maxH, maxUp) {
  var el = node, firstOk = null;
  for (var i = 0; i < maxUp && el; i++) {
    el = el.parentElement;
    if (!el) break;
    var r = el.getBoundingClientRect();
    var sizeOk = r.width >= minW && r.height >= minH && r.height <= maxH;
    if (sizeOk && !firstOk) firstOk = el;
    var tc = el.textContent || '';
    if (sizeOk && (tc.indexOf('\u5220\u9664') >= 0 || tc.indexOf('\u66f4\u591a') >= 0)) return el;
  }
  return firstOk || node.parentElement || node;
};

W.titleOf = function (card, timeNode) {
  // 1) 语义 class 优先（视频号后台是 .post-title）
  var prefer = null;
  try { prefer = card.querySelector('.post-title, .feed-title, .video-title'); } catch (e) { prefer = null; }
  if (prefer && W.visible(prefer)) {
    var pt = (prefer.textContent || '').replace(/\s+/g, ' ').trim();
    if (pt) return pt.slice(0, 200);
  }
  // 2) DOM 顺序第一个"可见且像标题"的叶子
  //    卡片里藏着 display:none 的 tooltip（"视频ID是视频的唯一标识…"），
  //    rect 为 0x0，必须用 visible() 挡掉，否则会被当成标题。
  var nodes = card.querySelectorAll('div,span,p,a,h1,h2,h3,h4,strong,em');
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    if (el.children && el.children.length > 0) continue;
    if (el === timeNode) continue;
    if (timeNode && timeNode.contains(el)) continue;
    if (!W.visible(el)) continue;
    var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length < 4 || t.length > 200) continue;
    if (/^(\u5220\u9664|\u66f4\u591a|\u7ba1\u7406|\u7f16\u8f91|\u7f6e\u9876|\u5206\u4eab|\u6570\u636e|\u53d6\u6d88|\u786e\u5b9a|\u5f39\u5e55\u7ba1\u7406|\.\.\.|\u2026)$/.test(t)) continue;
    if (/^20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}/.test(t)) continue;
    if (/^\d+(\.\d+)?[\u4e07\u4ebf]?$/.test(t)) continue;
    return t;
  }
  return '';
};

W.actOf = function (card, texts) {
  var nodes = card.querySelectorAll('button,a,span,div,i,em');
  var cand = [];
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 4) continue;
    if (texts.indexOf(t) < 0) continue;
    if (!W.visible(el)) continue;
    var inner = false;
    for (var c = 0; c < el.children.length; c++) {
      if (texts.indexOf((el.children[c].textContent || '').trim()) >= 0) { inner = true; break; }
    }
    if (inner) continue;
    cand.push(el);
  }
  for (var q = 0; q < texts.length; q++) {
    for (var p = 0; p < cand.length; p++) {
      if ((cand[p].textContent || '').replace(/\s+/g, ' ').trim() !== texts[q]) continue;
      // ⚠️ 关键：命中的往往只是文字标签（div.action-content，24x17），
      // 点它不会触发任何事件。真正带点击区域的是外层 .opr-item-wrap
      // （图标 + 文字，56x57）。必须向上归位到可点容器。
      var up = cand[p];
      for (var d = 0; d < 3 && up.parentElement; d++) {
        var pcls = (up.parentElement.className || '').toString();
        if (pcls.indexOf('opr-item') >= 0 || pcls.indexOf('action-item') >= 0) up = up.parentElement;
        else break;
      }
      return { id: W.stamp(up), text: texts[q], rect: W.rectOf(up) };
    }
  }
  return null;
};

W.scanList = function (opts) {
  opts = opts || {};
  var roots = opts.roots || [''];
  var maxUp = opts.maxUp || 8;
  var minW = opts.minW || 240;
  var minH = opts.minH || 40;
  var maxH = opts.maxH || 460;
  var deleteTexts = opts.deleteTexts || ['\u5220\u9664'];
  var moreTexts = opts.moreTexts || ['\u66f4\u591a', '\u2026', '...'];
  var absRe = /20\d{2}\s*[-/.\u5e74]\s*\d{1,2}\s*[-/.\u6708]\s*\d{1,2}/;
  var relRe = /^(\d{1,3}\s*(?:\u5929|\u5c0f\u65f6|\u5206\u949f)\u524d|\u6628\u5929|\u524d\u5929|\u521a\u521a)(?:\s+\d{1,2}:\d{2})?$/;

  var root = W.root();
  var kind = (root === document) ? 'document' : 'shadow';
  for (var i = 0; i < roots.length; i++) {
    if (!roots[i]) continue;
    var rr = null;
    try { rr = root.querySelector(roots[i]); } catch (e) { rr = null; }
    if (rr) { root = rr; break; }
  }

  var timeNodes = [], seen = new Set();
  var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  var tn;
  while ((tn = walker.nextNode())) {
    var s = tn.nodeValue;
    if (!s) continue;
    var trimmed = s.trim();
    if (!trimmed) continue;
    var isAbs = trimmed.length <= 40 && absRe.test(trimmed);
    var isRel = !isAbs && trimmed.length <= 16 && relRe.test(trimmed);
    if (!isAbs && !isRel) continue;
    var p = tn.parentElement;
    if (!p || seen.has(p)) continue;
    seen.add(p);
    if (!W.visible(p)) continue;
    timeNodes.push(p);
  }

  var items = [], usedCards = new Set();
  for (var k = 0; k < timeNodes.length; k++) {
    var node = timeNodes[k];
    var card = W.cardOf(node, minW, minH, maxH, maxUp);
    var cid = W.stamp(card);
    if (usedCards.has(cid)) continue;
    usedCards.add(cid);
    var act = null;
    try { act = W.actOf(card, deleteTexts.concat(moreTexts)); } catch (e) { act = null; }
    items.push({
      title: W.titleOf(card, node),
      timeText: (node.textContent || '').replace(/\s+/g, ' ').trim(),
      cardId: cid,
      rect: W.rectOf(card),
      actionId: act ? act.id : null,
      actionText: act ? act.text : null
    });
  }
  return { root: kind, count: items.length, items: items, totalTimeNodes: timeNodes.length };
};

W.findTexts = function (names, limit) {
  limit = limit || 20;
  var out = [];
  var nodes = W.qsa('button,a,div,span,li,p,i,em,strong');
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    var raw = el.textContent || '';
    if (!raw || raw.length > 24) continue;
    var t = raw.replace(/\s+/g, ' ').trim();
    if (!t || names.indexOf(t) < 0) continue;
    if (!W.visible(el)) continue;
    var dup = false;
    for (var c = 0; c < el.children.length; c++) {
      if ((el.children[c].textContent || '').replace(/\s+/g, ' ').trim() === t) { dup = true; break; }
    }
    if (dup) continue;
    out.push({ id: W.stamp(el), text: t, tag: el.tagName.toLowerCase(),
               cls: (el.className || '').toString().slice(0, 80),
               rect: W.rectOf(el), cursor: window.getComputedStyle(el).cursor });
    if (out.length >= limit) break;
  }
  return out;
};

W.findDialog = function (names) {
  var vw = window.innerWidth, vh = window.innerHeight, best = null;
  var nodes = W.qsa('div,section,dialog,article');
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    var cs = window.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    var r = el.getBoundingClientRect();
    // 确认弹窗有实际尺寸：太小的是图标/气泡，占满整屏的是遮罩
    if (r.width < 260 || r.height < 90) continue;
    if (r.width > vw * 0.95 && r.height > vh * 0.8) continue;
    // 必须横跨屏幕中线。左侧菜单里也有「删除」，不设这道闸会点歪到侧边栏，
    // 实测会把页面导航到草稿箱。
    if (r.left > vw * 0.5 || r.right < vw * 0.5) continue;
    var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 600) continue;
    // 真正的确认弹窗一定有「取消」和「确定/确认」
    var hasCancel = t.indexOf('\u53d6\u6d88') >= 0;
    var hasOk = t.indexOf('\u786e\u5b9a') >= 0 || t.indexOf('\u786e\u8ba4') >= 0;
    if (!hasCancel && !hasOk) continue;
    var hit = false;
    for (var j = 0; j < names.length; j++) { if (t.indexOf(names[j]) >= 0) { hit = true; break; } }
    if (!hit) continue;
    var area = r.width * r.height;
    if (!best || area < best.area) best = { el: el, r: r, t: t, area: area };
  }
  if (!best) return null;
  // 有些弹窗把「标题栏」和「按钮区」拆成两层，面积最小的匹配往往只是标题栏。
  // 向上扩展到真正同时包住「取消」和「确定」的容器。
  var el = best.el;
  for (var d = 0; d < 6 && el.parentElement; d++) {
    var t2 = el.textContent || '';
    if (t2.indexOf('\u53d6\u6d88') >= 0 && (t2.indexOf('\u786e\u5b9a') >= 0 || t2.indexOf('\u786e\u8ba4') >= 0)) break;
    if (el.getBoundingClientRect().height > 140) break;
    el = el.parentElement;
  }
  return { id: W.stamp(el), text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 300), rect: W.rectOf(el) };
};

/* 分页器：视频号后台的列表是「上一页 1 2 3 4 ... 138 下一页」，页码是纯文本
   节点（点不了），只能通过上一页/下一页逐页翻。 */
W.pager = function () {
  var r = W.root(), box = null;
  r.querySelectorAll('span,div').forEach(function (el) {
    if (box) return;
    var cls = (el.className || '').toString();
    if (cls.indexOf('pagination__num') >= 0 && W.visible(el)) box = el;
  });
  if (!box) return { found: false };

  var prev = null, next = null;
  if (box.parentElement) {
    box.parentElement.querySelectorAll('a').forEach(function (el) {
      var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (t === '\u4e0a\u4e00\u9875') prev = el;
      if (t === '\u4e0b\u4e00\u9875') next = el;
    });
  }
  var text = (box.innerText || '').replace(/\s+/g, ' ').trim();
  var nums = text.match(/\d+/g) || [];
  var total = nums.length ? parseInt(nums[nums.length - 1], 10) : 0;

  var active = null;
  box.querySelectorAll('*').forEach(function (el) {
    if (active) return;
    if (/active|current|selected/i.test((el.className || '').toString())) active = el;
  });

  return {
    found: true,
    text: text.slice(0, 120),
    total: total,
    current: active ? (active.textContent || '').trim() : '',
    prevId: prev ? W.stamp(prev) : null,
    nextId: next ? W.stamp(next) : null,
    prevDisabled: prev ? /disabled/i.test((prev.className || '').toString()) : true,
    nextDisabled: next ? /disabled/i.test((next.className || '').toString()) : true
  };
};

/* 用 DOM 的 click() 触发（分页按钮是 <a href="javascript:;">，坐标点宽度可能只有 8px） */
W.clickEl = function (id) {
  var el = W.get(id);
  if (!el) return false;
  try { el.click(); return true; } catch (e) { return false; }
};

/* 当前页"签名"：第一条的日期文本。翻页后拿它判断数据是否真的换了。
   比整页扫描轻得多，用来避免"翻页后数据还没到就扫描"的竞态。 */
W.firstItemSig = function () {
  var r = W.root();
  var re = /20\d{2}\s*[-/.\u5e74]\s*\d{1,2}\s*[-/.\u6708]\s*\d{1,2}/;
  var walker = document.createTreeWalker(r, NodeFilter.SHOW_TEXT, null);
  var tn;
  while ((tn = walker.nextNode())) {
    var v = (tn.nodeValue || '').trim();
    if (v && v.length <= 40 && re.test(v)) return v;
  }
  return '';
};

/* 分页器细节：页码元素 / 跳转输入框 / 跳转按钮 */
W.pagerDetail = function () {
  var r = W.root();
  var seed = null;
  W.qsa('span,div').forEach(function (el) {
    if (seed) return;
    var cls = (el.className || '').toString();
    if (cls.indexOf('pagination') >= 0 && W.visible(el) && el.getBoundingClientRect().height > 10) seed = el;
  });
  if (!seed) return { found: false };

  // 向上扩到最外层 pagination 容器（跳转框通常在页码的兄弟节点里）
  var box = seed;
  for (var i = 0; i < 5 && box.parentElement; i++) {
    var pcls = (box.parentElement.className || '').toString();
    var prc = box.parentElement.getBoundingClientRect();
    if (pcls.indexOf('pagination') < 0) break;
    if (prc.height > 140) break;
    box = box.parentElement;
  }

  var nums = [], inputs = [], jumps = [];
  box.querySelectorAll('*').forEach(function (el) {
    var rc = el.getBoundingClientRect();
    if (rc.width < 2 || rc.height < 2) return;
    var t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (/^\d{1,4}$/.test(t)) {
      nums.push({ t: t, tag: el.tagName, cls: (el.className || '').toString().slice(0, 44),
                  rect: W.rectOf(el), id: W.stamp(el) });
    }
    if (el.tagName === 'INPUT') {
      inputs.push({ type: el.type || '', ph: el.placeholder || '',
                    cls: (el.className || '').toString().slice(0, 44),
                    rect: W.rectOf(el), id: W.stamp(el) });
    }
    if (t === '\u8df3\u8f6c' || t === 'GO' || t === 'Go') {
      jumps.push({ t: t, tag: el.tagName, cls: (el.className || '').toString().slice(0, 44),
                   rect: W.rectOf(el), id: W.stamp(el) });
    }
  });
  return { found: true, cls: (box.className || '').toString().slice(0, 70),
           boxRect: W.rectOf(box), nums: nums, inputs: inputs, jumps: jumps };
};

/* 直接点可见的页码按钮 */
W.clickPageNum = function (n) {
  n = parseInt(n, 10);
  var d = W.pagerDetail();
  if (!d.found) return { ok: false, reason: 'no-pager' };
  for (var i = 0; i < d.nums.length; i++) {
    if (d.nums[i].t === String(n)) {
      if (W.clickEl(d.nums[i].id)) return { ok: true, how: 'page-link', page: n };
    }
  }
  return { ok: false, reason: 'page-not-visible',
           visible: d.nums.map(function (x) { return x.t; }) };
};

/* 跳转框：聚焦 + 设值 + 提交。真正输入由 CDP 的 Input.insertText 完成 */
W.prepareJumpInput = function () {
  var d = W.pagerDetail();
  if (!d.found) return { ok: false, reason: 'no-pager' };
  if (!d.inputs.length) return { ok: false, reason: 'no-input', jumps: d.jumps.length };
  var inp = d.inputs[0];
  var el = W.get(inp.id);
  if (!el) return { ok: false, reason: 'stale' };
  try { el.focus(); el.select && el.select(); } catch (e) {}
  // 跳转按钮取**最内层**：外层 span.weui-desktop-pagination__form 只是容器，
  // 点它的中心会落在 <a> 左边的空白上，什么都不会发生。
  var jumpId = d.jumps.length ? d.jumps[d.jumps.length - 1].id : null;
  return { ok: true, id: inp.id, rect: inp.rect, jumpId: jumpId };
};

/* 提交跳转：优先点「跳转」按钮，其次派发回车 */
W.submitJump = function (jumpId) {
  if (jumpId) {
    var b = W.get(jumpId);
    if (b) { try { b.click(); return { ok: true, how: 'button' }; } catch (e) {} }
  }
  var d = W.pagerDetail();
  if (d.found && d.inputs.length) {
    var el = W.get(d.inputs[0].id);
    if (el) {
      ['keydown', 'keypress', 'keyup'].forEach(function (t) {
        try {
          el.dispatchEvent(new KeyboardEvent(t, { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
        } catch (e) {}
      });
      return { ok: true, how: 'enter' };
    }
  }
  return { ok: false, reason: 'no-submit-target' };
};

W.scrollRoot = function () {
  var best = null, bestGap = 0;
  var nodes = W.qsa('div,main,section,ul');
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    var cs = window.getComputedStyle(el);
    if (!/auto|scroll|overlay/.test(cs.overflowY)) continue;
    var gap = el.scrollHeight - el.clientHeight;
    if (gap > 100 && gap > bestGap) { best = el; bestGap = gap; }
  }
  return best;
};

W.scrollDown = function (ratio) {
  ratio = (ratio == null) ? 0.85 : ratio;
  var root = W.scrollRoot();
  var tgt = root || document.scrollingElement || document.documentElement;
  var before = tgt.scrollTop;
  var step = Math.round((tgt.clientHeight || window.innerHeight) * ratio);
  tgt.scrollTop = before + step;
  return { before: before, after: tgt.scrollTop, height: tgt.scrollHeight,
           client: tgt.clientHeight, atBottom: (tgt.scrollTop + tgt.clientHeight) >= (tgt.scrollHeight - 8) };
};

/* shadow 里取可见文本（textContent 会把 <style> 的 CSS 源码也带上，不能用） */
W.visibleText = function (limit) {
  limit = limit || 1520;
  var r = W.root();
  if (r === document) return ((document.body && document.body.innerText) || '').slice(0, limit);
  var out = [], total = 0;
  var nodes = r.querySelectorAll('div,span,p,a,button,li,label,h1,h2,h3,h4,strong,em');
  for (var i = 0; i < nodes.length && i < 900; i++) {
    var el = nodes[i];
    if (el.children && el.children.length > 0) continue;
    var t = (el.textContent || '').trim();
    if (!t) continue;
    if (!W.visible(el)) continue;
    out.push(t);
    total += t.length + 1;
    if (total >= limit) break;
  }
  return out.join(' ').slice(0, limit);
};

W.pageInfo = function (headLen) {
  headLen = headLen || 1200;
  var text = W.visibleText(headLen + 200);
  return { url: location.href, title: document.title,
           bodyLen: text.length, bodyHead: text.slice(0, headLen),
           viewport: { w: window.innerWidth, h: window.innerHeight },
           scrollY: window.scrollY, hasFocus: document.hasFocus(),
           rootKind: W.rootKind() };
};

/* 关键词命中检查：给 guard / 结果校验用，比拉全文快。
   必须同时看主文档和 shadow —— toast 提示挂在 body 下，正文在 shadow 里。 */
W.hits = function (words) {
  var t = ((document.body && document.body.innerText) || '');
  var r = W.root();
  if (r !== document) t += '\n' + (r.textContent || '');
  var out = [];
  for (var i = 0; i < words.length; i++) {
    if (words[i] && t.indexOf(words[i]) >= 0) out.push(words[i]);
  }
  return { url: location.href, title: document.title, hits: out,
           hasFocus: document.hasFocus(), rootKind: W.rootKind() };
};

W.probe = function () {
  var del = W.findTexts(['\u5220\u9664', '\u66f4\u591a', '\u2026'], 40);
  var tabs = [], nodes = W.qsa('a,button,div,span,li');
  for (var i = 0; i < nodes.length && tabs.length < 60; i++) {
    var el = nodes[i];
    var raw = el.textContent || '';
    if (!raw || raw.length > 12) continue;
    var t = raw.replace(/\s+/g, ' ').trim();
    if (!/^[\u4e00-\u9fa5A-Za-z0-9 ]{1,12}$/.test(t)) continue;
    if (!W.visible(el)) continue;
    var r = el.getBoundingClientRect();
    if (r.top > 220) continue;
    tabs.push({ text: t, cls: (el.className || '').toString().slice(0, 60), rect: W.rectOf(el) });
  }
  return { info: W.pageInfo(2000), actions: del, topTabs: tabs, rootKind: W.rootKind(),
           scrollRoot: W.scrollRoot() ? 'found' : 'none' };
};
"""


def wrap(body: str) -> str:
    """把一段使用 W.* 的脚本包成可执行表达式。"""
    return "(function(){" + JS_LIB + "\n;return (" + body + ");})()"


def call(fn: str, *args_json: str) -> str:
    """构造 W.xxx(a, b) 表达式。args 需已序列化为 JSON 文本。"""
    return wrap("W." + fn + "(" + ", ".join(args_json) + ")")
