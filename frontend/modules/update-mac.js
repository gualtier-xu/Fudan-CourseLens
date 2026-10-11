/* UPDATE-UX-1（2026-10-09 用户令：mac 无自动更新，但有新版本时必须醒目提醒）：
   macOS 测试版的更新检查道。mac 版没有应用内自动更新链（MAC-3 定谳的 v1 形态），
   检查走公开 GitHub Releases 列表（client-v<semver>-macos-test tag 命名空间），
   与 Windows 更新链同一分发仓、同一闭集主机（api.github.com 属产品外联闭集
   既有成员；匿名只读、零凭据、零遥测）。纯逻辑+状态广播叶模块：不碰 DOM，
   面板（update-panel.js）与顶栏（update-widget.js）各自订阅渲染。 */

/* 冻结自真实分发面：R6-CHAIN 步骤⑦ mac prerelease tag 形如 client-v0.1.0-macos-test。 */
const MAC_RELEASE_TAG_RE = /^client-v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)-macos-test$/;
const VERSION_RE = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$/;
const MAC_RELEASES_API =
  "https://api.github.com/repos/gualtier-xu/Fudan-CourseLens/releases?per_page=30";

/* 检查态闭集：idle=未检查 / checking / available / up_to_date / unavailable。
   unavailable=检查失败诚实降级（网络/限流/解析），绝不误导成「已是最新」。 */
export function macUpdateEntry() {
  /* 轻量探测（零后端往返）：客户端只在两种 WebView 里运行——Windows=WebView2
     （UA 含 "Windows"），macOS=WKWebView（UA 含 "Macintosh"）。明确是 Mac 才
     判真；其余环境（调试浏览器、Node 测试桩）一律按 Windows 现行为呈现。 */
  try {
    const ua = typeof navigator === "undefined" ? "" : String(navigator.userAgent || "");
    return ua.includes("Macintosh") && !ua.includes("Windows");
  } catch {
    return false;
  }
}

function versionKey(value) {
  const match = VERSION_RE.exec(String(value || ""));
  if (!match) return null;
  const numeric = match.slice(1, 4).map((part) => parseInt(part, 10));
  /* 语义化版本序：带连字符预发布后缀 < 同号正式版（stable 渠道不发后缀版，
     macos-test tag 现行为纯三段号；比较器按 semver 序完整实现防未来漂移）。 */
  return [...numeric, match[4] === undefined ? 1 : 0];
}

export function compareVersions(left, right) {
  const a = versionKey(left);
  const b = versionKey(right);
  if (!a || !b) return 0;
  for (let index = 0; index < a.length; index += 1) {
    if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1;
  }
  return 0;
}

async function fetchLatestMacRelease() {
  const response = await fetch(MAC_RELEASES_API, {
    headers: { Accept: "application/vnd.github+json" },
    cache: "no-store",
  });
  if (!response.ok) throw new Error("mac_release_list_unavailable");
  const releases = await response.json();
  if (!Array.isArray(releases)) throw new Error("mac_release_list_unavailable");
  let latest = null;
  for (const release of releases) {
    if (!release || release.draft === true) continue;
    const match = MAC_RELEASE_TAG_RE.exec(String(release.tag_name || ""));
    if (!match) continue;
    const version = match[1];
    if (latest === null || compareVersions(version, latest) > 0) latest = version;
  }
  if (latest === null) throw new Error("mac_release_list_unavailable");
  return latest;
}

let macState = { phase: "idle", version: "", currentVersion: "", error: "" };
let checking = false;
const listeners = new Set();

export function macUpdateSnapshot() {
  return macState;
}

export function onMacUpdateChange(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function publish(next) {
  macState = next;
  for (const listener of [...listeners]) {
    try {
      listener(macState);
    } catch {
      /* 单个订阅者渲染异常不阻断检查道 */
    }
  }
}

function setMacUpdateState(patch) {
  publish({ ...macState, ...patch });
}

/* 一次完整检查：点击即反馈（phase=checking ≤0ms 上脸），终态人人有回音。
   currentVersion 来自后端快照 current_version；未知则诚实降级（无法比对就
   不假称结果），绝不静默吞掉。 */
export async function runMacUpdateCheck(currentVersion) {
  if (checking) return macState;
  if (typeof currentVersion !== "string" || !VERSION_RE.test(currentVersion.trim())) {
    setMacUpdateState({ phase: "unavailable", version: "", currentVersion: "", error: "current_version_unknown" });
    return macState;
  }
  checking = true;
  setMacUpdateState({ phase: "checking", version: "", currentVersion: currentVersion.trim(), error: "" });
  try {
    const latest = await fetchLatestMacRelease();
    if (compareVersions(latest, currentVersion.trim()) > 0) {
      setMacUpdateState({ phase: "available", version: latest, error: "" });
    } else {
      setMacUpdateState({ phase: "up_to_date", version: latest, error: "" });
    }
  } catch {
    setMacUpdateState({ phase: "unavailable", version: "", error: "mac_release_list_unavailable" });
  } finally {
    checking = false;
  }
  return macState;
}
