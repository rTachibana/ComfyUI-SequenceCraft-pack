import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

// ── Show Text / Debug / Preset Slot: in-node preview widget ──────────────

const _PREVIEW_NODES = new Set(["SCShowText", "SCRandomPromptsDebug", "SCImagePresetSlot"]);

app.registerExtension({
    name: "SequenceCraft.ShowText",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (!_PREVIEW_NODES.has(nodeData.name)) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, []);
            const { widget } = ComfyWidgets["STRING"](
                this,
                "preview_text",
                ["STRING", { multiline: true }],
                app
            );
            widget.label = "Preview";
            widget.element.readOnly = true;
            widget.options.read_only = true;
            widget.serialize = false;
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            onExecuted?.apply(this, [message]);
            const w = this.widgets?.find((w) => w.name === "preview_text");
            if (!w) return;
            const text = message.text ?? "";
            w.value = Array.isArray(text) ? text.join("\n\n") : text;
        };
    },
});

// ── Random Prompts: line numbers in the template textarea ─────────────────

const _LINE_NUMBER_NODES = new Set(["SCRandomPrompts", "SCRandomPromptsDebug"]);

function setupLineNumbers(textarea) {
    const parent = textarea.parentNode;
    if (!parent || parent.dataset.lineNumbersAttached) return;
    parent.dataset.lineNumbersAttached = "1";

    const wrapper = document.createElement("div");
    wrapper.style.cssText = "display:flex;width:100%;height:100%;";

    const gutter = document.createElement("div");
    gutter.style.cssText = [
        "padding: 4px 6px 4px 4px",
        "color: #666",
        "font-family: monospace",
        "font-size: inherit",
        "line-height: inherit",
        "white-space: pre",
        "user-select: none",
        "text-align: right",
        "min-width: 2.2em",
        "overflow: hidden",
        "flex-shrink: 0",
        "border-right: 1px solid #444",
        "background: rgba(0,0,0,0.25)",
    ].join(";");

    parent.insertBefore(wrapper, textarea);
    wrapper.appendChild(gutter);
    wrapper.appendChild(textarea);

    textarea.style.flexGrow = "1";
    textarea.style.width = "0";
    textarea.style.resize = "none";

    function update() {
        const count = textarea.value.split("\n").length;
        gutter.textContent = Array.from({ length: count }, (_, i) => i + 1).join("\n");
        gutter.style.fontSize = getComputedStyle(textarea).fontSize;
        gutter.style.lineHeight = getComputedStyle(textarea).lineHeight;
    }

    textarea.addEventListener("input", update);
    textarea.addEventListener("scroll", () => { gutter.scrollTop = textarea.scrollTop; });
    update();
}

app.registerExtension({
    name: "SequenceCraft.LineNumbers",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (!_LINE_NUMBER_NODES.has(nodeData.name)) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, []);
            const w = this.widgets?.find((w) => w.name === "text");
            const el = w?.element ?? w?.inputEl;
            if (el) {
                setupLineNumbers(el);
            } else if (w) {
                requestAnimationFrame(() => {
                    const delayed = w.element ?? w.inputEl;
                    if (delayed) setupLineNumbers(delayed);
                });
            }
        };
    },
});

// ── Image Preset Sequence Picker: step counter display + reset button ─────

app.registerExtension({
    name: "SequenceCraft.SequencePicker",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "SCImagePresetSequencePicker") return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, []);

            this._scCounterWidget = this.addWidget(
                "text", "step", "step: —", () => {}
            );
            this._scCounterWidget.readonly = true;

            this.addWidget("button", "Reset Counter", null, () => {
                fetch("/sequencecraft/reset", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ node_id: String(this.id) }),
                })
                    .then((r) => r.json())
                    .then((d) => {
                        if (this._scCounterWidget) {
                            this._scCounterWidget.value = `step: ${d.counter}`;
                        }
                    });
            });

            this._scRefreshCounter();
        };

        nodeType.prototype._scRefreshCounter = function () {
            fetch(`/sequencecraft/counter?node_id=${this.id}`)
                .then((r) => r.json())
                .then((d) => {
                    if (this._scCounterWidget) {
                        this._scCounterWidget.value = `step: ${d.counter}`;
                    }
                })
                .catch(() => {});
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            onExecuted?.apply(this, [message]);
            this._scRefreshCounter();
        };
    },
});
