// 公共：API、人员选择、状态徽标
async function api(path, body) {
  const opt = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) };
  const res = await fetch("/api/" + path, opt);
  const j = await res.json();
  if (!j.ok) throw new Error(j.error);
  return j.data;
}
function toast(msg, isErr) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}
async function call(path, body, after) {
  try { await api(path, body); toast("操作成功"); if (after) await after(); }
  catch (e) { toast(e.message, true); }
}
const STATUS_TAG = {
  DRAFT: ["gray", "待装盒"], PACKED: ["amber", "已封盒待签"], ISSUED: ["ok", "已签发"],
  VOID: ["red", "已作废"], PENDING: ["gray", "待扫码"], SCANNED_OK: ["green", "扫码通过"],
  BLOCKED: ["red", "阻断"], TAKEN: ["green", "已签收"], REFUSED: ["red", "拒服"],
  DEFERRED: ["amber", "暂缓"], MISSED: ["red", "漏签"], ACTIVE: ["green", "生效"],
  SUPERSEDED: ["gray", "已被新版替代"], STOPPED: ["red", "已停用"],
  IN: ["ok", "在院"], OUT: ["red", "外出"],
};
function tag(status) {
  const [cls, text] = STATUS_TAG[status] || ["gray", status];
  return `<span class="tag ${cls}">${text}</span>`;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function staffSelect(role, id, includeAll) {
  const s = (window.__STATE ||= await api("state"));
  let list = s.staff.filter(x => !role || x.role === role);
  return `<select id="${id}">` +
    (includeAll ? `<option value="">全部床位</option>` : `<option value="">选择人员…</option>`) +
    list.map(x => `<option value="${x.id}">${esc(x.name)}（${s.roles[x.role]} ${x.code}）</option>`)
      .join("") + "</select>";
}
