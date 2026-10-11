/* DATAMAP-P1：「你的数据在哪里」数据地图面板（设置页分节，D10 信任产品化）。
   消费冻结合同 courselens.data-map.v1（src/runtime/data_map.py）：三域声明 +
   本机类别计数 + 外联主机具名闭集 + 云端保留期 + 四动作入口。
   - 纯只读渲染：零新动作、零新开关（动作区只导航到既有入口，自动化缺省纪律）；
   - 未知名单键省略不渲染（同 course-data 缺类别省略模式，闭集漂移不炸页面）；
   - 文案口径与 docs/privacy-notice.md v1.2.1 / README「安全与隐私架构」同源，
     每节标题即答案句，禁技术性责备措辞。 */
import { apiV3 } from "../api.js";
import { selectPage } from "../shell.js";
import { $, clear, textElement } from "../ui.js";
import { CATEGORY_LABELS } from "../course-data.js";
import { formatBytes } from "./shared.js";

const DATA_MAP_SCHEMA = "courselens.data-map.v1";

const HOST_GROUP_LABELS = Object.freeze({
  campus: "复旦课程平台",
  github: "GitHub · 你的专属仓",
  ai: "AI · 你的 Key",
  update: "应用更新",
});

const DOMAIN_FIELD_LABELS = Object.freeze({
  where: "存哪里",
  who: "谁能看到",
  retention: "留多久",
});

const COUNTERS_INCOMPLETE_TEXT = "课程较多，以上为本机数据目录的主要部分；明细可在数据管理页查看。";
const COUNTERS_EMPTY_TEXT = "还没有本机学习数据。它只会随着你的使用出现在这台电脑上，不会先于你到达任何别的地方。";

/* 动作闭集 → 既有入口导航（零新动作）：delete-records 去数据管理页，
   revoke-cloud / reset-client 滚动到对应设置分节；erase-hotspots 是
   讲次级动作，没有全局入口，保持纯文字指路（不造新开关）。 */
const ACTION_NAVIGATIONS = Object.freeze({
  "delete-records": () => selectPage("data"),
  "revoke-cloud": () => document.querySelector('[data-settings-target="settings-network-group"]')?.click(),
  "reset-client": () => document.querySelector('[data-settings-target="settings-reset-group"]')?.click(),
});

const DATAMAP_ERROR_TEXT = "数据地图暂时读不出来，不影响其他功能。稍后再试，或先看《隐私与数据说明》。";

function categoryLabel(key) {
  return CATEGORY_LABELS[String(key)] || "";
}

function renderDomainCard(domain) {
  const key = String(domain?.key || "");
  if (!key) return null;
  const card = textElement("article", "", "datamap-domain");
  card.dataset.domain = key;
  const title = textElement("h4", String(domain?.title || ""), "datamap-domain-title");
  card.append(title);
  for (const [field, label] of Object.entries(DOMAIN_FIELD_LABELS)) {
    const value = String(domain?.[field] || "");
    if (!value) continue;
    const row = textElement("p", "", "datamap-domain-field");
    row.append(textElement("span", label, "datamap-field-label"));
    row.append(textElement("span", value, "datamap-field-value"));
    card.append(row);
  }
  const items = Array.isArray(domain?.items) ? domain.items : [];
  if (items.length) {
    const list = textElement("ul", "", "datamap-domain-items");
    for (const item of items) {
      if (!String(item || "")) continue;
      list.append(textElement("li", String(item)));
    }
    if (list.children.length) card.append(list);
  }
  return card;
}

function renderFlow(domains) {
  const flow = $("datamap-flow");
  clear(flow);
  const cards = [];
  for (const domain of Array.isArray(domains) ? domains : []) {
    const card = renderDomainCard(domain);
    if (card) cards.push(card);
  }
  cards.forEach((card, index) => {
    if (index > 0) {
      const arrow = textElement("span", "→", "datamap-arrow");
      arrow.setAttribute("aria-hidden", "true");
      flow.append(arrow);
    }
    flow.append(card);
  });
}

