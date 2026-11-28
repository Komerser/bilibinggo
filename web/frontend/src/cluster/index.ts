
import { fetchJSON } from "../api/client";
import { showToast } from "../shell/toast";
import { loadActivities, loadSummary } from "../activities";
import { mountDetectedActivities } from "./detections";
import "./styles.css";

type Counts = Record<string, number>;
type Conversion = { checked: number; matched: number };
type Node = {
  id: string; name: string; online: boolean; host: boolean; work: number; kind?: string;
  key?: string; seen?: number; completed: number; model_calls: number; task_label?: string;
  history_count?: number | null; history_complete?: number | null; search_pages?: number;
  videos_checked?: number; videos_total?: number; videos_counted?: number; videos_matched?: number;
  watch_batch?: boolean;
  local_work?: "idle" | "watch" | "participating" | "busy" | null;
  participation_pending?: number | null; participation_enabled?: boolean | null;
};
type WatchUser = { mid: string; name: string; sources: string[]; assigned: string | null;
  state: string | null; checked: number | null; hits: number | null; failed_refs?: number };
type WatchPage = { items: WatchUser[]; page: number; pages: number; total: number };
type QueuedPage = { page: number; state: "pending" | "leased"; node: string | null };
type SearchTask = {
  keyword: string; order: "pubdate" | "totalrank"; queued: QueuedPage[];
};
type VideoTask = { key: string; state: "pending" | "leased"; node: string | null };
type Assignment = { watch_nodes: string[] | null; collection_nodes: string[] | null };
type RoundImport = { round: number; node: string | null; pending: number; done: number; skipped: number; participating?: boolean };
type ClusterEvent = {
  id: number;
  at: number;
  level: "info" | "warning" | "error";
  node: string | null;
  node_name: string | null;
  message: string;
};
type Status = {
  role: "host" | "member" | null;
  address?: string;
  node?: string;
  node_name?: string;
  connected: boolean;
  worker_ready: boolean;
  last_sync: number;
  local_cursor: number;
  round_participation_enabled?: boolean;
  error: string;
  status: {
    round?: number; target?: number; counts?: Record<string, Counts>; nodes?: Node[]; events?: ClusterEvent[];
    local_work?: Record<"pages" | "found" | "videos" | "activities", number>;
    local_events?: ClusterEvent[];
    phase?: string; paused?: boolean; now?: number; next_round?: number; cooldown_until?: number;
    search_pages_total?: number; search_history_start?: number; concurrency_limit?: number; two_stage?: boolean;
    conversion?: Conversion; local_conversion?: Conversion; detected_open?: number; sequence?: number;
    known_videos?: number; history_syncing?: number; round_import?: RoundImport | null;
    search_tasks?: SearchTask[]; video_queue?: VideoTask[];
    round_keyword?: string | null; upcoming_keywords?: string[];
    watch_tracking?: boolean;
    watch_batch_supported?: boolean;
    watch_batch?: { counts: Counts; collection_pending?: number; strict?: boolean; references?: Counts } | null;
    watch_repeating?: boolean; watch_next?: number;
    work_assignments?: Assignment;
    collection_paused?: boolean;
    node_rename_supported?: boolean;
    round_participation_supported?: boolean;
  };
};

