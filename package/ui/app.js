// Homeward page shown inside the DSM app window (see desktop.js).
// Talks to api.cgi; all strings come from texts/<lang>/strings.
"use strict";

const API = "api.cgi";
const REFRESH_MS = 10000;
const REFRESH_RUNNING_MS = 3000; // while a job runs, so its progress moves
const strings = {};
let tokenRequest = null;
let accountsCache = [];
let settingsCache = null;

// --- strings ---------------------------------------------------------------------------

function dsmSession() {
    try {
        return window.parent.SYNO.SDS.Session || null;
    } catch (e) {
        return null;
    }
}

function language() {
    const session = dsmSession();
    const lang = session?.lang || "";
    if (lang && lang !== "def") return lang === "ger" ? "ger" : "enu";
    return (navigator.language || "").toLowerCase().startsWith("de") ? "ger" : "enu";
}

function parseStrings(text) {
    let section = "";
    for (const line of text.split(/\r?\n/)) {
        const head = line.match(/^\s*\[([^\]]+)\]\s*$/);
        if (head) {
            section = head[1];
            continue;
        }
        const entry = line.match(/^\s*([\w.-]+)\s*=\s*"(.*)"\s*$/);
        if (entry) strings[section + ":" + entry[1]] = entry[2].replaceAll(String.raw`\"`, '"');
    }
}

async function loadStrings() {
    // English first, so a string missing in the user's language falls back to it.
    const texts = await Promise.all(
        ["enu", language()].map(async (lang) => {
            const response = await fetch("texts/" + lang + "/strings", { cache: "no-cache" });
            return response.ok ? response.text() : "";
        })
    );
    texts.forEach(parseStrings);
}

function t(key, ...args) {
    const text = strings[key] || key;
    return text.replace(/\{(\d+)\}/g, (m, i) => (args[i] === undefined ? m : String(args[i])));
}

// --- DOM helpers -----------------------------------------------------------------------

function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
        if (value === undefined || value === null || value === false) continue;
        if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
        else if (name === "text") node.textContent = value;
        else node.setAttribute(name, value === true ? "" : value);
    }
    for (const child of children.flat()) {
        if (child === null || child === undefined || child === false) continue;
        node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
}

function table(headers, rows) {
    if (!rows.length) return el("p", { class: "muted", text: t("common:none") });
    return el(
        "table",
        {},
        el("thead", {}, el("tr", {}, headers.map((h) => el("th", { text: h })))),
        el("tbody", {}, rows)
    );
}

function showMessage(text, kind) {
    const box = document.getElementById("message");
    box.textContent = text;
    box.className = "message " + (kind || "info");
    box.hidden = !text;
    if (kind !== "error" && text) setTimeout(() => showMessage(""), 5000);
}

function formatTime(seconds) {
    if (!seconds) return t("common:never");
    return new Date(seconds * 1000).toLocaleString(language() === "ger" ? "de-DE" : "en-GB");
}

function formatDuration(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s >= 3600) return Math.floor(s / 3600) + " h " + Math.floor((s % 3600) / 60) + " min";
    return s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s";
}

function formatNumber(n) {
    return Number(n).toLocaleString(language() === "ger" ? "de-DE" : "en-GB");
}

function formatBytes(n) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = n, unit = 0;
    while (value >= 1000 && unit < units.length - 1) {
        value /= 1000;
        unit += 1;
    }
    const digits = unit === 0 || value >= 100 ? 0 : 1;
    return value.toLocaleString(language() === "ger" ? "de-DE" : "en-GB", { maximumFractionDigits: digits }) + " " + units[unit];
}

// --- API -------------------------------------------------------------------------------

class ApiError extends Error {
    constructor(code, detail) {
        super(code);
        this.code = code;
        this.detail = detail || "";
    }
}

async function synoToken() {
    const session = dsmSession();
    if (session?.SynoToken) return session.SynoToken;
    // Opened outside the DSM desktop: ask DSM for the session's token.
    try {
        const response = await fetch("/webman/login.cgi", { credentials: "same-origin" });
        const data = await response.json();
        return data.SynoToken || "";
    } catch {
        // No DSM session here: the API call that follows reports "not signed in".
        return "";
    }
}

async function api(method, params, write) {
    if (tokenRequest === null) tokenRequest = synoToken();
    const token = await tokenRequest;
    const body = new URLSearchParams({ method: method, ...params });
    let response;
    try {
        response = await fetch(write ? API : API + "?" + body, {
            method: write ? "POST" : "GET",
            credentials: "same-origin",
            headers: { "X-SYNO-TOKEN": token },
            body: write ? body : undefined,
        });
    } catch (e) {
        throw new ApiError("network");
    }
    let data;
    try {
        data = await response.json();
    } catch (e) {
        throw new ApiError("network");
    }
    if (!data.success) throw new ApiError(data.error.code, data.error.message);
    return data.data;
}

function errorText(error) {
    if (!(error instanceof ApiError)) return t("error:internal_error");
    const key = "error:" + error.code;
    return strings[key] ? t(key, error.detail) : error.code + ": " + error.detail;
}

async function guarded(action) {
    try {
        return await action();
    } catch (error) {
        showMessage(errorText(error), "error");
        return undefined;
    }
}

// --- dialogs ---------------------------------------------------------------------------

// Opens a modal form. `submit` gets the form values; throwing an ApiError keeps the
// dialog open and shows the error inside it.
function openDialog(title, fields, submitLabel, submit) {
    const dialog = document.getElementById("dialog");
    const form = document.getElementById("dialog-form");
    const error = el("p", { class: "error", hidden: true });
    const cancel = el("button", { type: "button", text: t("common:cancel") });
    const ok = el("button", { type: "submit", class: "primary", text: submitLabel });
    form.replaceChildren(
        el("h2", { text: title }),
        el("div", { class: "body" }, fields, error),
        el("div", { class: "foot" }, cancel, ok)
    );
    cancel.addEventListener("click", () => dialog.close());
    form.onsubmit = async (event) => {
        event.preventDefault();
        ok.disabled = true;
        try {
            const values = Object.fromEntries(new FormData(form).entries());
            await submit(values);
            dialog.close();
        } catch (e) {
            error.textContent = errorText(e);
            error.hidden = false;
        } finally {
            ok.disabled = false;
        }
    };
    dialog.showModal();
    const first = form.querySelector("input:not([readonly]), select, textarea");
    if (first) first.focus();
}

