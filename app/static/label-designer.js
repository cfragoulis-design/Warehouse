"use strict";

const DESIGNER_API = "/admin/labels/layouts";
const bootstrapElement = document.getElementById("label-designer-bootstrap");
const bootstrap = bootstrapElement ? JSON.parse(bootstrapElement.textContent || "{}") : {};
const products = Array.isArray(bootstrap.products) ? bootstrap.products : [];
const companyLogo = new Image();
let companyLogoReady = false;
companyLogo.addEventListener("load", () => { companyLogoReady = true; schedulePreview(); });
companyLogo.addEventListener("error", () => { companyLogoReady = false; schedulePreview(); });
companyLogo.src = String(bootstrap.company_logo_url || "/static/logo-icon.png");

const shell = document.getElementById("designerShell");
const sampleSelect = document.getElementById("sampleSelect");
const controlGroups = document.getElementById("controlGroups");
const canvas = document.getElementById("labelPreview");
const ctx = canvas.getContext("2d", { alpha: false });
const fitCard = document.getElementById("fitCard");
const fitTitle = document.getElementById("fitTitle");
const fitDetail = document.getElementById("fitDetail");
const runtimeBanner = document.getElementById("runtimeBanner");
const dirtyBadge = document.getElementById("dirtyBadge");
const reasonInput = document.getElementById("changeReason");
const saveDraftBtn = document.getElementById("saveDraftBtn");
const activateBtn = document.getElementById("activateBtn");
const resetBtn = document.getElementById("resetBtn");
const restoreWorkingBtn = document.getElementById("restoreWorkingBtn");
const refreshBtn = document.getElementById("refreshBtn");
const versionList = document.getElementById("versionList");
const emptyVersions = document.getElementById("emptyVersions");
const toast = document.getElementById("designerToast");

const FIELD_META = {
  title_font_px: ["title", "Γραμματοσειρά τίτλου"],
  title_height_px: ["title", "Ύψος τίτλου"],
  legal_name_font_px: ["title", "Γραμματοσειρά νόμιμης ονομασίας"],
  legal_name_height_px: ["title", "Ύψος νόμιμης ονομασίας"],
  ingredients_font_px: ["composition", "Γραμματοσειρά συστατικών"],
  ingredients_height_px: ["composition", "Ύψος συστατικών"],
  allergens_font_px: ["composition", "Γραμματοσειρά αλλεργιογόνων"],
  allergens_height_px: ["composition", "Ύψος αλλεργιογόνων"],
  allergens_gap_after_px: ["composition", "Κενό μετά τα αλλεργιογόνα"],
  nutrition_heading_font_px: ["nutrition", "Γραμματοσειρά επικεφαλίδας"],
  nutrition_heading_height_px: ["nutrition", "Ύψος επικεφαλίδας"],
  nutrition_cell_font_px: ["nutrition", "Γραμματοσειρά κελιών"],
  nutrition_row_height_px: ["nutrition", "Ύψος σειράς"],
  nutrition_gap_after_px: ["nutrition", "Κενό μετά τον πίνακα"],
  dates_font_px: ["traceability", "Γραμματοσειρά ημερομηνιών"],
  dates_height_px: ["traceability", "Ύψος ημερομηνιών"],
  lot_font_px: ["traceability", "Γραμματοσειρά LOT"],
  lot_height_px: ["traceability", "Ύψος LOT"],
  source_lot_font_px: ["traceability", "Γραμματοσειρά παρτίδας πηγής"],
  source_lot_height_px: ["traceability", "Ύψος παρτίδας πηγής"],
  storage_font_px: ["traceability", "Γραμματοσειρά συντήρησης"],
  storage_height_px: ["traceability", "Ύψος συντήρησης"],
  origin_font_px: ["traceability", "Γραμματοσειρά προέλευσης"],
  origin_height_px: ["traceability", "Ύψος προέλευσης"],
  usage_font_px: ["traceability", "Γραμματοσειρά οδηγιών"],
  usage_height_px: ["traceability", "Ύψος οδηγιών"],
  footer_caption_font_px: ["footer", "Γραμματοσειρά λεζάντας παραγωγού"],
  footer_name_font_px: ["footer", "Γραμματοσειρά επωνυμίας"],
  footer_address_font_px: ["footer", "Γραμματοσειρά διεύθυνσης"],
  approval_country_font_px: ["approval", "Γραμματοσειρά χώρας έγκρισης"],
  approval_number_font_px: ["approval", "Γραμματοσειρά αριθμού έγκρισης"],
  approval_suffix_font_px: ["approval", "Γραμματοσειρά κατάληξης έγκρισης"],
};

