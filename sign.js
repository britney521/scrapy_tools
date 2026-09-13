/**
 * 在 Node 中以「纯计算」方式驱动快手 sig4 签名 VM（Jose），产出 __NS_hxfalcon。
 *
 * 用法:
 *   node sign.js '<json>'
 * json = {
 *   "cookie": "did=xxx; ...",        // 浏览器当前 cookie（白名单为空，仅影响兜底逻辑）
 *   "url": "/rest/v/search/feed",
 *   "query": {"caver": "2"},
 *   "form": {},
 *   "requestBody": {"keyword": "乌拉", "page": "search", "webPageArea": "", "pcursor": ""}
 * }
 * 输出: {"caver": "...", "sign": "..."}
 */

'use strict';

function makeStorage() {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => data.set(k, String(v)),
    removeItem: (k) => data.delete(k),
    clear: () => data.clear(),
    key: (i) => Array.from(data.keys())[i] ?? null,
    get length() {
      return data.size;
    },
  };
}

/**
 * @param {object} opts
 * @param {string} opts.cookie
 * @param {string} [opts.href]
 * @param {number} [opts.scriptCount]
 */
function installBrowserEnv(opts) {
  const g = globalThis;

  // Node 22 上 navigator / performance 等是只读 getter，必须用 defineProperty 覆盖
  const def = (name, value) =>
    Object.defineProperty(g, name, { value, writable: true, configurable: true, enumerable: false });

  def('window', g);
  def('self', g);
  def('top', g);
  def('parent', g);

  def('document', {
    cookie: opts.cookie || '',
    scripts: new Array(opts.scriptCount == null ? 24 : opts.scriptCount),
    getElementById: () => null,
    createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
    documentElement: { style: {} },
    addEventListener() {},
    referrer: '',
    title: '乌拉 - 快手',
  });

  def('location', {
    href: opts.href || 'https://www.kuaishou.com/search/%E4%B9%8C%E6%8B%89?source=NewReco',
    origin: 'https://www.kuaishou.com',
    host: 'www.kuaishou.com',
    hostname: 'www.kuaishou.com',
    pathname: '/search/%E4%B9%8C%E6%8B%89',
    search: '?source=NewReco',
    protocol: 'https:',
  });

  def('navigator', {
    userAgent:
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
      '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
    platform: 'MacIntel',
    language: 'zh-CN',
    languages: ['zh-CN', 'zh'],
    hardwareConcurrency: 8,
    deviceMemory: 8,
    maxTouchPoints: 0,
    webdriver: false,
    sendBeacon: () => true,
  });

  const ls = makeStorage();
  def('localStorage', ls);
  def('sessionStorage', makeStorage());
  if (opts.kwfv1) ls.setItem('kwfv1', opts.kwfv1);

  def('screen', { width: 1512, height: 982, availWidth: 1512, availHeight: 900, colorDepth: 24 });
}

function signOnce(Jose, payload) {
  return new Promise((resolve, reject) => {
    try {
      Jose.call('$encode', [
        payload,
        {
          suc(res) {
            resolve(res);
          },
          err(e) {
            reject(e instanceof Error ? e : new Error(JSON.stringify(e)));
          },
        },
      ]);
    } catch (e) {
      reject(e);
    }
  });
}

function main() {
  const arg = process.argv[2] || '{}';
  const opts = JSON.parse(arg);

  // 必须在 require 之前装好浏览器环境：Jose 模块初始化时会读取 window/document
  installBrowserEnv(opts);

  const Jose = require('./jose.js');

  const caver = Jose.call('$getCatVersion') || '';
  const payload = {
    url: opts.url,
    query: Object.assign({ caver }, opts.query || {}),
    form: opts.form || {},
    requestBody: opts.requestBody || {},
  };

  signOnce(Jose, payload).then(
    (res) => {
      process.stdout.write(JSON.stringify({ caver, sign: res }));
    },
    (e) => {
      process.stderr.write(String((e && e.stack) || e));
      process.exit(1);
    }
  );
}

if (require.main === module) main();

module.exports = { installBrowserEnv };
