import assert from "node:assert/strict";
import {
  assembleFamily, elementRegistry, missingRecorder, FakeElement, macUpdateStubs,
} from "./frontend_exec_harness.mjs";

/* DATAMAP-P1「你的数据在哪里」数据地图真执行钉（settings 家族并集体装配，
   复用 settings_reset 已证形态）：
   1. 冻结载荷（courselens.data-map.v1）渲染三域卡/主机清单/保留期/四动作，
      未知元素 id 触碰=红（missing 哨兵）；
   2. 闭集漂移防线：无主机名的条目、未知类别、未知分组徽标一律省略不渲染，
      页面不炸；
   3. schema 不符 → 人话错误行，不静默空面板；
   4. 动作区只导航到既有入口（数据管理页 / 对应设置分节），零新动作。 */

const DATA_MAP_SCHEMA = "courselens.data-map.v1";

const CATEGORY_LABELS = Object.freeze({
  progress: "进度", transcript: "字幕", ppt: "课件", artifacts: "笔记",
  documents: "文档", references: "引用", timeline: "时间轴", search: "索引",
  bookmarks: "书签", quizzes: "测验", review: "复习", tasks: "任务",
  automation: "自动化",
});

const HOST_GROUP_LABELS = Object.freeze({
  campus: "复旦课程平台",
  github: "GitHub · 你的专属仓",
  ai: "AI · 你的 Key",
  update: "应用更新",
});

function stubPayload() {
  return {
    schema: DATA_MAP_SCHEMA,
    generated_at: 1,
    domains: [
      {
        key: "local", title: "你的电脑", where: "本机数据目录", who: "只有你能看到",
        retention: "一直保留，直到你在应用里删除或重置",
        items: ["课程目录、课表、观看进度", "书签、笔记、测验与复习计划"],
      },
      {
        key: "own_github", title: "你自己的 GitHub 仓库", where: "你的专属云端仓库",
        who: "只有你能看到内容：传输与仓库里只见密文",
        retention: "课程材料与结果（密文）最长保留 30 天",
        items: ["任务在端到端加密的「任务信封」里运送"],
      },
      {
        key: "cloud_state", title: "云端任务状态记录", where: "你自己的 GitHub 仓库里的任务状态",
        who: "只有你能看到", retention: "任务状态记录最长保留 90 天",
        items: ["派发、执行、回传、清理的状态记录"],
      },
    ],
    counters: {
      complete: true,
      database_bytes: { state_db: 2048, learning_db: 4096, total: 6144 },
      categories: [
        { category: "progress", count: 3, text_bytes: 30 },
        { category: "bookmarks", count: 2, text_bytes: 20 },
      ],
    },
    outbound_hosts: [
      { host: "icourse.fudan.edu.cn", group: "campus", purpose: "课程目录、课表、直播与回放" },
      { host: "api.github.com", group: "github", purpose: "云端任务的派发与状态读取" },
    ],
    retention: { materials_days: 30, state_days: 90 },
    actions: [
      { key: "erase-hotspots", title: "抹掉某一讲的回看热点", where: "该讲次播放器操作区" },
      { key: "delete-records", title: "删除本机学习记录", where: "设置的数据管理页" },
      { key: "revoke-cloud", title: "撤销云端授权", where: "连接卡「高级操作与诊断」" },
      { key: "reset-client", title: "一键重置", where: "设置页「重置」" },
    ],
  };
}

function stubTextElement(tag, text, className = "") {
  const node = new FakeElement("");
  node.tagName = String(tag).toUpperCase();
  node.textContent = String(text ?? "");
  node.className = className;
  return node;
}

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 5));
}

function freshContext({ apiAnswer }) {
  const registry = elementRegistry([
    "datamap-flow", "datamap-counters", "datamap-hosts",
    "datamap-retention", "datamap-actions", "datamap-error",
  ]);
  const { missing, $stub } = missingRecorder();
  const $ = $stub(registry.lookup);
  const selectTargets = [];
  const selectPage = (name) => selectTargets.push(name);
  const queriedSelectors = [];
  const documentStub = {
    querySelector(selector) {
      queriedSelectors.push(selector);
      return { click: () => queriedSelectors.push(`${selector}#clicked`) };
    },
  };
  const factory = assembleFamily("settings", [
    "apiV3", "selectPage", "$", "clear", "textElement", "document", "CATEGORY_LABELS",
    /* a3ddf5e（UPDATE-UX-1）起 update-panel 装配期顶层注册 mac 检查道监听——
       桩族退订桩（UPDATE-STUB-FIX-1 收敛到 harness macUpdateStubs） */
    "onMacUpdateChange",
  ], ["loadDataMap"]);
  const exports = factory(
    apiAnswer, selectPage, $,
    (node) => node.replaceChildren(),
    stubTextElement, documentStub, CATEGORY_LABELS,
    macUpdateStubs().onMacUpdateChange,
  );
  return { registry, missing, selectTargets, queriedSelectors, loadDataMap: exports.loadDataMap };
}

function chipTexts(container) {
  const found = [];
  const walk = (node) => {
    for (const child of node.children || []) {
      if (String(child.className || "").includes("t-chip")) found.push(child.textContent);
      else walk(child);
    }
  };
  walk(container);
  return found;
}

