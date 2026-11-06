import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ── Wildcard editor: a sidebar tab that edits the files in wildcards/ ─────
// Server side: /sequencecraft/wildcard-file(s) (wildcard_store.py).
// Edits are kept per file until saved, so switching files loses nothing.
// A save sends the version the text was based on; if the file changed on
// disk meanwhile the server answers 409 and the panel offers both ways out.

const CSS = `
.scw { display:flex; flex-direction:column; height:100%; gap:6px; padding:8px; box-sizing:border-box;
       color:var(--fg-color); font-size:13px; }
.scw input, .scw textarea { background:var(--comfy-input-bg); color:var(--input-text);
       border:1px solid var(--border-color); border-radius:4px; padding:4px 6px; box-sizing:border-box; }
.scw button { background:var(--comfy-input-bg); color:var(--input-text); border:1px solid var(--border-color);
       border-radius:4px; padding:3px 8px; cursor:pointer; white-space:nowrap; }
.scw button:hover:not(:disabled) { filter:brightness(1.25); }
.scw button:disabled { opacity:.45; cursor:default; }
.scw button.danger { color:var(--error-text, #f66); }
.scw .row { display:flex; gap:6px; align-items:center; }
.scw .row > input { flex:1; min-width:0; }
.scw .list { flex:0 0 auto; max-height:32%; overflow-y:auto; border:1px solid var(--border-color); border-radius:4px; }
.scw .item { padding:3px 8px; cursor:pointer; display:flex; gap:6px; align-items:baseline; }
.scw .item:hover { background:rgba(127,127,127,.15); }
.scw .item.active { background:rgba(127,127,127,.3); }
.scw .item .p { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.scw .item .n { color:var(--descrip-text); font-size:11px; }
.scw .item .bad { color:var(--error-text, #f66); }
.scw .ed { flex:1; display:flex; min-height:120px; border:1px solid var(--border-color); border-radius:4px; overflow:hidden; }
.scw .gut { padding:4px 6px 4px 4px; color:var(--descrip-text); font-family:monospace; white-space:pre;
       text-align:right; min-width:2.4em; overflow:hidden; user-select:none; background:rgba(0,0,0,.2); }
.scw .ed textarea { flex:1; border:0; border-radius:0; resize:none; font-family:monospace; font-size:12px;
       line-height:1.45; white-space:pre; overflow:auto; outline:none; }
.scw .gut { font-size:12px; line-height:1.45; }
.scw .title { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; }
.scw .names { display:flex; flex-wrap:wrap; gap:4px; }
.scw .chip { font-family:monospace; font-size:11px; padding:1px 6px; border-radius:9px; cursor:pointer;
       border:1px solid var(--border-color); }
.scw .chip:hover { filter:brightness(1.3); }
.scw .msg { font-size:12px; white-space:pre-wrap; word-break:break-word; }
.scw .msg.err { color:var(--error-text, #f66); }
.scw .msg.ok { color:#6c6; }
.scw .preview { font-family:monospace; font-size:11px; color:var(--descrip-text); white-space:pre-wrap;
       max-height:7em; overflow-y:auto; }
.scw .hidden { display:none !important; }
`;

function h(tag, props = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
        if (k === "class") el.className = v;
        else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
        else if (v !== undefined) el[k] = v;
    }
    el.append(...kids.filter((k) => k != null));
    return el;
}

async function call(route, body) {
    const opts = body === undefined ? {} : {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    };
    const r = await api.fetchApi(route, opts);
    const raw = await r.text();
    let data;
    try { data = JSON.parse(raw); } catch { data = { error: raw || r.statusText }; }
    return { status: r.status, ok: r.ok, data };
}

class WildcardEditor {
    constructor() {
        this.files = [];          // [{path, size, version, entries, error?}]
        this.drafts = new Map();  // path -> {text, base, saved}  (saved: the text as on disk at base)
        this.current = null;      // open path
        this.mode = null;         // null | "new" | "rename" | "delete" | "conflict"
        this.conflict = null;     // {current}  version now on disk
        this.build();
    }