function confirmDialog(text, label, action) {
    openDialog(t("common:confirm"), [el("p", { text: text })], label, action);
}

function field(label, input, hint) {
    return el("label", {}, label, input, hint ? el("span", { class: "hint", text: hint }) : null);
}

function select(name, options, value) {
    return el(
        "select",
        { name: name },
        options.map(([key, text]) => el("option", { value: key, selected: key === value, text: text }))
    );
}

// --- folder picker ---------------------------------------------------------------------

const PICKER_MAX = 2000;

// A text field for a NAS folder with a "Browse..." button that opens the picker.
function folderField(label, name, value, hint) {
    const input = el("input", { name: name, value: value, required: true });
    const browse = el("button", {
        type: "button",
        text: t("picker:browse"),
        onclick: () => pickFolder(input.value.trim(), (path) => (input.value = path)),
    });
    return el(
        "label",
        {},
        label,
        el("div", { class: "folder-input" }, input, browse),
        hint ? el("span", { class: "hint", text: hint }) : null
    );
}

// Lets the user browse the shared folders Homeward can open; `onPick` gets the path.
// It runs on top of the open form dialog, which keeps its values.
function pickFolder(start, onPick) {
    const dialog = document.getElementById("picker");
    const where = el("div", { class: "where" });
    const list = el("ul", { class: "folders" });
    const note = el("p", { class: "hint", hidden: true });
    const error = el("p", { class: "error", hidden: true });
    const up = el("button", { type: "button", text: t("picker:up") });
    const newName = el("input", { placeholder: t("picker:folder_name"), "aria-label": t("picker:folder_name") });
    const create = el("button", { type: "button", text: t("picker:create") });
    const newRow = el("div", { class: "folder-input", hidden: true }, newName, create);
    const newFolder = el("button", { type: "button", text: t("picker:new_folder") });
    const cancel = el("button", { type: "button", text: t("common:cancel") });
    const choose = el("button", { type: "button", class: "primary", text: t("picker:select") });
    let current = { path: null, parent: null, folders: [] };

    function fail(e) {
        error.textContent = errorText(e);
        error.hidden = false;
    }

    async function load(path, fallback) {
        let data;
        try {
            data = await api("folders.list", path ? { path: path } : {});
        } catch (e) {
            if (fallback) return load(null, false);
            fail(e);
            return;
        }
        error.hidden = true;
        current = data;
        where.textContent = data.path || t("picker:shares");
        up.disabled = !data.path;
        choose.disabled = !data.path;
        newFolder.disabled = !data.path || !data.writable;
        newRow.hidden = true;
        const notes = [];
        if (data.path && !data.writable) notes.push(t("picker:read_only_hint"));
        if (data.truncated) notes.push(t("picker:truncated", PICKER_MAX));
        note.textContent = notes.join(" ");
        note.hidden = !notes.length;
        list.replaceChildren(
            ...data.folders.map((folder) =>
                el(
                    "li",
                    {},
                    el(
                        "button",
                        { type: "button", class: "folder", title: folder.path, onclick: () => void load(folder.path, false) },
                        el("span", { class: "name", text: folder.name }),
                        data.path ? null : el("span", { class: "muted", text: folder.path }),
                        folder.writable ? null : el("span", { class: "muted", text: t("picker:read_only") })
                    )
                )
            )
        );
        if (!data.folders.length)
            list.append(el("li", { class: "muted", text: t(data.path ? "picker:empty" : "picker:no_shares") }));
    }

    up.addEventListener("click", () => {
        void load(current.parent, false);
    });
    newFolder.addEventListener("click", () => {
        newRow.hidden = false;
        newName.value = "";
        newName.focus();
    });
    async function createFolder() {
        try {
            const folder = await api("folders.create", { path: current.path, name: newName.value.trim() }, true);
            await load(folder.path, false);
        } catch (e) {
            fail(e);
        }
    }

    create.addEventListener("click", () => {
        if (current.path && newName.value.trim()) void createFolder();
    });
    newName.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            create.click();
        }
    });
    cancel.addEventListener("click", () => dialog.close());
    choose.addEventListener("click", () => {
        if (!current.path) return;
        onPick(current.path);
        dialog.close();
    });

    dialog.replaceChildren(
        el("h2", { text: t("picker:title") }),
        el("div", { class: "body" }, el("div", { class: "nav" }, up, where), list, newRow, note, error),
        el("div", { class: "foot" }, newFolder, el("span", { class: "spacer" }), cancel, choose)
    );
    dialog.showModal();
    void load(start, true);
}

// --- events ----------------------------------------------------------------------------

const LEVEL_BADGE = { info: "idle", warning: "warn", error: "bad" };

function eventSource(source) {
    const [kind, ...rest] = source.split(":");
    const key = "event:src_" + kind;
    return strings[key] ? t(key, rest.join(":")) : source;
}

function eventText(event) {
    const key = "event:" + event.code;
    let text = strings[key] ? t(key) : event.message || event.code;
    if (event.until && event.until * 1000 > Date.now()) text += " (" + t("status:waiting_until", formatTime(event.until)) + ")";
    return text;
}

function badge(cls, text) {
    return el("span", { class: "badge " + cls, text: text });
}

function eventRow(event, withSource) {
    return el(
        "tr",
        {},
        el("td", { class: "nowrap", text: formatTime(event.time) }),
        el("td", {}, badge(LEVEL_BADGE[event.level], t("event:level_" + event.level))),
        withSource ? el("td", { text: eventSource(event.source) }) : null,
        el("td", {}, eventText(event), event.message && strings["event:" + event.code]
            ? el("div", { class: "muted", text: event.message }) : null)
    );
}

function eventTable(events, withSource) {
    const headers = [t("log:time"), "", withSource ? t("log:source") : null, t("log:event")].filter((h) => h !== null);
    return table(headers, events.map((event) => eventRow(event, withSource)));
}

