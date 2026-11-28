// @ts-nocheck
/* eslint-disable */
/** Migrated from web/static/app.js — logic preserved. */

import { state } from "../state";
import { fetchJSON } from "../api/client";
import { isLoggedIn } from "../account/index";
import { sourceGrid, watchAddBtn, watchAddForm, watchAddMidError, watchAddMidInput, watchLastSynced, watchMetricCount, watchMetricLinks, watchNextWindow, watchUserGrid, watchUsersBadge, watchWindowCap } from "../dom";
import { bindActionButtons } from "../jobs/index";
import { showToast } from "../shell/toast";
import { formatUnixTimestamp, formatWatchWindow, formatWindowDays } from "../utils/format";
import { highlightWatchUserChip, setButtonLoading } from "../utils/motion";
import { escapeHtml } from "../utils/text";

export function clearWatchMidError() {
  if (!watchAddMidInput || !watchAddMidError) return;
  watchAddMidInput.classList.remove("is-invalid");
  watchAddMidError.hidden = true;
  watchAddMidError.textContent = "";
}

export function showWatchMidError(message) {
  if (!watchAddMidInput || !watchAddMidError) return;
  watchAddMidInput.classList.add("is-invalid");
  watchAddMidError.hidden = false;
  watchAddMidError.textContent = message;
}

export function closeWatchUserConfirm(exceptChip = null) {
  document.querySelectorAll(".watch-user-chip.is-confirming").forEach((chip) => {
    if (chip !== exceptChip) chip.classList.remove("is-confirming");
  });
}

export function renderWatchUsersPanel(data) {
  if (!data) return;
  state.watchUsers = data;
  const count = Number(data.count) || (data.users || []).length;
  if (watchMetricCount) watchMetricCount.textContent = String(count);
  if (watchMetricLinks) watchMetricLinks.textContent = String(data.last_scan_link_count ?? "—");
  if (watchUsersBadge) watchUsersBadge.textContent = `${count} 人`;
  if (watchLastSynced) watchLastSynced.textContent = formatUnixTimestamp(data.last_synced_at);
  if (watchNextWindow) {
    const window = data.next_window || {};
    watchNextWindow.textContent = formatWatchWindow(window.start, window.end);
  }
  if (watchWindowCap) {
    watchWindowCap.textContent = formatWindowDays(data.max_window_seconds);
  }
  updateWatchUserFormState();

  if (!watchUserGrid) return;
  const users = data.users || [];
  const canManage = isLoggedIn();
  if (!users.length) {
    watchUserGrid.innerHTML = `<p class="caption watch-user-empty">${canManage ? "暂无监控用户，可在上方添加" : "暂无监控用户"}</p>`;
    return;
  }
  watchUserGrid.innerHTML = users
    .map(
      (user) => `
      <article class="watch-user-chip" data-watch-mid="${escapeHtml(user.mid)}">
        <a class="watch-user-link" href="https://space.bilibili.com/${encodeURIComponent(user.mid)}/dynamic" target="_blank" rel="noopener" title="${escapeHtml(user.name)}">${escapeHtml(user.name)}</a>
        <span class="watch-user-mid">MID ${escapeHtml(user.mid)}</span>
        <div class="watch-user-actions">
          <button type="button" class="watch-user-remove" data-watch-remove="${escapeHtml(user.mid)}" aria-label="移除 ${escapeHtml(user.name)}" ${canManage ? "" : "disabled"} title="${canManage ? "移出监控名单" : "登录后可管理"}">×</button>
          <div class="watch-user-confirm-actions">
            <button type="button" class="btn btn-secondary btn-compact watch-user-confirm-yes" data-watch-confirm="${escapeHtml(user.mid)}">确认</button>
            <button type="button" class="watch-user-confirm-no" data-watch-cancel>取消</button>
          </div>
        </div>
      </article>`
    )
    .join("");
}

export function updateWatchUserFormState() {
  const canManage = isLoggedIn();
  if (watchAddMidInput) watchAddMidInput.disabled = !canManage;
  if (watchAddBtn) {
    watchAddBtn.disabled = !canManage;
    watchAddBtn.title = canManage ? "" : "登录后可添加监控用户";
  }
}