const GROUP_LABELS = {
  title: "Ονομασία προϊόντος",
  composition: "Συστατικά και αλλεργιογόνα",
  nutrition: "Διατροφική δήλωση",
  traceability: "Ιχνηλασιμότητα και οδηγίες",
  footer: "Στοιχεία παραγωγού",
  approval: "Κωδικός έγκρισης",
  other: "Λοιπές ρυθμίσεις",
};

const FIXED_MINIMUMS = {
  title_font_px: 17,
  legal_name_font_px: 9,
  ingredients_font_px: 9,
  allergens_font_px: 10,
  nutrition_heading_font_px: 9,
  nutrition_cell_font_px: 8,
  dates_font_px: 9,
  lot_font_px: 8,
  source_lot_font_px: 8,
  storage_font_px: 9,
  origin_font_px: 8,
  usage_font_px: 8,
  footer_caption_font_px: 8,
  footer_name_font_px: 9,
  footer_address_font_px: 8,
  approval_country_font_px: 10,
  approval_number_font_px: 9,
  approval_suffix_font_px: 9,
};

let state = null;
let workingSettings = {};
let workingContent = {};
let selectedVersionId = null;
let renderPending = false;

function cloneSettings(settings) {
  return Object.fromEntries(Object.entries(settings || {}).map(([key, value]) => [key, Number(value)]));
}

function canonicalSettings(settings) {
  return JSON.stringify(Object.keys(settings || {}).sort().map((key) => [key, Number(settings[key])]));
}

function cloneContent(content) {
  return Object.fromEntries(Object.entries(content || {}).map(([key, value]) => [key, String(value ?? "")]));
}

function canonicalContent(content) {
  return JSON.stringify(Object.keys(content || {}).sort().map((key) => [key, String(content[key] ?? "")]));
}

function canonicalWorkspace(settings, content) {
  return `${canonicalSettings(settings)}|${canonicalContent(content)}`;
}

function activeSettings() {
  return cloneSettings((state && state.active && state.active.settings) || (state && state.defaults) || {});
}

function activeContent() {
  return cloneContent((state && state.active && state.active.content) || (state && state.content_defaults) || {});
}

function getVersions() {
  return state && Array.isArray(state.versions) ? state.versions : [];
}

function versionId(version) {
  return version && (version.id ?? version.version_id ?? version.version);
}

function versionSettings(version) {
  return cloneSettings((version && version.settings) || {});
}

function versionContent(version) {
  return cloneContent((version && version.content) || (state && state.content_defaults) || {});
}

function activeVersionId() {
  return state && state.active ? versionId(state.active) : null;
}

function versionToken() {
  return state ? (state.version_token ?? activeVersionId() ?? 0) : 0;
}

function selectedVersion() {
  return getVersions().find((version) => String(versionId(version)) === String(selectedVersionId)) || null;
}

function normalizeBounds(rawBounds, defaults) {
  const result = {};
  Object.keys(defaults || {}).forEach((key) => {
    const source = rawBounds && rawBounds[key] ? rawBounds[key] : {};
    const minimum = Number(source.minimum ?? source.min ?? defaults[key]);
    const maximum = Number(source.maximum ?? source.max ?? defaults[key]);
    result[key] = {
      minimum: Number.isFinite(minimum) ? minimum : Number(defaults[key]),
      maximum: Number.isFinite(maximum) ? maximum : Number(defaults[key]),
    };
  });
  return result;
}