// --- state -----------------------------------------------------------------------------

// What the sidebar and the detail pane show: a sync pair or photos backup ("pair:<name>",
// "photos:<name>"), the background service ("service") or an Apple ID ("account:<name>").
const view = { mode: "jobs", key: null, tab: "overview", run: null };
let data = { status: null, pairs: [], photos: [], accounts: [] };

async function loadData() {
    const [status, pairs, photos, accounts] = await Promise.all([
        api("status.get"),
        api("pairs.list"),
        api("photos.list"),
        api("accounts.list"),
        settingsCache ? null : loadSettings(),
    ]);
    accountsCache = accounts.accounts;
    data = { status: status, pairs: pairs.pairs, photos: photos.photos, accounts: accounts.accounts };
}

// Every pair and photos backup with its live status and its settings.
function jobs() {
    const status = data.status || { pairs: [], photos: [] };
    const result = status.pairs.map((item) => ({
        key: "pair:" + item.name,
        run: item.name, // the name the daemon and cancel use
        kind: "pair",
        name: item.name,
        item: item,
        config: data.pairs.find((p) => p.name === item.name),
    }));
    for (const item of status.photos || []) {
        result.push({
            key: "photos:" + item.name,
            run: "photos:" + item.name,
            kind: "photos",
            name: item.name,
            item: item,
            config: data.photos.find((p) => p.name === item.name),
        });
    }
    return result;
}

function currentJob() {
    return jobs().find((job) => job.key === view.key) || null;
}

const JOB_BADGE = { ok: "ok", failed: "bad", blocked: "warn", config_error: "bad", killed: "idle", waiting: "idle", cancelled: "warn" };
const JOB_UNFINISHED = ["killed", "cancelled"];

// The state of a job as a badge class ("ok", "warn", "bad", "idle", "busy") and a text.
function jobState(job) {
    const item = job.item;
    const daemon = data.status?.daemon || {};
    const status = item.job;
    if (item.pending) return { cls: "busy", text: t("status:queued") };
    if (daemon.running === job.run)
        return { cls: "busy", text: status?.started_at ? t("status:running_since", formatTime(status.started_at)) : t("ui:running") };
    const run = item.last_run;
    // A cancelled or interrupted run leaves no entry in the run history of a pair.
    const unfinished = status && JOB_UNFINISHED.includes(status.result) && status.finished_at > run?.finished_at;
    if (job.kind === "pair" && run && !unfinished) return pairRunState(run);
    if (!status?.result) return { cls: "idle", text: t("ui:never_ran") };
    return { cls: JOB_BADGE[status.result] || "bad", text: t("status:job_" + status.result) };
}

function pairRunState(run) {
    if (run.blocked) return { cls: "warn", text: t("status:result_blocked") };
    if (run.errors) return { cls: "bad", text: t("status:result_errors", run.errors) };
    if (run.dry_run) return { cls: "idle", text: t("status:result_dry", run.actions) };
    return { cls: "ok", text: t("status:result_ok", run.actions) };
}

function remoteRow(c) {
    return [c.remote_provider === "folder" ? t("pair:provider_folder") : t("pair:remote"), c.remote];
}

function providerText(c) {
    return c.remote_provider === "folder" ? t("pair:provider_folder") : t("pair:provider_icloud");
}

const STATE_BADGE = { signed_in: "ok", needs_2fa: "warn", signed_out: "bad", unavailable: "idle" };

function kindText(job) {
    return job.kind === "photos" ? t("status:photos_kind") : t("mode:" + job.item.mode);
}

// --- sidebar ---------------------------------------------------------------------------

function sideItem(key, name, sub, cls, onSelect) {
    return el(
        "li",
        {},
        el(
            "button",
            { class: "side-item", "aria-current": view.key === key ? "true" : null, onclick: onSelect },
            el("span", { class: "dot " + cls }),
            el("span", { class: "side-text" }, el("span", { class: "side-name", text: name }), el("span", { class: "muted small", text: sub }))
        )
    );
}

function renderSidebar() {
    for (const button of document.querySelectorAll("[data-mode]")) {
        button.setAttribute("aria-current", String(button.dataset.mode === view.mode));
    }
    document.getElementById("service").setAttribute("aria-current", String(view.key === "service"));
    document.getElementById("version").textContent = data.status ? t("common:version", data.status.version) : "";
    const list = document.getElementById("side-list");
    if (view.mode === "accounts") {
        list.replaceChildren(
            ...data.accounts.map((account) =>
                sideItem("account:" + account.name, account.name, account.apple_id, STATE_BADGE[account.state],
                    () => choose("account:" + account.name))
            )
        );
        if (!data.accounts.length) list.append(el("li", { class: "muted side-empty", text: t("ui:no_accounts") }));
        return;
    }
    list.replaceChildren(
        ...jobs().map((job) => sideItem(job.key, job.name, kindText(job), jobState(job).cls, () => choose(job.key)))
    );
    if (!jobs().length) list.append(el("li", { class: "muted side-empty", text: t("ui:no_jobs") }));
}

// --- detail pane -----------------------------------------------------------------------

const TABS = {
    job: ["overview", "runs", "settings", "log"],
    service: ["overview", "log"],
    account: ["overview"],
    empty: ["overview"],
};

function viewKind() {
    if (view.key === "service") return "service";
    if (view.key?.startsWith("account:")) return "account";
    return currentJob() ? "job" : "empty";
}

function renderTabs() {
    const tabs = TABS[viewKind()];
    if (!tabs.includes(view.tab)) view.tab = "overview";
    document.getElementById("tabs").replaceChildren(
        ...tabs.map((tab) =>
            el("button", {
                role: "tab",
                "aria-selected": String(tab === view.tab),
                text: t("ui:tab_" + tab),
                onclick: () => {
                    view.tab = tab;
                    void guarded(renderDetail);
                },
            })
        )
    );
}