async function testFrozenPayloadRendersAllSections() {
  const apiCalls = [];
  const { registry, missing, selectTargets, queriedSelectors, loadDataMap } = freshContext({
    apiAnswer: async (route) => {
      apiCalls.push(route);
      return stubPayload();
    },
  });
  const flow = registry.byId.get("datamap-flow");
  const counters = registry.byId.get("datamap-counters");
  const hosts = registry.byId.get("datamap-hosts");
  const retention = registry.byId.get("datamap-retention");
  const actions = registry.byId.get("datamap-actions");
  const errorNode = registry.byId.get("datamap-error");

  await loadDataMap();
  await flush();

  assert.deepEqual(apiCalls, ["data-map"], "the map reads exactly its one read-only route");
  assert.equal(errorNode.hidden, true, "happy path never shows the error line");
  const domainCards = flow.children.filter((node) => String(node.className || "").includes("datamap-domain"));
  const arrows = flow.children.filter((node) => String(node.className || "").includes("datamap-arrow"));
  assert.equal(domainCards.length, 3, "three-domain flow renders three cards");
  assert.equal(arrows.length, 2, "two flow arrows connect the three domains");
  assert.equal(domainCards[0].dataset.domain, "local");
  assert.equal(domainCards[2].dataset.domain, "cloud_state");
  const domainFields = domainCards[1].children.filter((node) => String(node.className || "").includes("datamap-domain-field"));
  const domainFieldValues = domainFields.flatMap((node) => node.children
    .filter((child) => String(child.className || "").includes("datamap-field-value"))
    .map((child) => child.textContent));
  assert.equal(domainFieldValues.length, 3, "each domain states where/who/retention");
  assert.ok(domainFieldValues.some((text) => text.includes("只见密文")),
    "cloud domain must speak the ciphertext visibility copy");

  const counterChips = chipTexts(counters);
  assert.deepEqual(counterChips, ["进度 3", "书签 2"], "local counters render as labelled chips");
  const bytesLine = counters.children.find((node) => String(node.className || "").includes("datamap-bytes"));
  assert.ok(String(bytesLine?.textContent || "").includes("6 KiB"), "database size speaks in human units (shared formatBytes)");

  assert.equal(hosts.children.length, 2, "every declared host renders exactly one row");
  const hostName = hosts.children[0].children.find((node) => String(node.className || "").includes("datamap-host-name"));
  assert.equal(hostName.textContent, "icourse.fudan.edu.cn");
  const groupBadge = hosts.children[0].children.find((node) => String(node.className || "").includes("datamap-host-group"));
  assert.equal(groupBadge.textContent, HOST_GROUP_LABELS.campus);

  assert.equal(retention.children.length, 2, "materials 30d + state 90d render");
  assert.ok(retention.children[0].textContent.includes("30 天"));
  assert.ok(retention.children[1].textContent.includes("90 天"));

  assert.equal(actions.children.length, 4, "the four closed actions render");
  const deleteRow = actions.children[1];
  const deleteButton = deleteRow.children.find((node) => node.tagName === "BUTTON");
  assert.ok(deleteButton, "delete-records navigates via a button");
  deleteButton.dispatch("click");
  assert.deepEqual(selectTargets, ["data"], "delete-records opens the existing data page");
  const resetRow = actions.children[3];
  const resetButton = resetRow.children.find((node) => node.tagName === "BUTTON");
  resetButton.dispatch("click");
  assert.ok(
    queriedSelectors.includes('[data-settings-target="settings-reset-group"]#clicked'),
    "reset action clicks the existing settings nav target",
  );
  const hotspotRow = actions.children[0];
  assert.equal(
    hotspotRow.children.some((node) => node.tagName === "BUTTON"), false,
    "erase-hotspots stays text-only guidance (no global entry, no new switch)",
  );
  assert.deepEqual(missing, [], `unknown element ids touched: ${missing}`);
}

async function testClosedSetDriftIsOmittedNotFatal() {
  const payload = stubPayload();
  payload.counters.categories.push({ category: "brand_new_kind", count: 9, text_bytes: 1 });
  payload.outbound_hosts.unshift({ host: "", group: "campus", purpose: "匿名条目" });
  payload.outbound_hosts.push({ host: "new-host.example.com", group: "brand_new_group", purpose: "未分组" });
  const { registry, missing, loadDataMap } = freshContext({ apiAnswer: async () => payload });

  await loadDataMap();
  await flush();

  const counters = registry.byId.get("datamap-counters");
  assert.deepEqual(
    chipTexts(counters), ["进度 3", "书签 2"],
    "unknown category keys are omitted, never rendered as raw keys",
  );
  const hosts = registry.byId.get("datamap-hosts");
  assert.equal(hosts.children.length, 3, "nameless entries are skipped, named ones stay");
  const unknownRow = hosts.children[2];
  assert.equal(
    unknownRow.children.some((node) => String(node.className || "").includes("datamap-host-group")), false,
    "unknown host groups render without a badge instead of breaking",
  );
  const errorNode = registry.byId.get("datamap-error");
  assert.equal(errorNode.hidden, true, "forward-compatible drift stays silent and safe");
  assert.deepEqual(missing, [], `unknown element ids touched: ${missing}`);
}

async function testSchemaMismatchSpeaksHumanError() {
  const payload = stubPayload();
  payload.schema = "courselens.something-else.v9";
  const { registry, missing, loadDataMap } = freshContext({ apiAnswer: async () => payload });
  const errorNode = registry.byId.get("datamap-error");
  const flow = registry.byId.get("datamap-flow");

  await loadDataMap();
  await flush();

  assert.equal(errorNode.hidden, false, "schema mismatch must surface the honest error line");
  assert.ok(errorNode.textContent.includes("数据地图暂时读不出来"), "error copy speaks like a patient classmate");
  assert.equal(flow.children.length, 0, "panels stay empty rather than rendering foreign shapes");
  assert.deepEqual(missing, [], `unknown element ids touched: ${missing}`);
}

await testFrozenPayloadRendersAllSections();
await testClosedSetDriftIsOmittedNotFatal();
await testSchemaMismatchSpeaksHumanError();
console.log("frontend data map behavior passed");