    build() {
        const s = (this.ui = {});
        s.filter = h("input", { placeholder: "絞り込み", oninput: () => this.renderList() });
        s.list = h("div", { class: "list" });
        s.title = h("div", { class: "title", textContent: "ファイルを選んでください" });
        s.names = h("div", { class: "names" });
        s.gutter = h("div", { class: "gut", textContent: "1" });
        s.text = h("textarea", {
            spellcheck: false, disabled: true,
            oninput: () => this.onInput(),
            onscroll: () => { s.gutter.scrollTop = s.text.scrollTop; },
            onkeydown: (e) => {
                if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
                    e.preventDefault(); e.stopPropagation(); this.save();
                }
            },
        });
        s.save = h("button", { textContent: "保存", title: "Ctrl+S", onclick: () => this.save() });
        s.revert = h("button", { textContent: "元に戻す", onclick: () => this.revert() });
        s.preview = h("button", { textContent: "試し引き", onclick: () => this.tryDraw() });
        s.rename = h("button", { textContent: "名前変更", onclick: () => this.setMode("rename") });
        s.del = h("button", { textContent: "削除", class: "danger", onclick: () => this.setMode("delete") });

        // inline forms (no native dialogs)
        s.formInput = h("input", { onkeydown: (e) => {
            if (e.key === "Enter") this.submitForm();
            if (e.key === "Escape") this.setMode(null);
        } });
        s.formOk = h("button", { onclick: () => this.submitForm() });
        s.formCancel = h("button", { textContent: "やめる", onclick: () => this.setMode(null) });
        s.form = h("div", { class: "row hidden" }, s.formInput, s.formOk, s.formCancel);

        s.confirmText = h("span", { class: "msg", style: "flex:1" });
        s.confirmA = h("button", { onclick: () => this.confirm("a") });
        s.confirmB = h("button", { onclick: () => this.confirm("b") });
        s.confirmC = h("button", { textContent: "やめる", onclick: () => this.setMode(null) });
        s.confirm = h("div", { class: "row hidden", style: "flex-wrap:wrap" }, s.confirmText, s.confirmA, s.confirmB, s.confirmC);

        s.msg = h("div", { class: "msg" });
        s.out = h("div", { class: "preview hidden" });