async function renderDetail() {
    renderTabs();
    const content = document.getElementById("content");
    const kind = viewKind();
    let nodes;
    if (kind === "service") nodes = view.tab === "log" ? await serviceLog() : serviceOverview();
    else if (kind === "account") nodes = accountOverview(data.accounts.find((a) => "account:" + a.name === view.key));
    else if (kind === "empty") nodes = emptyOverview();
    else nodes = await JOB_TABS[view.tab](currentJob());
    content.replaceChildren(...[nodes].flat());
}

function choose(key) {
    if (key !== view.key) view.run = null;
    view.key = key;
    renderSidebar();
    void guarded(renderDetail);
}

// Loads everything again and redraws; keeps the selection if it still exists.
async function refresh() {
    await loadData();
    const keys = view.mode === "accounts" ? data.accounts.map((a) => "account:" + a.name) : jobs().map((j) => j.key);
    if (view.key !== "service" && !keys.includes(view.key)) {
        view.key = keys[0] ?? null;
        view.run = null;
    }
    renderSidebar();
    await renderDetail();
}

function setMode(mode) {
    view.mode = mode;
    view.key = null;
    view.tab = "overview";
    void guarded(refresh);
}

// A card with a big state icon, a title, a subtitle and actions, like DSM's Cloud Sync.
function headCard(cls, title, subtitle, actions) {
    return el(
        "section",
        { class: "card head" },
        el("div", { class: "state-icon " + cls, "aria-hidden": "true", text: STATE_ICON[cls] || "" }),
        el("div", { class: "head-text" }, el("h2", { text: title }), el("div", { class: "muted", text: subtitle }), el("div", { class: "head-actions" }, actions))
    );
}

const STATE_ICON = { ok: "✓", warn: "!", bad: "!", busy: "↻", idle: "–" };

function infoCard(title, rows) {
    return el(
        "section",
        { class: "card" },
        el("h3", { text: title }),
        el(
            "dl",
            { class: "info" },
            rows.filter(Boolean).flatMap(([label, value]) => [el("dt", { text: label }), el("dd", {}, value)])
        )
    );
}

// --- menus -----------------------------------------------------------------------------

let openMenu = null;

function closeMenu() {
    if (openMenu) openMenu.remove();
    openMenu = null;
}

// A drop-down below `anchor`; items are [label, action, class].
function showMenu(anchor, items) {
    closeMenu();
    const menu = el(
        "div",
        { class: "menu", role: "menu" },
        items.map(([label, action, cls]) =>
            el("button", {
                role: "menuitem",
                class: cls,
                text: label,
                onclick: (event) => {
                    event.stopPropagation();
                    closeMenu();
                    void action();
                },
            })
        )
    );
    const box = anchor.getBoundingClientRect();
    menu.style.left = box.left + "px";
    menu.style.top = box.bottom + 2 + "px";
    document.body.append(menu);
    openMenu = menu;
}

function menuButton(label, items) {
    return el("button", {
        class: "menu-button",
        text: label + " ▾",
        onclick: (event) => {
            event.stopPropagation();
            if (openMenu) closeMenu();
            else showMenu(event.currentTarget, items);
        },
    });
}

// --- job: overview ---------------------------------------------------------------------

function runJob(job, options) {
    return guarded(async () => {
        if (job.kind === "photos") await api("photos.run", { name: job.name, ...options }, true);
        else await api("pairs.run", { name: job.name, ...options }, true);
        showMessage(t("status:run_queued", job.name));
        await refresh();
    });
}

function cancelRun(job, running) {
    const label = job.kind === "photos" ? t("event:src_photos", job.name) : job.name;
    const cancel = () =>
        guarded(async () => {
            const result = await api("status.cancel", { name: job.run }, true);
            showMessage(t(result.cancelled === "queued" ? "status:cancel_dequeued" : "status:cancel_requested", label));
            await refresh();
        });
    if (running) confirmDialog(t("status:cancel_confirm", label), t("status:cancel"), cancel);
    else void cancel();
}

function jobActions(job) {
    const daemon = data.status?.daemon || {};
    const running = daemon.running === job.run;
    const busy = running || job.item.pending;
    const run = job.item.last_run;
    const photosBlocked = job.kind === "photos" && job.item.job?.result === "blocked";
    const blocked = !busy && ((job.kind === "pair" && run?.blocked) || photosBlocked);
    const confirmText = photosBlocked ? t("status:approve_confirm_photos", job.name) : t("status:approve_confirm", job.name, run?.blocked);
    const items = busy
        ? [[t("status:cancel"), () => cancelRun(job, running), "danger"]]
        : [
              [job.kind === "photos" ? t("status:backup_now") : t("status:run_now"), () => runJob(job, {})],
              [t("status:dry_run"), () => runJob(job, { dry_run: "true" })],
          ];
    const approve = blocked
        ? el("button", {
              class: "danger",
              text: t("status:approve_deletes"),
              onclick: () =>
                  confirmDialog(confirmText, t("status:approve_deletes"), () =>
                      runJob(job, { allow_deletes: "true" })
                  ),
          })
        : null;
    const primary = busy
        ? null
        : el("button", { class: "primary", text: items[0][0], onclick: () => void items[0][1]() });
    return [primary, menuButton(t("ui:manage"), items), approve];
}

function scheduleText(job) {
    const own = job.config?.interval_minutes;
    const interval = own || data.status?.interval_minutes;
    return interval ? t(own ? "ui:schedule_own" : "ui:schedule_default", interval) : "–";
}

function accountText(name) {
    const account = data.accounts.find((a) => a.name === name);
    return account ? account.name + " (" + account.apple_id + ")" : name;
}

function lastRunText(job) {
    const status = job.item.job;
    const finished = job.kind === "pair" ? job.item.last_run?.finished_at ?? status?.finished_at : status?.finished_at;
    return finished ? formatTime(finished) : t("common:never");
}

function noticesCard(notices, source) {
    if (!notices.length) return null;
    return el(
        "section",
        { class: "card" },
        el(
            "div",
            { class: "card-head" },
            el("h3", { text: t("status:notices") }),
            el("button", {
                text: t("status:clear_notices"),
                title: t("status:clear_notices_hint"),
                onclick: () => void clearNotices(source),
            })
        ),
        eventTable(notices, !source)
    );
}

