const feed = document.getElementById("feed");
const tasksEl = document.getElementById("tasks");
const goalEl = document.getElementById("goal");
const countsEl = document.getElementById("counts");
const backendEl = document.getElementById("backend");
const dot = document.getElementById("conn-dot");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const emptyEl = document.getElementById("empty");

let assistantBody = null; // the bubble currently being streamed into
const workerBodies = new Map(); // task_id -> element

function atBottom() {
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
}
function scroll(wasAtBottom) {
  if (wasAtBottom) feed.scrollTop = feed.scrollHeight;
}
function clearEmpty() {
  if (emptyEl && emptyEl.parentNode) emptyEl.remove();
}

function addMessage(who, text, cls = "") {
  clearEmpty();
  const stick = atBottom();
  const el = document.createElement("div");
  el.className = `msg ${cls}`.trim();
  const label = document.createElement("div");
  label.className = "who";
  label.textContent = who;
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text;
  el.append(label, body);
  feed.append(el);
  scroll(stick);
  return body;
}

// Planner text arrives as deltas; append into the open bubble rather than
// creating one per fragment.
function appendPlannerText(text) {
  const stick = atBottom();
  if (!assistantBody) assistantBody = addMessage("foreman", "");
  assistantBody.textContent += text;
  scroll(stick);
}

function addTool(ev) {
  const stick = atBottom();
  clearEmpty();
  const el = document.createElement("div");
  el.className = "tool" + (ev.is_error ? " err" : "");
  const name = document.createElement("b");
  name.textContent = ev.name;
  el.append(name);
  const args = ev.arguments && Object.keys(ev.arguments).length
    ? " " + JSON.stringify(ev.arguments)
    : "";
  if (args) el.append(document.createTextNode(args.length > 220 ? args.slice(0, 220) + "…" : args));
  if (ev.finished && ev.result) {
    const out = document.createElement("span");
    out.className = "out";
    out.textContent = ev.result.length > 600 ? ev.result.slice(0, 600) + "…" : ev.result;
    el.append(out);
  }
  feed.append(el);
  scroll(stick);
  // A tool call ends the current planner bubble; later text starts a new one.
  assistantBody = null;
}

function appendWorkerText(taskId, text) {
  const stick = atBottom();
  clearEmpty();
  let body = workerBodies.get(taskId);
  if (!body) {
    const box = document.createElement("div");
    box.className = "worker";
    const head = document.createElement("div");
    head.className = "head";
    head.textContent = `worker · ${taskId}`;
    body = document.createElement("div");
    body.className = "body";
    box.append(head, body);
    feed.append(box);
    workerBodies.set(taskId, body);
  }
  body.textContent += text;
  scroll(stick);
}

function renderPlan(plan) {
  if (plan.goal) {
    goalEl.textContent = plan.goal;
    goalEl.classList.remove("unset");
  } else {
    goalEl.textContent = "No goal set yet.";
    goalEl.classList.add("unset");
  }

  const c = plan.counts || {};
  const parts = ["done", "running", "dispatched", "blocked", "pending", "failed"]
    .filter((k) => c[k])
    .map((k) => `${c[k]} ${k}`);
  countsEl.textContent = parts.join(" · ");

  tasksEl.replaceChildren();
  if (!plan.tasks.length) {
    const li = document.createElement("li");
    li.className = "placeholder";
    li.textContent = "Tasks appear here as the foreman creates them.";
    tasksEl.append(li);
    return;
  }

  for (const t of plan.tasks) {
    const li = document.createElement("li");
    li.className = "task";

    const top = document.createElement("div");
    top.className = "top";
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = t.title;
    const pill = document.createElement("span");
    pill.className = `pill ${t.status}`;
    pill.textContent = t.status;
    top.append(title, pill);

    const id = document.createElement("div");
    id.className = "id";
    id.textContent = t.task_id;

    li.append(top, id);

    if (t.depends_on.length) {
      const deps = document.createElement("div");
      deps.className = "deps";
      deps.textContent = `after ${t.depends_on.join(", ")}`;
      li.append(deps);
    }
    if (t.result) {
      const res = document.createElement("div");
      res.className = "result";
      res.textContent = t.result;
      li.append(res);
    }
    tasksEl.append(li);
  }
}

function connect() {
  const source = new EventSource("/api/events");
  source.onopen = () => {
    dot.classList.add("live");
    dot.title = "connected";
  };
  source.onerror = () => {
    dot.classList.remove("live");
    dot.title = "reconnecting…";
  };
  source.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    switch (ev.type) {
      case "plan":
        renderPlan(ev.plan);
        break;
      case "text":
        appendPlannerText(ev.text);
        break;
      case "thinking":
        break; // the pulsing pill already signals activity
      case "tool":
        if (ev.finished) addTool(ev);
        break;
      case "worker":
        appendWorkerText(ev.task_id, ev.text);
        break;
      case "turn_ended":
        assistantBody = null;
        setBusy(false);
        break;
      case "error":
        addMessage("error", ev.message, "error");
        setBusy(false);
        break;
    }
  };
}

function setBusy(busy) {
  sendBtn.disabled = busy;
  sendBtn.textContent = busy ? "Working…" : "Send";
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  addMessage("you", text, "user");
  input.value = "";
  input.style.height = "auto";
  assistantBody = null;
  workerBodies.clear();
  setBusy(true);
  const res = await fetch("/api/message", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!res.ok) {
    const { error } = await res.json().catch(() => ({ error: "request failed" }));
    addMessage("error", error, "error");
    setBusy(false);
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

document.querySelectorAll("[data-fill]").forEach((b) =>
  b.addEventListener("click", () => {
    input.value = b.dataset.fill;
    input.focus();
    form.requestSubmit();
  })
);

document.getElementById("reset").addEventListener("click", async () => {
  await fetch("/api/reset", { method: "POST" });
  feed.replaceChildren();
  workerBodies.clear();
  assistantBody = null;
  setBusy(false);
  location.reload();
});

fetch("/api/status")
  .then((r) => r.json())
  .then((s) => {
    backendEl.textContent = s.model ? `${s.backend} · ${s.model}` : s.backend;
    backendEl.title = `workspace: ${s.workspace}`;
  })
  .catch(() => {
    backendEl.textContent = "unknown";
  });

connect();
