// R171 测试用最小 DOM 替身（只在 node 里跑，不进浏览器）。
//
// 为什么自己写而不是装 jsdom：本项目**没有** node_modules / package.json
// （前端零构建），为一个护栏引依赖就会让「打包机没网」直接跑不了测试。
// 这里只需要 _dislikeCol 用到的那几个 API：createElement / className /
// textContent / appendChild / querySelector(All) / addEventListener(click)
// / type / checked / value / setAttribute —— 实现成够用的样子即可。
//
// ⚠ 判据全落在**真跑出来的树**上：组头数、组名文字、按钮文字、勾选态。
//   只 grep 中文（"源码里有全选"）抓不到「全选改成选全部」这类变异。

function _El(tag) {
  this.tagName = String(tag || '').toUpperCase();
  this.className = '';
  this._text = '';
  this._attrs = {};
  this.childNodes = [];
  this.checked = false;
  this.value = '';
  this.type = '';
  this.placeholder = '';
  this._handlers = {};
}

Object.defineProperty(_El.prototype, 'textContent', {
  get: function () {
    var out = this._text || '';
    for (var i = 0; i < this.childNodes.length; i++) {
      out += this.childNodes[i].textContent;
    }
    return out;
  },
  set: function (v) { this._text = String(v == null ? '' : v); }
});

_El.prototype.appendChild = function (c) {
  this.childNodes.push(c);
  c.parentNode = this;
  return c;
};

_El.prototype.setAttribute = function (k, v) {
  this._attrs[k] = String(v);
  if (k === 'data-col') { this._datacol = String(v); }
};
_El.prototype.getAttribute = function (k) {
  return Object.prototype.hasOwnProperty.call(this._attrs, k)
    ? this._attrs[k] : null;
};

_El.prototype.addEventListener = function (ev, fn) {
  (this._handlers[ev] = this._handlers[ev] || []).push(fn);
};
_El.prototype._fire = function (ev) {
  var hs = this._handlers[ev] || [];
  for (var i = 0; i < hs.length; i++) {
    hs[i].call(this, { preventDefault: function () {}, stopPropagation: function () {} });
  }
};

function _matches(node, sel) {
  if (sel.charAt(0) === '.') { return node.className === sel.slice(1); }
  return node.tagName === sel.toUpperCase();
}

function _walk(root, sel, out) {
  for (var i = 0; i < root.childNodes.length; i++) {
    var c = root.childNodes[i];
    if (_matches(c, sel)) { out.push(c); }
    _walk(c, sel, out);
  }
  return out;
}

_El.prototype.querySelectorAll = function (sel) {
  var out = _walk(this, sel, []);
  out.slice = Array.prototype.slice;   // 测试里会 .slice() / .map()
  out.map = Array.prototype.map;
  out.forEach = Array.prototype.forEach;
  return out;
};
_El.prototype.querySelector = function (sel) {
  var got = this.querySelectorAll(sel);
  return got.length ? got[0] : null;
};

function _installDom() {
  global.document = {
    createElement: function (t) { return new _El(t); },
    createTextNode: function (t) {
      var n = new _El('#text');
      n._text = String(t == null ? '' : t);
      return n;
    }
  };
  global._El = _El;
}

// 只给 _dislikeCol / _blackGroups 这类纯函数用：它们不碰 document.body。
module.exports = { install: _installDom, El: _El };
