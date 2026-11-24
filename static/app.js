"use strict";

const CATEGORIES = ["Groceries", "Beverages", "Personal Care", "Household", "Electronics",
  "Fashion", "Home & Kitchen", "Other"];
const POLL_INTERVAL_MS = 1500;   // never poll more often than once a second
const SEARCH_DEBOUNCE_MS = 400;  // wait for a pause in typing before searching
const PAGE_SIZE = 20;
const JOB_STORAGE_KEY = "catalogiq.currentJob";

const $ = (id) => document.getElementById(id);

// ---- API helper ------------------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let body = null;
  try { body = await response.json(); } catch (e) { /* not JSON */ }
  if (!response.ok) {
    throw new Error((body && body.error) || "Request failed (" + response.status + ")");
  }
  return body;
}

// ---- CSV parsing (in the browser) ------------------------------------------

// Handles quoted fields, escaped quotes ("") and commas or newlines inside quotes.
function parseCSV(text) {
  const rows = [];
  let row = [], field = "", inQuotes = false;
  if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1); // strip BOM
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (ch === '"') inQuotes = false;
      else field += ch;
    } else if (ch === '"' && field === "") {
      inQuotes = true;
    } else if (ch === ",") {
      row.push(field); field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field); rows.push(row);
      row = []; field = "";
    } else {
      field += ch;
    }
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((cell) => cell.trim() !== ""));
}

const HEADER_ALIASES = {
  sku: ["sku", "id", "product_id"],
  raw_title: ["raw_title", "title", "name", "product_name"],
  raw_description: ["raw_description", "description", "desc"],
};

function csvToProducts(text) {
  const rows = parseCSV(text);
  if (rows.length < 2) throw new Error("The CSV needs a header row and at least one listing.");
  const header = rows[0].map((h) => h.trim().toLowerCase());
  const column = {};
  for (const [field, names] of Object.entries(HEADER_ALIASES)) {
    column[field] = header.findIndex((h) => names.includes(h));
  }
  if (column.sku < 0 || column.raw_title < 0) {
    throw new Error("The CSV must have 'sku' and 'raw_title' columns.");
  }
  return rows.slice(1).map((r) => ({
    sku: (r[column.sku] || "").trim(),
    raw_title: r[column.raw_title] || "",
    raw_description: column.raw_description >= 0 ? (r[column.raw_description] || "") : "",
  }));
}

// ---- Upload ----------------------------------------------------------------

$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("csv-file").files[0];
  const message = $("upload-message");
  if (!file) { message.textContent = "Choose a CSV file first."; return; }
  $("upload-button").disabled = true;
  message.className = "";
  message.textContent = "Reading " + file.name + "…";
  try {
    const products = csvToProducts(await file.text());
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ products }),
    });
    message.textContent = "Uploaded " + products.length + " listings.";
    startJob(job);
  } catch (error) {
    message.className = "error";
    message.textContent = error.message;
  } finally {
    $("upload-button").disabled = false;
  }
});

// ---- Job progress (polling) ------------------------------------------------

let pollTimer = null;
let lastAnnounced = -1;

function renderJob(job) {
  const finished = job.done + job.failed;
  const percent = job.total ? Math.floor((finished / job.total) * 100) : 0;
  $("job-section").hidden = false;
  $("job-id").textContent = job.id;
  $("job-status").textContent = job.status;
  $("job-progress").max = job.total || 1;
  $("job-progress").value = finished;
  $("job-percent").textContent = finished + " of " + job.total + " processed (" + percent + "%)";
  $("job-total").textContent = job.total;
  $("job-done").textContent = job.done;
  $("job-failed").textContent = job.failed;
  $("job-cache-hits").textContent = job.cache_hits;
  // Tell screen readers about progress in steps of 25%, not on every poll.
  const step = job.status === "completed" ? 100 : Math.floor(percent / 25) * 25;
  if (step !== lastAnnounced) {
    lastAnnounced = step;
    $("job-announce").textContent = job.status === "completed"
      ? "Job completed: " + job.done + " done, " + job.failed + " failed."
      : "Job " + step + "% processed.";
  }
}

function startJob(job) {
  clearTimeout(pollTimer);
  lastAnnounced = -1;
  localStorage.setItem(JOB_STORAGE_KEY, job.id);
  renderJob(job);
  if (job.status !== "completed") pollTimer = setTimeout(() => pollJob(job.id), POLL_INTERVAL_MS);
}

async function pollJob(jobId) {
  try {
    const job = await api("/api/jobs/" + encodeURIComponent(jobId));
    renderJob(job);
    if (job.status === "completed") {
      loadProducts();
      return;
    }
  } catch (error) {
    $("job-status").textContent = "unknown (" + error.message + ")";
    if (/unknown job/.test(error.message)) { localStorage.removeItem(JOB_STORAGE_KEY); return; }
  }
  // Schedule the next poll only after this one has finished, so slow responses
  // never pile up.
  pollTimer = setTimeout(() => pollJob(jobId), POLL_INTERVAL_MS);
}