// Hides the notices of one job (or all); the event log keeps them.
function clearNotices(source) {
    return guarded(async () => {
        await api("status.clear_notices", source ? { source: source } : {}, true);
        await refresh();
    });
}

function jobOverview(job) {
    const state = jobState(job);
    const c = job.config || {};
    const next = job.item.job?.next_due;
    const rows = [
        [t("ui:kind"), kindText(job)],
        [t("ui:account"), accountText(job.item.account)],
        job.kind === "photos" ? [t("photos:target"), c.target] : [t("pair:local"), c.local],
        job.kind === "photos"
            ? [t("photos:libraries"), (c.libraries || []).map((lib) => t("photos:lib_" + lib)).join(", ")]
            : remoteRow(c),
        [t("ui:schedule"), scheduleText(job)],
        [t("status:last_run"), lastRunText(job)],
        [t("ui:next_run"), next ? formatTime(next) : "–"],
    ];
    const notices = (data.status?.notices || []).filter((n) => n.source === job.key);
    return [
        headCard(state.cls, job.name, state.text, jobActions(job)),
        progressCard(job.item.progress),
        noticesCard(notices, job.key),
        infoCard(t("ui:details"), rows),
    ].filter(Boolean);
}

// What the running job does right now: its phase, how far it is, its speed and what it
// last logged (e.g. a wait for Apple), so a long run never looks stuck.
function progressCard(p) {
    if (!p || !p.phase) return null;
    const now = Date.now() / 1000;
    const known = p.total !== null && p.total !== undefined;
    const bar = known ? el("progress", { max: Math.max(p.total, 1), value: Math.min(p.done, p.total) }) : el("progress", {});
    let amount = known ? t("progress:of", formatNumber(p.done), formatNumber(p.total)) : t("progress:count", formatNumber(p.done));
    if (known && p.total) amount += " (" + Math.floor((Math.min(p.done, p.total) / p.total) * 100) + " %)";
    const rows = [
        [t("progress:phase"), t("progress:phase_" + p.phase)],
        [t("progress:amount"), amount],
        p.failed ? [t("progress:failed"), formatNumber(p.failed)] : null,
        p.bytes ? [t("progress:bytes"), p.total_bytes ? t("progress:of", formatBytes(p.bytes), formatBytes(p.total_bytes)) : formatBytes(p.bytes)] : null,
        p.byte_rate ? [t("progress:speed"), formatBytes(p.byte_rate) + "/s" + (p.rate ? " · " + t("progress:per_minute", formatNumber(Math.round(p.rate * 60))) : "")] : p.rate ? [t("progress:speed"), t("progress:per_minute", formatNumber(Math.round(p.rate * 60)))] : null,
        p.eta_seconds !== null && p.eta_seconds !== undefined ? [t("progress:eta"), t("progress:eta_value", formatDuration(p.eta_seconds))] : null,
        [t("progress:elapsed"), formatDuration(now - p.started_at) + " · " + t("progress:phase_since", formatDuration(now - p.phase_started_at))],
        p.current ? [t("progress:current"), p.current] : null,
        p.activity ? [t("progress:activity"), p.activity + " (" + t("progress:ago", formatDuration(now - p.activity_at)) + ")"] : null,
        p.waiting_until && p.waiting_until > now ? [t("progress:waiting"), t("progress:waiting_value", formatDuration(p.waiting_until - now))] : null,
    ];
    return el(
        "section",
        { class: "card progress-card" },
        el("h3", { text: t("progress:title") }),
        bar,
        el(
            "dl",
            { class: "info" },
            rows.filter(Boolean).flatMap(([label, value]) => [el("dt", { text: label }), el("dd", {}, value)])
        )
    );
}

// --- job: runs -------------------------------------------------------------------------

const RUN_BADGE = {
    sync_finished: "ok",
    sync_failed: "bad",
    config_error: "bad",
    delete_brake: "warn",
    sync_cancelled: "warn",
    sync_stopped: "idle",
    sync_postponed: "idle",
};

function runState(run, running) {
    if (!run.result) return running ? { cls: "busy", text: t("ui:running") } : { cls: "idle", text: t("run:unknown") };
    return { cls: RUN_BADGE[run.result] || "bad", text: t("run:" + run.result) };
}

function runBadge(run, running) {
    const state = runState(run, running);
    return badge(state.cls, state.text);
}

function countsText(counts) {
    const parts = Object.entries(counts || {}).map(([kind, n]) => {
        const key = "count:" + kind;
        const number = Number(n).toLocaleString(language() === "ger" ? "de-DE" : "en-GB");
        return strings[key] ? t(key, number) : number + " " + kind;
    });
    return parts.length ? parts.join(", ") : t("run:nothing");
}

async function jobRuns(job) {
    const history = await api("log.history", { source: job.key, limit: 50 });
    const runs = history.runs;
    if (!runs.length) return el("p", { class: "muted", text: t("run:none") });
    const running = data.status?.daemon?.running === job.run;
    const runKey = (run) => run.started_at ?? run.finished_at;
    const index = Math.max(0, runs.findIndex((run) => runKey(run) === view.run));
    const list = el(
        "ul",
        { class: "run-list" },
        runs.map((run, i) =>
            el(
                "li",
                {},
                el(
                    "button",
                    {
                        class: "run-item",
                        "aria-current": i === index ? "true" : null,
                        onclick: () => {
                            view.run = runKey(run);
                            void guarded(renderDetail);
                        },
                    },
                    el("span", { class: "dot " + runState(run, running && i === 0).cls }),
                    el(
                        "span",
                        { class: "side-text" },
                        el("span", { class: "nowrap", text: formatTime(runKey(run)) }),
                        el("span", { class: "muted small", text: runState(run, running && i === 0).text + (run.dry_run ? " · " + t("log:dry") : "") })
                    )
                )
            )
        )
    );
    return el("div", { class: "runs" }, list, runDetail(runs[index], running && index === 0));
}