async function api(path = "", options = {}) {
  const response = await fetch(`${DESIGNER_API}${path}`, {
    cache: "no-store",
    credentials: "same-origin",
    headers: { "Accept": "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) },
    ...options,
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) {
    const detail = typeof payload.detail === "string"
      ? payload.detail
      : (payload.detail && payload.detail.message) || payload.message || `HTTP ${response.status}`;
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function showToast(message, kind = "success") {
  toast.textContent = String(message || "");
  toast.className = `designer-toast is-visible is-${kind}`;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { toast.className = "designer-toast"; }, 4200);
}

function requireReason() {
  const reason = reasonInput.value.trim();
  if (reason.length < 3) {
    reasonInput.focus();
    showToast("Γράψε σύντομη αιτιολογία τουλάχιστον 3 χαρακτήρων.", "error");
    return null;
  }
  return reason;
}

function groupForField(field) {
  return FIELD_META[field] ? FIELD_META[field][0] : "other";
}

function labelForField(field) {
  if (FIELD_META[field]) return FIELD_META[field][1];
  return field.replaceAll("_", " ").replace(/\bpx\b/gi, "(px)");
}

function buildControls() {
  controlGroups.replaceChildren();
  const groups = new Map();
  Object.keys(state.defaults || {}).forEach((field) => {
    const group = groupForField(field);
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(field);
  });

  groups.forEach((fields, groupName) => {
    const details = document.createElement("details");
    details.className = "control-group";
    details.open = groupName === "title" || groupName === "nutrition";
    const summary = document.createElement("summary");
    summary.textContent = GROUP_LABELS[groupName] || GROUP_LABELS.other;
    const grid = document.createElement("div");
    grid.className = "control-group-grid";

    fields.forEach((field) => {
      const bounds = state.bounds[field];
      const wrapper = document.createElement("div");
      wrapper.className = "layout-control";
      const label = document.createElement("label");
      label.htmlFor = `layout-number-${field}`;
      label.textContent = labelForField(field);
      const number = document.createElement("input");
      number.id = `layout-number-${field}`;
      number.className = "number-input";
      number.type = "number";
      number.min = String(bounds.minimum);
      number.max = String(bounds.maximum);
      number.step = "1";
      number.value = String(workingSettings[field]);
      number.dataset.field = field;
      const range = document.createElement("input");
      range.type = "range";
      range.min = String(bounds.minimum);
      range.max = String(bounds.maximum);
      range.step = "1";
      range.value = String(workingSettings[field]);
      range.dataset.field = field;
      range.setAttribute("aria-label", labelForField(field));
      const limits = document.createElement("span");
      limits.className = "control-limits";
      limits.textContent = `${bounds.minimum}–${bounds.maximum} px`;

      const update = (event) => {
        const value = Math.max(bounds.minimum, Math.min(bounds.maximum, Math.round(Number(event.target.value))));
        if (!Number.isFinite(value)) return;
        workingSettings[field] = value;
        number.value = String(value);
        range.value = String(value);
        updateDirtyState();
        schedulePreview();
      };
      number.addEventListener("input", update);
      range.addEventListener("input", update);
      wrapper.append(label, number, range, limits);
      grid.appendChild(wrapper);
    });
    details.append(summary, grid);
    controlGroups.appendChild(details);
  });

  const contentDetails = document.createElement("details");
  contentDetails.className = "control-group";
  contentDetails.open = true;
  const contentSummary = document.createElement("summary");
  contentSummary.textContent = "Νόμιμα στοιχεία παραγωγού και εταιρικό σήμα";
  const contentGrid = document.createElement("div");
  contentGrid.className = "control-group-grid";
  const textFields = [
    ["footer_caption", "Λεζάντα παραγωγού"],
    ["company_name", "Επωνυμία εταιρείας"],
    ["company_address", "Διεύθυνση εταιρείας"],
  ];
  textFields.forEach(([field, title]) => {
    const wrapper = document.createElement("div");
    const label = document.createElement("label");
    label.className = "field-label";
    label.htmlFor = `label-content-${field}`;
    label.textContent = title;
    const input = document.createElement("input");
    input.id = `label-content-${field}`;
    input.className = "select-control";
    input.type = "text";
    input.maxLength = Number((state.content_limits || {})[field] || 255);
    input.value = String(workingContent[field] || "");
    input.dataset.contentField = field;
    input.autocomplete = "off";
    input.addEventListener("input", () => {
      workingContent[field] = input.value;
      updateDirtyState();
      schedulePreview();
    });
    wrapper.append(label, input);
    contentGrid.appendChild(wrapper);
  });

  const logoWrapper = document.createElement("div");
  const logoLabel = document.createElement("label");
  logoLabel.className = "field-label";
  logoLabel.htmlFor = "label-content-logo_asset_id";
  logoLabel.textContent = "Εταιρικό λογότυπο στην ετικέτα";
  const logoSelect = document.createElement("select");
  logoSelect.id = "label-content-logo_asset_id";
  logoSelect.className = "select-control";
  logoSelect.dataset.contentField = "logo_asset_id";
  [["NONE", "Χωρίς λογότυπο"], ["SKLAVOUNOS_MARK", "Σήμα εταιρείας Sklavounos"]].forEach(([value, title]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = title;
    logoSelect.appendChild(option);
  });
  logoSelect.value = String(workingContent.logo_asset_id || "NONE");
  logoSelect.addEventListener("change", () => {
    workingContent.logo_asset_id = logoSelect.value;
    updateDirtyState();
    schedulePreview();
  });
  const logoHelp = document.createElement("p");
  logoHelp.className = "field-help";
  logoHelp.textContent = "Επιτρέπεται μόνο το εγκεκριμένο εταιρικό σήμα. Δεν γίνεται μεταφόρτωση αυθαίρετου αρχείου.";
  logoWrapper.append(logoLabel, logoSelect, logoHelp);
  contentGrid.appendChild(logoWrapper);
  contentDetails.append(contentSummary, contentGrid);
  controlGroups.appendChild(contentDetails);
}

function syncControlValues() {
  controlGroups.querySelectorAll("input[data-field]").forEach((input) => {
    input.value = String(workingSettings[input.dataset.field]);
  });
  controlGroups.querySelectorAll("[data-content-field]").forEach((input) => {
    input.value = String(workingContent[input.dataset.contentField] || "");
  });
}

function updateDirtyState() {
  const canonicalWorking = canonicalWorkspace(workingSettings, workingContent);
  const matchingVersion = getVersions().find((version) => canonicalWorkspace(versionSettings(version), versionContent(version)) === canonicalWorking) || null;
  selectedVersionId = matchingVersion ? versionId(matchingVersion) : null;
  const matchesActive = canonicalWorking === canonicalWorkspace(activeSettings(), activeContent());
  if (!matchingVersion) {
    dirtyBadge.textContent = "Μη αποθηκευμένες αλλαγές";
    dirtyBadge.className = "badge badge-dirty";
  } else if (matchesActive) {
    dirtyBadge.textContent = "Ενεργή διάταξη";
    dirtyBadge.className = "badge badge-muted";
  } else {
    dirtyBadge.textContent = `Αποθηκευμένη έκδοση ${matchingVersion.version ?? versionId(matchingVersion)}`;
    dirtyBadge.className = "badge badge-draft";
  }
  versionList.querySelectorAll("input[name='layout-version']").forEach((radio) => {
    radio.checked = matchingVersion ? String(radio.value) === String(versionId(matchingVersion)) : false;
  });
  saveDraftBtn.disabled = !state || Boolean(matchingVersion);
  resetBtn.disabled = !state;
  restoreWorkingBtn.disabled = !state;
  activateBtn.disabled = !matchingVersion || String(versionId(matchingVersion)) === String(activeVersionId());
}

function setWorkingVersion(settings, content) {
  workingSettings = cloneSettings(settings);
  workingContent = cloneContent(content);
  syncControlValues();
  updateDirtyState();
  schedulePreview();
}

function sampleData() {
  const footerCaption = workingContent.footer_caption || "Παρασκευάζεται και συσκευάζεται από:";
  const selectedCompanyName = workingContent.company_name || "ΣΚΛΑΒΟΥΝΟΣ ΑΝΔΡΕΑΣ & ΣΚΛΑΒΟΥΝΟΣ ΧΡΗΣΤΟΣ Ο.Ε.";
  const selectedCompanyAddress = workingContent.company_address || "Πλατεία Γεωργίου Θεοτόκη 25, 49100 Κέρκυρα";
  const logoAssetId = workingContent.logo_asset_id || "NONE";
  if (sampleSelect.value.startsWith("product:")) {
    const id = sampleSelect.value.slice("product:".length);
    const item = products.find((product) => String(product.id) === id);
    if (item) {
      return {
        displayName: item.product.display_name || item.name,
        legalName: item.product.legal_name || item.name,
        ingredients: item.product.ingredients || "",
        allergens: item.product.allergens || "",
        nutrition: item.product.nutrition || "",
        storage: item.storage || "",
        origin: item.product.origin || "",
        usage: item.product.usage_instructions || "",
        productionDate: "30/08/2026",
        useByDate: "06/09/2026",
        lot: `${item.sku || item.id}-300826-W-01`,
        sourceLot: "ΠΡΟΜ-20260830-001",
        footerCaption,
        businessName: selectedCompanyName,
        businessAddress: selectedCompanyAddress,
        logoAssetId,
        approval: item.business.approval_number || "GR PE 620 CE",
      };
    }
  }
  return {
    displayName: "Κοτοπουλιές κοτόπουλο γεμιστές παραδοσιακές",
    legalName: "Παρασκεύασμα νωπού κρέατος κοτόπουλου",
    ingredients: "Κρέας κοτόπουλου 82%, τυρί, πιπεριά, κρεμμύδι, ελαιόλαδο, αλάτι, μπαχαρικά και αρωματικά φυτά",
    allergens: "ΓΑΛΑ και προϊόντα με βάση το ΓΑΛΑ, πιθανή παρουσία ΣΙΝΑΠΙΟΥ",
    nutrition: "Ανά 100 g: Ενέργεια 873,23 kJ / 210 kcal, Λιπαρά 14 g, Εκ των οποίων κορεσμένα 6 g, Υδατάνθρακες 3 g, Εκ των οποίων σάκχαρα 1,5 g, Πρωτεΐνες 18 g, Αλάτι 1,5 g",
    storage: "Διατηρείται στους 0–4 °C",
    origin: "Ελλάδα",
    usage: "Να καταναλωθεί κατόπιν πλήρους θερμικής επεξεργασίας. Ψήνεται με τον τρόπο αρεσκείας σας.",
    productionDate: "30/08/2026",
    useByDate: "06/09/2026",
    lot: "1075-300826-W-123456",
    sourceLot: "ΠΡΟΜΗΘΕΥΤΗΣ-20260830-987654",
    footerCaption,
    businessName: selectedCompanyName,
    businessAddress: selectedCompanyAddress,
    logoAssetId,
    approval: "GR PE 620 CE",
  };
}

function fontString(size, bold) {
  return `${bold ? "700 " : ""}${size}px Arial, sans-serif`;
}

function wrappedLines(text, width) {
  const words = String(text || "").trim().split(/\s+/u).filter(Boolean);
  if (!words.length) return [];
  const lines = [];
  let line = words.shift();
  words.forEach((word) => {
    const candidate = `${line} ${word}`;
    if (ctx.measureText(candidate).width <= width) line = candidate;
    else { lines.push(line); line = word; }
  });
  lines.push(line);
  return lines;
}

function drawFittedText(text, rect, options, failures) {
  const value = String(text || "").trim();
  if (!value) return true;
  const maximum = Number(options.maximum);
  const minimum = Number(options.minimum);
  const noWrap = Boolean(options.noWrap);
  let chosen = null;
  for (let size = maximum; size >= minimum; size -= 1) {
    ctx.font = fontString(size, options.bold);
    const lines = noWrap ? [value] : wrappedLines(value, rect.width);
    const lineHeight = Math.max(1, size * 1.16);
    const fitsWidth = noWrap ? ctx.measureText(value).width <= rect.width + 1 : lines.every((line) => ctx.measureText(line).width <= rect.width + 1);
    if (fitsWidth && lines.length * lineHeight <= rect.height + 1) {
      chosen = { size, lines, lineHeight };
      break;
    }
  }
  if (!chosen) {
    failures.push(options.label);
    chosen = { size: minimum, lines: noWrap ? [value] : wrappedLines(value, rect.width), lineHeight: minimum * 1.16 };
  }
  ctx.font = fontString(chosen.size, options.bold);
  ctx.textAlign = options.align || "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "#000";
  const totalHeight = chosen.lines.length * chosen.lineHeight;
  let y = rect.y + (rect.height - totalHeight) / 2 + chosen.lineHeight / 2;
  const x = ctx.textAlign === "left" ? rect.x : rect.x + rect.width / 2;
  ctx.save();
  ctx.beginPath();
  ctx.rect(rect.x, rect.y, rect.width, rect.height);
  ctx.clip();
  chosen.lines.forEach((line) => { ctx.fillText(line, x, y); y += chosen.lineHeight; });
  ctx.restore();
  return !failures.includes(options.label);
}

function setting(name, fallback) {
  const value = Number(workingSettings[name]);
  return Number.isFinite(value) ? value : fallback;
}

function minimumFor(field) {
  return state && state.bounds[field] ? state.bounds[field].minimum : (FIXED_MINIMUMS[field] ?? 8);
}

function splitNutrition(value) {
  const clean = String(value || "").replace(/^\s*Ανά\s+100\s*g\s*:\s*/iu, "");
  return clean.split(/,\s+(?=[^0-9])/u).map((entry) => entry.trim()).filter(Boolean).slice(0, 8);
}

function approvalParts(value) {
  const tokens = String(value || "").trim().split(/\s+/u).filter(Boolean);
  if (!tokens.length) return ["GR", "—", "CE"];
  const country = tokens[0] || "GR";
  const suffix = tokens.length > 1 && /^[A-ZΑ-Ω]{1,3}$/u.test(tokens[tokens.length - 1]) ? tokens.pop() : "CE";
  tokens.shift();
  return [country, tokens.join(" ") || "—", suffix];
}

function drawCompanyLogo(assetId, failures) {
  if (assetId === "NONE") return false;
  if (assetId !== "SKLAVOUNOS_MARK") {
    failures.push("Μη εγκεκριμένο εταιρικό λογότυπο");
    return false;
  }
  if (!companyLogoReady || !companyLogo.naturalWidth || !companyLogo.naturalHeight) {
    failures.push("Το εταιρικό λογότυπο δεν φορτώθηκε");
    return false;
  }
  const box = { x: 17, y: 478, width: 50, height: 64 };
  const scale = Math.min(box.width / companyLogo.naturalWidth, box.height / companyLogo.naturalHeight);
  const width = companyLogo.naturalWidth * scale;
  const height = companyLogo.naturalHeight * scale;
  ctx.drawImage(
    companyLogo,
    box.x + ((box.width - width) / 2),
    box.y + ((box.height - height) / 2),
    width,
    height,
  );
  return true;
}

function thresholdCanvas() {
  const image = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const pixels = image.data;
  for (let index = 0; index < pixels.length; index += 4) {
    const luminance = ((pixels[index] * 299) + (pixels[index + 1] * 587) + (pixels[index + 2] * 114)) / 1000;
    const value = luminance < 205 ? 0 : 255;
    pixels[index] = value;
    pixels[index + 1] = value;
    pixels[index + 2] = value;
    pixels[index + 3] = 255;
  }
  ctx.putImageData(image, 0, 0);
}

function renderPreview() {
  renderPending = false;
  const sample = sampleData();
  const failures = [];
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, 400, 560);
  ctx.strokeStyle = "#000";
  ctx.fillStyle = "#000";
  ctx.lineWidth = 1;
  let y = 7;

  const drawSection = (text, fontField, heightField, options = {}) => {
    const height = setting(heightField, options.height || 24);
    drawFittedText(text, { x: 14, y, width: 372, height }, {
      maximum: setting(fontField, options.maximum || 12),
      minimum: minimumFor(fontField),
      bold: Boolean(options.bold),
      noWrap: Boolean(options.noWrap),
      label: options.label || labelForField(fontField),
    }, failures);
    y += height;
  };

  drawSection(sample.displayName, "title_font_px", "title_height_px", { bold: true, height: 42, maximum: 27, label: "Τίτλος" });
  drawSection(sample.legalName, "legal_name_font_px", "legal_name_height_px", { height: 29, maximum: 14, label: "Νόμιμη ονομασία" });
  if (sample.ingredients) drawSection(`Συστατικά: ${sample.ingredients}`, "ingredients_font_px", "ingredients_height_px", { height: 52, maximum: 13, label: "Συστατικά" });
  if (sample.allergens) {
    drawSection(`ΑΛΛΕΡΓΙΟΓΟΝΑ: ${sample.allergens}`, "allergens_font_px", "allergens_height_px", { bold: true, height: 31, maximum: 14, label: "Αλλεργιογόνα" });
    y += setting("allergens_gap_after_px", 3);
  }

  const nutrition = splitNutrition(sample.nutrition);
  if (nutrition.length) {
    const headingHeight = setting("nutrition_heading_height_px", 19);
    drawFittedText("ΔΙΑΤΡΟΦΙΚΗ ΔΗΛΩΣΗ ΑΝΑ 100 g", { x: 14, y, width: 372, height: headingHeight }, {
      maximum: setting("nutrition_heading_font_px", 12), minimum: minimumFor("nutrition_heading_font_px"), bold: true, noWrap: true, label: "Επικεφαλίδα διατροφικής δήλωσης",
    }, failures);
    y += headingHeight;
    const rowHeight = setting("nutrition_row_height_px", 22);
    nutrition.forEach((entry, index) => {
      const column = index % 2;
      const row = Math.floor(index / 2);
      const x = 14 + column * 186;
      const cellY = y + row * rowHeight;
      ctx.strokeRect(x, cellY, 186, rowHeight);
      drawFittedText(entry, { x: x + 4, y: cellY, width: 178, height: rowHeight }, {
        maximum: setting("nutrition_cell_font_px", 11), minimum: minimumFor("nutrition_cell_font_px"), noWrap: true, label: `Διατροφικό στοιχείο ${index + 1}`,
      }, failures);
    });
    y += Math.ceil(nutrition.length / 2) * rowHeight + setting("nutrition_gap_after_px", 4);
  }

  drawSection(`ΠΑΡΑΓΩΓΗ: ${sample.productionDate}     ΑΝΑΛΩΣΗ ΕΩΣ: ${sample.useByDate}`, "dates_font_px", "dates_height_px", { bold: true, noWrap: true, height: 24, maximum: 13, label: "Ημερομηνίες" });
  drawSection(`LOT: ${sample.lot}`, "lot_font_px", "lot_height_px", { noWrap: true, height: 23, maximum: 12, label: "LOT" });
  if (sample.sourceLot) drawSection(`ΠΑΡΤΙΔΑ ΠΗΓΗΣ: ${sample.sourceLot}`, "source_lot_font_px", "source_lot_height_px", { noWrap: true, height: 20, maximum: 11, label: "Παρτίδα πηγής" });
  drawSection(sample.storage, "storage_font_px", "storage_height_px", { bold: true, height: 28, maximum: 13, label: "Συντήρηση" });
  drawSection(`ΠΡΟΕΛΕΥΣΗ: ${sample.origin}`, "origin_font_px", "origin_height_px", { noWrap: true, height: 21, maximum: 11, label: "Προέλευση" });
  if (sample.usage) drawSection(sample.usage, "usage_font_px", "usage_height_px", { height: 33, maximum: 11, label: "Οδηγίες χρήσης" });

  const protectedWorstCaseBottom = 7
    + setting("title_height_px", 42)
    + setting("legal_name_height_px", 29)
    + setting("ingredients_height_px", 52)
    + setting("allergens_height_px", 31)
    + setting("allergens_gap_after_px", 3)
    + setting("nutrition_heading_height_px", 19)
    + (4 * setting("nutrition_row_height_px", 22))
    + setting("nutrition_gap_after_px", 4)
    + setting("dates_height_px", 24)
    + setting("lot_height_px", 23)
    + setting("source_lot_height_px", 20)
    + setting("storage_height_px", 28)
    + setting("origin_height_px", 21)
    + setting("usage_height_px", 33);
  if (protectedWorstCaseBottom > 449) failures.push(`Η πλήρης διάταξη φτάνει στα ${protectedWorstCaseBottom}px (όριο 449px)`);
  if (y > 449) failures.push(`Το κύριο περιεχόμενο φτάνει στα ${y}px (όριο 449px)`);
  ctx.beginPath();
  ctx.moveTo(14, 452);
  ctx.lineTo(386, 452);
  ctx.stroke();

  drawFittedText(sample.footerCaption, { x: 14, y: 456, width: 278, height: 18 }, {
    maximum: setting("footer_caption_font_px", 10), minimum: minimumFor("footer_caption_font_px"), noWrap: true, label: "Λεζάντα παραγωγού",
  }, failures);
  const hasCompanyLogo = drawCompanyLogo(sample.logoAssetId, failures);
  const footerTextX = hasCompanyLogo ? 72 : 14;
  const footerTextWidth = hasCompanyLogo ? 220 : 278;
  drawFittedText(sample.businessName, { x: footerTextX, y: 473, width: footerTextWidth, height: 31 }, {
    maximum: setting("footer_name_font_px", 13), minimum: minimumFor("footer_name_font_px"), bold: true, label: "Επωνυμία",
  }, failures);
  drawFittedText(sample.businessAddress, { x: footerTextX, y: 503, width: footerTextWidth, height: 43 }, {
    maximum: setting("footer_address_font_px", 10), minimum: minimumFor("footer_address_font_px"), label: "Διεύθυνση",
  }, failures);

  ctx.beginPath();
  ctx.ellipse(344, 503, 42, 36, 0, 0, Math.PI * 2);
  ctx.stroke();
  const approval = approvalParts(sample.approval);
  drawFittedText(approval[0], { x: 310, y: 477, width: 68, height: 17 }, {
    maximum: setting("approval_country_font_px", 12), minimum: minimumFor("approval_country_font_px"), bold: true, noWrap: true, label: "Χώρα έγκρισης",
  }, failures);
  drawFittedText(approval[1], { x: 308, y: 493, width: 72, height: 25 }, {
    maximum: setting("approval_number_font_px", 14), minimum: minimumFor("approval_number_font_px"), bold: true, noWrap: true, label: "Αριθμός έγκρισης",
  }, failures);
  drawFittedText(approval[2], { x: 310, y: 517, width: 68, height: 16 }, {
    maximum: setting("approval_suffix_font_px", 11), minimum: minimumFor("approval_suffix_font_px"), bold: true, noWrap: true, label: "Κατάληξη έγκρισης",
  }, failures);

  thresholdCanvas();
  const uniqueFailures = [...new Set(failures)];
  if (uniqueFailures.length === 0) {
    fitCard.dataset.fit = "yes";
    fitTitle.textContent = "Χωράει στο 50×70";
    fitDetail.textContent = `Κύριο περιεχόμενο έως ${y}px από διαθέσιμα 449px.`;
  } else {
    fitCard.dataset.fit = "no";
    fitTitle.textContent = "Χρειάζεται προσαρμογή";
    fitDetail.textContent = uniqueFailures.slice(0, 3).join(" · ");
  }
}

function schedulePreview() {
  if (renderPending) return;
  renderPending = true;
  window.requestAnimationFrame(renderPreview);
}

function renderVersions() {
  versionList.replaceChildren();
  const versions = getVersions();
  emptyVersions.hidden = versions.length > 0;
  versions.forEach((version) => {
    const id = versionId(version);
    const label = document.createElement("label");
    label.className = "version-card";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "layout-version";
    radio.value = String(id);
    radio.checked = String(id) === String(selectedVersionId);
    const content = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = `Έκδοση ${version.version ?? id}`;
    const meta = document.createElement("div");
    meta.className = "version-meta";
    const badge = document.createElement("span");
    const active = String(id) === String(activeVersionId()) || version.is_active === true || version.status === "ACTIVE";
    badge.className = `badge ${active ? "badge-active" : "badge-draft"}`;
    badge.textContent = active ? "ΕΝΕΡΓΗ" : "DRAFT";
    const creator = document.createElement("span");
    const creatorId = version.created_by_user_id;
    creator.textContent = version.created_by_username || version.created_by || (creatorId ? `user #${creatorId}` : "SYSTEM");
    const created = document.createElement("span");
    created.textContent = formatTimestamp(version.created_at);
    meta.append(badge, creator, created);
    const reason = document.createElement("p");
    reason.className = "version-reason";
    reason.textContent = version.change_reason || version.reason || "Χωρίς καταγεγραμμένη αιτιολογία";
    content.append(title, meta, reason);
    label.append(radio, content);
    radio.addEventListener("change", () => {
      selectedVersionId = id;
      setWorkingVersion(versionSettings(version), versionContent(version));
      renderVersions();
    });
    versionList.appendChild(label);
  });
}

function formatTimestamp(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("el-GR", { dateStyle: "short", timeStyle: "short" });
}

async function loadState({ keepSelection = false } = {}) {
  shell.setAttribute("aria-busy", "true");
  try {
    const payload = await api();
    state = payload.state && typeof payload.state === "object" ? payload.state : payload;
    state.defaults = cloneSettings(state.defaults || (state.active && state.active.settings) || {});
    state.content_defaults = cloneContent(state.content_defaults || (state.active && state.active.content) || {});
    state.bounds = normalizeBounds(state.bounds || {}, state.defaults);
    if (!Object.keys(state.defaults).length) throw new Error("Το backend δεν επέστρεψε το canonical layout contract.");
    runtimeBanner.hidden = false;
    runtimeBanner.className = `runtime-banner${state.schema6_enabled || state.schema7_enabled ? " is-live" : ""}`;
    runtimeBanner.textContent = state.schema7_enabled
      ? "Η ενεργή έκδοση διάταξης και νόμιμων στοιχείων εφαρμόζεται στις νέες εργασίες ως immutable schema 7 snapshot. Οι queued εργασίες δεν αλλάζουν."
      : (state.schema6_enabled
        ? "Η ενεργή διάταξη εφαρμόζεται στις νέες εργασίες. Τα νόμιμα στοιχεία και το εταιρικό σήμα παραμένουν κλειστά από το schema 7 feature gate."
        : "Η έκδοση μπορεί να ρυθμιστεί με ασφάλεια, αλλά η εφαρμογή της στις νέες εκτυπώσεις παραμένει κλειστή από τα feature gates.");
    if (!keepSelection || !selectedVersion()) selectedVersionId = activeVersionId();
    workingSettings = selectedVersion() ? versionSettings(selectedVersion()) : activeSettings();
    workingContent = selectedVersion() ? versionContent(selectedVersion()) : activeContent();
    buildControls();
    renderVersions();
    updateDirtyState();
    schedulePreview();
  } catch (error) {
    fitCard.dataset.fit = "no";
    fitTitle.textContent = "Ο σχεδιαστής δεν φορτώθηκε";
    fitDetail.textContent = error.message;
    showToast(error.message, "error");
  } finally {
    shell.setAttribute("aria-busy", "false");
  }
}

async function mutate(path, body, successMessage, { selectCreated = false } = {}) {
  [saveDraftBtn, activateBtn, resetBtn].forEach((button) => { button.disabled = true; });
  try {
    const result = await api(path, { method: "POST", body: JSON.stringify(body) });
    reasonInput.value = "";
    showToast(successMessage, "success");
    if (selectCreated && result.version) selectedVersionId = versionId(result.version);
    await loadState({ keepSelection: selectCreated });
  } catch (error) {
    if (error.status === 409) showToast("Η διάταξη άλλαξε από άλλη συνεδρία. Έγινε ανανέωση· έλεγξε ξανά πριν συνεχίσεις.", "error");
    else showToast(error.message, "error");
    await loadState({ keepSelection: true });
  }
}

saveDraftBtn.addEventListener("click", async () => {
  const reason = requireReason();
  if (!reason) return;
  await mutate("", { settings: workingSettings, content: workingContent, reason, expected_version: versionToken() }, "Η νέα έκδοση αποθηκεύτηκε ως draft.", { selectCreated: true });
});

activateBtn.addEventListener("click", async () => {
  const reason = requireReason();
  const version = selectedVersion();
  if (!reason || !version) return;
  await mutate(`/${encodeURIComponent(versionId(version))}/activate`, { reason, expected_version: versionToken() }, "Η επιλεγμένη διάταξη ενεργοποιήθηκε.");
});

resetBtn.addEventListener("click", async () => {
  const reason = requireReason();
  if (!reason) return;
  if (!window.confirm("Να δημιουργηθεί και να ενεργοποιηθεί νέα έκδοση με τις canonical προεπιλογές;")) return;
  await mutate("/reset", { reason, expected_version: versionToken() }, "Οι canonical προεπιλογές ενεργοποιήθηκαν.");
});

restoreWorkingBtn.addEventListener("click", () => {
  selectedVersionId = activeVersionId();
  setWorkingVersion(activeSettings(), activeContent());
  renderVersions();
});

refreshBtn.addEventListener("click", () => loadState({ keepSelection: true }));
sampleSelect.addEventListener("change", schedulePreview);

loadState();
