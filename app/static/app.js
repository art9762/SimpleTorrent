"use strict";
const $ = (id) => document.getElementById(id);
const icon = (name) =>
  `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const escapeHTML = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
let csrf = "",
  filter = "all",
  torrents = [],
  snapshot = null,
  pollTimer,
  uploading = false,
  deleteId = "",
  filesId = "";
let renderKey = "",
  filesRenderKey = "",
  polling = false,
  pollAgain = false;
const statusNames = {
  metadata: "Получаем метаданные",
  ready: "Готово",
  downloading: "Скачивается",
  queued: "В очереди",
  checking: "Проверка",
  paused: "На паузе",
  error: "Ошибка",
};
const filterNames = {
  all: "Все загрузки",
  active: "В процессе",
  ready: "Готовые",
  paused: "На паузе",
};
const number = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const fmt = (bytes, rate = false) => {
  const units = ["Б", "КиБ", "МиБ", "ГиБ", "ТиБ"];
  let n = Math.max(0, Number(bytes) || 0),
    i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return { value: number.format(n), unit: units[i] + (rate ? "/с" : "") };
};
const bytes = (value) => {
  const f = fmt(value);
  return `${f.value} ${f.unit}`;
};
const speedHTML = (value) => {
  const f = fmt(value, true);
  return `${f.value} <small>${f.unit}</small>`;
};
function eta(value) {
  if (value < 0) return "Ищем пиры";
  if (value < 60) return "Меньше минуты";
  if (value < 3600) return `~ ${Math.ceil(value / 60)} мин`;
  if (value < 86400) return `~ ${number.format(value / 3600)} ч`;
  return `~ ${number.format(value / 86400)} дн`;
}
function toast(message, error = false) {
  const node = document.createElement("div");
  node.className = `toast${error ? " error" : ""}`;
  node.textContent = message;
  $("toasts").append(node);
  setTimeout(() => node.remove(), error ? 8000 : 4500);
}
function showLogin() {
  clearTimeout(pollTimer);
  csrf = "";
  $("workspace").hidden = true;
  $("login-screen").hidden = false;
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
}
async function api(url, options = {}) {
  const headers = { ...options.headers };
  if (csrf && options.method && options.method !== "GET")
    headers["X-CSRF-Token"] = csrf;
  if (options.body && !(options.body instanceof FormData))
    headers["Content-Type"] = "application/json";
  const response = await fetch(url, {
    ...options,
    headers,
    credentials: "same-origin",
    signal: options.signal || AbortSignal.timeout(20000),
  });
  let result;
  try {
    result = await response.json();
  } catch {
    result = {};
  }
  if (!response.ok) {
    if (response.status === 401 && url !== "/api/login") showLogin();
    const detail =
      typeof result.detail === "string"
        ? result.detail
        : "Не удалось выполнить действие. Попробуйте ещё раз.";
    throw new Error(detail);
  }
  return result;
}
function showWorkspace() {
  $("login-screen").hidden = true;
  $("workspace").hidden = false;
  $("password").value = "";
  $("login-error").textContent = "";
  renderKey = "";
  poll();
}
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  $("login-error").textContent = "";
  try {
    const result = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ password: $("password").value }),
    });
    csrf = result.csrf;
    showWorkspace();
  } catch (error) {
    $("login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("logout-button").addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST" });
    showLogin();
  } catch (error) {
    toast(error.message, true);
  }
});
function match(t, f) {
  if (f === "ready") return isReady(t);
  if (f === "paused") return t.paused && !isReady(t);
  if (f === "active") return !t.paused && !isReady(t);
  return true;
}
const isReady = (t) => t.metadata_ready !== false && t.remaining === 0;
function updateSummary(data) {
  $("download-speed").innerHTML = speedHTML(data.download_speed);
  $("upload-speed").innerHTML = speedHTML(data.upload_speed);
  const ready = torrents.filter(isReady).length;
  $("ready-count").innerHTML =
    `${ready} <small>${ready === 1 ? "загрузка" : ready >= 2 && ready <= 4 ? "загрузки" : "загрузок"}</small>`;
  for (const f of ["all", "active", "ready", "paused"])
    $(`count-${f}`).textContent = torrents.filter((t) => match(t, f)).length;
  const disk = data.storage;
  $("disk-free").textContent = bytes(disk.free);
  $("storage-meter").value = disk.total ? (100 * disk.used) / disk.total : 0;
  $("mobile-storage-caption").textContent =
    `${bytes(disk.free)} свободно${disk.reserved ? ` · в очереди ${bytes(disk.reserved)}` : ""}`;
  $("storage-caption").textContent =
    `${bytes(disk.used)} из ${bytes(disk.total)} занято`;
  $("disk-reserved").textContent = disk.reserved
    ? `В очереди: ${bytes(disk.reserved)}`
    : `Доступно для загрузок: ${bytes(disk.available)}`;
  if (disk.pending_metadata)
    $("disk-reserved").textContent += ` · размер неизвестен: ${disk.pending_metadata} magnet`;
  $("updated-at").textContent =
    `Обновлено в ${new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
  $("connection-label").textContent = "Сервер на связи";
  $("connection-dot").classList.remove("offline");
  $("server-error").hidden = !disk.low;
  $("server-error").textContent =
    "Недостаточно места с учётом очереди. Скачивания приостанавливаются. Освободите место и продолжите нужные загрузки.";
}
function render() {
  if (!snapshot) return;
  const query = $("search").value.trim().toLocaleLowerCase("ru");
  const visible = torrents.filter(
    (t) => match(t, filter) && t.name.toLocaleLowerCase("ru").includes(query),
  );
  const sort = $("sort").value;
  visible.sort((a, b) =>
    sort === "old"
      ? a.added_at - b.added_at
      : sort === "size"
        ? b.size - a.size
        : sort === "name"
          ? a.name.localeCompare(b.name, "ru")
          : b.added_at - a.added_at,
  );
  $("list-title").firstChild.textContent = `${filterNames[filter]} `;
  $("visible-count").textContent = visible.length;
  $("empty-state").hidden = visible.length > 0;
  $("empty-title").textContent = torrents.length
    ? "Загрузок не найдено"
    : "Здесь будут ваши файлы";
  $("empty-copy").textContent = torrents.length
    ? "Попробуйте другой фильтр или измените запрос."
    : "Добавьте первый торрент — сервер скачает его, а вы заберёте файлы, когда будет удобно.";
  $("empty-add").hidden = torrents.length > 0;
  const key = JSON.stringify([visible, filter, query, sort]);
  if (key === renderKey) return;
  renderKey = key;
  const focused = document.activeElement?.closest("[data-action]");
  const focusedKey = focused
    ? `${focused.dataset.id}:${focused.dataset.action}`
    : null;
  $("torrent-list").innerHTML = visible
    .map((t) => {
      const complete = isReady(t),
        progress = Math.max(0, Math.min(100, (t.metadata_ready === false ? t.metadata_progress : t.progress) * 100));
      const subtext =
        t.error ||
        (t.metadata_ready === false
          ? t.paused ? "Получение метаданных на паузе" : "Ждём список файлов и размер от пиров"
          : complete
          ? t.paused
            ? "На диске · раздача остановлена"
            : "На диске · раздаётся"
          : t.status === "downloading"
            ? `${eta(t.eta)} · ${t.peers} пиров`
            : t.status === "paused"
              ? "Можно продолжить позже"
              : t.status === "queued"
                ? "Ждёт свободного слота"
                : "Проверяем части файла");
      const date = new Date(t.added_at * 1000).toLocaleDateString("ru-RU", {
        day: "2-digit",
        month: "short",
      });
      const controls = `<button class="icon-button subtle" data-action="${t.paused ? "resume" : "pause"}" data-id="${t.id}" aria-label="${t.paused ? "Продолжить" : "Пауза"}: ${escapeHTML(t.name)}" title="${t.paused ? "Продолжить" : "Пауза"}">${icon(t.paused ? "play" : "pause")}</button><button class="icon-button subtle" data-action="delete" data-id="${t.id}" aria-label="Удалить: ${escapeHTML(t.name)}" title="Удалить">${icon("trash")}</button>`;
      return `<article class="torrent-row"><div class="file-cell"><div class="file-badge">${icon("file")}</div><div class="file-info"><button class="file-name" data-action="files" data-id="${t.id}" title="${escapeHTML(t.name)}">${escapeHTML(t.name)}</button><div class="file-meta"><span>${t.metadata_ready === false ? "Размер определяется" : bytes(t.size)}</span><span class="separator date">·</span><span class="date">${date}</span></div></div></div><div class="status-cell"><div class="status-line"><span class="status-label ${t.status}">${complete ? icon("check") : ""}${statusNames[t.status] || t.status}</span><span class="percent">${number.format(progress)}%</span></div>${!complete ? `<progress value="${progress}" max="100" aria-label="Прогресс: ${escapeHTML(t.name)}"></progress>` : ""}<p title="${escapeHTML(subtext)}">${escapeHTML(subtext)}</p></div><div class="speed-cell">${complete ? "—" : bytes(t.download_speed) + "/с"}<span>${t.upload_speed ? `↑ ${bytes(t.upload_speed)}/с` : ""}</span></div><div class="row-actions">${complete ? `<button class="primary take-button" data-action="files" data-id="${t.id}">${icon("down")}Забрать</button>` : ""}${controls}</div></article>`;
    })
    .join("");
  if (focusedKey) {
    const candidate = [
      ...$("torrent-list").querySelectorAll("[data-action]"),
    ].find((n) => `${n.dataset.id}:${n.dataset.action}` === focusedKey);
    candidate?.focus({ preventScroll: true });
  }
}
async function poll() {
  clearTimeout(pollTimer);
  if (!csrf) return;
  if (polling) {
    pollAgain = true;
    return;
  }
  polling = true;
  try {
    const data = await api("/api/torrents");
    if (!csrf) return;
    snapshot = data;
    torrents = data.torrents;
    updateSummary(data);
    render();
    if ($("files-dialog").open && filesId) await refreshFiles(false);
  } catch (error) {
    if (csrf) {
      $("connection-label").textContent = "Нет соединения";
      $("connection-dot").classList.add("offline");
      $("server-error").hidden = false;
      $("server-error").textContent = error.message;
    }
  } finally {
    polling = false;
    if (csrf)
      pollTimer = setTimeout(
        poll,
        pollAgain ? 100 : document.hidden ? 10000 : 2500,
      );
    pollAgain = false;
  }
}
// Actions refresh the next poll without allowing concurrent poll responses.
function refreshSoon() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, 100);
}
document.querySelectorAll("[data-filter]").forEach((button) =>
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((b) => {
      b.classList.toggle("selected", b === button);
      b.setAttribute("aria-pressed", String(b === button));
    });
    render();
  }),
);
$("search").addEventListener("input", render);
$("sort").addEventListener("change", render);
for (const id of ["add-button", "dropzone", "empty-add"])
  $(id).addEventListener("click", () => {
    if (!uploading) $("torrent-input").click();
  });
