'use strict';
/* gh_login_driver.cjs — 持久测试身份（件①M1/M2）的 Playwright 登录/验证驱动。
 *
 * 设计出处：archive/external-artifacts/top-model-results-20260930/product-testbench1-result-20261009.md 件①；
 * 实测配方：TEST-LOGIN-1（product-testlogin1-result-20260909 → product-testlogin1-result-20261009.md）
 *   headless=False 账密直登 SUCCESS、storageState 新 context 复用闭环、meta[user-login] 判据。
 *
 * 由 tests/testbench/github_identity.py 经 subprocess 编排：
 *   node gh_login_driver.cjs login  --secrets <Tier A 文件路径> --state-out <Tier B 落盘路径> [--headless]
 *   node gh_login_driver.cjs verify --state <storageState 路径>
 *
 * 凭据纪律（S4）：
 *   - 凭据只经 --secrets 指到的文件路径自读（文件→内存），argv/env/stdout/日志零明文；
 *   - stdout 仅最后一个 JSON 行（login 一律脱敏为前 2 字符+***）；
 *   - CAPTCHA/2FA/设备验证类挑战=分类为 challenge 即停（红线：不代批、不暴力重试）。
 *
 * 退出码：0=成功/会话有效；2=配置或环境致命（凭据/playwright/chromium 缺失）；
 *         3=会话无效（探测 401/登录态缺失）；4=遇反自动化挑战（即停转人工）。
 */

const fs = require('fs');
const path = require('path');

const LOGIN_URL = 'https://github.com/login';
/* 探活正典端点=github.com/settings/profile（设计件①R2 零导航探测配方；200=会话有效，
 * 登出态 302 到登录墙=无效）。api.github.com/user 实测 401——api 主机不收 web 会话
 * cookie（TB-IDT-M1 实证），禁用作探针。 */
const PROBE_URL = 'https://github.com/settings/profile';
const NAV_TIMEOUT_MS = 30000;
const SETTLE_POLL_MS = 500;
const SETTLE_TIMEOUT_MS = 30000;

function redact(login) {
  return login && login.length > 2 ? login.slice(0, 2) + '***' : '***';
}

function emit(payload) {
  process.stdout.write(JSON.stringify(payload) + '\n');
}

function fatal(error, detail) {
  emit(Object.assign({ ok: false, error }, detail ? { detail } : {}));
  process.exit(2);
}

function parseArgs(argv) {
  const mode = argv[0];
  const opts = { headless: false };
  for (let i = 1; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--headless') opts.headless = true;
    else if (a === '--secrets') opts.secrets = argv[++i];
    else if (a === '--state-out') opts.stateOut = argv[++i];
    else if (a === '--state') opts.state = argv[++i];
    else fatal('bad-args', 'unrecognized argument: ' + a);
  }
  if (mode !== 'login' && mode !== 'verify') fatal('bad-args', 'mode must be login|verify');
  return { mode, opts };
}

function loadSecrets(secretsPath) {
  // 学校凭据/其他形态一律拒收（本驱动只认 GitHub 测试身份的 username+password 形态）。
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(secretsPath, 'utf8'));
  } catch (e) {
    fatal('secrets-unreadable', String(e.code || e.message));
  }
  const schoolish = Object.keys(raw).filter((k) =>
    /uis|school|student|campus|学号|校园/i.test(k)
  );
  if (schoolish.length) {
    fatal('secrets-refused-school-shape', 'refused keys present (names only): ' + schoolish.join(','));
  }
  if (typeof raw.username !== 'string' || !raw.username.trim() ||
      typeof raw.password !== 'string' || !raw.password) {
    fatal('secrets-shape', 'need non-empty username+password');
  }
  return { username: raw.username, password: raw.password };
}

function requirePlaywright() {
  let pw;
  try {
    pw = require('playwright');
  } catch (e) {
    fatal('playwright-missing',
      'node 找不到 playwright 模块；请设 COURSELENS_TESTBENCH_NODE_MODULES 指向含 playwright 的 node_modules');
  }
  return pw;
}

async function probeSession(request) {
  // 零导航探测：独立 cookie 通道打 settings/profile（设计件①复用探测配方）。
  try {
    const resp = await request.get(PROBE_URL, {
      timeout: NAV_TIMEOUT_MS,
      failOnStatusCode: false,
      maxRedirects: 0, // 登出态会 302 到登录墙——原始 302 即无效信号
    });
    return { status: resp.status() };
  } catch (e) {
    return { status: -1, errorKind: 'network', message: String(e.message || e).split('\n')[0] };
  }
}

