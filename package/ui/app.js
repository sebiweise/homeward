// Homeward page shown inside the DSM app window (see desktop.js).
// Talks to api.cgi; all strings come from texts/<lang>/strings.
"use strict";

const API = "api.cgi";
const REFRESH_MS = 10000;
const strings = {};
let tokenRequest = null;
let activeTab = "status";
let refreshTimer = null;
let accountsCache = [];

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
    return s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s";
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

function eventRow(event) {
    return el(
        "tr",
        {},
        el("td", { text: formatTime(event.time) }),
        el("td", {}, el("span", { class: "badge " + LEVEL_BADGE[event.level], text: t("event:level_" + event.level) })),
        el("td", { text: eventSource(event.source) }),
        el("td", {}, eventText(event), event.message && strings["event:" + event.code]
            ? el("div", { class: "muted", text: event.message }) : null)
    );
}

function eventTable(events) {
    return table([t("log:time"), "", t("log:source"), t("log:event")], events.map(eventRow));
}

// --- status tab ------------------------------------------------------------------------

function resultBadge(pair) {
    const run = pair.last_run;
    if (pair.pending) return el("span", { class: "badge idle", text: t("status:queued") });
    if (!run) return el("span", { class: "muted", text: t("common:never") });
    if (run.blocked) return el("span", { class: "badge warn", text: t("status:result_blocked") });
    if (run.errors) return el("span", { class: "badge bad", text: t("status:result_errors", run.errors) });
    if (run.dry_run) return el("span", { class: "badge idle", text: t("status:result_dry", run.actions) });
    return el("span", { class: "badge ok", text: t("status:result_ok", run.actions) });
}

const JOB_BADGE = { ok: "ok", failed: "bad", blocked: "warn", config_error: "bad", killed: "idle", waiting: "idle" };

function jobBadge(item) {
    if (item.pending) return el("span", { class: "badge idle", text: t("status:queued") });
    const job = item.job;
    if (job?.started_at && !job.finished_at)
        return el("span", { class: "badge idle", text: t("status:running_since", formatTime(job.started_at)) });
    if (!job?.result) return el("span", { class: "muted", text: t("common:never") });
    return el("span", { class: "badge " + (JOB_BADGE[job.result] || "bad"), text: t("status:job_" + job.result) });
}

function nextRun(item) {
    const due = item.job?.next_due;
    return due ? el("div", { class: "muted", text: t("status:next_run", formatTime(due)) }) : null;
}

function runPhotos(job, options) {
    return guarded(async () => {
        await api("photos.run", { name: job.name, ...options }, true);
        showMessage(t("status:run_queued", job.name));
        await renderStatus();
    });
}

function runPair(pair, options) {
    return guarded(async () => {
        await api("pairs.run", { name: pair.name, ...options }, true);
        showMessage(t("status:run_queued", pair.name));
        await renderStatus();
    });
}

function daemonText(daemon) {
    if (daemon.running) {
        const job = daemon.running.startsWith("photos:")
            ? t("event:src_photos", daemon.running.slice(7))
            : daemon.running;
        return t("status:daemon_running", job);
    }
    return Object.keys(daemon).length ? t("status:daemon_idle") : t("status:daemon_unknown");
}

async function renderStatus() {
    const data = await api("status.get");
    document.getElementById("version").textContent = t("common:version", data.version);
    const daemon = data.daemon || {};
    document.getElementById("daemon").textContent = daemonText(daemon);
    const service = [t("status:api_user", data.api_user || "?")];
    if (daemon.interval_minutes) service.push(t("status:interval", daemon.interval_minutes));
    document.getElementById("service").textContent = service.join(" · ");
    const notices = data.notices || [];
    document
        .getElementById("notices")
        .replaceChildren(notices.length ? el("div", {}, el("h3", { text: t("status:notices") }), eventTable(notices)) : "");
    const rows = data.pairs.map((pair) => {
        const run = pair.last_run;
        const blocked = run?.blocked && !pair.pending;
        return el(
            "tr",
            {},
            el("td", { text: pair.name }),
            el("td", { text: t("mode:" + pair.mode) }),
            el("td", {}, run ? formatTime(run.finished_at) : t("common:never"), nextRun(pair)),
            el("td", {}, resultBadge(pair), blocked ? el("div", { class: "muted", text: run.blocked }) : null),
            el(
                "td",
                { class: "actions" },
                blocked
                    ? el("button", {
                          class: "danger",
                          text: t("status:approve_deletes"),
                          onclick: () =>
                              confirmDialog(
                                  t("status:approve_confirm", pair.name, run.blocked),
                                  t("status:approve_deletes"),
                                  () => runPair(pair, { allow_deletes: "true" })
                              ),
                      })
                    : null,
                el("button", { text: t("status:dry_run"), onclick: () => runPair(pair, { dry_run: "true" }) }),
                el("button", { class: "primary", text: t("status:run_now"), onclick: () => runPair(pair, {}) })
            )
        );
    });
    for (const job of data.photos || []) {
        const finished = job.job?.finished_at;
        rows.push(
            el(
                "tr",
                {},
                el("td", { text: job.name }),
                el("td", { text: t("status:photos_kind") }),
                el("td", {}, finished ? formatTime(finished) : t("common:never"), nextRun(job)),
                el("td", {}, jobBadge(job)),
                el(
                    "td",
                    { class: "actions" },
                    el("button", { text: t("status:dry_run"), onclick: () => runPhotos(job, { dry_run: "true" }) }),
                    el("button", { class: "primary", text: t("status:backup_now"), onclick: () => runPhotos(job, {}) })
                )
            )
        );
    }
    document
        .getElementById("status-table")
        .replaceChildren(
            table([t("status:pair"), t("status:mode"), t("status:last_run"), t("status:result"), ""], rows)
        );
}