// ---- Catalogue -------------------------------------------------------------

const view = { page: 1, total: 0, q: "", category: "", status: "" };
let listRequest = null;

async function loadProducts() {
  if (listRequest) listRequest.abort(); // drop a slower, older request
  listRequest = new AbortController();
  const params = new URLSearchParams({ page: view.page, page_size: PAGE_SIZE });
  if (view.q) params.set("q", view.q);
  if (view.category) params.set("category", view.category);
  if (view.status) params.set("status", view.status);
  try {
    const data = await api("/api/products?" + params, { signal: listRequest.signal });
    view.total = data.total;
    renderProducts(data);
  } catch (error) {
    if (error.name !== "AbortError") $("catalogue-message").textContent = error.message;
  }
}

function renderProducts(data) {
  const list = $("product-list");
  list.replaceChildren();
  for (const product of data.items) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.append(
      span("sku", product.sku),
      span("title", product.clean_title || product.raw_title),
      span("category", product.category || "—"),
      span("badge " + product.status, product.status));
    button.setAttribute("aria-label",
      "Review " + (product.clean_title || product.raw_title) + ", " + product.status);
    button.addEventListener("click", () => openReview(product.sku, button));
    item.append(button);
    list.append(item);
  }
  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  $("catalogue-message").textContent = data.total === 0
    ? "No products match."
    : data.total + " product" + (data.total === 1 ? "" : "s") + " found.";
  $("page-label").textContent = "Page " + data.page + " of " + pages;
  $("prev-page").disabled = data.page <= 1;
  $("next-page").disabled = data.page >= pages;
}

function span(className, text) {
  const element = document.createElement("span");
  element.className = className;
  element.textContent = text;
  return element;
}

let searchTimer = null;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    view.q = $("search").value.trim();
    view.page = 1;
    loadProducts();
  }, SEARCH_DEBOUNCE_MS);
});
$("filters").addEventListener("submit", (event) => event.preventDefault());
$("category-filter").addEventListener("change", () => {
  view.category = $("category-filter").value; view.page = 1; loadProducts();
});
$("status-filter").addEventListener("change", () => {
  view.status = $("status-filter").value; view.page = 1; loadProducts();
});
$("refresh-button").addEventListener("click", loadProducts);
$("prev-page").addEventListener("click", () => { view.page--; loadProducts(); });
$("next-page").addEventListener("click", () => { view.page++; loadProducts(); });

// ---- Review dialog ---------------------------------------------------------

let reviewSku = null;
let reviewOpener = null;

async function openReview(sku, opener) {
  try {
    const product = await api("/api/products/" + encodeURIComponent(sku));
    reviewSku = sku;
    reviewOpener = opener;
    $("review-sku").textContent = product.sku;
    $("review-status").textContent = product.status;
    $("review-status").className = "badge " + product.status;
    $("review-raw-title").textContent = product.raw_title;
    $("review-raw-description").textContent = product.raw_description || "(none)";
    $("review-title").value = product.clean_title || "";
    $("review-category").value = product.category || "Other";
    $("review-tags").value = product.tags.join(", ");
    $("review-brand").textContent = product.brand || "unknown";
    $("review-error").hidden = !product.error;
    $("review-error").textContent = product.error ? "Enrichment failed: " + product.error : "";
    $("review-message").textContent = "";
    $("review-dialog").showModal(); // traps focus; Esc closes it
    $("review-title").focus();
  } catch (error) {
    $("catalogue-message").textContent = error.message;
  }
}

$("review-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const tags = $("review-tags").value.split(",").map((t) => t.trim()).filter(Boolean);
  $("review-save").disabled = true;
  try {
    await api("/api/products/" + encodeURIComponent(reviewSku), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        clean_title: $("review-title").value,
        category: $("review-category").value,
        tags,
      }),
    });
    $("review-dialog").close();
    await loadProducts();
  } catch (error) {
    $("review-message").textContent = error.message;
  } finally {
    $("review-save").disabled = false;
  }
});
$("review-cancel").addEventListener("click", () => $("review-dialog").close());
$("review-dialog").addEventListener("close", () => {
  // Put keyboard focus back where the user was.
  if (reviewOpener && reviewOpener.isConnected) reviewOpener.focus();
  else $("search").focus();
});

// ---- Start-up --------------------------------------------------------------

function fillCategories(select) {
  for (const category of CATEGORIES) select.append(new Option(category, category));
}

async function init() {
  fillCategories($("category-filter"));
  fillCategories($("review-category"));
  try {
    const health = await api("/api/health");
    $("health").textContent = "LLM provider: " + health.llm_provider +
      " · up to " + health.llm_concurrency + " calls at once";
  } catch (error) {
    $("health").textContent = "Server unreachable: " + error.message;
  }
  loadProducts();
  // After a page reload, pick the last job back up.
  const savedJob = localStorage.getItem(JOB_STORAGE_KEY);
  if (savedJob) pollJob(savedJob);
}

init();