function renderCounters(counters) {
  const node = $("datamap-counters");
  clear(node);
  node.setAttribute("aria-busy", "false");
  const rows = Array.isArray(counters?.categories) ? counters.categories : [];
  if (!rows.length) {
    node.append(textElement("span", COUNTERS_EMPTY_TEXT, "datamap-counters-empty"));
    return;
  }
  const chips = textElement("span", "", "datamap-chip-row");
  for (const row of rows) {
    const label = categoryLabel(row?.category);
    const count = Number(row?.count) || 0;
    if (!label || count <= 0) continue; /* 未知类别省略不占位（闭集漂移防线） */
    chips.append(textElement("span", `${label} ${count}`, "t-chip data-chip"));
  }
  if (chips.children.length) node.append(chips);
  const totalBytes = Number(counters?.database_bytes?.total) || 0;
  if (totalBytes > 0) {
    node.append(textElement(
      "span",
      `本机数据库文件共 ${formatBytes(totalBytes)}（只存在这台电脑上）`,
      "hint datamap-bytes",
    ));
  }
  if (counters?.complete === false) {
    node.append(textElement("span", COUNTERS_INCOMPLETE_TEXT, "hint"));
  }
}

function renderHosts(hosts) {
  const list = $("datamap-hosts");
  clear(list);
  for (const host of Array.isArray(hosts) ? hosts : []) {
    const name = String(host?.host || "");
    const purpose = String(host?.purpose || "");
    if (!name) continue; /* 未知键省略不渲染 */
    const item = textElement("li", "", "datamap-host-row");
    const groupKey = String(host?.group || "");
    if (groupKey && HOST_GROUP_LABELS[groupKey]) {
      item.append(textElement("span", HOST_GROUP_LABELS[groupKey], "t-chip datamap-host-group"));
    }
    item.append(textElement("code", name, "datamap-host-name"));
    item.append(textElement("span", purpose, "datamap-host-purpose"));
    list.append(item);
  }
}

function renderRetention(retention) {
  const node = $("datamap-retention");
  clear(node);
  const materials = Number(retention?.materials_days) || 0;
  const state = Number(retention?.state_days) || 0;
  if (materials > 0) {
    node.append(textElement("li", `课程材料与结果（密文）：最长保留 ${materials} 天，到期不再可用。`));
  }
  if (state > 0) {
    node.append(textElement("li", `任务状态记录：最长保留 ${state} 天（只含状态与错误码，不含课程内容）。`));
  }
}

function renderActions(actions) {
  const list = $("datamap-actions");
  clear(list);
  for (const action of Array.isArray(actions) ? actions : []) {
    const key = String(action?.key || "");
    const title = String(action?.title || "");
    const where = String(action?.where || "");
    if (!key || !title) continue;
    const item = textElement("li", "", "datamap-action-row");
    const navigate = ACTION_NAVIGATIONS[key];
    if (navigate) {
      const button = textElement("button", title, "text-button datamap-action-button");
      button.type = "button";
      button.addEventListener("click", navigate);
      item.append(button);
    } else {
      item.append(textElement("strong", title, "datamap-action-title"));
    }
    if (where) item.append(textElement("span", where, "hint datamap-action-where"));
    list.append(item);
  }
}

export async function loadDataMap() {
  const errorNode = $("datamap-error");
  try {
    const payload = await apiV3("data-map");
    if (payload?.schema !== DATA_MAP_SCHEMA) {
      throw new Error("datamap schema mismatch");
    }
    if (errorNode) errorNode.hidden = true;
    renderFlow(payload.domains);
    renderCounters(payload.counters);
    renderHosts(payload.outbound_hosts);
    renderRetention(payload.retention);
    renderActions(payload.actions);
  } catch {
    /* 诚实降级：地图读不出时保留占位与人话提示，不静默空面板 */
    if (errorNode) {
      errorNode.textContent = DATAMAP_ERROR_TEXT;
      errorNode.hidden = false;
    }
  }
}