const byId = <T extends HTMLElement>(name: string) => document.getElementById("cluster-" + name) as T;
const set = (name: string, value: string | number) => {
  const element = byId(name);
  const label = String(value);
  if (element.textContent !== label) element.textContent = label;
};
const number = new Intl.NumberFormat("zh-CN");
const percent = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 });
const count = (value?: number) => value === undefined ? "—" : number.format(value);
const time = (stamp: number) => new Date(stamp * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
const kinds: Record<string, string> = { search: "检索页", video: "视频检测", watch: "追踪检查", publisher: "活动发布UP主检查", dynamic: "动态核验", short: "短链展开", article: "专栏检查", author: "发布者检查" };
const phases: Record<string, string> = {
  collecting: "协作检索中", detecting: "分配检测中", running: "协作检索中",
  importing: "轮间导入活动",
  complete: "本轮完成", incomplete: "本轮有任务未完成", exhausted: "本轮完成，收录未达目标",
};

let current: Status | undefined;
let busy = false;
let refreshing = false;
let updateDetections: ReturnType<typeof mountDetectedActivities>;
let searchSignature = "";
let nodeSignature = "";
let watchPage = 1;
let watchPending = false;
let watchRows = "";
let watchTotal: number | undefined;
let dispatchSignature = "";
let dispatchRows = "";
let dispatchDirty = false;
const assignmentFields = ["watch_nodes", "collection_nodes"] as const;
const eventPanels: Array<(snapshot: Status) => void> = [];
const eventTime = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

function eventRow(event: ClusterEvent, showNode = true) {
  const item = document.createElement("li");
  item.dataset.level = event.level;
  const date = document.createElement("time");
  date.dateTime = new Date(event.at * 1000).toISOString();
  date.textContent = eventTime.format(event.at * 1000);
  item.append(date);
  if (showNode) {
    const node = document.createElement("span");
    node.className = "cluster-event-node";
    node.textContent = event.node_name || "集群";
    item.append(node);
  }
  const message = document.createElement("span");
  message.textContent = event.level === "error" ? "失败 · " + event.message
    : event.level === "warning" ? "异常 · " + event.message : event.message;
  item.append(message);
  return item;
}

let lastDiscoveryEvents = "";
function renderConversion(prefix: string, conversion: Conversion | undefined, completed: number) {
  const id = prefix ? prefix + "-" : "";
  byId(id + "yield").hidden = conversion === undefined;
  if (!conversion) return;
  const { matched, checked } = conversion;
  set(id + "conversion", checked ? percent.format(matched / checked) : "—");
  const missing = Math.max(0, completed - checked);
  set(id + "conversion-detail", "命中 " + count(matched) + " / 已统计 " + count(checked) + " 个视频" +
    (missing ? "，另有 " + count(missing) + " 个早期结果缺少明细" : ""));
}

function renderDiscoveryWork(snapshot: Status) {
  const panel = byId("discovery-work");
  if (!panel) return;
  panel.hidden = !snapshot.role;
  if (!snapshot.role) return;
  const status = snapshot.status;
  const node = status.nodes?.find((record) => record.id === snapshot.node);
  set("discovery-note", [node?.name, status.round ? "第 " + status.round + " 轮" : ""].filter(Boolean).join(" · "));
  let task: string;
  if (!snapshot.connected) {
    task = snapshot.error || "等待同步本机集群状态";
  } else if (node?.kind) {
    task = node.task_label || kinds[node.kind] + " · " + (node.key || "");
  } else if (status.paused) {
    task = "集群已暂停领取新任务";
  } else if ((status.cooldown_until || 0) > (status.now || 0)) {
    task = "平台限流，暂停至 " + time(status.cooldown_until!);
  } else if (status.history_syncing) {
    task = "正在合并节点历史 BV";
  } else if (status.round_import) {
    task = status.round_import.node === snapshot.node
      ? (status.round_import.participating ? "本机轮间顺序参与" : "本机正在导入集群活动") : "等待轮间同步";
  } else if (status.collection_paused) {
    task = "主动搜集已暂停";
  } else if (status.next_round) {
    task = "本轮已结束，下一轮 " + time(status.next_round);
  } else {
    task = snapshot.worker_ready ? "本机等待分配任务" : "本机仅同步活动";
  }
  const currentTask = byId("discovery-current");
  if (currentTask.textContent !== task) currentTask.textContent = task;
  for (const metric of ["pages", "found", "videos", "activities"] as const) {
    set("discovery-" + metric, status.local_work?.[metric] ?? "—");
  }
  renderConversion("local", status.local_conversion, status.local_work?.videos || 0);

  const events = status.local_events ?? (status.events || []).filter((event) => event.node === snapshot.node).slice(0, 6);
  const list = byId("discovery-events");
  list.hidden = events.length === 0;
  const signature = JSON.stringify(events);
  if (signature !== lastDiscoveryEvents) {
    list.replaceChildren(...events.map((event) => eventRow(event, false)));
    lastDiscoveryEvents = signature;
  }
}

function mountEventPanels() {
  const template = document.getElementById("cluster-log-template") as HTMLTemplateElement | null;
  if (!template) return;
  for (const mount of document.querySelectorAll<HTMLElement>("[data-cluster-log]")) {
    const panel = template.content.firstElementChild!.cloneNode(true) as HTMLElement;
    const scope = panel.querySelector<HTMLSelectElement>("[data-event-scope]")!;
    const errors = panel.querySelector<HTMLInputElement>("[data-event-errors]")!;
    const meta = panel.querySelector<HTMLElement>("[data-event-meta]")!;
    const empty = panel.querySelector<HTMLElement>("[data-event-empty]")!;
    const list = panel.querySelector<HTMLOListElement>("[data-event-list]")!;
    scope.value = mount.dataset.clusterLog || "all";
    let rendered = "";
    const update = (snapshot: Status) => {
      panel.hidden = !snapshot.role;
      if (!snapshot.role) return;
      const events = snapshot.status.events;
      meta.textContent = snapshot.error || (!snapshot.connected ? "等待与主节点同步" : "最近 80 条事件");
      const visible = (events || []).filter((event) =>
        (scope.value === "all" || !event.node || event.node === snapshot.node) &&
        (!errors.checked || event.level !== "info"));
      empty.hidden = visible.length !== 0;
      empty.textContent = !events && snapshot.connected ? "主节点版本尚未提供日志"
        : errors.checked ? "暂无异常事件" : "暂无集群事件";
      list.hidden = !empty.hidden;
      const signature = JSON.stringify(visible);
      if (signature === rendered) return;
      rendered = signature;
      list.replaceChildren(...visible.map((event) => eventRow(event)));
    };
    for (const control of [scope, errors]) {
      control.addEventListener("change", () => { if (current) update(current); });
    }
    mount.append(panel);
    eventPanels.push(update);
  }
}

function render(snapshot: Status) {
  const priorRole = current?.role;
  const priorNode = current?.node;
  current = snapshot;
  if (priorNode !== snapshot.node) {
    watchPage = 1;
    watchRows = "";
    watchTotal = undefined;
    byId<HTMLDetailsElement>("rename").open = false;
    byId("watch-list").replaceChildren();
  }
  updateDetections({ node: snapshot.node, total: snapshot.status.detected_open, sequence: snapshot.status.sequence });
  renderDiscoveryWork(snapshot);
  eventPanels.forEach((update) => update(snapshot));
  const status = snapshot.status;
  const joined = Boolean(snapshot.role);
  if (priorRole !== snapshot.role) {
    byId<HTMLDetailsElement>("manage").open = false;
    hideInvite();
  }
  const nav = byId("nav-indicator");
  nav.hidden = !joined;
  nav.classList.toggle("cluster-dot-offline", !snapshot.connected);
  byId("setup").hidden = joined;
  byId("monitor").hidden = !joined;
  if (!joined) return;

  byId("host-actions").hidden = snapshot.role !== "host";
  const pause = byId<HTMLButtonElement>("collection-pause");
  pause.hidden = snapshot.role !== "host";
  const paused = status.collection_paused ?? Boolean(status.paused);
  pause.textContent = paused ? "恢复主动搜集" : "暂停主动搜集";
  pause.setAttribute("aria-pressed", String(paused));
  byId("rename").hidden = !status.node_rename_supported;
  set("leave", snapshot.role === "host" ? "解散集群" : "退出集群");
  const local = status.nodes?.find(node => node.id === snapshot.node);
  set("local-name", snapshot.node_name || local?.name || "本机");
  set("role", snapshot.role === "host" ? "主节点" : "工作节点");
  const localTask = local?.task_label || (local?.local_work === "participating" ? "参与活动中" : "");
  set("local-task", localTask);
  byId("local-task").hidden = !localTask;
  set("address", snapshot.address || "");
  set("link-state", snapshot.error || (snapshot.connected ? "同步正常" : "等待同步"));
  byId("link-state").dataset.connected = String(snapshot.connected && !snapshot.error);
  set("sync-time", snapshot.last_sync ? "上次同步 " + time(snapshot.last_sync) : "");
  byId("worker-note").hidden = snapshot.worker_ready;
  const counts = status.counts || {};
  const video = counts.video || {};
  const found = (video.pending || 0) + (video.leased || 0) + (video.done || 0) + (video.failed || 0);
  const target = status.target;
  set("progress-value", target ? count(found) + " / " + count(target) : "—");
  const progress = byId<HTMLProgressElement>("progress");
  progress.max = target || 1;
  progress.value = found;
  set("video-pending", count(status.counts ? video.pending || 0 : undefined));
  set("video-running", count(status.counts ? video.leased || 0 : undefined));
  set("video-done", count(status.counts ? video.done || 0 : undefined));
  renderConversion("", status.conversion, video.done || 0);
  set("detected-open", count(status.detected_open));
  byId("video-failed").hidden = !video.failed;
  set("video-failed", "本轮视频检测失败 " + count(video.failed) + " 项");
  set("search-round", count(status.counts ? counts.search?.done || 0 : undefined));
  set("search-total", count(status.search_pages_total));
  set("known-videos", count(status.known_videos));
  set("history-note", status.history_syncing ? count(status.history_syncing) + " 个节点正在上传历史"
    : status.known_videos !== undefined ? "含已同步的历史 BV 与集群检测结果" : "等待主节点提供历史统计");
  set("search-history", status.search_pages_total === undefined
    ? (status.counts ? "主节点版本尚未提供" : "等待同步")
    : "自第 " + (status.search_history_start || 1) + " 轮起的成功检索页");
  const cooldown = status.cooldown_until || 0;
  const next = status.next_round || 0;
  const phase = status.paused ? "协作已暂停"
    : status.collection_paused ? "主动搜集已暂停"
    : cooldown > (status.now || 0) ? "平台限流，暂停至 " + time(cooldown)
    : status.history_syncing ? "合并历史 BV"
    : (phases[status.phase || ""] || "等待节点");
  set("phase", [status.round ? "第 " + status.round + " 轮" : "", phase,
    next && !status.paused ? "下一轮 " + time(next) : ""].filter(Boolean).join(" · "));

  renderSearchTasks(snapshot, found);
  renderNodes(snapshot, found);
  renderDispatch(snapshot);
  renderWatchControls(snapshot);
  if (byId("root").closest(".view-section")?.classList.contains("active")) void refreshWatch();
}

function renderWatchControls(snapshot: Status) {
  byId("watch").hidden = !snapshot.status.watch_tracking;
  byId("round-actions").hidden = !snapshot.status.round_participation_supported;
  set("round-opt-in", snapshot.round_participation_enabled ? "关闭本机轮间参与" : "开启本机轮间参与");
  byId("round-opt-in").setAttribute("aria-pressed", String(Boolean(snapshot.round_participation_enabled)));
}

function assignmentNodes(snapshot: Status) {
  return [...(snapshot.status.nodes || [])].sort((a, b) => Number(b.host) - Number(a.host) ||
    Number(b.online) - Number(a.online) || a.name.localeCompare(b.name, "zh-CN") || a.id.localeCompare(b.id));
}

function readAssignments() {
  const fields = new FormData(byId<HTMLFormElement>("dispatch-form"));
  return { watch_nodes: fields.getAll("watch_nodes").map(String),
    collection_nodes: fields.getAll("collection_nodes").map(String) };
}

function updateDispatchControls(snapshot: Status) {
  if (!snapshot.status.work_assignments || snapshot.role !== "host") return;
  const plan = snapshot.status.work_assignments;
  const chosen = readAssignments();
  const nodes = assignmentNodes(snapshot);
  dispatchDirty = assignmentFields.some(key => nodes.some(({ id }) =>
    (plan[key] === null || plan[key].includes(id)) !== chosen[key].includes(id)));
  const batch = snapshot.status.watch_batch;
  const tracking = Boolean((batch?.counts.pending || 0) + (batch?.counts.leased || 0));
  const repeating = snapshot.status.watch_repeating;
  set("watch-start", repeating ? "停止追踪轮转" : tracking ? "追踪进行中" : "开始追踪");
  byId<HTMLButtonElement>("dispatch-save").disabled = busy || !dispatchDirty;
  byId<HTMLButtonElement>("watch-start").disabled = busy || (!repeating &&
    (tracking || dispatchDirty || chosen.watch_nodes.length === 0 || watchTotal === 0));
  const waitingForCollection = batch?.collection_pending && !nodes.some(node => node.online && node.work === 2 &&
    node.watch_batch && (!batch.strict || chosen.watch_nodes.includes(node.id)));
  const references = batch?.references || {};
  const referenceTotal = Object.values(references).reduce((sum, value) => sum + value, 0);
  const referenceProgress = referenceTotal
    ? `合集线索已核验 ${count(references.done || 0)} / ${count(referenceTotal)} 条${references.leased ? `，${count(references.leased)} 条核验中` : ""}${references.failed ? `，${count(references.failed)} 条失败` : ""}`
    : "";
  const nextWatch = snapshot.status.watch_next;
  const note = dispatchDirty ? "保存后应用新的节点分工，已领取的任务会继续完成。"
    : waitingForCollection ? "合集读取正在等待已勾选追踪的新版节点。"
    : repeating && nextWatch && !tracking ? "下一轮追踪 " + time(nextWatch) : referenceProgress;
  set("dispatch-note", note);
  byId("dispatch-note").hidden = !note;
}

function renderDispatch(snapshot: Status) {
  const plan = snapshot.status.work_assignments;
  byId("dispatch").hidden = snapshot.role !== "host" || !plan;
  if (snapshot.role !== "host" || !plan) return;
  const nodes = assignmentNodes(snapshot);
  const signature = JSON.stringify([snapshot.node, plan]);
  const rows = JSON.stringify(nodes.map(node => [node.id, node.name, node.online]));
  if (signature !== dispatchSignature || rows !== dispatchRows) {
    const draft = signature === dispatchSignature && dispatchDirty ? readAssignments() : plan;
    dispatchSignature = signature;
    dispatchRows = rows;
    byId("assignment-rows").replaceChildren(...nodes.map(node => {
      const row = document.createElement("tr");
      const name = document.createElement("th");
      name.scope = "row";
      name.textContent = node.name;
      const tag = document.createElement("small");
      tag.className = "caption";
      tag.textContent = node.id === snapshot.node ? "本机" : node.online ? "在线" : "离线";
      name.appendChild(tag);
      row.appendChild(name);
      for (const kind of assignmentFields) {
        const cell = document.createElement("td");
        const label = document.createElement("label");
        label.className = "cluster-assignment-cell";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.name = kind;
        checkbox.value = node.id;
        checkbox.checked = draft[kind] === null || draft[kind].includes(node.id);
        checkbox.setAttribute("aria-label", node.name + (kind === "watch_nodes" ? " 追踪 UP 主" : " 主动搜集"));
        label.appendChild(checkbox);
        cell.appendChild(label);
        row.appendChild(cell);
      }
      return row;
    }));
  }
  updateDispatchControls(snapshot);
}

async function refreshWatch(page = watchPage) {
  const snapshot = current;
  if (!snapshot?.node || !snapshot.status.watch_tracking || watchPending) return;
  watchPending = true;
  try {
    const result = await fetchJSON<WatchPage>(`/api/cluster/tracked-users?page=${page}`, {
      headers: { "X-Binggo-Dashboard": "1" }, timeoutMs: 10000,
    });
    if (current?.node !== snapshot.node) return;
    watchPage = result.page;
    watchTotal = result.total;
    set("watch-total", `${count(result.total)} 位`);
    const plan = snapshot.status.watch_batch;
    const batch = plan?.counts;
    set("watch-note", batch ? [
      `${count(batch.done || 0)} / ${count(Object.values(batch).reduce((sum, value) => sum + value, 0))} 位已检查`,
      batch.leased ? `${count(batch.leased)} 位追踪中` : "",
      batch.pending ? `${count(batch.pending)} 位待检查` : "",
      plan?.collection_pending ? `${count(plan.collection_pending)} 位待补查合集` : "",
      batch.failed ? `${count(batch.failed)} 位失败` : "",
    ].filter(Boolean).join(" · ") : result.total ? "合集与监控名单按 UID 合并" : "暂无追踪名单");
    updateDispatchControls(snapshot);
    byId("watch-pages").hidden = result.pages <= 1;
    set("watch-page", `${result.page} / ${result.pages} 页`);
    byId<HTMLButtonElement>("watch-prev").disabled = result.page <= 1;
    byId<HTMLButtonElement>("watch-next").disabled = result.page >= result.pages;
    const signature = JSON.stringify(result.items);
    if (watchRows === signature) return;
    watchRows = signature;
    const states: Record<string, string> = { pending: "待检查", leased: "追踪中", checking: "核验合集线索",
      failed: "检查失败", skipped: "已取消" };
    byId("watch-list").replaceChildren(...result.items.map(item => {
      const row = document.createElement("li");
      const content = document.createElement("div");
      const link = document.createElement("a");
      link.href = `https://space.bilibili.com/${item.mid}/dynamic`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = item.name;
      const sources = document.createElement("p");
      sources.className = "caption";
      sources.textContent = `来源 ${item.sources.join("、")}`;
      const detail = document.createElement("p");
      detail.className = "caption";
      const progress = item.state ? states[item.state] : undefined;
      const hits = `${count(item.hits ?? undefined)} 项开放活动`;
      if (item.state === "failed" && item.failed_refs) {
        detail.textContent = `${count(item.failed_refs)} 条线索核验失败 · ${hits}`;
      } else if (progress) {
        detail.textContent = [progress, item.assigned].filter(Boolean).join(" · ");
      } else if (item.checked) {
        const summary = [`${eventTime.format(item.checked * 1000)} 检查`];
        if (item.state === "done" && item.failed_refs === 0) summary.push("0 条线索核验失败");
        summary.push(hits);
        detail.textContent = summary.join(" · ");
      } else {
        detail.textContent = "尚未检查";
      }
      content.append(link, sources, detail);
      row.append(content);
      return row;
    }));
  } catch {
    if (current?.node === snapshot.node) set("watch-note", "暂时无法读取集群追踪列表");
  } finally {
    watchPending = false;
  }
}

function renderSearchTasks(snapshot: Status, found: number) {
  const { status } = snapshot;
  const keyword = status.round_keyword;
  const upcoming = status.upcoming_keywords || [];
  const pages = (status.search_tasks || []).filter(task => task.keyword === keyword)
    .flatMap(task => task.queued.map(page => ({ ...page, order: task.order })));
  const videos = status.video_queue || [];
  byId("search-plan").hidden = !status.round && !upcoming.length;
  set("search-keyword", keyword ? `本轮关键词 ${keyword}` : status.round ? "本轮沿用旧版混合检索" : "等待首轮检索");
  const goalReached = Boolean(status.target && found >= status.target);
  set("search-tasks-note", status.collection_paused && status.phase === "collecting" ? "恢复后继续检索"
    : !status.round ? (upcoming.length ? "每轮一个关键词" : "等待同步")
    : status.phase === "collecting" ? (goalReached ? "等待已领取的检索交回" : "")
    : goalReached ? "本轮已达到收录目标" : "本轮检索已结束");
  byId("search-upcoming").hidden = !upcoming.length;
  set("search-upcoming", `${status.round ? "下一轮起" : "首轮起"}依次检索  ${upcoming.join(" → ")}`);
  byId("search-pages").hidden = !pages.length;
  byId("video-queue").hidden = !videos.length;
  const queueSize = (status.counts?.video?.pending || 0) + (status.counts?.video?.leased || 0);
  set("video-queue-note", queueSize > videos.length ? `显示前 ${videos.length} 项 / 共 ${count(queueSize)} 项`
    : `共 ${count(queueSize)} 项`);
  const signature = JSON.stringify([pages, videos]);
  if (signature === searchSignature) return;
  searchSignature = signature;
  byId("search-pages").replaceChildren(...pages.map((page) => {
    const item = document.createElement("li");
    item.className = "cluster-queue-state";
    item.dataset.state = page.state;
    const order = page.order === "pubdate" ? "最新发布" : "综合排序";
    const state = page.state === "leased" ? page.node || "检索中" : "待检索";
    item.textContent = `${order}第 ${page.page} 页 · ${state}`;
    return item;
  }));
  byId("video-queue-rows").replaceChildren(...videos.map((task) => {
    const row = document.createElement("li");
    const video = document.createElement("span");
    video.textContent = task.key;
    const state = document.createElement("span");
    state.className = "cluster-queue-state";
    state.dataset.state = task.state;
    state.textContent = task.state === "leased" ? `检测中 · ${task.node || "工作节点"}` : "待检测";
    row.append(video, state);
    return row;
  }));
}

function renderNodes(snapshot: Status, found: number) {
  const status = snapshot.status;
  const nodes = [...(status.nodes || [])].sort((a, b) => Number(b.host) - Number(a.host) ||
    Number(b.online) - Number(a.online) || a.name.localeCompare(b.name, "zh-CN"));
  const online = nodes.filter((node) => node.online).length;
  set("online", status.nodes ? online + " / " + nodes.length + " 在线" : "等待同步");
  const limit = status.concurrency_limit;
  const importing = status.round_import;
  const importNode = importing && nodes.find((node) => node.id === importing.node);
  const rule = importing ? [importNode
    ? `${importNode.name} ${importing.participating ? "正在轮间顺序参与" : "正在导入活动"}` : "轮间处理已完成",
    `完成 ${importing.done} / ${importing.done + importing.pending + importing.skipped} 个节点`,
    importing.skipped ? `跳过 ${importing.skipped} 个` : ""].filter(Boolean).join(" · ")
    : status.collection_paused ? "主动搜集已暂停，待检测视频留在队列。"
    : status.history_syncing ? "历史 BV 合并完成后继续派发任务。"
    : status.phase === "collecting"
    ? (status.target && found >= status.target ? "已达收录目标，等待各节点交回已领取的检索。"
      : "达到收录目标或搜完可用结果页后，开始分配检测。")
    : status.two_stage && status.phase === "detecting" && !nodes.some((node) => node.online && node.work === 2)
      ? "等待支持两阶段协作且模型已就绪的节点。"
      : "就绪的空闲节点每次领取一个视频" + (limit ? "，最多 " + limit + " 个并行检测。" : "。");
  set("dispatch-rule", status.counts ? rule : "");
  set("nodes-empty", "等待节点同步");
  byId("nodes-empty").hidden = nodes.length > 0;
  const signature = JSON.stringify([nodes.map(({ seen, ...node }) => node), snapshot.node, status.paused, status.phase, importing?.node]);
  if (signature === nodeSignature) return;
  nodeSignature = signature;
  byId("nodes").replaceChildren(...nodes.map((node) => {
    const row = document.createElement("li");
    const heading = document.createElement("div");
    heading.className = "cluster-node-heading";
    const name = document.createElement("strong");
    name.textContent = node.name;
    const role = document.createElement("span");
    role.className = "caption";
    role.textContent = (node.host ? "主节点" : "工作节点") + (node.id === snapshot.node ? "（本机）" : "");
    const state = document.createElement("span");
    state.className = "cluster-pill";
    const importingHere = importing?.node === node.id;
    state.dataset.state = !node.online ? "offline" : node.kind || importingHere || (node.local_work && node.local_work !== "idle") ? "working" : node.work ? "idle" : "sync";
    const canWork = status.two_stage && status.phase === "detecting" ? node.work === 2 : Boolean(node.work);
    state.textContent = !node.online ? "离线" : node.local_work === "participating" ? "参与活动中"
      : node.kind === "search" ? "检索中" : node.kind === "watch" || node.local_work === "watch" ? "追踪中" : node.kind ? "检测中"
      : importingHere ? "导入中" : node.history_complete === 0 ? "同步 BV"
      : node.local_work === "busy" ? "本机任务中" : !canWork ? "仅同步" : status.paused ? "已暂停" : "空闲";
    heading.append(name, role, state);
    row.append(heading);
    const task = document.createElement("p");
    task.className = "caption cluster-node-task";
    task.textContent = node.task_label || (importingHere ? "导入集群活动目录" : "累计完成 " + count(node.completed) + " 项任务");
    row.append(task);
    const stats = document.createElement("dl");
    stats.className = "cluster-node-stats";
    const checked = node.videos_counted || 0;
    const matched = node.videos_matched || 0;
    for (const [label, value, note] of [
      ["本机历史 BV", count(node.history_count ?? undefined), node.history_complete === 0 ? "上传中" : ""],
      ["本轮检索页", count(node.search_pages), ""],
      ["累计已检测", count(node.videos_total), node.videos_checked === undefined ? "" : `本轮 ${count(node.videos_checked)}`],
      ["累计命中率", checked ? percent.format(matched / checked) : "—",
        checked ? `${count(matched)} / ${count(checked)}` : ""],
      ["待参与活动", count(node.participation_pending ?? undefined),
        node.participation_pending == null ? "等待节点上报"
          : !node.online ? "上次上报" : node.participation_enabled ? "已开启轮间参与" : "轮间参与未开启"],
    ]) {
      const item = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = label;
      const data = document.createElement("dd");
      data.textContent = value;
      if (note) {
        const detail = document.createElement("small");
        detail.textContent = note;
        data.append(detail);
      }
      item.append(term, data);
      stats.append(item);
    }
    row.append(stats);
    return row;
  }));
}

function hideInvite() {
  const invite = byId<HTMLInputElement>("invite-text");
  invite.hidden = true;
  invite.value = "";
  set("show-invite", "显示邀请码");
  byId("show-invite").setAttribute("aria-expanded", "false");
}

async function refresh() {
  if (busy || refreshing) return;
  refreshing = true;
  try {
    const snapshot = await fetchJSON<Status>("/api/cluster/status");
    if (!busy) {
      const prior = current?.local_cursor;
      render(snapshot);
      if (prior !== undefined && prior !== snapshot.local_cursor) {
        void loadSummary().catch(() => {});
        void loadActivities().catch(() => {});
      }
    }
  } catch (error) {
    set("link-state", "无法读取集群状态");
    byId("link-state").dataset.connected = "false";
  } finally {
    refreshing = false;
  }
}

async function action(path: string, data?: object) {
  if (busy) return;
  busy = true;
  byId("root").setAttribute("aria-busy", "true");
  const buttons = [...byId("root").querySelectorAll<HTMLButtonElement>(
    "#cluster-setup button, #cluster-manage button, #cluster-collection-pause, #cluster-rename button, #cluster-dispatch button",
  )];
  buttons.forEach((button) => button.disabled = true);
  try {
    const result = await fetchJSON<Status & { invite?: string }>("/api/cluster/" + path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: data ? JSON.stringify(data) : undefined,
    });
    if (result.invite) {
      if (byId<HTMLDetailsElement>("manage").open) {
        const invite = byId<HTMLInputElement>("invite-text");
        invite.value = result.invite;
        invite.hidden = false;
        invite.select();
        set("show-invite", "隐藏邀请码");
        byId("show-invite").setAttribute("aria-expanded", "true");
      }
    } else {
      render(result);
      if (path === "join") byId<HTMLInputElement>("invite").value = "";
      if (path === "work-assignments") showToast("节点分工已保存", "success");
      if (path === "node-name") {
        byId<HTMLDetailsElement>("rename").open = false;
        showToast("节点名称已更新", "success");
      }
    }
  } catch (error) {
    showToast(error instanceof Error ? error.message : "集群操作失败", "error");
  } finally {
    buttons.forEach((button) => button.disabled = false);
    byId("root").removeAttribute("aria-busy");
    busy = false;
    if (current) updateDispatchControls(current);
  }
}