// --- pairs tab -------------------------------------------------------------------------

async function renderPairs() {
    const [pairs, accounts] = await Promise.all([api("pairs.list"), api("accounts.list")]);
    accountsCache = accounts.accounts;
    const rows = pairs.pairs.map((pair) =>
        el(
            "tr",
            {},
            el("td", { text: pair.name }),
            el("td", { text: pair.remote_provider === "folder" ? t("pair:provider_folder") : pair.account }),
            el("td", { text: pair.local }),
            el("td", { text: pair.remote }),
            el("td", { text: t("mode:" + pair.mode) }),
            el(
                "td",
                { class: "actions" },
                el("button", { text: t("common:edit"), onclick: () => pairDialog(pair) }),
                el("button", {
                    class: "danger",
                    text: t("common:delete"),
                    onclick: () =>
                        confirmDialog(t("pair:delete_confirm", pair.name), t("common:delete"), async () => {
                            await api("pairs.delete", { name: pair.name }, true);
                            await renderPairs();
                        }),
                })
            )
        )
    );
    document
        .getElementById("pairs-table")
        .replaceChildren(
            table([t("pair:name"), t("pair:account"), t("pair:local"), t("pair:remote"), t("pair:mode"), ""], rows)
        );
}

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
        editing ? el("p", { class: "hint", text: t("pair:reset_hint") }) : null,
    ];
    openDialog(t(editing ? "pair:edit_title" : "pair:add_title"), fields, t("common:save"), async (v) => {
        const data = {
            name: v.name,
            account: v.account || "default",
            local: v.local,
            remote: v.remote,
            mode: v.mode,
            remote_provider: v.remote_provider,
            exclude: v.exclude.split("\n").map((s) => s.trim()).filter(Boolean),
            max_delete_ratio: Number(v.ratio) / 100,
            max_delete_count: Number(v.count),
        };
        await api(editing ? "pairs.update" : "pairs.create", { pair: JSON.stringify(data) }, true);
        await renderPairs();
    });
}

// --- photos tab ------------------------------------------------------------------------

const LIBRARIES = ["personal", "shared", "shared_albums"];
const PHOTO_SWITCHES = ["albums", "favorites", "videos", "live_videos", "raw_alternative", "edited", "xmp", "hidden"];
const PHOTO_FOLDERS = ["folder_scheme", "shared_folder", "albums_folder", "favorites_folder", "hidden_folder", "shared_albums_folder"];
let photoDefaults = null;

async function renderPhotos() {
    const [jobs, accounts] = await Promise.all([api("photos.list"), api("accounts.list")]);
    accountsCache = accounts.accounts;
    if (photoDefaults === null) photoDefaults = await api("photos.defaults");
    const rows = jobs.photos.map((job) =>
        el(
            "tr",
            {},
            el("td", { text: job.name }),
            el("td", { text: job.account }),
            el("td", { text: job.target }),
            el("td", { text: job.libraries.map((lib) => t("photos:lib_" + lib)).join(", ") }),
            el(
                "td",
                { class: "actions" },
                el("button", { text: t("common:edit"), onclick: () => photosDialog(job) }),
                el("button", {
                    class: "danger",
                    text: t("common:delete"),
                    onclick: () =>
                        confirmDialog(t("photos:delete_confirm", job.name), t("common:delete"), async () => {
                            await api("photos.delete", { name: job.name }, true);
                            await renderPhotos();
                        }),
                })
            )
        )
    );
    document
        .getElementById("photos-table")
        .replaceChildren(
            table([t("photos:name"), t("photos:account"), t("photos:target"), t("photos:libraries"), ""], rows)
        );
}

function checkbox(name, label, checked) {
    return el("label", { class: "check" }, el("input", { type: "checkbox", name: name, checked: checked }), label);
}

function photosDialog(job) {
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
            PHOTO_SWITCHES.map((key) => checkbox(key, t("photos:" + key), j[key]))),
        el("fieldset", {}, el("legend", { text: t("photos:folders") }),
            PHOTO_FOLDERS.map((key) =>
                field(t("photos:" + key), el("input", { name: key, value: j[key], required: true }),
                    key === "folder_scheme" ? t("photos:folder_scheme_hint") : null))),
    ];
    openDialog(t(editing ? "photos:edit_title" : "photos:add_title"), fields, t("common:save"), async (v) => {
        const data = { name: v.name, account: v.account, target: v.target };
        data.libraries = LIBRARIES.filter((lib) => v["lib_" + lib] === "on");
        for (const key of PHOTO_SWITCHES) data[key] = v[key] === "on";
        for (const key of PHOTO_FOLDERS) data[key] = v[key];
        await api(editing ? "photos.update" : "photos.create", { photos: JSON.stringify(data) }, true);
        await renderPhotos();
    });
}

