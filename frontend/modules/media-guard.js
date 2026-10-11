/* 页面级防泄露护栏（NIGHT5-U2）：媒体面（视频/图片/播放器舞台）的右键菜单、
   拖拽另存与浏览器 Ctrl+S/U 默认行为一律拦下；可编辑字段（输入框/文本域/
   内容可编辑）的右键菜单与 Ctrl+S/U 快捷键一并保留——复制粘贴与输入是正常
   学习动作（D14-P34：keydown 与 contextmenu 同款 EDITABLE_SELECTOR 豁免，
   两处纪律一致）。课程列表行的拖拽换位不受影响（拦截只认媒体元素本身）。
   诚实边界见 docs/media-protection-boundary.md：这只挡住「顺手另存/误拖出」
   类路径；DevTools 网络面板与录屏不在本护栏范围内。duck-typing 判元素（不
   依赖宿主 Element 构造器），root/surface 可注入，便于行为级钉测。 */

const EDITABLE_SELECTOR = "input, textarea, select, [contenteditable]:not([contenteditable='false'])";
const MEDIA_SELECTOR = "video, img, .player-stage-shell";

const isElement = (node) => Boolean(node && typeof node.closest === "function");

export function installMediaGuard(_store, root = document, surface = window) {
  const handleContextMenu = (event) => {
    const target = event.target;
    if (!isElement(target)) return;
    if (target.closest(EDITABLE_SELECTOR)) return;
    if (target.closest(MEDIA_SELECTOR)) event.preventDefault();
  };
  const handleDragStart = (event) => {
    const target = event.target;
    if (!isElement(target)) return;
    if (target.closest(MEDIA_SELECTOR)) event.preventDefault();
  };
  const handleKeyDown = (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
    /* 与 handleContextMenu 同款豁免（D14-P34）：输入框内恢复浏览器默认——
       守的是全局快捷键误触，不是打字监控。非元素 target（无聚焦等）照旧拦。 */
    const target = event.target;
    if (isElement(target) && target.closest(EDITABLE_SELECTOR)) return;
    const key = String(event.key || "").toLowerCase();
    if (key === "s" || key === "u") event.preventDefault();
  };
  const hardenMedia = () => {
    root.querySelectorAll("video, img").forEach((node) => { node.draggable = false; });
  };
  root.addEventListener("contextmenu", handleContextMenu, { capture: true });
  root.addEventListener("dragstart", handleDragStart, { capture: true });
  surface.addEventListener("keydown", handleKeyDown, { capture: true });
  hardenMedia();
  return () => {
    root.removeEventListener("contextmenu", handleContextMenu, { capture: true });
    root.removeEventListener("dragstart", handleDragStart, { capture: true });
    surface.removeEventListener("keydown", handleKeyDown, { capture: true });
  };
}
