import { fetchJSON } from "../api/client";
import { openAppConfirm } from "../shell/confirm";
import { switchSection } from "../shell/nav";
import { showToast } from "../shell/toast";
import { checkForUpdates } from "./index";
import "./local-update.css";

interface UpdateEvent { phase: string; message: string; at: number }
interface Update { id?: string; phase: string; message: string; current?: string; target?: string; demo?: boolean; finished?: boolean; data_status?: string; backup?: string; events?: UpdateEvent[] }
interface Capabilities { supported: boolean; mode: "server" | "desktop"; label: string; current: string; reason: string }
interface Snapshot extends Capabilities { demo_enabled: boolean; update: Update }
interface ReleaseSource { tag?: string; version?: string; detail?: string }
interface Plan extends Capabilities {
  plan_id: string; available: boolean; latest: string; notes: string; busy: boolean;
  sources?: { fork: ReleaseSource; upstream: ReleaseSource }; upstream_pending?: boolean;
}

const phases = [["download", "下载更新包"], ["checksum", "校验更新包"], ["prepare", "检查运行环境"], ["waiting", "等待任务结束"], ["backup", "备份数据和旧版本"], ["switch", "切换到新版本"], ["restart", "重启服务"], ["verify", "验证运行状态"]];
const terminal = new Set(["success", "error", "rolled_back", "cancelled"]);
const el = (id: string) => document.getElementById(id)!;
const btn = (id: string) => el(id) as HTMLButtonElement;
function post<T>(path: string, body?: unknown, token = "") {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (token) headers.set("X-Binggo-Update-Admin", token);
  return fetchJSON<T>(`/api/updates/${path}`, { method: "POST", headers, body: JSON.stringify(body ?? {}) });
}

