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

  // 3. 启动（detached 防止子进程阻塞）
  console.log(`启动 TradingView: ${exe}`);
  spawn(exe, [`--remote-debugging-port=${PORT}`], { detached: true, stdio: "ignore" }).unref();
  console.log(`已发出启动命令（调试端口 ${PORT}），等待加载...`);

  // 4. 等待端口就绪（最多 30 秒）
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    if (await isPortListening(PORT)) {
      console.log(`SUCCESS: TradingView 启动成功，CDP 端口 ${PORT} 已就绪`);
      process.exit(0);
    }
  }
  console.log(`ERROR: 等待端口 ${PORT} 超时，TradingView 可能启动失败`);
  process.exit(1);
})();