$("torrent-input").addEventListener("change", () => {
  uploadFiles([...$("torrent-input").files]);
  $("torrent-input").value = "";
});
let dragDepth = 0;
["dragenter", "dragover", "dragleave", "drop"].forEach((type) =>
  document.addEventListener(type, (event) => {
    if (!event.dataTransfer?.types.includes("Files") || $("workspace").hidden)
      return;
    event.preventDefault();
    if (type === "dragenter") {
      dragDepth++;
      $("dropzone").classList.add("dragging");
    }
    if (type === "dragleave" && --dragDepth <= 0) {
      $("dropzone").classList.remove("dragging");
      dragDepth = 0;
    }
    if (type === "drop") {
      dragDepth = 0;
      $("dropzone").classList.remove("dragging");
      uploadFiles([...event.dataTransfer.files]);
    }
  }),
);
async function uploadFiles(files) {
  if (uploading || !files.length) return;
  uploading = true;
  $("add-button").disabled = true;
  $("dropzone").disabled = true;
  $("empty-add").disabled = true;
  $("upload-status").hidden = false;
  let added = 0;
  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    $("upload-status").textContent =
      `Добавляем ${i + 1} из ${files.length}: ${file.name}`;
    if (!file.name.toLowerCase().endsWith(".torrent")) {
      toast(`${file.name}: выберите .torrent файл`, true);
      continue;
    }
    if (file.size > 4 * 1024 * 1024) {
      toast(`${file.name}: максимум 4 МиБ`, true);
      continue;
    }
    const body = new FormData();
    body.append("file", file);
    try {
      await api("/api/torrents", { method: "POST", body });
      added++;
    } catch (error) {
      toast(`${file.name}: ${error.message}`, true);
    }
  }
  uploading = false;
  $("add-button").disabled = false;
  $("dropzone").disabled = false;
  $("empty-add").disabled = false;
  $("upload-status").hidden = true;
  if (added)
    toast(
      added === 1
        ? "Торрент добавлен в очередь"
        : `Добавлено в очередь: ${added}`,
    );
  refreshSoon();
}
$("magnet-button").addEventListener("click", () => {
  $("magnet-error").textContent = "";
  $("magnet-dialog").showModal();
});
$("magnet-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("magnet-submit");
  button.disabled = true;
  $("magnet-error").textContent = "";
  try {
    await api("/api/torrents/magnet", { method: "POST", body: JSON.stringify({ uri: $("magnet-uri").value.trim() }) });
    $("magnet-dialog").close();
    $("magnet-uri").value = "";
    toast("Magnet добавлен. Получаем метаданные от пиров.");
    refreshSoon();
  } catch (error) {
    $("magnet-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("torrent-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const { id, action } = button.dataset;
  if (action === "files") return openFiles(id);
  if (action === "delete") {
    deleteId = id;
    $("delete-name").textContent =
      torrents.find((t) => t.id === id)?.name || "";
    $("delete-data").checked = true;
    $("delete-dialog").showModal();
    return;
  }
  button.disabled = true;
  try {
    await api(`/api/torrents/${id}/${action}`, { method: "POST" });
    refreshSoon();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});
async function openFiles(id) {
  filesId = id;
  filesRenderKey = "";
  $("files-title").textContent =
    torrents.find((t) => t.id === id)?.name || "Файлы";
  $("files-subtitle").textContent = "Загружаем список…";
  $("files-list").textContent = "";
  $("archive-link").hidden = true;
  $("files-dialog").showModal();
  await refreshFiles(true);
}
async function refreshFiles(showError) {
  const id = filesId;
  try {
    const data = await api(`/api/torrents/${id}/files`);
    if (id !== filesId || !$("files-dialog").open) return;
    $("files-title").textContent = data.torrent.name;
    $("files-subtitle").textContent =
      data.torrent.metadata_ready === false
        ? "Получаем метаданные. Список файлов и размер пока неизвестны."
        : `${data.files.length} файлов · ${bytes(data.torrent.size)} · готово ${data.files.filter((f) => f.ready).length}`;
    const markup = data.files
      .map(
        (f) =>
          `<div class="file-detail">${icon("file")}<div class="file-detail-info"><div class="file-detail-name">${escapeHTML(f.name)}</div><div class="file-detail-meta">${bytes(f.size)}${!f.ready ? ` · ${number.format(f.size ? (100 * f.completed) / f.size : 0)}%` : ""}</div></div>${f.ready ? `<a class="secondary" href="/api/torrents/${id}/files/${f.index}/download" download>${icon("down")}Скачать</a>` : '<span class="file-waiting">Ещё скачивается</span>'}</div>`,
      )
      .join("");
    const key = JSON.stringify([id, data.files]);
    if (filesRenderKey !== key) {
      $("files-list").innerHTML = markup;
      filesRenderKey = key;
    }
    $("archive-link").hidden =
      !isReady(data.torrent) || !data.files.length || data.files.some((f) => !f.ready);
    $("archive-link").href = `/api/torrents/${id}/archive`;
    $("archive-link").setAttribute("download", "");
  } catch (error) {
    if (showError) {
      $("files-subtitle").textContent = error.message;
      toast(error.message, true);
    }
  }
}
$("files-dialog").addEventListener("close", () => {
  filesId = "";
});
$("confirm-delete").addEventListener("click", async () => {
  const button = $("confirm-delete");
  button.disabled = true;
  try {
    await api(
      `/api/torrents/${deleteId}?delete_data=${$("delete-data").checked}`,
      { method: "DELETE" },
    );
    $("delete-dialog").close();
    toast("Загрузка удалена");
    refreshSoon();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});
$("settings-button").addEventListener("click", async () => {
  try {
    const data = await api("/api/settings");
    $("download-limit").value = data.download_kib;
    $("upload-limit").value = data.upload_kib;
    $("engine-version").textContent = `Transmission ${data.engine_version}`;
    $("settings-dialog").showModal();
  } catch (error) {
    toast(error.message, true);
  }
});
$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        download_kib: Number($("download-limit").value),
        upload_kib: Number($("upload-limit").value),
      }),
    });
    $("settings-dialog").close();
    toast("Настройки сохранены");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});
document
  .querySelectorAll("[data-close]")
  .forEach((b) =>
    b.addEventListener("click", () => $(b.dataset.close).close()),
  );
document.querySelectorAll("dialog").forEach((dialog) =>
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      const rect = dialog.getBoundingClientRect();
      if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      )
        dialog.close();
    }
  }),
);
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("st-theme", theme);
  } catch {}
}
try {
  setTheme(
    localStorage.getItem("st-theme") ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
  );
} catch {
  setTheme("light");
}
$("theme-button").addEventListener("click", () =>
  setTheme(
    document.documentElement.dataset.theme === "dark" ? "light" : "dark",
  ),
);
(async () => {
  try {
    const session = await api("/api/session");
    csrf = session.csrf;
    showWorkspace();
  } catch {
    showLogin();
  }
})();
