/* 直播页专用精简播放器（N6L S1 · L6）：自 player-core 直播段迁移
 * （handleLivePlay 语义 + 追帧层 + 回到直播中 pill + U0 透码失败卡），
 * video 元素直播页自有，与学习桌播放器物理分离。功能面=F2 切割：
 * 无进度条/字幕/书签/九档倍速——直播就是上课，不背回放的债。
 * 会话建立链（grants→sessions）与恢复梯由 live-page.js 拥有；
 * 本模块只管媒体面：起播、缓冲、追帧、失败透卡。 */

import { $ } from "./ui.js";
import { resolveLiveErrorCard } from "./live-state.js";

/* ---- 追帧层闭集阈值（G3-L2 / P25，迁移自 player-core）：behind 的度量
 * 基准是 liveSyncPosition（hls.js 期望学生稳态所在的同步点）。落后超过
 * pill 阈值浮出「回到直播中」；超过追帧起始阈值且当前 1x 时温和加速。 ---- */
const LIVE_PILL_BEHIND_SECONDS = 10;
const LIVE_CATCHUP_START_SECONDS = 6;
const LIVE_CATCHUP_SETTLED_SECONDS = 2;
const LIVE_CATCHUP_MAX_RATE = 1.5;
/* N5PR-P4 同款防闪：waiting >300ms 仍未恢复才显示缓冲指示 */
const SPINNER_DELAY_MS = 300;
/* 弱网（L2 #10）：60s 窗口内 ≥3 次缓冲 → weaknet 提示条；稳定播放 15s 解除 */
const WEAKNET_WINDOW_MS = 60000;
const WEAKNET_THRESHOLD = 3;
const WEAKNET_STABLE_MS = 15000;

function seekableEnd(player) {
  try {
    const seekable = player.seekable;
    if (seekable && seekable.length > 0) {
      const end = Number(seekable.end(seekable.length - 1));
      if (Number.isFinite(end) && end >= 0) return end;
    }
  } catch {
    /* seekable 不可用时退回 duration 钳制 */
  }
  const duration = Number(player.duration);
  return Number.isFinite(duration) && duration > 0 ? duration : Number.NaN;
}

