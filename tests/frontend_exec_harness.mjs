import { readFileSync, readdirSync } from "node:fs";

/* 真执行装配骨架（N15-W1 落地，源自 frontend_update_notes_behavior.mjs 的
   已证形态 + 夜14-R7 TOP10 草案的多行 import 剥法）。

   用法：readModuleSource → assemble(relPath, [符号名...], [导出名...]) 得到
   工厂，用桩件按位注入后调用。零缺断言由调用方用 missingRecorder 配合 $
   桩完成：import 面漂移（模块新增被测路径引用的符号/元素 id）即红，防
   静默脱钩，替代字符串钉的碎裂性而不失其提醒价值。 */

export function readModuleSource(relPath) {
  return readFileSync(new URL(relPath, import.meta.url), "utf8");
}

/* 家族并集源（ARCH-DEBT-1）：被拆模块（settings.js 等）= 门面 + 子目录
   <stem>/*.js。源码内容断言应检索家族并集而非门面单文件——纯移动保证
   并集文本行为不变，钉的强度不因拆分而减弱。无子目录时退化为门面本身。 */
export function familySource(stem) {
  let text = readModuleSource(`../frontend/modules/${stem}.js`);
  let entries;
  try {
    entries = readdirSync(new URL(`../frontend/modules/${stem}/`, import.meta.url));
  } catch {
    return text;
  }
  for (const entry of entries.sort()) {
    if (entry.endsWith(".js")) text += `\n${readModuleSource(`../frontend/modules/${stem}/${entry}`)}`;
  }
  return text;
}

/* 剥 import（单行与跨行 named 块两种形态）与裸导入、门面 re-export 行
   （ARCH-DEBT-1 拆分后门面新增形态），再去 export 前缀，
   使模块体可在 new Function 内以注入参数运行。 */
export function moduleBody(source) {
  return source
    .replace(/^import[\s\S]*?from\s+["'][^"']+["'];[ \t]*$/gm, "")
    .replace(/^import\s+["'][^"']+["'];[ \t]*$/gm, "")
    .replace(/^export\s*\{[^}]*\}\s*from\s*["'][^"']+["'];[ \t]*$/gm, "")
    .replace(/^export (?=async function|function|const|let|var|\{)/gm, "");
}

/* 装配工厂：调用方按 symbolNames 顺序注入桩件，读取 exportNames。 */
export function assemble(relPath, symbolNames, exportNames) {
  const body = moduleBody(readModuleSource(relPath));
  return new Function(...symbolNames, `${body}\nreturn { ${exportNames.join(", ")} };`);
}

/* 家族装配工厂（ARCH-DEBT-1）：被拆模块按家族并集体装配——纯移动保证
   并集体内所有家族内引用自洽，自由变量=原单文件的外部导入桩，桩面零变化。 */
export function assembleFamily(stem, symbolNames, exportNames) {
  const body = moduleBody(familySource(stem));
  return new Function(...symbolNames, `${body}\nreturn { ${exportNames.join(", ")} };`);
}

/* 「符号表零缺」哨兵的录制器：$ 桩包一层，未知 id 记入 missing。 */
export function missingRecorder() {
  const missing = [];
  return {
    missing,
    $stub(lookup) {
      return (id) => {
        const element = lookup(id);
        if (!element) missing.push(id);
        return element;
      };
    },
  };
}

export class FakeElement {
  constructor(id) {
    this.id = id;
    this.textContent = "";
    this.innerHTML = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.className = "";
    this.children = [];
    this.dataset = {};
    this.listeners = {};
  }
  addEventListener(kind, handler) {
    (this.listeners[kind] ||= []).push(handler);
  }
  removeEventListener(kind, handler) {
    this.listeners[kind] = (this.listeners[kind] || []).filter((item) => item !== handler);
  }
  /* 同步派发并返回各 handler 的返回值（async handler 的 promise 由调用方
     Promise.all 收割后再断言终态）。 */
  dispatch(kind, event = {}) {
    return (this.listeners[kind] || []).map((handler) => handler(event));
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter((item) => item !== child);
  }
  replaceChildren(...replacement) {
    this.children = replacement;
  }
  append(...children) {
    this.children.push(...children);
  }
  showModal() {
    this.open = true;
  }
  close() {
    this.open = false;
    this.dispatch("close");
  }
  setAttribute(name, value) {
    this.dataset[name] = String(value);
  }
  removeAttribute(name) {
    delete this.dataset[name];
  }
  focus() {}
  classList = {
    add: () => {},
    remove: () => {},
    toggle: () => {},
    contains: () => false,
  };
}

/* 按 id 清单建注册表；unknown() 允许桩面按需补挂（仍过 missing 哨兵）。 */
export function elementRegistry(ids) {
  const byId = new Map();
  for (const id of ids) byId.set(id, new FakeElement(id));
  return {
    byId,
    lookup(id) {
      return byId.get(id) || null;
    },
  };
}

/* api 桩：路径+payload 录制器，envelope 按调用方注入应答。 */
export function recordedApiStub(responses = {}) {
  const calls = [];
  const answer = (path) => {
    if (!(path in responses)) throw new Error(`unexpected api path: ${path}`);
    return responses[path];
  };
  return {
    calls,
    apiV3: (path) => {
      calls.push({ kind: "GET", path });
      return answer(path);
    },
    postV3: (path, payload) => {
      calls.push({ kind: "POST", path, payload });
      return answer(path);
    },
    putV3: (path, payload) => {
      calls.push({ kind: "PUT", path, payload });
      return answer(path);
    },
    deleteV3: (path) => {
      calls.push({ kind: "DELETE", path });
      return answer(path);
    },
  };
}

/* update-mac.js 桩族（UPDATE-STUB-FIX-1，a3ddf5e 起 settings 家族并集体在
   装配期执行 update-panel.js 顶层 onMacUpdateChange 注册——任何家族装配都
   需要注入退订桩；update-widget.js 装配同理需要四符号全桩）。
   缺省=Windows 宿主空态（macUpdateEntry()=false，与真模块对非 Mac 环境的
   判定同语义）；publish(next) 供行为钉驱动三态迁移重绘。 */
export function macUpdateStubs() {
  let snapshot = { phase: "idle", version: "", currentVersion: "", error: "" };
  const subscribers = new Set();
  return {
    macUpdateEntry: () => false,
    macUpdateSnapshot: () => snapshot,
    onMacUpdateChange: (listener) => {
      subscribers.add(listener);
      return () => subscribers.delete(listener);
    },
    runMacUpdateCheck: async () => snapshot,
    publish(next) {
      snapshot = next;
      for (const listener of [...subscribers]) listener(snapshot);
    },
  };
}