        this.root = h("div", { class: "scw" },
            h("style", { textContent: CSS }),
            h("div", { class: "row" }, s.filter,
                h("button", { textContent: "新規", onclick: () => this.setMode("new") }),
                h("button", { textContent: "↻", title: "一覧を読み直す", onclick: () => this.refresh() })),
            s.list,
            h("div", { class: "row" }, s.title),
            s.names,
            h("div", { class: "ed" }, s.gutter, s.text),
            h("div", { class: "row", style: "flex-wrap:wrap" }, s.save, s.revert, s.preview, s.rename, s.del),
            s.form, s.confirm, s.msg, s.out,
        );
        this.updateButtons();
    }

    mount(el) {
        el.replaceChildren(this.root);
        if (!this.loaded) { this.loaded = true; this.refresh(); }
    }

    say(text, kind = "") { this.ui.msg.textContent = text; this.ui.msg.className = `msg ${kind}`; }

    dirty(path) {
        const d = this.drafts.get(path);
        return !!d && d.text !== d.saved;
    }

    async refresh() {
        const r = await call("/sequencecraft/wildcard-files");
        if (!r.ok) return this.say(`一覧を読めませんでした: ${r.data.error}`, "err");
        this.files = r.data.files;
        this.renderList();
    }

    renderList() {
        const q = this.ui.filter.value.trim().toLowerCase();
        const items = this.files
            .filter((f) => !q || f.path.toLowerCase().includes(q)
                || f.entries.some((e) => e.name.toLowerCase().includes(q)))
            .map((f) => {
                const count = f.entries.reduce((n, e) => n + e.count, 0);
                const info = f.error ? h("span", { class: "bad", textContent: "エラー", title: f.error })
                    : h("span", { class: "n", textContent: f.entries.length > 1 ? `${f.entries.length}件 / ${count}` : `${count}` });
                return h("div", {
                    class: `item${f.path === this.current ? " active" : ""}`,
                    title: f.entries.map((e) => `__${e.name}__`).join("\n"),
                    onclick: () => this.open(f.path),
                }, h("span", { class: "p", textContent: (this.dirty(f.path) ? "● " : "") + f.path }), info);
            });
        this.ui.list.replaceChildren(...items);
    }

    async open(path, { reload = false } = {}) {
        this.setMode(null);
        if (reload || !this.drafts.has(path)) {
            const r = await call(`/sequencecraft/wildcard-file?path=${encodeURIComponent(path)}`);
            if (!r.ok) return this.say(`開けませんでした: ${r.data.error}`, "err");
            this.drafts.set(path, { text: r.data.text, base: r.data.version, saved: r.data.text });
            this.setEntries(path, r.data.entries, r.data.error);
        }
        this.current = path;
        const d = this.drafts.get(path);
        this.ui.text.disabled = false;
        this.ui.text.value = d.text;
        this.ui.text.scrollTop = 0;
        this.ui.title.textContent = path;
        this.ui.out.classList.add("hidden");
        const f = this.files.find((f) => f.path === path);
        this.say(f?.error ? f.error : "", f?.error ? "err" : "");
        this.renderNames();
        this.updateGutter();
        this.updateButtons();
        this.renderList();
    }

    setEntries(path, entries, error) {
        const f = this.files.find((f) => f.path === path);
        if (f) { f.entries = entries; if (error) f.error = error; else delete f.error; }
    }

    renderNames() {
        const f = this.files.find((f) => f.path === this.current);
        this.ui.names.replaceChildren(...(f?.entries ?? []).map((e) => h("span", {
            class: "chip", textContent: `__${e.name}__`, title: `${e.count} 個 — クリックでコピー`,
            onclick: () => navigator.clipboard?.writeText(`__${e.name}__`).then(
                () => this.say(`__${e.name}__ をコピーしました`, "ok"), () => {}),
        })));
    }

    onInput() {
        const d = this.drafts.get(this.current);
        if (!d) return;
        const wasDirty = d.text !== d.saved;
        d.text = this.ui.text.value;
        this.updateGutter();
        this.updateButtons();
        if (wasDirty !== (d.text !== d.saved)) this.renderList();
    }

    updateGutter() {
        const n = this.ui.text.value.split("\n").length;
        this.ui.gutter.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
        this.ui.gutter.scrollTop = this.ui.text.scrollTop;
    }

    updateButtons() {
        const open = !!this.current, dirty = open && this.dirty(this.current);
        this.ui.save.disabled = !dirty;
        this.ui.revert.disabled = !dirty;
        for (const b of [this.ui.preview, this.ui.rename, this.ui.del]) b.disabled = !open;
    }

    async save({ force = false } = {}) {
        const path = this.current, d = this.drafts.get(path);
        if (!d || (!force && d.text === d.saved)) return;
        const base = force ? this.conflict?.current : d.base;
        const r = await call("/sequencecraft/wildcard-file", { path, text: d.text, base: base ?? undefined });
        if (r.status === 409) {
            this.conflict = { current: r.data.current };
            return this.setMode("conflict");
        }
        if (!r.ok) return this.say(`保存できませんでした: ${r.data.error}`, "err");
        d.base = r.data.version; d.saved = d.text;
        this.setEntries(path, r.data.entries);
        const f = this.files.find((f) => f.path === path);
        if (f) f.version = r.data.version;
        this.setMode(null);
        this.say(`保存しました（前の内容は ${path}.bak）`, "ok");
        this.renderNames(); this.updateButtons(); this.renderList();
    }

    revert() {
        const d = this.drafts.get(this.current);
        if (!d) return;
        d.text = d.saved;
        this.ui.text.value = d.text;
        this.say("");
        this.updateGutter(); this.updateButtons(); this.renderList();
    }

    async tryDraw() {
        const f = this.files.find((f) => f.path === this.current);
        const names = f?.entries ?? [];
        if (!names.length) return this.say("このファイルにはまだ使える名前がありません（保存すると反映されます）", "err");
        const text = names.map((e) => `__${e.name}__`).join("\n");
        const seed = Math.floor(Math.random() * 2 ** 31);
        const r = await call("/sequencecraft/expand", { text, seed, count: 5 });
        if (!r.ok) return this.say(`試し引きできませんでした: ${r.data.error}`, "err");
        const label = names.length > 1 ? names.map((e) => e.name).join(" / ") + "\n" : "";
        this.ui.out.textContent = label + r.data.results.join(names.length > 1 ? "\n—\n" : "\n");
        this.ui.out.classList.remove("hidden");
        this.say(this.dirty(this.current) ? "未保存の変更は試し引きに入りません" : "");
    }

    setMode(mode) {
        this.mode = mode;
        const s = this.ui;
        s.form.classList.toggle("hidden", mode !== "new" && mode !== "rename");
        s.confirm.classList.toggle("hidden", mode !== "delete" && mode !== "conflict");
        if (mode === "new") {
            s.formInput.value = ""; s.formInput.placeholder = "例: pose/arms.txt（.yaml も可）";
            s.formOk.textContent = "作成";
        } else if (mode === "rename") {
            s.formInput.value = this.current; s.formInput.placeholder = "新しいパス";
            s.formOk.textContent = "変更";
        } else if (mode === "delete") {
            s.confirmText.textContent = `${this.current} を削除しますか？（${this.current}.bak として残ります）`;
            s.confirmA.textContent = "削除する"; s.confirmA.className = "danger";
            s.confirmB.classList.add("hidden");
        } else if (mode === "conflict") {
            s.confirmText.textContent = "読み込んだあとにファイルが変更されています。";
            s.confirmA.textContent = "こちらで上書き"; s.confirmA.className = "danger";
            s.confirmB.textContent = "向こうを読み込む（編集は破棄）";
            s.confirmB.classList.remove("hidden");
        }
        if (mode === "new" || mode === "rename") { s.formInput.focus(); s.formInput.select(); }
    }

    async submitForm() {
        let value = this.ui.formInput.value.trim();
        if (!value) return;
        if (!/\.(txt|ya?ml|json)$/i.test(value)) value += ".txt";
        if (this.mode === "new") {
            const r = await call("/sequencecraft/wildcard-file", { path: value, text: "" });
            if (r.status === 409) return this.say(`${value} はもうあります`, "err");
            if (!r.ok) return this.say(`作成できませんでした: ${r.data.error}`, "err");
            await this.refresh();
            await this.open(r.data.path);
            this.ui.text.focus();
        } else if (this.mode === "rename") {
            const from = this.current, d = this.drafts.get(from);
            if (value === from) return this.setMode(null);
            if (this.dirty(from)) return this.say("先に保存するか元に戻してください", "err");
            const r = await call("/sequencecraft/wildcard-file/rename", { path: from, to: value, base: d.base });
            if (r.status === 409) return this.say(r.data.current ? "ファイルが変更されています。↻ で読み直してください" : `${value} はもうあります`, "err");
            if (!r.ok) return this.say(`名前を変えられませんでした: ${r.data.error}`, "err");
            this.drafts.delete(from);
            this.drafts.set(r.data.path, { ...d, base: r.data.version });
            this.current = null;
            await this.refresh();
            await this.open(r.data.path);
            this.say(`${from} → ${r.data.path}`, "ok");
        }
    }

    async confirm(which) {
        if (this.mode === "delete" && which === "a") {
            const path = this.current, d = this.drafts.get(path);
            const r = await call("/sequencecraft/wildcard-file/delete", { path, base: d.base });
            if (r.status === 409) return this.setMode(null), this.say("ファイルが変更されています。↻ で読み直してください", "err");
            if (!r.ok) return this.say(`削除できませんでした: ${r.data.error}`, "err");
            this.drafts.delete(path);
            this.current = null;
            this.setMode(null);
            Object.assign(this.ui.text, { value: "", disabled: true });
            this.ui.title.textContent = "ファイルを選んでください";
            this.ui.names.replaceChildren();
            this.ui.out.classList.add("hidden");
            this.updateGutter(); this.updateButtons();
            await this.refresh();
            this.say(`削除しました（${path}.bak に残っています）`, "ok");
        } else if (this.mode === "conflict") {
            if (which === "a") await this.save({ force: true });
            else { await this.open(this.current, { reload: true }); this.say("読み込み直しました", "ok"); }
        }
    }
}

let editor = null;

app.registerExtension({
    name: "SequenceCraft.WildcardEditor",
    setup() {
        const tabs = app.extensionManager?.registerSidebarTab;
        if (!tabs) {
            console.warn("[SequenceCraft] sidebar tabs are not available; wildcard editor disabled");
            return;
        }
        app.extensionManager.registerSidebarTab({
            id: "sequencecraft-wildcards",
            icon: "pi pi-list",
            title: "Wildcards",
            tooltip: "SequenceCraft ワイルドカード編集",
            type: "custom",
            render: (el) => {
                editor ??= new WildcardEditor();
                editor.mount(el);
            },
        });
        window.addEventListener("beforeunload", (e) => {
            if (editor && [...editor.drafts.keys()].some((p) => editor.dirty(p))) e.preventDefault();
        });
    },
});