export function bindLocalUpdates() {
  if (!document.getElementById("apply-local-update")) return;
  let reading = false, acting = false, snapshot: Snapshot | undefined;
  let logSignature = "";
  let seenFinish = "";

  function openLogs() { switchSection("logs"); el("local-update-history").scrollIntoView({ behavior: "smooth", block: "start" }); }
  function render(data: Snapshot) {
    snapshot = data;
    const update = data.update;
    const active = Boolean(update.id && !terminal.has(update.phase));
    btn("apply-local-update").disabled = acting || active;
    if (!acting) btn("apply-local-update").textContent = data.label;
    el("local-update-support").textContent = data.supported
      ? `更新对象：${data.mode === "server" ? "当前服务器" : "此电脑"} · 安装前备份数据与配置` : data.reason;
    el("local-update-demo").hidden = !data.demo_enabled;
    btn("preview-update").disabled = active || acting;
    btn("preview-update-failure").disabled = active || acting;
    el("local-update-progress").hidden = !update.id;
    if (!update.id) return;
    el("local-update-title").textContent = `${update.demo ? "流程演示" : data.mode === "server" ? "服务器版本更新" : "本机版本更新"} · ${update.current} → ${update.target}`;
    el("local-update-message").textContent = update.message;
    el("local-update-data").textContent = update.data_status ? `数据状态：${update.data_status}` : "结果会保存到本机更新日志";
    btn("cancel-local-update").hidden = update.phase !== "waiting" || Boolean(update.demo);
    btn("reload-after-update").hidden = !terminal.has(update.phase);
    const list = el("local-update-steps");
    list.replaceChildren();
    const index = phases.findIndex(([key]) => key === update.phase);
    const visited = new Set((update.events || []).map(event => event.phase));
    phases.forEach(([key, label], i) => {
      if (key === "waiting" && update.demo) return;
      const item = document.createElement("li");
      const done = update.phase === "success" || (index > i) || (terminal.has(update.phase) && visited.has(key) && key !== update.events?.at(-2)?.phase);
      item.dataset.state = done ? "done" : key === update.phase ? "active" : "pending";
      item.textContent = `${done ? "✓" : key === update.phase ? "●" : "○"} ${label}`;
      list.append(item);
    });
    if (update.finished && seenFinish !== update.id) {
      seenFinish = update.id;
      showToast(update.message, update.phase === "success" ? "success" : "info", "已记录到本机更新日志，可点击下方“查看本机日志”。");
    }
  }

  async function logs() {
    const data = await fetchJSON<{ entries: Update[] }>("/api/updates/history");
    const signature = JSON.stringify(data.entries);
    if (signature === logSignature) return;
    logSignature = signature;
    const list = el("local-update-log-list");
    list.replaceChildren();
    el("local-update-log-empty").hidden = data.entries.length > 0;
    for (const update of data.entries) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      const at = update.events?.at(-1)?.at;
      summary.textContent = `${at ? new Date(at * 1000).toLocaleString() + " · " : ""}${update.demo ? "[演示] " : ""}${update.message}`;
      details.append(summary);
      const text = document.createElement("pre");
      text.textContent = [`${update.current} → ${update.target}`, ...(update.events || []).map(event => `${new Date(event.at * 1000).toLocaleTimeString()}  ${event.message}`), update.data_status ? `数据状态：${update.data_status}` : "", update.backup ? `备份位置：${update.backup}` : ""].filter(Boolean).join("\n");
      details.append(text);
      list.append(details);
    }
  }

  async function refresh() {
    if (reading) return;
    reading = true;
    try {
      render(await fetchJSON<Snapshot>("/api/updates/status", { timeoutMs: 4000 }));
      await logs();
    } catch {
      if (snapshot?.update.id && !snapshot.update.finished) el("local-update-message").textContent = "暂时无法连接本机程序，正在等待恢复；尚未确认更新结果。";
    } finally { reading = false; }
  }

  async function action(work: () => Promise<void>) {
    if (acting) return;
    acting = true;
    btn("apply-local-update").disabled = true;
    try { await work(); } catch (error) { showToast(error instanceof Error ? error.message : String(error), "error"); }
    finally { acting = false; await refresh(); }
  }

  function renderSources(plan: Plan): string {
    if (!plan.sources) return plan.available ? `发现新版本 ${plan.latest}` : `当前 ${plan.current} 已是最新正式版`;
    const { fork, upstream } = plan.sources;
    el("server-update-sources").hidden = false;
    const describe = (parts: Array<string | undefined>) => parts.filter(Boolean).join(" · ");
    el("server-update-fork").textContent = describe([fork.tag, fork.detail]);
    el("server-update-upstream").textContent = describe([upstream.version, upstream.detail]);
    const messages = [];
    if (plan.available) messages.push(`可安装合并版 ${plan.latest}`);
    if (plan.upstream_pending) messages.push("上游有更新，待合并并发布本站部署包");
    if (!messages.length) {
      messages.push(fork.detail || upstream.detail ? "暂无已确认可安装的新版本" : "已安装当前合并版");
    }
    const message = messages.join("。");
    el("server-update-alignment").textContent = message;
    return message;
  }

  async function check() {
    const plan = await post<Plan>("prepare");
    return { plan, message: renderSources(plan) };
  }

  btn("check-updates").addEventListener("click", () => {
    void action(async () => {
      const button = btn("check-updates");
      button.disabled = true;
      try {
        if (!snapshot) render(await fetchJSON<Snapshot>("/api/updates/status", { timeoutMs: 4000 }));
        if (snapshot?.mode !== "server") {
          await checkForUpdates(button);
          return;
        }
        const { message } = await check();
        showToast(message, "info");
      } finally {
        button.disabled = false;
      }
    });
  });

  btn("apply-local-update").addEventListener("click", () => void action(async () => {
    btn("apply-local-update").textContent = "检查中…";
    try {
      const { plan, message } = await check();
      if (!plan.available) { showToast(message, "info"); return; }
      if (!plan.supported) { showToast(plan.reason, "info"); return; }
      const server = plan.mode === "server";
      let token = "";
      const confirmed = await openAppConfirm({ eyebrow: server ? "仅更新当前服务器" : "仅更新此电脑", title: `准备更新：${plan.current} → ${plan.latest}`,
        desc: plan.notes || "新版本的变更说明请查看发布页。",
        bullets: [...(plan.upstream_pending ? ["上游还有更新，等待发布后续合并版"] : []), plan.busy ? "当前有任务运行，将等待任务结束后更新" : "安装期间会短暂重启服务", "更新前备份数据、配置和旧程序", "新版启动失败时尝试恢复旧版本和升级前数据", "结果仅写入本机工作日志"],
        secretLabel: server ? "服务器更新口令（由 SSH 管理员提供）" : "",
        onSecret: (value: string) => { token = value; },
        confirmLabel: plan.busy ? "等待任务完成后更新" : "备份并更新", cancelLabel: "取消" });
      if (confirmed) render(await post<Snapshot>("apply", { plan_id: plan.plan_id }, token));
    } finally { btn("apply-local-update").textContent = snapshot?.label || "更新版本"; }
  }));
  for (const [id, failure] of [["preview-update", false], ["preview-update-failure", true]] as const) {
    btn(id).addEventListener("click", () => void action(async () => {
      if (await openAppConfirm({ eyebrow: "本机流程演示", title: failure ? "演示更新失败后的恢复流程" : "演示一键更新流程", desc: "这次只演示界面和本机日志，不下载、停止、替换任何程序，也不会修改真实版本号。", confirmLabel: "开始演示" })) {
        render(await post<Snapshot>(`demo?failure=${failure}`));
      }
    }));
  }
  btn("cancel-local-update").addEventListener("click", () => void action(async () => {
    let token = "";
    if (snapshot?.mode === "server" && !await openAppConfirm({
      title: "取消本次更新？", desc: "当前任务会继续运行。",
      secretLabel: "服务器更新口令", onSecret: (value: string) => { token = value; }, confirmLabel: "取消更新",
    })) return;
    await post("cancel", undefined, token);
  }));
  btn("reload-after-update").addEventListener("click", () => location.reload());
  btn("open-update-log").addEventListener("click", openLogs);
  window.setInterval(() => void refresh(), 1500);
  void refresh();
}
