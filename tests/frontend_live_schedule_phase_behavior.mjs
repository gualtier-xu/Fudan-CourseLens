import assert from "node:assert/strict";

/* LIVEEXP-1：直播入口时间驱动重估——课表相位纯函数闭集（live-state.js 唯一
 * 出处）+ 学习页直播行（live-room.js）过窗自愈行为流。
 * 病灶回归：页面静止跨过课表窗口后「按课表进行中/入口可用」残留。 */

class FakeElement {
  constructor(tagName = "div", id = "") {
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this.className = "";
    this.textContent = "";
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.hidden = false;
    this.disabled = false;
    this.type = "";
    this._listeners = new Map();
  }
  setAttribute(name, value) { this.attributes.set(String(name), String(value)); }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  append(...nodes) { this.children.push(...nodes); }
  addEventListener(type, listener) {
    if (!this._listeners.has(type)) this._listeners.set(type, new Set());
    this._listeners.get(type).add(listener);
  }
  removeEventListener(type, listener) { this._listeners.get(type)?.delete(listener); }
  dispatchEvent(event) {
    if (!Object.prototype.hasOwnProperty.call(event, "target")) {
      Object.defineProperty(event, "target", { value: this, configurable: true });
    }
    for (const listener of [...(this._listeners.get(event.type) || [])]) listener.call(this, event);
    return true;
  }
}

const liveState = await import("../frontend/modules/live-state.js");

/* ---- schedulePhase：课表窗口相位闭集 ---- */
const base = { date: "2026-09-09", start_time: "09:55", end_time: "10:40" };
assert.equal(liveState.schedulePhase(base, new Date(2026, 8, 9, 10, 15)), "now", "窗口内=now");
assert.equal(liveState.schedulePhase(base, new Date(2026, 8, 9, 9, 0)), "future", "窗口前=future");
assert.equal(liveState.schedulePhase(base, new Date(2026, 8, 9, 9, 55)), "now", "start 边界=进行中");
assert.equal(liveState.schedulePhase(base, new Date(2026, 8, 9, 10, 40)), "past", "end 边界=已过窗");
assert.equal(liveState.schedulePhase(base, new Date(2026, 8, 9, 18, 0)), "past", "窗口后=past");
assert.equal(liveState.schedulePhase(base, new Date(2026, 8, 10, 10, 15)), "", "非今天不裁决");
assert.equal(liveState.schedulePhase({ start_time: "09:55" }, new Date(2026, 8, 9, 10, 15)), "", "缺 end 不裁决");
assert.equal(liveState.schedulePhase({ date: "2026-09-09", start_time: "junk", end_time: "10:40" }, new Date(2026, 8, 9, 10, 15)), "", "缺无效时间不裁决");
assert.equal(liveState.schedulePhase(null, new Date(2026, 8, 9, 10, 15)), "", "无 meeting 不裁决");
/* date 缺省视为今天（live-page todayLiveMeetings 只产今天对象） */
const todayIso = (() => {
  const d = new Date(2026, 8, 9, 10, 15);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
})();
assert.equal(
  liveState.schedulePhase({ start_time: "09:55", end_time: "10:40" }, new Date(2026, 8, 9, 10, 15)),
  todayIso === "2026-09-09" ? "now" : liveState.schedulePhase({ date: todayIso, start_time: "09:55", end_time: "10:40" }, new Date(2026, 8, 9, 10, 15)),
  "date 缺省按今天解释",
);

/* ---- scheduleBoundaryDistance：距最近窗口边界分钟数 ---- */
assert.equal(liveState.scheduleBoundaryDistance(base, new Date(2026, 8, 9, 10, 15)), 20, "距 end 20 分钟");
assert.equal(liveState.scheduleBoundaryDistance(base, new Date(2026, 8, 9, 9, 52)), 3, "距 start 3 分钟");
assert.equal(liveState.scheduleBoundaryDistance(base, new Date(2026, 8, 9, 10, 38)), 2, "过窗前 2 分钟");
assert.equal(liveState.scheduleBoundaryDistance(base, new Date(2026, 8, 9, 10, 43)), 3, "过窗后 3 分钟（end 距离对称）");
assert.equal(liveState.scheduleBoundaryDistance(base, new Date(2026, 8, 10, 10, 15)), null, "非今天=null");
assert.equal(liveState.scheduleBoundaryDistance({ date: "2026-09-09" }, new Date(2026, 8, 9, 10, 15)), null, "缺时间=null");

