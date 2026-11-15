// Rename and delete dialogs for takes, shared by the status page and the trajectory page.
// Native <dialog>: focus stays inside, Escape cancels, everything works from the keyboard.
"use strict";
const QT = (() => {
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const css = `
dialog.qt { background: #0c0d11; color: #f3f4f6; border: 1px solid rgba(243,244,246,.32); padding: 0; width: min(640px, calc(100vw - 32px)); max-height: calc(100vh - 48px); }
dialog.qt::backdrop { background: rgba(6,7,9,.78); }
dialog.qt form { padding: 28px 30px 26px; display: grid; gap: 16px; }
dialog.qt h2 { font-size: 26px; line-height: 1.1; letter-spacing: -.03em; font-weight: 500; margin: 0; }
dialog.qt p { margin: 0; color: #a9acb3; font-size: 15px; }
dialog.qt .warn { color: #e8c77e; display: flex; gap: 10px; align-items: baseline; }
dialog.qt .warn::before { content: ""; width: 10px; height: 10px; border: 2px solid #e8c77e; flex: none; transform: translateY(1px); }
dialog.qt ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 12px; max-height: 36vh; overflow: auto; border-top: 1px solid rgba(243,244,246,.18); border-bottom: 1px solid rgba(243,244,246,.18); padding: 12px 0; }
dialog.qt li .t { font-size: 16px; font-weight: 500; }
dialog.qt li .f { font-family: "Martian Mono", ui-monospace, monospace; font-size: 12px; color: #a9acb3; overflow-wrap: anywhere; display: block; margin-top: 3px; }
dialog.qt label { display: grid; gap: 6px; font-size: 14px; color: #a9acb3; }
dialog.qt label.chk { display: flex; gap: 10px; align-items: center; font-size: 15px; color: #f3f4f6; }
dialog.qt input[type=text], dialog.qt textarea { font: inherit; color: #f3f4f6; background: #060709; border: 1px solid rgba(243,244,246,.36); padding: 10px 12px; width: 100%; }
dialog.qt input[type=checkbox] { width: 18px; height: 18px; accent-color: #8d90ff; flex: none; }
dialog.qt .row { display: flex; gap: 8px; align-items: center; }
dialog.qt .suffix { font-family: "Martian Mono", ui-monospace, monospace; font-size: 13px; color: #a9acb3; }
dialog.qt .msg { min-height: 1.4em; font-size: 14px; color: #f0713a; }
dialog.qt .msg.ok { color: #a9acb3; }
dialog.qt .buttons { display: flex; justify-content: flex-end; gap: 12px; margin-top: 4px; }
dialog.qt button { font: inherit; color: #f3f4f6; background: transparent; border: 1px solid rgba(243,244,246,.36); padding: 9px 20px; min-height: 42px; cursor: pointer; }
dialog.qt button.primary { background: #f3f4f6; color: #060709; border-color: #f3f4f6; font-weight: 500; }
dialog.qt button.danger { border-color: #f0713a; color: #f0713a; font-weight: 500; }
dialog.qt button:disabled { opacity: .45; cursor: default; }
dialog.qt :focus-visible { outline: 2px solid #8d90ff; outline-offset: 3px; }`;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  async function post(path, body) {
    const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Requested-With": "quest-teleop"}, body: JSON.stringify(body)});
    let d = {};
    try { d = await r.json(); } catch (e) { /* not JSON */ }
    if (!r.ok) throw new Error(d.error || `the server answered ${r.status}`);
    return d;
  }
  const label = (t) => t.title ? t.title : `Take ${t.take}`;
  const sub = (t) => `take ${t.take} of ${t.session}`;

  function open(html, onSubmit) {
    const d = document.createElement("dialog");
    d.className = "qt";
    d.innerHTML = `<form method="dialog">${html}</form>`;
    document.body.appendChild(d);
    const opener = document.activeElement;
    const key = opener && opener.dataset ? opener.dataset.k : null;      // the page may rebuild the opener meanwhile
    d.addEventListener("close", () => {
      d.remove();
      const back = opener && opener.isConnected ? opener : ((key && document.querySelector(`[data-k="${CSS.escape(key)}"]`)) || document.getElementById("recordings"));
      if (back) back.focus();
    });
    d.querySelector("form").addEventListener("submit", (e) => { e.preventDefault(); onSubmit(d); });
    d.querySelector("[data-cancel]").addEventListener("click", () => d.close());
    d.showModal();
    return d;
  }

  // takes: [{file, session, take, title, files: [...], converted: [...]}]
  function deleteDialog(takes, onDone) {
    const conv = [...new Set(takes.flatMap((t) => t.converted || []))];
    const n = takes.length;
    const d = open(`
      <h2 id="qt-h">Delete ${n === 1 ? "this take" : n + " takes"}?</h2>
      <p class="warn">This removes the files below from the disk. It cannot be undone.</p>
      <ul aria-label="Takes and files that will be deleted">${takes.map((t) => `<li><span class="t">${esc(label(t))}</span> <span style="color:#a9acb3">(${esc(sub(t))})</span>${(t.files || [t.file]).map((f) => `<span class="f">${esc(f)}</span>`).join("")}</li>`).join("")}</ul>
      ${conv.length ? `<label class="chk"><input type="checkbox" id="qt-conv"> Also delete the converted clips made from ${n === 1 ? "it" : "them"} (${esc(conv.join(", "))})</label>` : ""}
      <div class="msg" role="alert" id="qt-msg"></div>
      <div class="buttons"><button type="button" data-cancel autofocus>Cancel</button><button type="submit" class="danger">Delete ${n === 1 ? "the take" : n + " takes"}</button></div>`,
    async (dlg) => {
      const btn = dlg.querySelector("button[type=submit]");
      btn.disabled = true;
      try {
        const out = await post("/api/delete", {files: takes.map((t) => t.file), converted: !!(dlg.querySelector("#qt-conv") || {}).checked});
        if (out.refused && out.refused.length) {
          dlg.querySelector("#qt-msg").textContent = `Not deleted: ${out.refused.map((r) => `${r.file} (${r.reason})`).join("; ")}`;
          btn.disabled = false;
          if (out.deleted.length) onDone(out, true);
          return;
        }
        dlg.close();
        onDone(out, false);
      } catch (e) { dlg.querySelector("#qt-msg").textContent = "Could not delete: " + e.message; btn.disabled = false; }
    });
    d.setAttribute("aria-labelledby", "qt-h");
  }

  function checkName(v) {
    v = v.trim().replace(/\.npz$/i, "");
    if (!v) return "Enter a file name.";
    if (/[\\/]/.test(v) || v.includes("..")) return "The file name must not contain / \\ or two dots in a row.";
    if (v.length > 80) return "At most 80 characters.";
    if (/[^A-Za-z0-9 ._-]/.test(v)) return "ok:Other characters than letters, digits, space, dot, dash and underscore will be replaced by _.";
    return "";
  }

  // take: {file, session, take, title, note}
  function renameDialog(take, onDone) {
    const stem = take.file.split("/").pop().replace(/\.npz$/i, "");
    const d = open(`
      <h2 id="qt-h">Rename the take</h2>
      <p>${esc(sub(take))}<span class="f" style="font-family:'Martian Mono',monospace;font-size:12px;display:block;margin-top:4px">${esc(take.file)}</span></p>
      <label>Name shown in the list<input type="text" id="qt-title" maxlength="120" value="${esc(take.title || "")}" placeholder="Take ${esc(take.take)}" autofocus></label>
      <label>Note (optional)<textarea id="qt-note" rows="3" maxlength="2000">${esc(take.note || "")}</textarea></label>
      <label class="chk"><input type="checkbox" id="qt-file-on" aria-controls="qt-file-box"> Also rename the file on disk</label>
      <div id="qt-file-box" hidden><label>File name<span class="row"><input type="text" id="qt-file" maxlength="90" value="${esc(stem)}" aria-describedby="qt-file-msg"><span class="suffix">.npz</span></span></label></div>
      <div class="msg" id="qt-file-msg" aria-live="polite"></div>
      <div class="msg" role="alert" id="qt-msg"></div>
      <div class="buttons"><button type="button" data-cancel>Cancel</button><button type="submit" class="primary">Save</button></div>`,
    async (dlg) => {
      const on = dlg.querySelector("#qt-file-on").checked, name = dlg.querySelector("#qt-file").value;
      const problem = on ? checkName(name) : "";
      if (problem && !problem.startsWith("ok:")) { dlg.querySelector("#qt-file").focus(); return; }
      const btn = dlg.querySelector("button[type=submit]");
      btn.disabled = true;
      try {
        const out = await post("/api/rename", {file: take.file, title: dlg.querySelector("#qt-title").value, note: dlg.querySelector("#qt-note").value, rename_file: on, new_name: name});
        dlg.close();
        onDone(out.entry);
      } catch (e) { dlg.querySelector("#qt-msg").textContent = "Not renamed: " + e.message; btn.disabled = false; }
    });
    d.setAttribute("aria-labelledby", "qt-h");
    const box = d.querySelector("#qt-file-box"), fileIn = d.querySelector("#qt-file"), msg = d.querySelector("#qt-file-msg");
    const validate = () => {
      const p = d.querySelector("#qt-file-on").checked ? checkName(fileIn.value) : "";
      msg.textContent = p.replace(/^ok:/, "");
      msg.className = "msg" + (p.startsWith("ok:") ? " ok" : "");
      fileIn.setAttribute("aria-invalid", p && !p.startsWith("ok:") ? "true" : "false");
    };
    d.querySelector("#qt-file-on").addEventListener("change", (e) => { box.hidden = !e.target.checked; if (e.target.checked) fileIn.focus(); validate(); });
    fileIn.addEventListener("input", validate);
  }

  return {post, deleteDialog, renameDialog, label, sub, esc};
})();