export async function loadWatchUsers() {
  try {
    const data = await fetchJSON("/api/watch-users");
    renderWatchUsersPanel(data);
    return data;
  } catch (error) {
    if (watchUserGrid) {
      watchUserGrid.innerHTML = `<p class="caption watch-user-empty">加载失败：${escapeHtml(String(error.message || error))}</p>`;
    }
    throw error;
  }
}

export function parseWatchMidInput(raw) {
  const text = String(raw || "").trim();
  if (!/^\d+$/.test(text)) return null;
  if (text.length > 16) return null;
  try {
    const mid = BigInt(text);
    if (mid <= 0n) return null;
    if (mid <= BigInt(Number.MAX_SAFE_INTEGER)) return Number(text);
    return text;
  } catch {
    return null;
  }
}

export async function submitWatchUser(event) {
  event.preventDefault();
  clearWatchMidError();
  if (!isLoggedIn()) {
    showToast("请先扫码登录", "info", "登录后才能管理监控用户");
    return;
  }
  const rawMid = String(watchAddMidInput?.value || "").trim();
  if (!rawMid) {
    showWatchMidError("请输入用户 MID");
    watchAddMidInput?.focus();
    watchAddMidInput?.classList.add("is-shake");
    window.setTimeout(() => watchAddMidInput?.classList.remove("is-shake"), 420);
    return;
  }
  const mid = parseWatchMidInput(rawMid);
  if (mid === null) {
    showWatchMidError("请输入有效的 B 站用户 MID");
    watchAddMidInput?.focus();
    watchAddMidInput?.classList.add("is-shake");
    window.setTimeout(() => watchAddMidInput?.classList.remove("is-shake"), 420);
    return;
  }
  setButtonLoading(watchAddBtn, true, { label: "添加中…" });
  try {
    const result = await fetchJSON("/api/watch-users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mid }),
    });
    if (watchAddForm) watchAddForm.reset();
    clearWatchMidError();
    await loadWatchUsers();
    highlightWatchUserChip(mid);
    const addedName = result?.user?.name || String(mid);
    if (result?.name_fallback) {
      showToast("已添加监控用户（昵称暂用 MID）", "info", addedName);
    } else {
      showToast("已添加监控用户", "success", addedName);
    }
  } catch (error) {
    const message = String(error.message || error);
    if (message.includes("已在监控列表")) {
      showWatchMidError(message);
      watchAddMidInput?.focus();
    } else {
      showWatchMidError(message);
    }
  } finally {
    setButtonLoading(watchAddBtn, false);
    updateWatchUserFormState();
  }
}

export async function removeWatchUser(mid) {
  if (!isLoggedIn()) {
    showToast("请先扫码登录", "info", "登录后才能管理监控用户");
    return;
  }
  try {
    await fetchJSON(`/api/watch-users/${encodeURIComponent(mid)}`, { method: "DELETE" });
    await loadWatchUsers();
    showToast("已移出监控名单", "success");
  } catch (error) {
    showToast(String(error.message || error), "error");
  }
}

export function bindWatchUsers() {
  bindCollectionViews();
  watchAddForm?.addEventListener("submit", submitWatchUser);
  watchAddMidInput?.addEventListener("input", clearWatchMidError);
  watchUserGrid?.addEventListener("click", async (event) => {
    const cancelBtn = event.target.closest("[data-watch-cancel]");
    if (cancelBtn) {
      cancelBtn.closest(".watch-user-chip")?.classList.remove("is-confirming");
      return;
    }

    const confirmBtn = event.target.closest("[data-watch-confirm]");
    if (confirmBtn && !confirmBtn.disabled) {
      const mid = Number(confirmBtn.dataset.watchConfirm || 0);
      const chip = confirmBtn.closest(".watch-user-chip");
      if (!mid) return;
      setButtonLoading(confirmBtn, true);
      try {
        await removeWatchUser(mid);
        chip?.classList.remove("is-confirming");
      } finally {
        setButtonLoading(confirmBtn, false);
      }
      return;
    }

    const removeBtn = event.target.closest("[data-watch-remove]");
    if (!removeBtn || removeBtn.disabled) return;
    const chip = removeBtn.closest(".watch-user-chip");
    if (!chip) return;
    closeWatchUserConfirm(chip);
    chip.classList.add("is-confirming");
  });
}