function runDetail(run, running) {
    const end = run.finished_at ?? (running ? Date.now() / 1000 : null);
    const rows = [
        [t("run:result"), runBadge(run, running)],
        [t("log:started"), run.started_at ? formatTime(run.started_at) : t("run:unknown")],
        [t("run:finished"), run.finished_at ? formatTime(run.finished_at) : "–"],
        run.started_at && end ? [t("log:duration"), formatDuration(end - run.started_at)] : null,
        [t("run:kind"), run.dry_run ? t("log:dry") : t("run:real")],
        [t("run:counts"), run.counts ? countsText(run.counts) : "–"],
        run.message && !/^exit code \d+$/.test(run.message) ? [t("run:message"), run.message] : null,
    ];
    return el(
        "div",
        { class: "run-detail" },
        infoCard(t("run:title"), rows),
        el("section", { class: "card" }, el("h3", { text: t("run:events") }),
            run.events.length ? eventTable(run.events, false) : el("p", { class: "muted", text: t("run:no_events") }))
    );
}

// --- job: settings ---------------------------------------------------------------------

function onOff(value) {
    return value ? t("ui:yes") : t("ui:no");
}

function jobSettings(job) {
    const c = job.config;
    if (!c) return el("p", { class: "muted", text: t("common:loading") });
    const rows =
        job.kind === "photos"
            ? [
                  [t("photos:account"), accountText(c.account)],
                  [t("photos:target"), c.target],
                  [t("photos:libraries"), c.libraries.map((lib) => t("photos:lib_" + lib)).join(", ")],
                  ...PHOTO_SWITCHES.map((key) => [t("photos:" + key), onOff(c[key])]),
                  [t("photos:source_folders"), onOff(c.source_folders)],
                  ...PHOTO_FOLDERS.map((key) => [t("photos:" + key), c[key]]),
                  [t("ui:schedule"), scheduleText(job)],
              ]
            : [
                  [t("pair:provider"), providerText(c)],
                  [t("pair:account"), accountText(c.account)],
                  [t("pair:local"), c.local],
                  [t("pair:remote"), c.remote],
                  [t("pair:mode"), t("mode:" + c.mode)],
                  [t("pair:exclude"), c.exclude.length ? c.exclude.join(", ") : "–"],
                  [t("pair:brake_ratio"), Math.round(c.max_delete_ratio * 100) + " %"],
                  [t("pair:brake_count"), String(c.max_delete_count)],
                  [t("ui:schedule"), scheduleText(job)],
              ];
    const edit = () => (job.kind === "photos" ? void guarded(() => photosDialog(c)) : pairDialog(c));
    const remove = () =>
        confirmDialog(t(job.kind === "photos" ? "photos:delete_confirm" : "pair:delete_confirm", job.name), t("common:delete"), async () => {
            await api(job.kind === "photos" ? "photos.delete" : "pairs.delete", { name: job.name }, true);
            await refresh();
        });
    return [
        el("div", { class: "toolbar" }, el("button", { class: "primary", text: t("common:edit"), onclick: edit }),
            el("button", { class: "danger", text: t("common:delete"), onclick: remove })),
        infoCard(t("ui:tab_settings"), rows),
        job.kind === "photos" ? el("p", { class: "muted", text: t(c.mirror_deletes ? "photos:mirror_on" : "photos:only_download") }) : null,
    ].filter(Boolean);
}

// --- logs ------------------------------------------------------------------------------

let eventLevel = "info";

function levelSelect() {
    const choice = el(
        "select",
        {
            onchange: (event) => {
                eventLevel = event.target.value;
                void guarded(renderDetail);
            },
        },
        ["info", "warning", "error"].map((level) =>
            el("option", { value: level, selected: level === eventLevel, text: t("log:level_" + level) })
        )
    );
    return el("div", { class: "toolbar" }, el("label", { class: "inline" }, t("log:level"), choice),
        el("button", { text: t("common:refresh"), onclick: () => void guarded(refresh) }));
}

async function jobLog(job) {
    const events = await api("log.events", { limit: 200, level: eventLevel, source: job.key });
    return [levelSelect(), eventTable(events.events, false)];
}

const JOB_TABS = { overview: jobOverview, runs: jobRuns, settings: jobSettings, log: jobLog };

// --- service ---------------------------------------------------------------------------

function daemonState(daemon) {
    if (daemon.running) {
        const job = daemon.running.startsWith("photos:") ? t("event:src_photos", daemon.running.slice(7)) : daemon.running;
        return { cls: "busy", text: t("status:daemon_running", job) };
    }
    return Object.keys(daemon).length ? { cls: "ok", text: t("status:daemon_idle") } : { cls: "warn", text: t("status:daemon_unknown") };
}

function serviceOverview() {
    const status = data.status || {};
    const state = daemonState(status.daemon || {});
    const change = el("button", { text: t("schedule:change"), onclick: () => void guarded(scheduleDialog) });
    const rows = [
        [t("ui:version"), status.version || "–"],
        [t("ui:api_user"), status.api_user || "?"],
        [t("ui:schedule"), status.interval_minutes ? t("schedule:every_long", status.interval_minutes) : "–"],
        [t("ui:jobs"), String(jobs().length)],
    ];
    return [
        headCard(state.cls, t("nav:service_title"), state.text, [change]),
        noticesCard(status.notices || [], null),
        infoCard(t("ui:details"), rows),
    ].filter(Boolean);
}

async function serviceLog() {
    const [events, log] = await Promise.all([
        api("log.events", { limit: 200, level: eventLevel }),
        api("log.tail", { lines: 300 }),
    ]);
    const pre = el("pre", { class: "log", text: log.lines.join("\n") });
    setTimeout(() => (pre.scrollTop = pre.scrollHeight), 0);
    return [levelSelect(), eventTable(events.events, true), el("h3", { text: t("log:lines") }), pre];
}

// --- schedule --------------------------------------------------------------------------

async function loadSettings() {
    settingsCache = await api("settings.get");
    return settingsCache;
}

function intervalInput(name, value, s, required) {
    return el("input", {
        name: name,
        type: "number",
        min: s.min_interval_minutes,
        max: s.max_interval_minutes,
        step: 1,
        value: value || "",
        required: required,
        placeholder: required ? null : t("schedule:use_default", s.interval_minutes),
    });
}

