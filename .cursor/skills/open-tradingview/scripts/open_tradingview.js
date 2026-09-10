/**
 * Launch TradingView Desktop with CDP on localhost:9222.
 * Usage: node open_tradingview.js [--json] [--allow-restart]
 * A running instance is never terminated without --allow-restart.
 * PowerShell and MSIX package launch methods share CDP readiness checks.
 */
const { execFile } = require("child_process");
const net = require("net");
const fs = require("fs");
const path = require("path");

const PORT = 9222;
const WINDOWS_APPS_DIR = "C:/Program Files/WindowsApps";

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** 检测本地端口是否已监听 */
function isPortListening(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ port, host: "127.0.0.1" });
    socket.setTimeout(1500, () => { socket.destroy(); resolve(false); });
    socket.once("connect", () => { socket.destroy(); resolve(true); });
    socket.once("error", () => resolve(false));
  });
}

/** 在 WindowsApps 中查找 TradingView.exe（处理版本号通配） */
function findTradingViewExe() {
  try {
    const entries = fs.readdirSync(WINDOWS_APPS_DIR);
    for (const entry of entries) {
      if (!entry.startsWith("TradingView.Desktop_")) continue;
      const exe = path.join(WINDOWS_APPS_DIR, entry, "TradingView.exe");
      if (fs.existsSync(exe)) return exe;
    }
  } catch (e) {
    // 目录不可读时返回 null
  }
  return null;
}

/**
 * 通过 AppxPackage 查询 TradingView 安装位置（新增兜底路径）
 *
 * WindowsApps 目录受系统保护、普通权限无法枚举（readdirSync 会抛 PermissionDenied），
 * 此时用系统官方接口 Get-AppxPackage 也能拿到 InstallLocation，从而定位 TradingView.exe。
 * 仅在原 findTradingViewExe() 找不到时才调用。
 */
function findTradingViewExeByAppx() {
  return new Promise((resolve) => {
    execFile(
      "powershell.exe",
      [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-AppxPackage -Name TradingView.Desktop | Select-Object -ExpandProperty InstallLocation",
      ],
      { timeout: 15000, windowsHide: true },
      (err, stdout) => {
        if (err) return resolve(null);
        const loc = String(stdout).trim();
        if (!loc) return resolve(null);
        const exe = path.join(loc, "TradingView.exe");
        if (fs.existsSync(exe)) return resolve(exe);
        resolve(null);
      }
    );
  });
}

/** 检测 TradingView 进程是否已在运行 */
function hasTradingViewProcess() {
  return new Promise((resolve) => {
    execFile("tasklist", ["/FI", "IMAGENAME eq TradingView.exe"], { timeout: 10000, windowsHide: true }, (err, stdout) => {
      if (err) return resolve(true);
      resolve(/TradingView\.exe/i.test(stdout));
    });
  });
}

/** 强制结束所有 TradingView 进程（taskkill /F /IM） */
function killTradingView() {
  return new Promise((resolve) => {
    execFile("taskkill", ["/F", "/IM", "TradingView.exe"], { timeout: 15000, windowsHide: true }, () => resolve());
  });
}

/** 等待 TradingView 进程完全退出（最多 5 秒） */
async function waitProcessExit() {
  for (let i = 0; i < 5; i++) {
    if (!(await hasTradingViewProcess())) return true;
    await sleep(1000);
  }
  return !(await hasTradingViewProcess());
}

/**
 * 通过 PowerShell Start-Process 带调试参数启动（兜底方式）
 *
 * 实测发现：MSIX 打包的 TradingView 桌面端直接用 node spawn 启动时，
 * --remote-debugging-port 参数会被应用忽略（进程起来但端口未监听）；
 * 而用 PowerShell Start-Process 传参可以正常生效，因此作为兜底重试。
 */
function launchWithPowerShell(exe) {
  const args = [
    "-NoProfile", "-NonInteractive", "-Command",
    `Start-Process -FilePath '${exe.replace(/'/g, "''")}' -ArgumentList '--remote-debugging-port=${PORT}'`,
  ];
  return new Promise((resolve) => {
    execFile("powershell.exe", args, { timeout: 15000, windowsHide: true }, (err) => resolve(!err));
  });
}

