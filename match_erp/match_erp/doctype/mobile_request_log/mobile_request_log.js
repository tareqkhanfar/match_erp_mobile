// Desk client script for Mobile Request Log.
//
// Renders a custom HTML/CSS/JS JSON tree editor into the `json_editor`
// HTML field. It correctly handles nested objects and arrays (the old
// flattened child-table view mangled those). Editing a value/key, adding
// or deleting nodes, or changing a value's type writes the rebuilt JSON
// back into `request_body`, which the "Replay Request" button then sends.

frappe.ui.form.on("Mobile Request Log", {
	refresh(frm) {
		// ── Replay button ──────────────────────────────────────────────
		if (!frm.is_new()) {
			frm.add_custom_button(__("Replay Request"), () => {
				frappe.confirm(
					__("Re-send this request to {0} as user {1}?", [
						frm.doc.endpoint,
						frm.doc.request_user || frappe.session.user,
					]),
					() => {
						// Flush any pending editor edits into request_body and
						// save first, so the replay uses the latest payload.
						const doReplay = () => {
							frappe.dom.freeze(__("Replaying…"));
							frappe
								.call({
									method:
										"match_erp.match_erp.doctype.mobile_request_log.mobile_request_log.replay_request",
									args: { name: frm.doc.name },
								})
								.then((r) => {
									frappe.dom.unfreeze();
									const res = r.message || {};
									if (res.ok) {
										frappe.show_alert({
											message: __("Replay succeeded"),
											indicator: "green",
										});
									} else {
										frappe.msgprint({
											title: __("Replay failed"),
											message:
												res.error ||
												__("The endpoint returned an error. See Response Body."),
											indicator: "red",
										});
									}
									frm.reload_doc();
								})
								.catch(() => frappe.dom.unfreeze());
						};
						if (frm.is_dirty()) {
							frm.save().then(doReplay);
						} else {
							doReplay();
						}
					}
				);
			}).addClass("btn-primary");
		}

		if (frm.doc.status === "Failed") {
			frm.dashboard.set_headline_alert(
				__("This request failed: {0}", [frm.doc.error_message || ""]),
				"red"
			);
		}

		render_json_editor(frm);
	},

	// Re-render the tree if the raw JSON field is edited directly. Skip
	// when the editor itself wrote the value (guard set in commit()) so a
	// scalar edit doesn't rebuild the tree and steal focus mid-typing.
	request_body(frm) {
		if (frm.__mrl_internal_write) {
			frm.__mrl_internal_write = false;
			return;
		}
		render_json_editor(frm);
	},
});

// ───────────────────────────────────────────────────────────────────────────
// JSON tree editor
// ───────────────────────────────────────────────────────────────────────────
function render_json_editor(frm) {
	const wrapper = frm.get_field("json_editor")?.$wrapper;
	if (!wrapper) return;

	inject_styles();

	let data;
	try {
		data = frm.doc.request_body ? JSON.parse(frm.doc.request_body) : {};
	} catch (e) {
		wrapper.html(
			`<div class="mrl-json-error">${__(
				"Raw JSON is invalid — fix it in the Raw JSON field below."
			)}<br><small>${frappe.utils.escape_html(String(e))}</small></div>`
		);
		return;
	}

	// Wrap the root so even a primitive / array root edits uniformly.
	const model = { root: data };

	// Commit the edited model back into request_body (pretty-printed).
	// Mark the write as internal so the request_body trigger doesn't
	// re-render the tree (which would steal focus during a scalar edit).
	const commit = () => {
		frm.__mrl_internal_write = true;
		frm.set_value("request_body", JSON.stringify(model.root, null, 2));
	};

	wrapper.empty();
	const $root = $('<div class="mrl-json-editor"></div>');
	const $toolbar = $(`
		<div class="mrl-toolbar">
			<button class="btn btn-xs btn-default mrl-expand">${__("Expand all")}</button>
			<button class="btn btn-xs btn-default mrl-collapse">${__("Collapse all")}</button>
			<span class="mrl-hint">${__("Click a value to edit. Use + / × to add or remove fields.")}</span>
		</div>
	`);
	const $tree = $('<div class="mrl-tree"></div>');
	$root.append($toolbar, $tree);
	wrapper.append($root);

	// Render the root node. The root container is whatever type `data` is.
	$tree.append(
		build_node({
			parent: model,
			keyOrIndex: "root",
			isRootChild: true,
			commit,
			frm,
		})
	);

	$toolbar.find(".mrl-expand").on("click", () =>
		$tree.find(".mrl-children").show().parent().find(".mrl-caret").text("▼")
	);
	$toolbar.find(".mrl-collapse").on("click", () =>
		$tree.find(".mrl-children").hide().parent().find(".mrl-caret").text("▶")
	);
}