async function classifyLoginSettle(page) {
  // 提交后轮询归类：SUCCESS(meta[user-login]) / CHALLENGE(2FA·设备验证·CAPTCHA) / FAILED(登录页错误)。
  const deadline = Date.now() + SETTLE_TIMEOUT_MS;
  let lastUrl = '';
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, SETTLE_POLL_MS));
    let url = '';
    try { url = page.url(); } catch (e) { continue; }
    lastUrl = url;
    if (/\/(two-factor|sessions\/verified-device|session-check)/i.test(url)) {
      return { verdict: 'CHALLENGE', kind: url.match(/two-factor/i) ? '2fa' : 'device-verification', finalUrl: url };
    }
    try {
      const meta = await page.$('meta[name="user-login"]');
      if (meta) {
        let loginEcho = null;
        try { loginEcho = await meta.getAttribute('content'); } catch (e) { /* 保持 null */ }
        return { verdict: 'SUCCESS', finalUrl: url, loginEcho };
      }
    } catch (e) { /* 页面切换中，继续轮询 */ }
    if (/github\.com\/login/i.test(url)) {
      try {
        const flash = await page.$('.js-flash-error, .flash-error');
        if (flash) return { verdict: 'FAILED', kind: 'bad-credentials', finalUrl: url };
      } catch (e) { /* 继续轮询 */ }
    }
    if (/\/captcha|\/login\/captcha/i.test(url)) {
      return { verdict: 'CHALLENGE', kind: 'captcha', finalUrl: url };
    }
  }
  return { verdict: 'TIMEOUT', finalUrl: lastUrl };
}

async function runLogin(opts) {
  const secrets = loadSecrets(opts.secrets);
  const pw = requirePlaywright();
  const { chromium } = pw;

  const browser = await chromium.launch({
    headless: opts.headless,
    channel: undefined,
    args: ['--disable-blink-features=AutomationControlled'],
  });
  try {
    // storageState-first：非持久 context（免 profile 目录锁，多车道可并发各开各的 context）。
    const context = await browser.newContext({ acceptDownloads: false });
    const page = await context.newPage();
    page.setDefaultTimeout(NAV_TIMEOUT_MS);

    await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT_MS });
    await page.fill('#login_field', secrets.username);
    await page.fill('#password', secrets.password);
    await Promise.all([
      page.waitForLoadState('domcontentloaded', { timeout: NAV_TIMEOUT_MS }).catch(() => {}),
      (page.click('input[type="submit"][name="commit"], input[value="Sign in"]', { timeout: NAV_TIMEOUT_MS })),
    ]);

    const settled = await classifyLoginSettle(page);
    if (settled.verdict !== 'SUCCESS') {
      emit(Object.assign({ ok: false, phase: 'login' }, settled, {
        loginRedacted: redact(secrets.username),
      }));
      process.exit(settled.verdict === 'CHALLENGE' ? 4 : 3);
    }

    const probe = await probeSession(context.request);
    if (probe.status !== 200) {
      emit({ ok: false, phase: 'probe-after-login', probe });
      process.exit(3);
    }

    fs.mkdirSync(path.dirname(opts.stateOut), { recursive: true });
    await context.storageState({ path: opts.stateOut });
    const bytes = fs.statSync(opts.stateOut).size;

    emit({
      ok: true,
      phase: 'login',
      loginRedacted: redact(settled.loginEcho || secrets.username),
      stateOut: opts.stateOut,
      stateBytes: bytes,
      finalUrl: settled.finalUrl,
    });
    process.exit(0);
  } finally {
    await browser.close().catch(() => {});
  }
}

async function runVerify(opts) {
  if (!fs.existsSync(opts.state)) fatal('state-missing', opts.state);
  let shapeOk = false;
  try {
    const parsed = JSON.parse(fs.readFileSync(opts.state, 'utf8'));
    shapeOk = Array.isArray(parsed.cookies);
  } catch (e) { shapeOk = false; }
  if (!shapeOk) { emit({ ok: false, phase: 'verify', status: 0, errorKind: 'state-shape' }); process.exit(3); }

  const pw = requirePlaywright();
  const { chromium } = pw;
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ storageState: opts.state, acceptDownloads: false });
    const probe = await probeSession(context.request);
    const ok = probe.status === 200;
    emit(Object.assign({ ok, phase: 'verify' }, probe, { state: opts.state }));
    process.exit(ok ? 0 : 3);
  } finally {
    await browser.close().catch(() => {});
  }
}

(async () => {
  const { mode, opts } = parseArgs(process.argv.slice(2));
  if (mode === 'login') {
    if (!opts.secrets || !opts.stateOut) fatal('bad-args', 'login needs --secrets and --state-out');
    await runLogin(opts);
  } else {
    if (!opts.state) fatal('bad-args', 'verify needs --state');
    await runVerify(opts);
  }
})().catch((e) => fatal('driver-crash', String(e && e.message || e).split('\n')[0]));