// Interval field of a pair or photos dialog; empty means the global interval.
function jobIntervalField(value) {
    const s = settingsCache;
    return field(t("schedule:job_interval"), intervalInput("interval", value, s, false),
        t("schedule:job_hint", s.interval_minutes, s.min_interval_minutes));
}

function jobInterval(value) {
    return value === "" || value === undefined ? null : Number(value);
}

async function scheduleDialog() {
    const s = await loadSettings();
    const fields = [
        field(t("schedule:interval"), intervalInput("interval", s.interval_minutes, s, true),
            t("schedule:hint", s.min_interval_minutes, s.max_interval_minutes)),
    ];
    openDialog(t("schedule:title"), fields, t("common:save"), async (v) => {
        await api("settings.update", { interval_minutes: v.interval }, true);
        await loadSettings();
        showMessage(t("schedule:saved", v.interval));
        await refresh();
    });
}

// --- pair dialog -----------------------------------------------------------------------

function pairDialog(pair) {
    const editing = Boolean(pair);
    const p = pair || {
        name: "",
        account: accountsCache.length ? accountsCache[0].name : "",
        local: "",
        remote: "",
        mode: "upload",
        remote_provider: "icloud_drive",
        exclude: [],
        max_delete_ratio: 0.1,
        max_delete_count: 50,
        interval_minutes: null,
    };
    const accountOptions = accountsCache.map((a) => [a.name, a.name + " (" + a.apple_id + ")"]);
    if (p.account && !accountOptions.some(([name]) => name === p.account)) accountOptions.push([p.account, p.account]);
    const fields = [
        field(t("pair:name"), el("input", { name: "name", value: p.name, required: true, readonly: editing })),
        field(
            t("pair:provider"),
            select(
                "remote_provider",
                [
                    ["icloud_drive", t("pair:provider_icloud")],
                    ["folder", t("pair:provider_folder")],
                ],
                p.remote_provider
            )
        ),
        field(t("pair:account"), select("account", accountOptions, p.account)),
        folderField(t("pair:local"), "local", p.local, t("pair:local_hint")),
        field(t("pair:remote"), el("input", { name: "remote", value: p.remote, required: true }), t("pair:remote_hint")),
        field(
            t("pair:mode"),
            select(
                "mode",
                ["upload", "download", "bidirectional"].map((m) => [m, t("mode:" + m)]),
                p.mode
            )
        ),
        field(
            t("pair:exclude"),
            el("textarea", { name: "exclude", text: p.exclude.join("\n") }),
            t("pair:exclude_hint")
        ),
        el(
            "div",
            { class: "row" },
            field(
                t("pair:brake_ratio"),
                el("input", {
                    name: "ratio",
                    type: "number",
                    min: 0,
                    max: 100,
                    step: 1,
                    value: Math.round(p.max_delete_ratio * 100),
                })
            ),
            field(
                t("pair:brake_count"),
                el("input", { name: "count", type: "number", min: 0, step: 1, value: p.max_delete_count })
            )
        ),
        jobIntervalField(p.interval_minutes),
        editing ? el("p", { class: "hint", text: t("pair:reset_hint") }) : null,
    ];
    openDialog(t(editing ? "pair:edit_title" : "pair:add_title"), fields, t("common:save"), async (v) => {
        const pair = {
            name: v.name,
            account: v.account || "default",
            local: v.local,
            remote: v.remote,
            mode: v.mode,
            remote_provider: v.remote_provider,
            exclude: v.exclude.split("\n").map((s) => s.trim()).filter(Boolean),
            max_delete_ratio: Number(v.ratio) / 100,
            max_delete_count: Number(v.count),
            interval_minutes: jobInterval(v.interval),
        };
        await api(editing ? "pairs.update" : "pairs.create", { pair: JSON.stringify(pair) }, true);
        view.key = "pair:" + pair.name;
        await refresh();
    });
}

// --- photos dialog ---------------------------------------------------------------------

const LIBRARIES = ["personal", "shared", "shared_albums"];
const PHOTO_SWITCHES = ["albums", "favorites", "videos", "live_videos", "raw_alternative", "edited", "xmp", "hidden", "mirror_deletes"];
const PHOTO_FOLDERS = ["folder_scheme", "shared_folder", "albums_folder", "favorites_folder", "hidden_folder", "shared_albums_folder"];
let photoDefaults = null;

function checkbox(name, label, checked) {
    return el("label", { class: "check" }, el("input", { type: "checkbox", name: name, checked: checked }), label);
}

async function photosDialog(job) {
    if (photoDefaults === null) photoDefaults = await api("photos.defaults");
    const editing = Boolean(job);
    const j = job || { name: "", account: accountsCache.length ? accountsCache[0].name : "", target: "", ...photoDefaults };
    const accountOptions = accountsCache.map((a) => [a.name, a.name + " (" + a.apple_id + ")"]);
    const fields = [
        field(t("photos:name"), el("input", { name: "name", value: j.name, required: true, readonly: editing })),
        field(t("photos:account"), select("account", accountOptions, j.account)),
        folderField(t("photos:target"), "target", j.target, t("photos:target_hint")),
        el("fieldset", {}, el("legend", { text: t("photos:libraries") }),
            LIBRARIES.map((lib) => checkbox("lib_" + lib, t("photos:lib_" + lib), j.libraries.includes(lib)))),
        el("fieldset", {}, el("legend", { text: t("photos:options") }),
            PHOTO_SWITCHES.map((key) => checkbox(key, t("photos:" + key), j[key])),
            el("p", { class: "hint", text: t("photos:mirror_deletes_hint") })),
        el("fieldset", {}, el("legend", { text: t("photos:folders") }),
            checkbox("source_folders", t("photos:source_folders"), j.source_folders),
            el("p", { class: "hint", text: t("photos:source_folders_hint") }),
            PHOTO_FOLDERS.map((key) =>
                field(t("photos:" + key), el("input", { name: key, value: j[key], required: true }),
                    key === "folder_scheme" ? t("photos:folder_scheme_hint") : null))),
        jobIntervalField(j.interval_minutes),
    ];
    openDialog(t(editing ? "photos:edit_title" : "photos:add_title"), fields, t("common:save"), async (v) => {
        const photos = { name: v.name, account: v.account, target: v.target, interval_minutes: jobInterval(v.interval) };
        photos.libraries = LIBRARIES.filter((lib) => v["lib_" + lib] === "on");
        for (const key of PHOTO_SWITCHES) photos[key] = v[key] === "on";
        photos.source_folders = v.source_folders === "on";
        for (const key of PHOTO_FOLDERS) photos[key] = v[key];
        await api(editing ? "photos.update" : "photos.create", { photos: JSON.stringify(photos) }, true);
        view.key = "photos:" + photos.name;
        await refresh();
    });
}

