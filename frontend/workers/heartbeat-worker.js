/* 心跳节拍器（BACKEND-DEATH-1②）：Dedicated Worker 的定时器不受页面可见性节流，
   标签页退到后台后仍按拍发消息；主线程收到拍点才真正发心跳。
   协议闭集：{type:"start", intervalMs} 启动；{type:"stop"} 停止；拍点 postMessage("heartbeat")。 */
let timer = 0;

self.onmessage = (event) => {
  const data = event.data || {};
  if (data.type === "start") {
    if (timer) self.clearInterval(timer);
    timer = self.setInterval(() => self.postMessage("heartbeat"), Math.max(1000, Number(data.intervalMs) || 100000));
  } else if (data.type === "stop") {
    if (timer) self.clearInterval(timer);
    timer = 0;
  }
};
