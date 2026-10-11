import { store } from "./modules/store.js";
import { installShell } from "./modules/shell.js";
import { installStudy } from "./modules/study.js";
import { installReviewMultiView } from "./modules/review-multiview.js";
import { installPlayerCore } from "./modules/player-core.js";
import { installStagePreload } from "./modules/stage-preload.js";
import { installLiveRoom } from "./modules/live-room.js";
import { installLivePage } from "./modules/live-page.js";
import { installTasksDrawer } from "./modules/tasks-drawer.js";
import { installSearchPalette } from "./modules/search-palette.js";
import { installTimetable } from "./modules/timetable.js";
import { installHomeOverview } from "./modules/home-overview.js";
import { installSettings } from "./modules/settings.js";
import { installOnboarding } from "./modules/onboarding.js";
import { installMediaGuard } from "./modules/media-guard.js";
import { initPageDropdowns } from "./modules/dropdown.js";
import { setGlobalStatus } from "./modules/ui.js";

const installers = [
  installShell,
  installStudy,
  installReviewMultiView,
  installPlayerCore,
  installStagePreload,
  installLiveRoom,
  installLivePage,
  installTasksDrawer,
  installSearchPalette,
  installTimetable,
  installHomeOverview,
  installSettings,
  installOnboarding,
  installMediaGuard,
];

async function bootstrap() {
  const cleanups = [];
  for (const install of installers) {
    const cleanup = await install(store);
    if (typeof cleanup === "function") cleanups.push(cleanup);
  }
  initPageDropdowns(document); /* 甲3：页面级 select 换装（倍速由 player-core 自附 media 皮） */
  window.addEventListener("pagehide", () => {
    cleanups.reverse().forEach((cleanup) => cleanup());
  }, { once: true });
}

bootstrap().catch((error) => {
  console.error(error);
  setGlobalStatus(error.message || "客户端初始化失败", "error");
});
