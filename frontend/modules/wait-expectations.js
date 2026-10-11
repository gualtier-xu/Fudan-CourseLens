/* 等待预期（ETA）文案集中管理（WAIT-UX-1，2026-10-09 立）。
   数据来源（唯一权威）：WAIT-MEASURE-1 全产品等待点普查与实测——
   archive/external-artifacts/top-model-results-20260930/product-waitmeasure1-result-20261009.md
   （39 点普查 + 合成壳 P50/P90 实测 + R3/R4/E2E2 历史真实链分布，采集 2026-10-09）。
   纪律：
   ① 有实测锚点才写数字区间，区间宁宽勿窄；无实测样本的链路只写因果/档位
     措辞，绝不发明死数（零呆等三律：ETA 必须来自实测分布）；
   ② 弱网/大包一律带上浮措辞（「慢网可能更久」「包越大越久」）；
   ③ 等待链路变更后由测量道重测并回填本文件，重测日期随条目注明；
   ④ 全产品「预计约…」类文案只准从本表取值，禁止各面散落另立（闭集单源）。 */

/* ---- 远程连接异步动作（C4/C5/C6/C7 段） ----
   锚点：bootstrap 20.5s（R4 段①）；test-channel 9s（E2E2v5）/约 60s（E2E2v6）；
   repair-worker 约 113s（E2E2v4）；rotate-worker-keys 无独立样本，沿用保守区间。
   重测日期：2026-10-09。 */
export const REMOTE_ACTION_EXPECTATIONS = Object.freeze({
  bootstrap: "约需半分钟到两分钟，请稍候",
  "test-channel": "约需十秒到一分钟，慢网可能更久，请稍候",
  "repair-worker": "通常约两分钟，慢网可能更久，请稍候",
  "rotate-worker-keys": "约需一至两分钟，请稍候",
});
export const REMOTE_ACTION_EXPECTATION_FALLBACK = "约需一至两分钟，请稍候";

/* ---- 课件 PDF 排队窗（E5+：提交后、画面计数上报前的空窗） ----
   全链时长无实测样本（2026-10-09），按纪律只写因果措辞不写数字；生成中的
   实时进度由后端事实（counts/percent，coursewarePdfDetailText）承担。
   重测待办：测量道补全链分布后回填数字区间。 */
export const COURSEWARE_PDF_QUEUED_HINT
  = "整理快慢看本讲画面的多少，请给它一点时间；完成后这里会直接出现下载入口";

/* ---- 搬家包导入「校验、解密并安家」服务段（J3+）按包体分档 ----
   锚点：344KB 全程 P50 1.1s / P90 1.6s（合成壳环回，含上传，2026-10-09）；
   GB 级大包无样本（闭集上限 8GiB），高档位只给量级与上浮措辞。
   重测日期：2026-10-09。 */
export function migrationImportExpectation(bytes) {
  const size = Number(bytes);
  if (!Number.isFinite(size) || size <= 0) return "时长看包的大小";
  const mb = size / (1024 * 1024);
  if (mb < 16) return "这个包不大，通常几秒内完成";
  if (mb < 512) return "预计约一两分钟，包越大越久";
  return "大包预计要几分钟，请耐心等";
}

/* ---- 客户端重置（K1+） ----
   锚点：本机段 P50 约 0.2s（合成壳 2026-10-09，不含网络段）；勾选删仓时含
   GitHub 网络段，无直接样本，按同族远端仓库操作（20.5s-113s）给宽区间。
   重测日期：2026-10-09。 */
export const RESET_RUNNING_LOCAL = "正在重置…（本机数据通常几秒内清完；完成后应用会自动关闭）";
export const RESET_RUNNING_WITH_REPOS
  = "正在重置…（含删除 GitHub 仓库，视网络约需半分钟到两分钟；完成后应用会自动关闭）";

/* ---- 深度回答端到端时长（F3：点击深度→答案可读，含云端排队） ----
   锚点：真实链 5 样本 87.5/94.9/103.5/125.8/127.5 秒（AI-QUALITY-SWEEP-1
   2026-10-10 沙箱 3 笔 + ONBOARD-REAL-1-C 125.8s + D13-REPLAY 127.5s）→
   P50≈104s、P90≈127s；runner 净算 <2s，>98% 为 GitHub 排队等待。
   区间宁宽勿窄取一分半到两分半，排队慢时上浮。
   重测日期：2026-10-10。 */
export const DEEP_ANSWER_EXPECTATION = "通常约需一分半到两分半，排队慢时可能更久";

/* ---- 深度回答·远程通道就绪窗（F3 派发前守卫提示，U3 根修伴生） ----
   锚点：登录后远程通道就绪窗约 25 秒（AI-QUALITY-SWEEP-1 a1 活体单样本，
   样本量 1，待测量道补分布）；前端守卫窗上限 60 秒（约 2 倍观测窗）是工程
   停线不是实测 ETA，不进文案死数。
   重测日期：2026-10-10。 */
export const REMOTE_CHANNEL_WARMING_HINT = "远程通道就绪中，通常半分钟内";