// --- accounts tab ----------------------------------------------------------------------

const STATE_BADGE = { signed_in: "ok", needs_2fa: "warn", signed_out: "bad", unavailable: "idle" };

async function renderAccounts() {
    const data = await api("accounts.list");
    accountsCache = data.accounts;
    const rows = data.accounts.map((account) =>
        el(
            "tr",
            {},
            el("td", { text: account.name }),
            el("td", { text: account.apple_id }),
            el("td", {}, el("span", { class: "badge " + STATE_BADGE[account.state], text: t("state:" + account.state) })),
            el("td", { text: account.pairs.concat(account.photos || []).join(", ") }),
            el(
                "td",
                { class: "actions" },
                account.state === "needs_2fa"
                    ? el("button", { class: "primary", text: t("account:verify"), onclick: () => codeDialog(account.name) })
                    : null,
                account.state === "signed_out"
                    ? el("button", { class: "primary", text: t("account:login"), onclick: () => loginDialog(account) })
                    : null,
                el("button", {
                    class: "danger",
                    text: t("common:delete"),
                    onclick: () =>
                        confirmDialog(t("account:delete_confirm", account.name), t("common:delete"), async () => {
                            await api("accounts.remove", { name: account.name }, true);
                            await renderAccounts();
                        }),
                })
            )
        )
    );
    document
        .getElementById("accounts-table")
        .replaceChildren(
            table([t("account:name"), t("account:apple_id"), t("account:state"), t("account:pairs"), ""], rows)
        );
}

function afterLogin(result) {
    // The dialog closes after this returns; open the code dialog on the next tick.
    if (result.state === "needs_2fa") setTimeout(() => codeDialog(result.name), 0);
    void guarded(renderAccounts);
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
        await renderAccounts();
    });
}

// --- log tab ---------------------------------------------------------------------------

async function renderLog() {
    const level = document.getElementById("event-level").value;
    const [events, runs, log] = await Promise.all([
        api("log.events", { limit: 200, level: level }),
        api("log.runs", { limit: 50 }),
        api("log.tail", { lines: 300 }),
    ]);
    document.getElementById("events-table").replaceChildren(eventTable(events.events));
    const rows = runs.runs.map((run) =>
        el(
            "tr",
            {},
            el("td", { text: formatTime(run.started_at) }),
            el("td", { text: run.pair + (run.dry_run ? " (" + t("log:dry") + ")" : "") }),
            el("td", { text: formatDuration(run.finished_at - run.started_at) }),
            el("td", { text: run.actions }),
            el("td", { text: run.errors }),
            el("td", { text: run.blocked || "" })
        )
    );
    document
        .getElementById("runs-table")
        .replaceChildren(
            table([t("log:started"), t("status:pair"), t("log:duration"), t("log:actions"), t("log:errors"), ""], rows)
        );
    const pre = document.getElementById("log-lines");
    pre.textContent = log.lines.join("\n");
    pre.scrollTop = pre.scrollHeight;
}

// --- tabs ------------------------------------------------------------------------------

const RENDER = {
    status: renderStatus,
    pairs: renderPairs,
    photos: renderPhotos,
    accounts: renderAccounts,
    log: renderLog,
};

function selectTab(name) {
    activeTab = name;
    for (const button of document.querySelectorAll("[data-tab]")) {
        button.setAttribute("aria-selected", String(button.dataset.tab === name));
    }
    for (const section of document.querySelectorAll(".tab")) {
        section.hidden = section.id !== "tab-" + name;
    }
    void guarded(RENDER[name]);
    clearInterval(refreshTimer);
    if (name === "status") {
        refreshTimer = setInterval(() => {
            if (!document.querySelector("dialog[open]")) void guarded(renderStatus);
        }, REFRESH_MS);
    }
}

async function start() {
    document.documentElement.lang = language() === "ger" ? "de" : "en";
    await loadStrings();
    for (const node of document.querySelectorAll("[data-t]")) node.textContent = t(node.dataset.t);
    for (const button of document.querySelectorAll("[data-tab]")) {
        button.addEventListener("click", () => selectTab(button.dataset.tab));
    }
    document.getElementById("pair-add").addEventListener("click", () => pairDialog(null));
    document.getElementById("photos-add").addEventListener("click", () => photosDialog(null));
    document.getElementById("account-add").addEventListener("click", accountDialog);
    document.getElementById("log-refresh").addEventListener("click", () => guarded(renderLog));
    document.getElementById("event-level").addEventListener("change", () => guarded(renderLog));
    selectTab(activeTab);
}

await start();