/* ---- liveRecheckDecision：裁决表（翻转/身份/边界 → full；否则 skip） ---- */
const at = (h, m) => new Date(2026, 8, 9, h, m, 0);
assert.equal(
  liveState.liveRecheckDecision({ meeting: base, meetingKey: "m1", now: at(10, 41), lastPhase: "now", lastKey: "m1" }).action,
  "full", "相位翻转 → 全量重查",
);
assert.equal(
  liveState.liveRecheckDecision({ meeting: base, meetingKey: "m1", now: at(10, 15), lastPhase: "past", lastKey: "m1" }).action,
  "full", "反向翻转（恢复进行中）→ 全量重查",
);
assert.equal(
  liveState.liveRecheckDecision({ meeting: base, meetingKey: "m1", now: at(10, 15), lastPhase: "now", lastKey: "m2" }).action,
  "full", "目标身份变化 → 全量重查",
);
assert.equal(
  liveState.liveRecheckDecision({ meeting: base, meetingKey: "m1", now: at(10, 38), lastPhase: "now", lastKey: "m1" }).action,
  "full", "临近 end 边界 → 全量重查",
);
assert.equal(
  liveState.liveRecheckDecision({ meeting: base, meetingKey: "m1", now: at(9, 52), lastPhase: "future", lastKey: "m1" }).action,
  "full", "临近 start 边界 → 全量重查",
);
assert.equal(
  liveState.liveRecheckDecision({ meeting: base, meetingKey: "m1", now: at(10, 15), lastPhase: "now", lastKey: "m1" }).action,
  "skip", "相位/身份不变且远离边界 → 跳过",
);
assert.equal(
  liveState.liveRecheckDecision({ meeting: null, now: at(10, 15), lastPhase: "", lastKey: "" }).action,
  "skip", "无课表事实 → 跳过",
);
const flipped = liveState.liveRecheckDecision({ meeting: base, meetingKey: "m1", now: at(10, 41), lastPhase: "now", lastKey: "m1" });
assert.equal(flipped.phase, "past", "裁决返回当前相位（供红点收敛判定）");

/* ---- 周期常量：≤60s 过窗判据 ---- */
assert.equal(liveState.LIVE_RECHECK_INTERVAL_MS, 30000, "重估周期 30s");
assert.equal(liveState.LIVE_RECHECK_NEAR_MINUTES, 5, "边界临近窗 5 分钟");

/* ---- LIVE-DISCLOSURE-1：早期功能小字闭集原句（客户端/README/Pages 三面同源） ---- */
assert.equal(
  liveState.LIVE_DISCLOSURE_TEXT,
  "直播转写为早期功能，可用性视网络与课程环境而定。",
  "早期功能小字闭集原句（改一处须三面同改）",
);

/* ---- expirePastMeetingObservation：红点相位过期 ---- */
assert.deepEqual(
  liveState.expirePastMeetingObservation(["c1", "c2"], "c1"),
  ["c2"], "退出进行中的目标从红点集合移除",
);
assert.deepEqual(liveState.expirePastMeetingObservation(["c1"], ""), ["c1"], "空 id 不动集合");
assert.deepEqual(liveState.expirePastMeetingObservation(null, "c1"), [], "空集合安全");

