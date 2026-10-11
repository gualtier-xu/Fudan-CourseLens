const listeners = new Map();

export const store = {
  auth: null,
  courses: [],
  activeCourse: null,
  activeLecture: null,
  /* 直播页目标课程键（N6L S1）：卡片跳转先写本键再 selectPage("live")，
     live-page 消费预选。内存键不持久化（课程身份禁落 localStorage 纪律）。 */
  liveTarget: "",
  /* 当前观测到 state=live 的课程 id 列表（live-page/home-overview 的 status
     观测顺带回写，零新增轮询）；header「直播」钮红点消费。内存键不持久化。 */
  liveActiveCourses: [],
  tasks: [],
  /* ---- U1「继续学习」last-lecture 记忆面（FIRST-LOGIN-UX-2）----
     最小键 courselens.last-lecture.v1：{course_id, sub_id, ts} 纯 ID+时刻，
     零课程名/零个人数据（与 courselens.playback-rate.v1 同类的 ID 级偏好键；
     「询问记忆」的「课程身份绝不落 localStorage」场景守卫不在此列——那是
     会话内存语义，本键与倍速记忆同族）。study.js 讲次打开单一漏斗写入；
     着陆页据目录闭集解析「继续学习」一击直达，解析失败（陈旧/换账号/目录
     刷新后讲次不在）一律视为无历史，卡不出现。存储不可得（隐私模式/配额）
     静默降级为无历史，绝不报错。方法挂 store 对象而非模块导出：全链既有
     装配注入面零新增自由变量。 ---- */
  rememberLastLecture(entry) {
    const courseId = String(entry?.course_id ?? "").trim();
    const subId = String(entry?.sub_id ?? "").trim();
    if (!courseId || !subId) return; /* 空 ID 是垃圾条目，绝不落键 */
    const ts = Number(entry?.ts);
    const clean = { course_id: courseId, sub_id: subId, ts: Number.isFinite(ts) ? ts : 0 };
    try {
      localStorage.setItem("courselens.last-lecture.v1", JSON.stringify(clean));
    } catch { /* 存储不可得：静默放弃，着陆页退回现路径 */ }
  },
  readLastLecture() {
    try {
      const raw = localStorage.getItem("courselens.last-lecture.v1");
      if (!raw) return null;
      let value;
      try {
        value = JSON.parse(raw);
      } catch {
        localStorage.removeItem("courselens.last-lecture.v1"); /* 损坏键自愈清除 */
        return null;
      }
      const courseId = String(value?.course_id ?? "").trim();
      const subId = String(value?.sub_id ?? "").trim();
      if (!courseId || !subId) {
        localStorage.removeItem("courselens.last-lecture.v1"); /* 形坏键自愈清除 */
        return null;
      }
      const ts = Number(value?.ts);
      return { course_id: courseId, sub_id: subId, ts: Number.isFinite(ts) ? ts : 0 };
    } catch {
      return null;
    }
  },
  set(key, value) {
    /* PF1 同值去重（引用级，2026-10-07）：对象/数组同引用重设零信息——不再
       重复扇出。仅对象引用级：原语键存在「同值重设=合法重绑」的消费语义
       （transcriptHasTiming 跨讲次 true→true 时 player-core.js:2559 必须重跑
       重绑 transcriptTimedSubId），值级判重会吞掉它；对象值写者全部纯函数
       返新数组（live-state merge 族），同引用重设必为纯冗余。 */
    if (value !== null && typeof value === "object" && this[key] === value) return;
    this[key] = value;
    (listeners.get(key) || []).forEach((listener) => listener(value));
    (listeners.get("*") || []).forEach((listener) => listener({ key, value }));
  },
  subscribe(key, listener) {
    if (!listeners.has(key)) listeners.set(key, new Set());
    listeners.get(key).add(listener);
    return () => listeners.get(key)?.delete(listener);
  },
};
