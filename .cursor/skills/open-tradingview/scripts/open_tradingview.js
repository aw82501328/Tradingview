/**
 * 打开 TradingView Desktop（带 CDP 调试端口 9222）
 *
 * 功能：
 *   1. 检查端口 9222 是否已监听（已运行则直接返回）
 *   2. 在 WindowsApps 目录查找 TradingView.exe（MSIX 安装，版本号通配）
 *   3. 以 --remote-debugging-port=9222 启动
 *   4. 轮询等待调试端口就绪（最多 30 秒）
 *
 * 用法：node open_tradingview.js
 */
const { spawn, execFile } = require("child_process");
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
      { timeout: 15000 },
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
    execFile("tasklist", ["/FI", "IMAGENAME eq TradingView.exe"], { timeout: 10000 }, (err, stdout) => {
      if (err) return resolve(false);
      resolve(/TradingView\.exe/i.test(stdout));
    });
  });
}

/** 强制结束所有 TradingView 进程（taskkill /F /IM） */
function killTradingView() {
  return new Promise((resolve) => {
    execFile("taskkill", ["/F", "/IM", "TradingView.exe"], { timeout: 15000 }, () => resolve());
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
    `Start-Process -FilePath '${exe}' -ArgumentList '--remote-debugging-port=${PORT}'`,
  ];
  return new Promise((resolve) => {
    execFile("powershell.exe", args, { timeout: 15000 }, (err) => resolve(!err));
  });
}

/** 轮询等待调试端口就绪（最多 seconds 秒），就绪返回 true */
async function waitPortReady(seconds) {
  for (let i = 0; i < seconds; i++) {
    await sleep(1000);
    if (await isPortListening(PORT)) return true;
  }
  return false;
}

(async () => {
  // 1. 已运行则直接返回
  if (await isPortListening(PORT)) {
    console.log(`TradingView 已在运行（端口 ${PORT} 已监听），无需重新启动`);
    process.exit(0);
  }

  // 2. 查找可执行文件（先按原逻辑枚举 WindowsApps，找不到时再走新增的 AppxPackage 查询兜底）
  const exe = findTradingViewExe() || (await findTradingViewExeByAppx());
  if (!exe) {
    console.log("ERROR: 未找到 TradingView.exe，请确认已安装 TradingView Desktop");
    process.exit(1);
  }

  // 3. 首次尝试：直接 spawn 启动（detached 防止子进程阻塞），等待 15 秒
  console.log(`启动 TradingView: ${exe}`);
  spawn(exe, [`--remote-debugging-port=${PORT}`], { detached: true, stdio: "ignore" }).unref();
  console.log(`已发出启动命令（调试端口 ${PORT}），等待加载...`);
  if (await waitPortReady(15)) {
    console.log(`SUCCESS: TradingView 启动成功，CDP 端口 ${PORT} 已就绪`);
    process.exit(0);
  }

  // 4. spawn 后端口未就绪：检测到进程已起来说明调试参数被忽略，
  //    先清理该实例，再用 PowerShell Start-Process 兜底重试一次
  if (await hasTradingViewProcess()) {
    console.log(`调试端口 ${PORT} 未就绪且进程已启动（参数可能被忽略），清理后用 PowerShell 重试...`);
    await killTradingView();
    if (!(await waitProcessExit())) {
      console.log("WARN: 清理 TradingView 进程超时，继续尝试 PowerShell 启动");
    }
  }
  const sent = await launchWithPowerShell(exe);
  console.log(sent
    ? `已用 PowerShell 发出启动命令（调试端口 ${PORT}），等待加载...`
    : "PowerShell 启动命令发送失败，继续等待端口超时检查");
  if (await waitPortReady(20)) {
    console.log(`SUCCESS: TradingView 启动成功，CDP 端口 ${PORT} 已就绪`);
    process.exit(0);
  }

  // 5. 仍超时则报错退出
  console.log(`ERROR: 等待端口 ${PORT} 超时，TradingView 可能启动失败`);
  process.exit(1);
})();