/* ---- SWEEPFIX-2（SWEEP1-01）：单源一致态对账——ended 观测 × 未开始 meeting ----
 * 修前红：直播卡/直播行两个状态源（课表事实 headline + 后端观测 detail）
 * 各自渲染、互斥性缺失——目标 meeting 明天开始时，上一场直播的 ended 观测
 * 照常上能力行/原因行，四行同屏互相矛盾（「按课表 明天 10:50 开始」×
 * 「直播已结束」×「可以在讲次列表中查看已授权回放」）。 */
{
  const reconcile = liveState.reconcileMeetingEndedState;
  const futureToday = { date: "2026-09-09", start_time: "14:25", end_time: "15:10" };
  const tomorrowMeeting = { date: "2026-09-10", start_time: "10:50", end_time: "11:40" };
  assert.equal(reconcile({ state: "ended", meeting: tomorrowMeeting, mode: "later", now: at(10, 15) }), "upcoming", "未来日期 meeting × ended 观测 → upcoming");
  assert.equal(reconcile({ state: "ended", meeting: futureToday, mode: "next", now: at(10, 15) }), "upcoming", "今天未到点 × ended 观测 → upcoming");
  assert.equal(reconcile({ state: "ended", meeting: base, mode: "", now: at(10, 41) }), "ended", "过窗后的 ended=后端新知，如实呈现");
  assert.equal(reconcile({ state: "ended", meeting: base, mode: "", now: at(10, 15) }), "ended", "窗口内 ended=后端新知（可能提前结束），如实呈现");
  assert.equal(reconcile({ state: "live", meeting: tomorrowMeeting, mode: "later", now: at(10, 15) }), "live", "live 观测永不被改写（绝不藏真实入口）");
  assert.equal(reconcile({ state: "unknown", meeting: tomorrowMeeting, mode: "later", now: at(10, 15) }), "unknown", "非 ended 态不对账");
  assert.equal(reconcile({ state: "ended", meeting: null, mode: "active", now: at(10, 15) }), "ended", "无 meeting 事实不对账");
  assert.equal(reconcile({ state: "", meeting: tomorrowMeeting, mode: "later", now: at(10, 15) }), "", "空态透传");
}

/* ---- SWEEPFIX-2（SWEEP1-02）：观测时间行标签化——裸「7/28」闭集改写 ----
 * 修前红：讲次页直播条时间行裸渲染 starts_at/ends_at 人话化结果，跨日值只剩
 * 「7/28」，无单位无标签。闭集：非 live 态加「上次直播」前缀（观测窗口属于
 * 上一场）；live 态照述本场窗口；无观测时间诚实回退。 */
{
  const timeText = liveState.liveObservationTimeText;
  const fmtFake = (v) => (String(v || "").includes("2026-07-28") ? "7/28" : String(v || ""));
  assert.equal(
    timeText({ startsAt: "2026-07-28T19:00:00", endsAt: "", state: "unknown", hasCourse: true, format: fmtFake }),
    "上次直播 7/28", "非 live 态裸日期加「上次直播」标签（修前红=「7/28」）",
  );
  assert.equal(
    timeText({ startsAt: "今晚 19:00", endsAt: "晚上 20:40", state: "live", hasCourse: true, format: (v) => String(v || "") }),
    "今晚 19:00 至 晚上 20:40", "live 态照述本场窗口，不加前缀",
  );
  assert.equal(
    timeText({ startsAt: "", endsAt: "", state: "offline", hasCourse: true, format: fmtFake }),
    "时间尚未确认", "无观测时间诚实回退",
  );
  assert.equal(
    timeText({ startsAt: "", endsAt: "", state: "idle", hasCourse: false, format: fmtFake }),
    "选择已授权课程后自动确认", "无课程空态回退",
  );
}

/* ---- SWEEPFIX-2（SWEEP1-02）：unknown 原因行人话化（闭集唯一出处改写） ----
 * 修前红：「尚无可用于进入直播的新状态。」——「新状态」是内部术语，学生读到等于没读。 */
assert.equal(
  liveState.liveStateDetail("unknown").reason,
  "还没拿到这门课的直播状态，拿到后这里会更新。",
  "unknown 原因行人话化",
);

