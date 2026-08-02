const $ = (id) => document.getElementById(id);
const icons = { 建筑: "🏰", 英雄: "👑", 军队: "⚔️", 法术: "🧪", 陷阱: "💣", 宠物: "🐾", 其他: "🔨" };

const example = {
  village: { id: "home-1", name: "我的家乡", player_tag: "#ABC123" },
  upgrades: [
    { id: "builder-1", name: "大本营", category: "建筑", level_from: 14, level_to: 15, duration_seconds: 3600 },
    { id: "hero-1", name: "弓箭女皇", category: "英雄", level_from: 78, level_to: 79, duration_seconds: 7200 }
  ]
};

function headers(json = false) {
  return json ? { "Content-Type": "application/json" } : {};
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...headers(Boolean(options.body)), ...(options.headers || {}) } });
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function remaining(seconds) {
  if (seconds <= 0) return "已完成";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return d ? `${d}天 ${h}小时` : [h, m, s].map(v => String(v).padStart(2, "0")).join(":");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));
}

function upgradeHtml(u) {
  const done = u.status === "completed" || u.remaining_seconds <= 0;
  const finish = new Date(u.ends_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  return `<div class="upgrade">
    <div class="icon">${icons[u.category] || icons.其他}</div>
      <div><div class="upgrade-name">${escapeHtml(u.name)} ${escapeHtml(u.level_text)}</div><div class="meta">${escapeHtml(u.category)}</div></div>
    <div class="time ${done ? "done" : ""}">${remaining(u.remaining_seconds)}<small>${finish}</small></div>
    <button class="delete" data-delete="${escapeHtml(u.id)}" title="删除">✕</button>
  </div>`;
}

async function load() {
  try {
    const [health, data] = await Promise.all([api("/health"), api("/api/v1/villages")]);
    $("wecomBadge").textContent = health.wecom_configured ? "● 企业微信已配置" : "○ 企业微信未配置";
    $("wecomBadge").className = health.wecom_configured ? "badge ok" : "badge";
    const villages = data.villages;
    const all = villages.flatMap(v => v.upgrades);
    $("villageCount").textContent = villages.length;
    $("activeCount").textContent = all.filter(u => u.status === "upgrading" && u.remaining_seconds > 0).length;
    $("doneCount").textContent = all.filter(u => u.status === "completed" || u.remaining_seconds <= 0).length;
    $("villages").innerHTML = villages.length ? villages.map(v => `
      <div class="village">
        <div class="village-head"><h3>${escapeHtml(v.name)}</h3><span>${escapeHtml(v.player_tag || v.id)} · ${v.upgrades.length} 项</span></div>
        ${v.upgrades.length ? v.upgrades.map(upgradeHtml).join("") : '<div class="empty">暂无升级项目</div>'}
      </div>`).join("") : '<div class="empty">尚未导入村庄数据</div>';
  } catch (error) {
    feedback(error.message, true);
  }
}

function feedback(message, error = false) {
  $("feedback").textContent = message;
  $("feedback").className = error ? "feedback error" : "feedback";
}

function settingsFeedback(message, error = false) {
  $("settingsFeedback").textContent = message;
  $("settingsFeedback").className = error ? "feedback error" : "feedback";
}

async function loadSettings() {
  try {
    const data = await api("/api/v1/settings/wecom");
    const s = data.settings;
    $("corpId").value = s.corp_id || "";
    $("agentId").value = s.agent_id || "";
    $("wecomSecret").value = "";
    $("wecomSecret").placeholder = s.secret_set ? "已保存，留空表示不修改" : "请输入应用 Secret";
    $("toUser").value = s.to_user || "@all";
    $("apiBase").value = s.api_base || "https://qyapi.weixin.qq.com";
    $("outboundProxy").value = s.outbound_proxy || "";
    $("callbackUrl").value = s.callback_url || "";
    $("callbackToken").value = "";
    $("callbackToken").placeholder = s.callback_token_set ? "已保存，留空表示不修改" : "输入企业微信回调 Token";
    $("callbackAesKey").value = "";
    $("callbackAesKey").placeholder = s.callback_aes_key_set ? "已保存，留空表示不修改" : "输入 43 位 EncodingAESKey";
  } catch (error) {
    settingsFeedback(error.message, true);
  }
}

$("saveSettingsBtn").onclick = async () => {
  try {
    $("saveSettingsBtn").disabled = true;
    const payload = {
      corp_id: $("corpId").value,
      agent_id: $("agentId").value,
      secret: $("wecomSecret").value,
      to_user: $("toUser").value,
      api_base: $("apiBase").value,
      outbound_proxy: $("outboundProxy").value,
      callback_token: $("callbackToken").value,
      callback_aes_key: $("callbackAesKey").value
    };
    await api("/api/v1/settings/wecom", { method: "PUT", body: JSON.stringify(payload) });
    settingsFeedback("企业微信设置已保存");
    await Promise.all([loadSettings(), load()]);
  } catch (error) {
    settingsFeedback(error.message, true);
  } finally {
    $("saveSettingsBtn").disabled = false;
  }
};

$("exampleBtn").onclick = () => { $("jsonInput").value = JSON.stringify(example, null, 2); };
$("refreshBtn").onclick = load;
$("importBtn").onclick = async () => {
  try {
    const payload = JSON.parse($("jsonInput").value);
    $("importBtn").disabled = true;
    const result = await api("/api/v1/import", { method: "POST", body: JSON.stringify(payload) });
    feedback(`已识别并导入 ${result.imported} 个进行中的升级项目`);
    await load();
  } catch (error) {
    feedback(error.message, true);
  } finally {
    $("importBtn").disabled = false;
  }
};
$("testBtn").onclick = async () => {
  try {
    $("testBtn").disabled = true;
    await api("/api/v1/notifications/test", { method: "POST" });
    settingsFeedback("测试通知已发送，请查看企业微信");
  } catch (error) {
    settingsFeedback(error.message, true);
  } finally {
    $("testBtn").disabled = false;
  }
};
$("menuBtn").onclick = async () => {
  try {
    $("menuBtn").disabled = true;
    await api("/api/v1/settings/wecom/menu", { method: "POST" });
    settingsFeedback("企业微信菜单已创建/刷新，请重新进入 COC 应用查看");
  } catch (error) {
    settingsFeedback(error.message, true);
  } finally {
    $("menuBtn").disabled = false;
  }
};
$("villages").onclick = async (event) => {
  const id = event.target.dataset.delete;
  if (!id || !confirm("删除这个升级提醒？")) return;
  try { await api(`/api/v1/upgrades/${encodeURIComponent(id)}`, { method: "DELETE" }); await load(); }
  catch (error) { feedback(error.message, true); }
};

Promise.all([loadSettings(), load()]);
setInterval(load, 30000);