// Build the DOM for a single node (the value held at parent[keyOrIndex]).
function build_node({ parent, keyOrIndex, isRootChild, commit, frm }) {
	const value = parent[keyOrIndex];
	const type = json_type(value);
	const isContainer = type === "object" || type === "array";

	const $node = $('<div class="mrl-node"></div>');
	const $row = $('<div class="mrl-row"></div>');
	$node.append($row);

	// Caret for containers.
	if (isContainer) {
		const $caret = $('<span class="mrl-caret">▼</span>');
		$row.append($caret);
		$caret.on("click", () => {
			const $kids = $node.children(".mrl-children");
			const showing = $kids.is(":visible");
			$kids.toggle(!showing);
			$caret.text(showing ? "▶" : "▼");
		});
	} else {
		$row.append('<span class="mrl-caret-spacer"></span>');
	}

	// Key / index label. Root child shows "(root)". Object keys are
	// editable; array indices are fixed.
	const parentIsArray = Array.isArray(parent);
	if (isRootChild) {
		$row.append('<span class="mrl-key mrl-key-root">(root)</span>');
	} else if (parentIsArray) {
		$row.append(`<span class="mrl-index">[${keyOrIndex}]</span>`);
	} else {
		const $key = $(
			`<span class="mrl-key" contenteditable="true">${frappe.utils.escape_html(
				String(keyOrIndex)
			)}</span>`
		);
		$key.on("blur", function () {
			const newKey = $(this).text().trim();
			if (newKey && newKey !== keyOrIndex) {
				rename_key(parent, keyOrIndex, newKey);
				commit();
				render_json_editor(frm); // re-render to rebind handlers
			}
		});
		$key.on("keydown", function (e) {
			if (e.key === "Enter") {
				e.preventDefault();
				$(this).blur();
			}
		});
		$row.append($key);
	}

	if (isContainer) {
		// Container summary + add button.
		const count = type === "array" ? value.length : Object.keys(value).length;
		$row.append(
			`<span class="mrl-type-badge mrl-badge-${type}">${type} · ${count}</span>`
		);
		const $add = $('<button class="mrl-btn-add" title="Add field">+</button>');
		$add.on("click", () => {
			if (type === "array") {
				value.push("");
			} else {
				let k = "new_field";
				let n = 1;
				while (Object.prototype.hasOwnProperty.call(value, k)) {
					k = `new_field_${n++}`;
				}
				value[k] = "";
			}
			commit();
			render_json_editor(frm);
		});
		$row.append($add);
	} else {
		// Scalar: a type selector + an editable value.
		const $typeSel = $(`
			<select class="mrl-type-select">
				<option value="string">str</option>
				<option value="number">num</option>
				<option value="boolean">bool</option>
				<option value="null">null</option>
			</select>
		`);
		$typeSel.val(type);
		$typeSel.on("change", function () {
			parent[keyOrIndex] = coerce_to_type(parent[keyOrIndex], $(this).val());
			commit();
			render_json_editor(frm);
		});
		$row.append($typeSel);

		const $val = build_value_editor(value, type, (newVal) => {
			parent[keyOrIndex] = newVal;
			commit();
		});
		$row.append($val);
	}

	// Delete button (not for root).
	if (!isRootChild) {
		const $del = $('<button class="mrl-btn-del" title="Remove">×</button>');
		$del.on("click", () => {
			if (parentIsArray) {
				parent.splice(keyOrIndex, 1);
			} else {
				delete parent[keyOrIndex];
			}
			commit();
			render_json_editor(frm);
		});
		$row.append($del);
	}

	// Children for containers.
	if (isContainer) {
		const $children = $('<div class="mrl-children"></div>');
		const keys = type === "array" ? value.map((_, i) => i) : Object.keys(value);
		keys.forEach((k) => {
			$children.append(
				build_node({ parent: value, keyOrIndex: k, commit, frm })
			);
		});
		$node.append($children);
	}

	return $node;
}

// A small inline editor appropriate to the scalar type.
function build_value_editor(value, type, onChange) {
	if (type === "boolean") {
		const $sel = $(`
			<select class="mrl-value mrl-value-bool">
				<option value="true">true</option>
				<option value="false">false</option>
			</select>
		`);
		$sel.val(String(value));
		$sel.on("change", () => onChange($sel.val() === "true"));
		return $sel;
	}
	if (type === "null") {
		return $('<span class="mrl-value mrl-value-null">null</span>');
	}
	// string / number → contenteditable span.
	const $val = $(
		`<span class="mrl-value mrl-value-${type}" contenteditable="true">${frappe.utils.escape_html(
			String(value)
		)}</span>`
	);
	$val.on("blur", function () {
		const text = $(this).text();
		if (type === "number") {
			const n = Number(text);
			onChange(Number.isNaN(n) ? 0 : n);
		} else {
			onChange(text);
		}
	});
	$val.on("keydown", function (e) {
		if (e.key === "Enter") {
			e.preventDefault();
			$(this).blur();
		}
	});
	return $val;
}