// --- Apple IDs -------------------------------------------------------------------------

function accountOverview(account) {
    if (!account) return emptyOverview();
    const actions = [
        account.state === "needs_2fa"
            ? el("button", { class: "primary", text: t("account:verify"), onclick: () => codeDialog(account.name) })
            : null,
        // Also offered while signed in: a session can look valid here and still fail at
        // Apple. Signing in again replaces it; pairs, backups and their state stay.
        account.state === "unavailable"
            ? null
            : el("button", {
                  class: account.state === "signed_out" ? "primary" : null,
                  text: t("account:login"),
                  title: t("account:login_hint"),
                  onclick: () => loginDialog(account),
              }),
        el("button", {
            class: "danger",
            text: t("common:delete"),
            onclick: () =>
                confirmDialog(t("account:delete_confirm", account.name), t("common:delete"), async () => {
                    await api("accounts.remove", { name: account.name }, true);
                    await refresh();
                }),
        }),
    ];
    const rows = [
        [t("account:apple_id"), account.apple_id],
        [t("account:state"), badge(STATE_BADGE[account.state], t("state:" + account.state))],
        [t("account:pairs"), account.pairs.concat(account.photos || []).join(", ") || "–"],
    ];
    return [headCard(STATE_BADGE[account.state], account.name, t("state:" + account.state), actions), infoCard(t("ui:details"), rows)];
}

function afterLogin(result) {
    // The dialog closes after this returns; open the code dialog on the next tick.
    if (result.state === "needs_2fa") setTimeout(() => codeDialog(result.name), 0);
    view.key = "account:" + result.name;
    void guarded(refresh);
}

function accountDialog() {
    const fields = [
        field(t("account:name"), el("input", { name: "name", required: true })),
        field(t("account:apple_id"), el("input", { name: "apple_id", type: "email", required: true })),
        field(
            t("account:password"),
            el("input", { name: "password", type: "password", required: true, autocomplete: "off" }),
            t("account:password_hint")
        ),
    ];
    openDialog(t("account:add_title"), fields, t("common:add"), async (v) => {
        afterLogin(await api("accounts.add", v, true));
    });
}

function loginDialog(account) {
    const fields = [
        field(t("account:apple_id"), el("input", { value: account.apple_id, readonly: true })),
        field(
            t("account:password"),
            el("input", { name: "password", type: "password", required: true, autocomplete: "off" }),
            t("account:password_hint")
        ),
        el("p", { class: "muted", text: t("account:login_keeps") }),
    ];
    openDialog(t("account:login_title", account.name), fields, t("account:login"), async (v) => {
        afterLogin(await api("accounts.login", { name: account.name, password: v.password }, true));
    });
}

function codeDialog(name) {
    const fields = [
        el("p", { text: t("account:code_label") }),
        field(
            t("account:code"),
            el("input", {
                name: "code",
                inputmode: "numeric",
                autocomplete: "one-time-code",
                pattern: "[0-9 ]{6,7}",
                maxlength: 7,
                required: true,
            })
        ),
    ];
    openDialog(t("account:code_title"), fields, t("account:verify"), async (v) => {
        await api("accounts.verify", { name: name, code: v.code }, true);
        await refresh();
    });
}

// --- empty state and "+" ---------------------------------------------------------------

function emptyOverview() {
    if (view.mode === "accounts")
        return el("section", { class: "card empty" }, el("p", { text: t("ui:no_accounts") }),
            el("button", { class: "primary", text: t("account:add_title"), onclick: accountDialog }));
    return el(
        "section",
        { class: "card empty" },
        el("p", { text: t("ui:no_jobs") }),
        el("div", { class: "toolbar" },
            el("button", { class: "primary", text: t("pair:add_title"), onclick: () => pairDialog(null) }),
            el("button", { class: "primary", text: t("photos:add_title"), onclick: () => void guarded(() => photosDialog(null)) }))
    );
}

function addClicked(event) {
    event.stopPropagation();
    if (view.mode === "accounts") {
        accountDialog();
        return;
    }
    if (openMenu) {
        closeMenu();
        return;
    }
    showMenu(event.currentTarget, [
        [t("pair:add_title"), () => pairDialog(null)],
        [t("photos:add_title"), () => guarded(() => photosDialog(null))],
    ]);
}

// --- start -----------------------------------------------------------------------------

async function autoRefresh() {
    const live = view.tab === "overview" || view.tab === "runs";
    if (live && !openMenu && !document.querySelector("dialog[open]")) await guarded(refresh);
    setTimeout(autoRefresh, data.status?.daemon?.running ? REFRESH_RUNNING_MS : REFRESH_MS);
}

async function start() {
    document.documentElement.lang = language() === "ger" ? "de" : "en";
    await loadStrings();
    for (const node of document.querySelectorAll("[data-t]")) node.textContent = t(node.dataset.t);
    for (const button of document.querySelectorAll("[data-mode]")) {
        button.addEventListener("click", () => setMode(button.dataset.mode));
    }
    document.getElementById("add").addEventListener("click", addClicked);
    document.getElementById("add").title = t("ui:add_hint");
    document.getElementById("service").addEventListener("click", () => {
        view.mode = "jobs";
        choose("service");
    });
    document.addEventListener("click", closeMenu);
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeMenu();
    });
    await guarded(refresh);
    setTimeout(autoRefresh, data.status?.daemon?.running ? REFRESH_RUNNING_MS : REFRESH_MS);
}

await start();
