'use strict';
(() => {
  const button = document.getElementById('restartService');
  const status = document.createElement('div');
  status.className = 'note'; status.hidden = true; status.setAttribute('role', 'status');
  document.querySelector('main header').after(status);
  let busy = false, observed = null;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function service() {
    const r = await fetch('/api/status', {cache: 'no-store', signal: AbortSignal.timeout(2000)});
    if (!r.ok) throw Error('服务暂不可用');
    const d = await r.json();
    if (!d.service?.instanceId) throw Error('当前服务尚未加载重启功能，请先手动重启一次');
    return d.service;
  }
  async function waitForRestart(previous) {
    busy = true; button.disabled = true; button.textContent = '正在重启…';
    status.hidden = false; status.textContent = '正在重启 WEB 服务，请稍候…';
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      await sleep(1000);
      try {
        const current = await service();
        if (current.instanceId !== previous.instanceId && !current.restarting) {
          location.reload(); return;
        }
      } catch {}
    }
    status.textContent = '60 秒内未能确认服务恢复。请检查 .cache/service-restart.log，并在项目目录手动启动：' + previous.startCommand;
    button.textContent = '重启未恢复';
    // Do not automatically retry an uncertain restart request.
  }
  button.onclick = async () => {
    if (busy) return;
    if (!window.confirm('将中止正在运行的任务，清空回测、回放和监控信号记录。已保存配置与文件保留，任务不会自动续跑。是否重启 WEB 服务？')) return;
    busy = true; button.disabled = true;
    let previous;
    try {
      previous = await service();
      if (!previous.restarting) {
        let r;
        try {
          r = await fetch('/api/service/restart', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}', signal: AbortSignal.timeout(5000)});
        } catch {
          // The accepted response may be lost as the old service exits.
          return await waitForRestart(previous);
        }
        if (!r.ok) {
          const d = await r.json(); throw Error(d.error || '重启请求失败');
        }
      }
      await waitForRestart(previous);
    } catch (e) {
      status.hidden = false; status.textContent = e.message;
      busy = false; button.disabled = false; button.textContent = '重启 WEB 服务';
    }
  };
  // Other open workbench pages observe the same restart, without issuing a POST.
  setInterval(async () => {
    if (busy) return;
    try {
      const d = await service();
      if (observed && observed.instanceId !== d.instanceId) { location.reload(); return; }
      observed = d;
      if (d.restarting) await waitForRestart(d);
    } catch {}
  }, 1000);
})();
