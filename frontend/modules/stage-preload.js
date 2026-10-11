/* PERF-C23 舞台级预载提示（夜批15 R2 C2-3 定谳落地）。
 *
 * 痛点：讲次选中→媒体元数据就绪之间（慢网约 8-10s），舞台只剩「选择讲次后…」
 * 占位——spinner 钉的 waiting/playing/canplay 事件族在预载期根本不触发（video
 * 尚未上屏，夜批15 R2 实证），学生面对无解释的空窗。
 *
 * 本件是纯观察者：只订阅 store.activeLecture、监听 video 的 loadedmetadata/
 * error，绝不写播放器任何状态（player-core 播放逻辑面零触碰）。
 * 纪律：
 * - 防闪 500ms（对齐 N5PR-P4 spinner 防闪先例）：本机/快网元数据先到则提示
 *   永不出现，绝不闪一下就走。
 * - 让位平滑：内容就绪先淡出（--motion 过渡）再摘除，禁瞬跳；
 *   prefers-reduced-motion 由 accessibility.css 全局 .01ms 规则压平（无动效
 *   语义不变）。
 * - 恢复面板互斥（对齐 spinner 纪律）：失败面归恢复面板说，不叠加「准备中」。
 * - 文案/形态走舞台恒暗族 tokens（--stage-*，:root 冻结字面常量），双主题按
 *   构造恒常；闭集文案钉于 tests/frontend_stage_preload_behavior.mjs。 */

const PRELOAD_HINT_DELAY_MS = 500;
/* 让位过渡兜底：--motion(180ms) 淡出后摘除。不监听 transitionend——reduce 压平
   或后台标签页下该事件可能不派发，固定兜底更可 Deterministic（测试同源）。 */
const HIDE_FALLBACK_MS = 240;

export function installStagePreload(store) {
  const video = document.getElementById("player-stage");
  const hint = document.getElementById("player-stage-preload");
  const recovery = document.getElementById("player-recovery");
  if (!video || !hint) return undefined;
  let armTimer = 0;
  let leaveTimer = 0;

  const clearLeave = () => {
    if (leaveTimer) {
      window.clearTimeout(leaveTimer);
      leaveTimer = 0;
    }
    hint.classList.remove("is-leaving");
  };

  const disarm = () => {
    if (armTimer) {
      window.clearTimeout(armTimer);
      armTimer = 0;
    }
  };

  const hide = () => {
    disarm();
    if (leaveTimer) {
      window.clearTimeout(leaveTimer);
      leaveTimer = 0;
    }
    if (hint.hidden) return;
    /* 平滑让位：先淡出再摘除，视频上屏与提示退场交叉完成，无瞬跳 */
    hint.classList.add("is-leaving");
    leaveTimer = window.setTimeout(() => {
      leaveTimer = 0;
      hint.classList.remove("is-leaving");
      hint.hidden = true;
    }, HIDE_FALLBACK_MS);
  };

  /* 锚定可见舞台：预载期 video 未挂载，stage-shell/fit 塌缩为 0 高（2026-10-07
     实测），提示若用 top:50% 会沉到塌缩点、半截出界。以占位盒（预载期唯一
     可见舞台面）实测半高写 CSS 变量，把提示竖直锚在占位盒中心。纯读几何，
     不写任何播放器状态。 */
  const anchorToStage = () => {
    const placeholder = document.getElementById("player-placeholder");
    if (!placeholder || placeholder.hidden === true) return;
    const half = Math.round(placeholder.getBoundingClientRect().height / 2);
    if (half > 0) hint.style.setProperty("--preload-stage-half", `${half}px`);
  };

  const arm = (lecture) => {
    disarm();
    if (leaveTimer) {
      /* 切讲打断让位：取消摘除、回到亮起态——新讲次的预载马上接续，
         「正在准备」语义仍真，保持提示连续（收-亮往返=闪烁，禁止） */
      window.clearTimeout(leaveTimer);
      leaveTimer = 0;
      hint.classList.remove("is-leaving");
    }
    if (!lecture) {
      /* 归空即收：无讲次无提示（舞台回到未选讲次占位），走同一平滑让位路径 */
      hide();
      return;
    }
    armTimer = window.setTimeout(() => {
      armTimer = 0;
      if (recovery && recovery.hidden === false) return; /* 恢复面板互斥 */
      anchorToStage();
      hint.classList.remove("is-leaving");
      hint.hidden = false;
    }, PRELOAD_HINT_DELAY_MS);
  };

  const handleReady = () => hide();
  const handleFailure = () => hide();
  video.addEventListener("loadedmetadata", handleReady);
  video.addEventListener("error", handleFailure);
  const unsubscribe = store.subscribe("activeLecture", arm);
  /* 安装时已有活动讲次（装配序/热重载边界）：按当前事实武装 */
  if (store.activeLecture) arm(store.activeLecture);

  return () => {
    unsubscribe();
    disarm();
    if (leaveTimer) {
      window.clearTimeout(leaveTimer);
      leaveTimer = 0;
    }
    video.removeEventListener("loadedmetadata", handleReady);
    video.removeEventListener("error", handleFailure);
    hint.classList.remove("is-leaving");
    hint.hidden = true;
  };
}