// ── helpers ────────────────────────────────────────────────────────────────
function json_type(v) {
	if (v === null) return "null";
	if (Array.isArray(v)) return "array";
	return typeof v; // object | string | number | boolean
}

function coerce_to_type(value, target) {
	switch (target) {
		case "string":
			return value === null || value === undefined ? "" : String(value);
		case "number": {
			const n = Number(value);
			return Number.isNaN(n) ? 0 : n;
		}
		case "boolean":
			return value === true || value === "true" || value === 1;
		case "null":
			return null;
		default:
			return value;
	}
}

// Rename an object key while preserving insertion order.
function rename_key(obj, oldKey, newKey) {
	if (Array.isArray(obj)) return;
	const rebuilt = {};
	for (const k of Object.keys(obj)) {
		rebuilt[k === oldKey ? newKey : k] = obj[k];
	}
	// Mutate in place so the caller's reference stays valid.
	for (const k of Object.keys(obj)) delete obj[k];
	Object.assign(obj, rebuilt);
}

function inject_styles() {
	if (document.getElementById("mrl-json-editor-styles")) return;
	const css = `
	.mrl-json-editor { border: 1px solid var(--border-color); border-radius: 8px;
		background: var(--card-bg); overflow: hidden; }
	.mrl-toolbar { display: flex; align-items: center; gap: 8px;
		padding: 8px 10px; border-bottom: 1px solid var(--border-color);
		background: var(--subtle-fg, #f7f7f7); }
	.mrl-toolbar .mrl-hint { color: var(--text-muted); font-size: 11px; margin-left:auto; }
	.mrl-tree { padding: 8px 10px; font-family: var(--font-stack-mono, monospace);
		font-size: 12.5px; max-height: 520px; overflow: auto; }
	.mrl-node { }
	.mrl-row { display: flex; align-items: center; gap: 6px; padding: 2px 0;
		white-space: nowrap; }
	.mrl-row:hover { background: var(--highlight-color, #f0f4ff); border-radius: 4px; }
	.mrl-caret { cursor: pointer; width: 14px; display: inline-block;
		color: var(--text-muted); user-select: none; text-align:center; }
	.mrl-caret-spacer { width: 14px; display: inline-block; }
	.mrl-children { margin-left: 18px; border-left: 1px dashed var(--border-color);
		padding-left: 8px; }
	.mrl-key { color: #8250df; font-weight: 600; padding: 1px 4px; border-radius: 3px; }
	.mrl-key[contenteditable="true"]:focus { outline: none; background: #fff3cd;
		box-shadow: 0 0 0 1px #e0a800; }
	.mrl-key-root { color: var(--text-muted); font-style: italic; }
	.mrl-index { color: var(--text-muted); }
	.mrl-value { padding: 1px 6px; border-radius: 3px; min-width: 24px;
		display: inline-block; }
	.mrl-value-string { color: #0a7d22; background: #eafbef; }
	.mrl-value-number { color: #b35900; background: #fff6e6; }
	.mrl-value-null { color: var(--text-muted); font-style: italic; }
	.mrl-value-bool { color: #0b62d6; }
	.mrl-value[contenteditable="true"]:focus { outline: none;
		box-shadow: 0 0 0 1px #4dabf7; background: #fff; }
	.mrl-type-select, .mrl-value-bool { font-size: 11px; border: 1px solid var(--border-color);
		border-radius: 4px; background: var(--control-bg, #fff); padding: 0 2px; }
	.mrl-type-badge { font-size: 10.5px; padding: 1px 6px; border-radius: 10px;
		font-weight: 600; }
	.mrl-badge-object { background: #e7e3ff; color: #6f42c1; }
	.mrl-badge-array { background: #e6f2ff; color: #1366d6; }
	.mrl-btn-add, .mrl-btn-del { border: none; border-radius: 4px; cursor: pointer;
		width: 20px; height: 20px; line-height: 18px; font-weight: 700; font-size: 14px; }
	.mrl-btn-add { background: #e6f7ec; color: #1a7f37; }
	.mrl-btn-add:hover { background: #c8eed5; }
	.mrl-btn-del { background: #fde8e8; color: #cf222e; }
	.mrl-btn-del:hover { background: #fbc9c9; }
	.mrl-json-error { padding: 14px; color: #cf222e; background: #fde8e8;
		border-radius: 8px; }
	`;
	const style = document.createElement("style");
	style.id = "mrl-json-editor-styles";
	style.textContent = css;
	document.head.appendChild(style);
}