function formatClock(milliseconds) {
  const total = Math.floor(milliseconds / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

export function createLivePlayer({ onEvent }) {
  const video = $("live-video");
  const pill = $("live-pill");
  const badge = $("live-badge");
  const clock = $("live-clock");
  const spinner = $("live-buffering");

  let hls = null;
  /* U17 B1 / 第廿六案：最近一次直播失败的闭集原因（失败响应体 error_code），
     每次起播复位；失败卡一律透码，绝不静默吞成未知。 */
  let lastFailureCode = "";
  let active = false; /* 会话已建立且媒体面在场（playing 前后都算） */
  let catchupActive = false;
  let catchupWrite = false;
  let playedMs = 0;
  let lastTick = 0;
  let spinnerTimer = 0;
  let bufferStamps = [];
  let weaknetLatched = false;
  let stableSince = 0;
  let disposed = false;

  const emit = (payload) => {
    if (!disposed) onEvent(payload);
  };

  const setPillVisible = (visible, behindSeconds) => {
    if (!pill) return;
    if (visible) {
      pill.title = `已经落后直播 ${Math.round(behindSeconds)} 秒`;
      pill.hidden = false;
    } else {
      pill.hidden = true;
    }
  };

  const clearSpinner = () => {
    if (spinnerTimer) {
      window.clearTimeout(spinnerTimer);
      spinnerTimer = 0;
    }
    if (spinner) spinner.hidden = true;
  };

  const showSpinner = () => {
    if (spinnerTimer) return;
    spinnerTimer = window.setTimeout(() => {
      spinnerTimer = 0;
      if (spinner) spinner.hidden = false;
    }, SPINNER_DELAY_MS);
  };

  const noteBufferStall = () => {
    const now = Date.now();
    bufferStamps = bufferStamps.filter((stamp) => now - stamp < WEAKNET_WINDOW_MS);
    bufferStamps.push(now);
    stableSince = 0;
    if (!weaknetLatched && bufferStamps.length >= WEAKNET_THRESHOLD) {
      weaknetLatched = true;
      emit({ playback: "weaknet" });
    }
  };

  /* 追帧层（迁移自 player-core updateLiveCatchup）：度量与 seek 统一以
     同步点为基准；无时间基准的源一律安静。 */
  const liveSyncTarget = () => {
    if (hls) {
      const sync = Number(hls.liveSyncPosition);
      if (Number.isFinite(sync) && sync >= 0) return sync;
    }
    return seekableEnd(video);
  };

  const updateCatchup = () => {
    if (!active) {
      if (catchupActive) {
        catchupActive = false;
        video.playbackRate = 1;
      }
      setPillVisible(false, 0);
      return;
    }
    const target = liveSyncTarget();
    const behind = Number.isFinite(target)
      ? Math.max(0, target - Number(video.currentTime))
      : Number.NaN;
    if (!Number.isFinite(behind)) {
      setPillVisible(false, 0);
      return;
    }
    setPillVisible(behind > LIVE_PILL_BEHIND_SECONDS, behind);
    /* behind 态只在越过 pill 阈值时成立（页态机闭集），0.5s 级落后不算 */
    emit({ playback: behind > LIVE_PILL_BEHIND_SECONDS ? "behind" : "playing" });
    const catchupRate = Math.min(
      LIVE_CATCHUP_MAX_RATE,
      1 + (behind - LIVE_CATCHUP_SETTLED_SECONDS) / 20,
    );
    if (catchupActive) {
      if (behind < LIVE_CATCHUP_SETTLED_SECONDS) {
        catchupActive = false;
        catchupWrite = true;
        video.playbackRate = 1;
        catchupWrite = false;
      } else {
        catchupWrite = true;
        video.playbackRate = catchupRate;
        catchupWrite = false;
      }
      return;
    }
    if (behind > LIVE_CATCHUP_START_SECONDS && Number(video.playbackRate) === 1 && video.paused === false) {
      catchupActive = true;
      catchupWrite = true;
      video.playbackRate = catchupRate;
      catchupWrite = false;
    }
  };

  const showFailure = (failureCode, mediaCode) => {
    /* 第廿六案：一律经 resolveLiveErrorCard 透码——已知闭集码走本卡，
       未闭集码 fatal 卡附诊断行，绝不静默吞成未知。 */
    lastFailureCode = String(failureCode || "");
    const card = resolveLiveErrorCard(lastFailureCode);
    console.warn(`[live] 播放器失败已透卡 card=${card.code}${lastFailureCode ? ` upstream=${lastFailureCode}` : ""}${mediaCode ? ` media=${mediaCode}` : ""}`);
    stop();
    emit({ playback: "error", card });
  };

  const handleVideoError = () => {
    if (!active) return;
    const mediaCode = Number(video.error?.code || 0);
    if (mediaCode === 3) {
      showFailure("live_decode_failed", mediaCode);
    } else if (mediaCode === 4) {
      showFailure(lastFailureCode, mediaCode);
    } else {
      showFailure(lastFailureCode || "live_network_interrupted", mediaCode);
    }
  };

  const handleTimeUpdate = () => {
    const now = performance.now();
    if (lastTick) playedMs += Math.min(now - lastTick, 5000);
    lastTick = now;
    if (clock) {
      clock.hidden = false;
      clock.textContent = `已播 ${formatClock(playedMs)}`;
    }
    if (weaknetLatched) {
      if (!stableSince) stableSince = now;
      if (now - stableSince >= WEAKNET_STABLE_MS) {
        weaknetLatched = false;
        bufferStamps = [];
        stableSince = 0;
        emit({ playback: "stable" });
      }
    } else {
      stableSince = 0;
    }
    updateCatchup();
  };

  const handlePillClick = () => {
    const target = liveSyncTarget();
    if (!Number.isFinite(target) || target <= 0) return;
    catchupActive = false;
    video.currentTime = target;
    setPillVisible(false, 0);
  };

  const handleRateChange = () => {
    if (catchupWrite) return;
    if (catchupActive) {
      /* 学生改速让位：追帧停，绝不与学生抢倍速 */
      catchupActive = false;
    }
  };

  /* 起播（直播页会话建立后调用）：与 player-core handleLivePlay 同配置——
     sync/maxLatency 成对（9=3×sync 保守跳边），缺配对延迟单调漂移。 */
  const attach = (manifestPath) => {
    stop();
    active = true;
    playedMs = 0;
    lastTick = 0;
    bufferStamps = [];
    weaknetLatched = false;
    stableSince = 0;
    lastFailureCode = "";
    if (badge) badge.hidden = false;
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = manifestPath;
      video.load();
      void video.play().catch(() => {});
      return;
    }
    if (window.Hls?.isSupported()) {
      hls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: true,
        backBufferLength: 30,
        maxBufferLength: 30,
        liveSyncDurationCount: 3,
        liveMaxLatencyDurationCount: 9,
      });
      hls.loadSource(manifestPath);
      hls.attachMedia(video);
      hls.on(window.Hls.Events.MANIFEST_PARSED, () => void video.play().catch(() => {}));
      hls.on(window.Hls.Events.ERROR, (_event, data) => {
        if (disposed || !data?.fatal) return;
        if (data.type === window.Hls.ErrorTypes?.MEDIA_ERROR) {
          showFailure("live_decode_failed", 0);
          return;
        }
        /* U17 B1 同款：网络级失败先读失败响应体闭集 error_code 再透卡；
           读不到码按连接中断诚实呈现。恢复梯由 live-page 的 onEvent
           reconnect 裁决（n/3），媒体面先离场。 */
        void fetch(manifestPath, { credentials: "same-origin" }).then((response) => {
          try { return response.json(); } catch { return {}; }
        }).then((body) => {
          lastFailureCode = String(body?.error_code || "");
          emit({ playback: "reconnect", code: lastFailureCode });
        }).catch(() => {
          emit({ playback: "reconnect", code: "" });
        });
      });
    } else {
      showFailure("live_format_unsupported", 0);
    }
  };

  const stop = () => {
    if (hls) hls.destroy();
    hls = null;
    active = false;
    catchupActive = false;
    lastTick = 0;
    clearSpinner();
    setPillVisible(false, 0);
    if (badge) badge.hidden = true;
    if (clock) {
      clock.hidden = true;
      clock.textContent = "";
    }
    video.removeAttribute("src");
    try { video.load(); } catch { /* 引擎清理竞态不致命 */ }
  };

  const pauseForAway = () => {
    if (video.paused === false) video.pause();
  };

  const setBuffering = (waiting) => {
    if (!active) return;
    if (waiting) {
      showSpinner();
      noteBufferStall();
      emit({ playback: "buffering" });
    } else {
      clearSpinner();
      emit({ playback: "streaming" });
    }
  };

  const handleWaiting = () => setBuffering(true);
  const handleStreaming = () => setBuffering(false);
  video.addEventListener("waiting", handleWaiting);
  video.addEventListener("stalled", handleWaiting);
  video.addEventListener("playing", handleStreaming);
  video.addEventListener("canplay", handleStreaming);
  video.addEventListener("timeupdate", handleTimeUpdate);
  video.addEventListener("error", handleVideoError);
  video.addEventListener("ratechange", handleRateChange);
  pill?.addEventListener("click", handlePillClick);

  return {
    attach,
    stop,
    pauseForAway,
    /* B6 续播尝试：只试 play，成败交回调用方裁决（静默续建恰一次在页侧） */
    resume() {
      return video.play().then(() => true).catch(() => false);
    },
    get isActive() { return active; },
    get lastFailureCode() { return lastFailureCode; },
    dispose() {
      disposed = true;
      stop();
      video.removeEventListener("waiting", handleWaiting);
      video.removeEventListener("stalled", handleWaiting);
      video.removeEventListener("playing", handleStreaming);
      video.removeEventListener("canplay", handleStreaming);
      video.removeEventListener("timeupdate", handleTimeUpdate);
      video.removeEventListener("error", handleVideoError);
      video.removeEventListener("ratechange", handleRateChange);
      pill?.removeEventListener("click", handlePillClick);
    },
  };
}