/**
 * 通过 Invoke-CommandInDesktopPackage 在包内上下文启动（最终兜底方式）
 *
 * 实测：部分机器上 MSIX 打包的 TradingView 不允许脱离包身份直接跑 exe，
 * spawn 与 PowerShell Start-Process 都会"启动即退"（进程秒退、端口不监听）；
 * 而用包内命令执行方式携带调试参数可以正常启动并开放 CDP 端口。
 */
function launchInPackageContext() {
  const ps = [
    "$pkg = Get-AppxPackage -Name TradingView.Desktop",
    "if (-not $pkg) { exit 1 }",
    "$appId = (Get-AppxPackageManifest $pkg).Package.Applications.Application.Id | Select-Object -First 1",
    "Invoke-CommandInDesktopPackage -PackageFamilyName $pkg.PackageFamilyName -AppId $appId -Command (Join-Path $pkg.InstallLocation 'TradingView.exe') -Args '--remote-debugging-port=" + PORT + "'",
  ].join("; ");
  return new Promise((resolve) => {
    execFile("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", ps], { timeout: 30000, windowsHide: true }, (err) =>
      resolve(!err)
    );
  });
}

// Web and CLI share the same explicit restart policy.
async function probe() {
  if (!(await isPortListening(PORT))) return {state:'offline'};
  try {
    const get = async route => {
      const r = await fetch(`http://127.0.0.1:${PORT}${route}`, {signal:AbortSignal.timeout(3000)});
      if (!r.ok) throw Error('invalid CDP response');
      return r.json();
    };
    const version = await get('/json/version');
    const pages = await get('/json');
    const tv = p => { try { const h=new URL(p.url).hostname; return h==='tradingview.com'||h.endsWith('.tradingview.com'); } catch { return false; } };
    if (!version.webSocketDebuggerUrl || !Array.isArray(pages) || !pages.some(tv))
      return {state:'error',message:'9222端口已占用，但未识别到TradingView调试页面'};
    return {state:'ready',message:'TD已连接',hasCharts:pages.some(p=>tv(p)&&p.type==='page'&&new URL(p.url).pathname.startsWith('/chart/'))};
  } catch { return {state:'error',message:'9222端口已占用，但不是可用的TradingView调试服务'}; }
}
async function launch(allowRestart=false, deps={}) {
  const d={probe,hasTradingViewProcess,find:async()=>findTradingViewExe()||await findTradingViewExeByAppx(),
    killTradingView,waitProcessExit,launchWithPowerShell,launchInPackageContext,sleep,...deps};
  if (process.platform!=='win32' && !deps.probe) return {state:'error',message:'启动TD仅支持Windows'};
  const initial=await d.probe();
  if(initial.state!=='offline') return initial;
  const running=await d.hasTradingViewProcess();
  if(running&&!allowRestart) return {state:'needs_confirmation',message:'需要重启 TD，当前窗口会关闭，请先保存布局'};
  const exe=await d.find();
  if(!exe) return {state:'error',message:'未找到TradingView.exe，请确认已安装TradingView Desktop'};
  async function clear() {
    if(!(await d.hasTradingViewProcess())) return true;
    if(!allowRestart) return false;
    await d.killTradingView();
    if(!(await d.waitProcessExit())) throw Error('TD未能退出，请手动关闭后重试');
    return true;
  }
  if(running) await clear();
  for(const start of [()=>d.launchWithPowerShell(exe),()=>d.launchInPackageContext()]) {
    if(!(await clear())) return {state:'needs_confirmation',message:'需要重启 TD，当前窗口会关闭，请先保存布局'};
    await start();
    for(let i=0;i<30;i++) {
      await d.sleep(1000);
      const result=await d.probe();
      if(result.state==='ready') return result;
    }
  }
  const last=await d.probe();
  return last.state==='error'?last:{state:'error',message:'等待TD调试端口9222超时，请检查TD是否成功启动后重试'};
}
module.exports={launch,probe};
if(require.main===module) {
  launch(process.argv.includes('--allow-restart')).catch(e=>({state:'error',message:e.message})).then(result=>{
    console.log(process.argv.includes('--json')?JSON.stringify(result):result.message);
    process.exitCode=result.state==='ready'?0:result.state==='needs_confirmation'?2:1;
  });
}
