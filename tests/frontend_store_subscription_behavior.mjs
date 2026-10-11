import assert from "node:assert/strict";

/* 共享 store「订阅面」行为钉（夜14-R7 积压 27 清偿：store.js 此前零行为钉，
   全站状态扇出的唯一真相源）。契约：subscribe 返回退订函数（泄漏防线）；
   set 键内+「*」双通道扇出；PF1 同值去重仅对象引用级（原语同值重设=合法
   重绑——transcriptHasTiming 跨讲次 true→true 必须重跑重绑）；null 值绕过
   去重（清场语义恒达）。 */

const { store } = await import("../frontend/modules/store.js");

/* 1) 退订即静默：subscribe 返回的函数生效后零扇出（订阅泄漏防线） */
{
  const seen = [];
  const unsubscribe = store.subscribe("probeUnsub", (value) => seen.push(value));
  store.set("probeUnsub", { generation: 1 });
  assert.equal(seen.length, 1, "退订前正常扇出");
  unsubscribe();
  store.set("probeUnsub", { generation: 2 });
  assert.equal(seen.length, 1, "退订后零扇出（监听不残留）");
  assert.equal(store.probeUnsub.generation, 2, "值本身照常写入（退订只断扇出不断真相）");
  console.log("ok: 退订即静默");
}

/* 2) 双通道扇出：键内监听收值、「*」监听收 {key, value} 信封 */
{
  const keyed = [];
  const wildcard = [];
  const offKey = store.subscribe("probeDual", (value) => keyed.push(value));
  const offAll = store.subscribe("*", (event) => wildcard.push(event));
  store.set("probeDual", 7);
  assert.deepEqual(keyed, [7], "键内监听收新值");
  assert.deepEqual(wildcard, [{ key: "probeDual", value: 7 }], "「*」监听收 {key, value} 信封");
  offKey();
  offAll();
  console.log("ok: 键内+星号双通道");
}

/* 3) PF1 同值去重=对象引用级：同引用零扇出；新引用照常扇出 */
{
  const seen = [];
  const off = store.subscribe("probeRef", (value) => seen.push(value));
  const first = [{ id: 1 }];
  store.set("probeRef", first);
  assert.equal(seen.length, 1);
  store.set("probeRef", first); /* 同引用重设：纯冗余，零扇出 */
  assert.equal(seen.length, 1, "PF1：同引用对象重设不重复扇出");
  store.set("probeRef", [{ id: 1 }]); /* 新引用（纯函数返新数组）：照常扇出 */
  assert.equal(seen.length, 2, "新引用对象重设照常扇出");
  off();
  console.log("ok: 对象引用级去重");
}

/* 4) 原语同值重设=合法重绑：true→true 必须重扇（transcriptHasTiming 跨讲次
   重绑语义，player-core 写侧绑定守卫依赖；值级判重会吞掉它） */
{
  const seen = [];
  const off = store.subscribe("transcriptHasTiming", (value) => seen.push(value));
  store.set("transcriptHasTiming", true);
  assert.equal(seen.length, 1);
  store.set("transcriptHasTiming", true); /* 同值重设：必须重扇（重绑） */
  assert.equal(seen.length, 2, "原语同值重设不被判重（PF1 刻意只做对象引用级）");
  off();
  console.log("ok: 原语同值重绑");
}

/* 5) null 清场语义：null 恒扇出（去重对 null 不生效——清场必须到达） */
{
  const seen = [];
  const off = store.subscribe("probeNull", (value) => seen.push(value));
  store.set("probeNull", { id: 1 });
  store.set("probeNull", null);
  assert.deepEqual(seen, [{ id: 1 }, null], "对象→null 恒扇出");
  store.set("probeNull", null); /* null→null 同样扇出（去重绕过） */
  assert.equal(seen.length, 3, "null 重设不被判重（清场语义恒达）");
  off();
  console.log("ok: null 清场恒达");
}

console.log("frontend store subscription behavior passed");