function bindCollectionViews() {
  const tabs = document.querySelectorAll("[data-collection-view]");
  const publishers = document.getElementById("collection-publishers");
  const organizers = document.getElementById("collection-organizers");
  const status = document.getElementById("publisher-source-status");
  const list = document.getElementById("publisher-source-list");
  const previous = document.getElementById("publisher-source-prev");
  const next = document.getElementById("publisher-source-next");
  if (!publishers || !organizers || !status || !list || !previous || !next) return;
  let page = 1;
  let latestRequest = 0;
  async function load(target) {
    const request = ++latestRequest;
    previous.disabled = next.disabled = true;
    status.textContent = "正在读取发布者名单…";
    try {
      const result = await fetchJSON(`/api/cluster/publisher-sources?page=${target}`, {
        headers: { "X-Binggo-Dashboard": "1" },
      });
      if (request !== latestRequest) return;
      const last = Math.max(1, Math.ceil(result.total / result.page_size));
      if (target > last) return load(last);
      page = target;
      previous.disabled = page <= 1;
      next.disabled = page >= last;
      list.replaceChildren(...result.items.map((item) => {
        const row = document.createElement("li");
        const link = document.createElement("a");
        link.href = `https://space.bilibili.com/${encodeURIComponent(item.mid)}/dynamic`;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = item.name;
        row.append(link, ` · 最近发布 ${formatUnixTimestamp(item.last_activity)}`);
        return row;
      }));
      status.textContent = result.total ? `${result.total} 位发布者 · 第 ${page}/${last} 页`
        : "暂无达到入选条件的发布者。名单会在轮检和主动检索核验后更新。";
    } catch (error) {
      if (request !== latestRequest) return;
      list.replaceChildren();
      status.textContent = String(error.message || error);
    }
  }
  tabs.forEach((button) => button.addEventListener("click", () => {
    const view = button.dataset.collectionView;
    organizers.hidden = view !== "organizers";
    publishers.hidden = view !== "publishers";
    tabs.forEach((tab) => {
      const selected = tab === button;
      tab.classList.toggle("active", selected);
      tab.setAttribute("aria-pressed", String(selected));
    });
    if (!publishers.hidden) void load(page);
  }));
  previous.addEventListener("click", () => void load(page - 1));
  next.addEventListener("click", () => void load(page + 1));
  window.addEventListener("binggo:publishers-refresh", () => {
    if (!publishers.hidden) void load(page);
  });
}

export function renderSources(sources) {
  sourceGrid.innerHTML = (sources || [])
    .map((source, index) => {
      const links = [];
      if (source.space_url) {
        links.push(`<a class="source-link" href="${escapeHtml(source.space_url)}" target="_blank" rel="noopener">UP 主页</a>`);
      }
      if (source.container_url) {
        links.push(`<a class="source-link" href="${escapeHtml(source.container_url)}" target="_blank" rel="noopener">当前合集</a>`);
      }
      const statusClass = source.updated ? "fresh" : "cached";
      const statusText = source.updated ? "本次有更新" : "使用缓存";
      return `
      <article class="source-row" data-source-id="${escapeHtml(source.id)}" style="--row-delay:${index * 40}ms">
        <div class="source-row-index" aria-hidden="true">${escapeHtml(source.id)}</div>
        <div class="source-row-body">
          <div class="source-row-head">
            <h3>${escapeHtml(source.name)}</h3>
            <div class="source-row-actions">
              <span class="source-status ${statusClass}">${statusText}</span>
              <button
                type="button"
                class="btn btn-secondary btn-compact btn-pill source-refresh-btn"
                data-action="refresh_source"
                data-source-id="${escapeHtml(source.id)}"
              >更新此源</button>
            </div>
          </div>
          <p class="source-row-meta"><span class="source-link-count">${source.link_count}</span> 条链接 · ${escapeHtml(source.title || "暂无标题")}</p>
          <p class="source-row-time">最近检查：${escapeHtml(source.checked_at_text || "尚未更新")}</p>
          <div class="source-links">${links.join("") || '<span class="caption">暂无外链</span>'}</div>
        </div>
      </article>`;
    })
    .join("");
  bindActionButtons();
}