/* ---- live-room.js：学习页直播行过窗自愈 ---- */
const byId = Object.fromEntries([
  "live-room-row", "live-room-label", "live-room-capability", "live-room-time", "live-room-reason",
  "live-room-disclosure",
].map((id) => [id, new FakeElement("div", id)]));
byId["enter-live-room"] = new FakeElement("button", "enter-live-room");
byId["live-room-recheck"] = new FakeElement("button", "live-room-recheck");
globalThis.document = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new FakeElement(tag, `created-${tag}`),
};
globalThis.CustomEvent = class extends Event {
  constructor(type, options = {}) { super(type); this.detail = options.detail; }
};
const windowTarget = new EventTarget();
windowTarget.setTimeout = () => 0;
windowTarget.clearTimeout = () => {};
let roomTick = null;
let roomRecheckMs = 0;
const roomCleared = [];
windowTarget.setInterval = (fn, ms) => { roomTick = fn; roomRecheckMs = Number(ms); return 91; };
windowTarget.clearInterval = (id) => { roomCleared.push(id); };
globalThis.window = windowTarget;
const settle = async () => { for (let i = 0; i < 4; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

const roomStore = {
  activeCourse: { course_id: "c1", title: "集成电路" },
  listeners: new Map(),
  set(key, value) { this[key] = value; },
  subscribe(key, listener) {
    if (!this.listeners.has(key)) this.listeners.set(key, new Set());
    this.listeners.get(key).add(listener);
    return () => this.listeners.get(key)?.delete(listener);
  },
};

let roomStatusState = "live";
let roomStatusGets = 0;
globalThis.fetch = async (path) => {
  const route = String(path);
  if (route.includes("/live-room/status")) {
    roomStatusGets += 1;
    return new Response(JSON.stringify({ schema: "courselens.api.v3", data: { state: roomStatusState, can_enter: roomStatusState === "live" } }), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  }
  throw new Error(`unexpected synthetic route: ${route}`);
};

const { installLiveRoom } = await import("../frontend/modules/live-room.js");
let roomClockNow = new Date(2026, 8, 9, 10, 15, 0); /* 课中 */
const roomSnapshot = {
  days: [{ weekday: 3, date: "2026-09-09", meetings: [
    { meeting_id: "m1", title: "集成电路", catalog_course_id: "c1", date: "2026-09-09", start_time: "09:55", end_time: "10:40" },
  ]}],
};
const cleanupRoom = await installLiveRoom(roomStore, { clock: () => roomClockNow });
await settle();
assert.equal(roomRecheckMs, 30000, "学习页重估周期默认 30s");
assert.equal(typeof roomTick, "function", "学习页重估 tick 已注册");
/* LIVE-DISCLOSURE-1：状态条下方早期功能小字——静态装一次，闭集同源不进状态机 */
assert.equal(
  byId["live-room-disclosure"].textContent,
  liveState.LIVE_DISCLOSURE_TEXT,
  "直播行下方早期功能小字装一次即定",
);

/* 无快照：headline 无课表事实（label=后端态） */
assert.equal(byId["live-room-label"].textContent, "正在直播", "无快照时用后端态标题");

/* 快照到达：课中 → 按课表进行中 */
windowTarget.dispatchEvent(new CustomEvent("courselens:timetable-snapshot", { detail: roomSnapshot }));
await settle();
assert.equal(byId["live-room-label"].textContent, "按课表进行中 · 09:55–10:40", "课中：按课表进行中 · 起止");

/* 节流钉：相位不变 → tick 零请求 */
const roomGetsBaseline = roomStatusGets;
roomTick();
await settle();
assert.equal(roomStatusGets, roomGetsBaseline, "相位不变 tick 零请求");

/* 过窗翻转钉：时钟跨 end_time + 观测 ended → tick → 全量重查 + headline 改述已结束 */
roomStatusState = "ended";
roomClockNow = new Date(2026, 8, 9, 10, 41, 0);
roomTick();
await settle();
assert.equal(roomStatusGets, roomGetsBaseline + 1, "过窗相位翻转触发全量重查");
assert.equal(byId["live-room-label"].textContent, "按课表 09:55–10:40 已结束", "过窗：绝不把结束的课写成「开始」");
assert.equal(byId["live-room-capability"].textContent, "直播已结束", "能力行收敛到新观测");

/* 未来课措辞钉：时钟回到课前 → tick → 「开始」 */
roomStatusState = "upcoming";
roomClockNow = new Date(2026, 8, 10, 8, 0, 0); /* 非今天：snapshot 无该日 meeting → headline 退场 */
roomTick();
await settle();
assert.equal(byId["live-room-label"].textContent, "直播尚未开始", "非今天快照无课表事实，回退后端态标题");

/* SWEEPFIX-2（SWEEP1-01）live-row 复现钉：今天未到点 meeting × ended 观测。
   修前红：标题「按课表 09:55 开始」× 能力行「直播已结束」同屏矛盾。 */
roomStatusState = "ended";
roomClockNow = new Date(2026, 8, 9, 8, 30, 0); /* 回到今天课前 */
roomTick();
await settle();
assert.equal(byId["live-room-label"].textContent, "按课表 09:55 开始", "复现前置：课前课表事实标题");
assert.equal(byId["live-room-capability"].textContent, "直播未开始", "能力行单一一致态（修前红=「直播已结束」）");
assert.equal(byId["live-room-reason"].textContent, "开始时间尚未到，开始前会保持安静。", "原因行不残留回放话术（修前红）");

/* dispose 钉：清定时器 + disposed 后 tick 零动作 */
const roomGetsBeforeDispose = roomStatusGets;
cleanupRoom();
await settle();
assert.ok(roomCleared.includes(91), "dispose 清除学习页重估定时器");
roomClockNow = new Date(2026, 8, 9, 10, 42, 0);
roomStatusState = "live";
roomTick();
await settle();
assert.equal(roomStatusGets, roomGetsBeforeDispose, "dispose 后 tick 零动作");

console.log("frontend_live_schedule_phase_behavior: all assertions passed");

/* ---- F6（RR-PARK-1 P4）：模糊态 × 课表事实改述闭集 ---- */
const restate = liveState.restateAmbiguousLiveState;
const inWindowMeeting = { date: "2026-09-09", start_time: "10:00", end_time: "11:40" };
const laterMeeting = { date: "2026-09-09", start_time: "23:00", end_time: "23:50" };
const nineNine = new Date(2026, 8, 9, 10, 30, 0);

const nowRestate = restate({ statusState: "offline", meeting: inWindowMeeting, now: nineNine });
assert.ok(nowRestate, "窗口内 offline 应改述课表相位");
assert.equal(nowRestate.title, "按课表正在进行中");
assert.ok(nowRestate.body.includes("课表"), "改述必须陈述课表事实");
assert.ok(!nowRestate.body.includes("正在直播"), "绝不凭课表宣称正在直播");
assert.deepEqual(nowRestate.actions, ["recheck"], "offline 保留重查");

const unknownRestate = restate({ statusState: "unknown", meeting: inWindowMeeting, now: nineNine });
assert.deepEqual(unknownRestate.actions, [], "unknown 不放重查 CTA（与 liveRetryVisible 同律）");

const staleRestate = restate({ statusState: "stale", meeting: inWindowMeeting, now: nineNine });
assert.deepEqual(staleRestate.actions, ["recheck"], "stale 保留重查");

const futureRestate = restate({ statusState: "offline", meeting: laterMeeting, now: nineNine });
assert.ok(futureRestate, "窗口前 offline 改述「还没到点」");
assert.equal(futureRestate.title, "还没到点");
assert.deepEqual(futureRestate.actions, []);

const pastRestate = restate({
  statusState: "unknown",
  meeting: { date: "2026-09-09", start_time: "08:00", end_time: "08:50" },
  now: nineNine,
});
assert.equal(pastRestate, null, "窗口后无改述，落原中性卡");

const noMeetingRestate = restate({ statusState: "offline", meeting: null, now: nineNine });
assert.equal(noMeetingRestate, null, "无课表事实不改述");

const liveRestate = restate({ statusState: "live", meeting: inWindowMeeting, now: nineNine });
assert.equal(liveRestate, null, "权威态（live/upcoming/ended/denied）绝不改述");

const readyRestate = restate({ statusState: "ready", meeting: inWindowMeeting, now: nineNine });
assert.equal(readyRestate, null, "非闭集态一律不改述");

console.log("F6 restateAmbiguousLiveState: all assertions passed");