export function bindCluster() {
  if (!byId("root")) return;
  mountEventPanels();
  updateDetections = mountDetectedActivities(byId);
  for (const mode of ["host", "join"]) {
    byId<HTMLFormElement>(mode + "-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.currentTarget as HTMLFormElement));
      void action(mode, data);
    });
  }
  byId("collection-pause").addEventListener("click", () => {
    void action("collection-pause?paused=" + String(!current?.status.collection_paused));
  });
  byId("watch-prev").addEventListener("click", () => void refreshWatch(watchPage - 1));
  byId("watch-next").addEventListener("click", () => void refreshWatch(watchPage + 1));
  byId("watch-start").addEventListener("click", () => void action(current?.status.watch_repeating ? "watch-stop" : "watch-start"));
  byId("dispatch-form").addEventListener("change", () => {
    if (current) updateDispatchControls(current);
  });
  byId("dispatch-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (dispatchDirty) void action("work-assignments", readAssignments());
  });
  const rename = byId<HTMLDetailsElement>("rename");
  rename.addEventListener("toggle", () => {
    if (!rename.open) return;
    byId<HTMLDetailsElement>("manage").open = false;
    const input = byId<HTMLInputElement>("node-name");
    input.value = byId("local-name").textContent || "";
    input.focus();
    input.select();
  });
  byId("rename-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const name = byId<HTMLInputElement>("node-name").value.trim();
    if (name) void action("node-name", { name });
    else showToast("请输入节点名称", "error");
  });
  byId("round-opt-in").addEventListener("click", () => {
    void action("round-participation", { enabled: !current?.round_participation_enabled });
  });
  byId("show-invite").addEventListener("click", () => {
    if (byId("invite-text").hidden) void action("invite");
    else hideInvite();
  });
  byId("manage").addEventListener("toggle", (event) => {
    if ((event.currentTarget as HTMLDetailsElement).open) rename.open = false;
    else hideInvite();
  });
  byId("leave").addEventListener("click", () => {
    if (current?.role === "host" && !window.confirm("解散后所有节点将断开。确定解散集群？")) return;
    void action("leave");
  });
  document.querySelectorAll('[data-section="logs"], [data-section="sources"], [data-source-view="discovery"]')
    .forEach((button) => button.addEventListener("click", () => void refresh()));
  const section = byId("root").closest(".view-section");
  if (section) {
    const observer = new MutationObserver(() => {
      if (section.classList.contains("active")) void refresh();
      else {
        byId<HTMLDetailsElement>("manage").open = false;
        rename.open = false;
      }
    });
    observer.observe(section, { attributes: true, attributeFilter: ["class"] });
  }
  void refresh();
  window.setInterval(() => { if (!document.hidden) void refresh(); }, 5000);
}
