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
const englishLogo = new Image();
let englishLogoReady = false;
englishLogo.addEventListener("load", () => { englishLogoReady = true; schedulePreview(); });
englishLogo.addEventListener("error", () => { englishLogoReady = false; schedulePreview(); });
englishLogo.src = String(bootstrap.english_logo_url || "/static/company-logo-sklavounos-english.png");

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
const profileButtons = [...document.querySelectorAll("button[data-profile]")];
const eligibilityBanner = document.getElementById("eligibilityBanner");
const autoFitBtn = document.getElementById("autoFitBtn");
const profileDefaultsBtn = document.getElementById("profileDefaultsBtn");
const autoFitDetail = document.getElementById("autoFitDetail");
const previewProductName = document.getElementById("previewProductName");
const previewProfileBadge = document.getElementById("previewProfileBadge");
const bodySpaceBar = document.getElementById("bodySpaceBar");

const FIELD_META = {
  logo_height_px: ["logo", "Ύψος περιοχής λογοτύπου"],
  logo_gap_after_px: ["logo", "Κενό μετά το λογότυπο"],
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
  logo: "Εταιρικό λογότυπο · επάνω κέντρο",
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
let workingProfiles = { full: {}, simple: {} };
let workingContractVersion = 1;
let editingProfile = "full";
let workingContent = {};
let selectedVersionId = null;
let renderPending = false;
let previewFits = false;
let mutationPending = false;

function isProfileBundle(settings) {
  return Boolean(settings && typeof settings.full === "object" && typeof settings.simple === "object");
}

function cloneSettings(settings) {
  if (isProfileBundle(settings)) return { full: cloneSettings(settings.full), simple: cloneSettings(settings.simple) };
  return Object.fromEntries(Object.entries(settings || {}).map(([key, value]) => [key, Number(value)]));
}

function canonicalSettings(settings) {
  if (isProfileBundle(settings)) return `v2:${canonicalSettings(settings.full)}:${canonicalSettings(settings.simple)}`;
  return JSON.stringify(Object.keys(settings || {}).sort().map((key) => [key, Number(settings[key])]));
}

function workspaceSettings() {
  return workingContractVersion === 2 ? workingProfiles : workingSettings;
}

function currentDefaults() {
  return workingContractVersion === 2 ? state.profiles_defaults[editingProfile] : state.defaults;
}

function currentBounds() {
  return workingContractVersion === 2 ? state.profiles_bounds : state.bounds;
}

function upgradeToProfiles() {
  if (workingContractVersion === 2) return false;
  const legacy = cloneSettings(workingSettings);
  workingProfiles = {
    full: { ...state.profiles_defaults.full, ...legacy },
    simple: cloneSettings(state.profiles_defaults.simple),
  };
  workingContractVersion = 2;
  workingSettings = workingProfiles[editingProfile];
  return true;
}

function simpleEligibility(item) {
  const data = item && item.product ? item.product : (item || {});
  return data.plain_traceability === true && data.nutrition_exempt === true
    && !String(data.ingredients || "").trim() && !String(data.allergens || "").trim()
    && !String(data.nutrition || "").trim();
}

function selectedProduct() {
  if (!sampleSelect.value.startsWith("product:")) return null;
  return products.find((item) => String(item.id) === sampleSelect.value.slice(8)) || null;
}

function updateProfileContext() {
  const product = selectedProduct();
  const eligible = Boolean(product && simpleEligibility(product));
  profileButtons.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.profile === editingProfile)));
  const label = editingProfile === "full" ? "Πλήρης · Full" : "Απλή · Simple";
  previewProfileBadge.textContent = workingContractVersion === 1 ? "Ιστορική διάταξη · v1" : label;
  previewProductName.textContent = product ? product.name : "Δοκιμή μεγάλου περιεχομένου";
  eligibilityBanner.className = `eligibility-banner${editingProfile === "simple" && !eligible ? " is-warning" : ""}`;
  if (workingContractVersion === 1) {
    eligibilityBanner.textContent = "Ιστορική έκδοση v1: η προεπισκόπηση διατηρεί την παλιά διάταξη. Η πρώτη αλλαγή δημιουργεί νέα έκδοση δύο προφίλ.";
  } else if (eligible) {
    eligibilityBanner.textContent = `Το προϊόν πληροί τους κανόνες της Απλής ετικέτας. Προεπισκόπηση: ${label}.`;
  } else if (editingProfile === "simple") {
    eligibilityBanner.textContent = "Προσοχή: το επιλεγμένο προϊόν δεν δικαιούται Απλή ετικέτα. Βλέπεις το προφίλ Simple για επεξεργασία, με όλα τα πραγματικά στοιχεία του προϊόντος. Η εκτύπωση θα επιλέξει Full.";
  } else {
    eligibilityBanner.textContent = "Πλήρης ετικέτα: η Απλή απαιτεί απλή ιχνηλασιμότητα, διατροφική εξαίρεση και κενά συστατικά, αλλεργιογόνα και διατροφικά στοιχεία.";
  }
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
  const openGroups = new Set([...controlGroups.querySelectorAll("details[open]")].map((item) => item.dataset.group));
  controlGroups.replaceChildren();
  const groups = new Map();
  Object.keys(currentDefaults() || {}).forEach((field) => {
    const group = groupForField(field);
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(field);
  });

  groups.forEach((fields, groupName) => {
    const details = document.createElement("details");
    details.className = "control-group";
    details.dataset.group = groupName;
    details.open = openGroups.size ? openGroups.has(groupName) : ["logo", "title", "nutrition"].includes(groupName);
    const summary = document.createElement("summary");
    summary.textContent = GROUP_LABELS[groupName] || GROUP_LABELS.other;
    const grid = document.createElement("div");
    grid.className = "control-group-grid";

    fields.forEach((field) => {
      const bounds = currentBounds()[field];
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
        const upgraded = upgradeToProfiles();
        workingSettings[field] = value;
        number.value = String(value);
        range.value = String(value);
        updateDirtyState();
        schedulePreview();
        if (upgraded) buildControls();
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
  contentDetails.dataset.group = "content";
  contentDetails.open = true;
  const contentSummary = document.createElement("summary");
  contentSummary.textContent = "Κοινά στοιχεία παραγωγού και λογότυπο";
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
      const upgraded = upgradeToProfiles();
      workingContent[field] = input.value;
      updateDirtyState();
      schedulePreview();
      if (upgraded) buildControls();
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
  [["SKLAVOUNOS_ENGLISH", "Sklavounos · Αγγλικό λογότυπο (PDF)"], ["SKLAVOUNOS_MARK", "Sklavounos · Παλαιό εταιρικό σήμα"], ["NONE", "Χωρίς λογότυπο"]].forEach(([value, title]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = title;
    logoSelect.appendChild(option);
  });
  logoSelect.value = String(workingContent.logo_asset_id || "NONE");
  logoSelect.addEventListener("change", () => {
    const upgraded = upgradeToProfiles();
    workingContent.logo_asset_id = logoSelect.value;
    updateDirtyState();
    schedulePreview();
    if (upgraded) buildControls();
  });
  const logoHelp = document.createElement("p");
  logoHelp.className = "field-help";
  logoHelp.textContent = "Κοινή επιλογή και για τα δύο προφίλ. Στη νέα διάταξη το λογότυπο βρίσκεται επάνω, στο κέντρο. Το ύψος του ρυθμίζεται χωριστά ανά προφίλ.";
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
  const canonicalWorking = canonicalWorkspace(workspaceSettings(), workingContent);
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
  saveDraftBtn.disabled = mutationPending || !state || Boolean(matchingVersion) || !previewFits || renderPending;
  resetBtn.disabled = mutationPending || !state || (isProfileBundle(activeSettings()) && !state.schema8_enabled);
  restoreWorkingBtn.disabled = mutationPending || !state;
  const needsSchema8 = matchingVersion && isProfileBundle(matchingVersion.settings);
  activateBtn.disabled = mutationPending || !matchingVersion || !previewFits || renderPending
    || String(versionId(matchingVersion)) === String(activeVersionId()) || (needsSchema8 && !state.schema8_enabled);
  autoFitBtn.disabled = mutationPending || !state;
  profileDefaultsBtn.disabled = mutationPending || !state;
  updateProfileContext();
}

function setWorkingVersion(settings, content) {
  workingContractVersion = isProfileBundle(settings) ? 2 : 1;
  if (workingContractVersion === 2) {
    workingProfiles = cloneSettings(settings);
    workingSettings = workingProfiles[editingProfile];
  } else {
    workingSettings = cloneSettings(settings);
  }
  workingContent = cloneContent(content);
  previewFits = false;
  buildControls();
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

function fittedText(text, rect, options) {
  const value = String(text || "").trim();
  if (!value) return { size: options.maximum, lines: [], lineHeight: 0 };
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
  return chosen;
}

function drawFittedText(text, rect, options, failures) {
  const value = String(text || "").trim();
  if (!value) return true;
  let chosen = fittedText(value, rect, options);
  if (!chosen) {
    failures.push(options.label);
    // Schema 8 fails closed: never display an ellipsis or a clipped legal value.
    if (workingContractVersion === 2) return false;
    ctx.font = fontString(options.minimum, options.bold);
    chosen = { size: options.minimum, lines: options.noWrap ? [value] : wrappedLines(value, rect.width), lineHeight: options.minimum * 1.16 };
  }
  if (workingContractVersion === 2 && options.body && rect.y + rect.height > 449) {
    failures.push(`${options.label}: υπέρβαση της περιοχής 449 px`);
    return false;
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
  const bounds = state ? currentBounds() : {};
  return bounds[field] ? bounds[field].minimum : (FIXED_MINIMUMS[field] ?? 8);
}

function splitNutrition(value) {
  let clean = String(value || "").trim();
  const heading = /^\s*(?:(?:Ανά|Per)\s*100\s*g\s*:?\s*|Θερμίδες\s+και\s+Συστατικά\s*\(\s*ανά\s*100\s*g\s*\)\s*:?\s*)/iu;
  while (heading.test(clean)) clean = clean.replace(heading, "");
  // Consume the complete label so a shorter name inside it is not split twice.
  // No word boundary: existing entries may contain "kcalΠρωτεΐνη" or "gΛιπαρά".
  const nutrient = /(?:Ενεργειακή\s+αξία|Ενέργεια|Θερμίδες|Energy|Εκ\s+των\s+οποίων\s+κορεσμένα|of\s+which\s+saturates|Κορεσμένα|Εκ\s+των\s+οποίων\s+σάκχαρα|of\s+which\s+sugars|Σάκχαρα|Υδατάνθρακες|Carbohydrates?|Πρωτεΐνες|Πρωτεΐνη|Proteins?|Εδώδιμες\s+ίνες|Φυτικές\s+ίνες|Fibre|Fiber|Ίνες|Λιπαρά|Λίπη|Fat|Αλάτι|Salt)(?=\s*:?\s*[0-9])/giu;
  clean = clean.replace(nutrient, "\n$&");
  return clean.split(/(?:\r\n|[\r\n\u0085\u2028\u2029;|])|,\s*(?=\p{L})/u)
    .map((entry) => entry.trim()).filter(Boolean);
}

function approvalParts(value) {
  const tokens = String(value || "").trim().split(/\s+/u).filter(Boolean);
  if (!tokens.length) return ["GR", "—", "CE"];
  const country = tokens[0] || "GR";
  const suffix = tokens.length > 1 && /^[A-ZΑ-Ω]{1,3}$/u.test(tokens[tokens.length - 1]) ? tokens.pop() : "CE";
  tokens.shift();
  return [country, tokens.join(" ") || "—", suffix];
}

function drawCompanyLogo(assetId, failures, box = { x: 17, y: 478, width: 50, height: 64 }) {
  if (assetId === "NONE") return false;
  if (!["SKLAVOUNOS_MARK", "SKLAVOUNOS_ENGLISH"].includes(assetId)
      || (assetId === "SKLAVOUNOS_ENGLISH" && workingContractVersion !== 2)) {
    failures.push("Μη εγκεκριμένο εταιρικό λογότυπο");
    return false;
  }
  const logo = assetId === "SKLAVOUNOS_ENGLISH" ? englishLogo : companyLogo;
  const ready = assetId === "SKLAVOUNOS_ENGLISH" ? englishLogoReady : companyLogoReady;
  if (!ready || !logo.naturalWidth || !logo.naturalHeight) {
    failures.push("Το εταιρικό λογότυπο δεν φορτώθηκε");
    return false;
  }
  const scale = Math.min(box.width / logo.naturalWidth, box.height / logo.naturalHeight);
  const width = logo.naturalWidth * scale;
  const height = logo.naturalHeight * scale;
  ctx.drawImage(
    logo,
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
  if (!state) return;
  const sample = sampleData();
  const failures = [];
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, 400, 560);
  ctx.strokeStyle = "#000";
  ctx.fillStyle = "#000";
  ctx.lineWidth = 1;
  let y = 7;
  const profileLayout = workingContractVersion === 2;
  if (profileLayout && sample.logoAssetId !== "NONE") {
    const logoHeight = setting("logo_height_px", editingProfile === "simple" ? 80 : 48);
    drawCompanyLogo(sample.logoAssetId, failures, { x: 14, y, width: 372, height: logoHeight });
    y += logoHeight + setting("logo_gap_after_px", 6);
  }

  const drawSection = (text, fontField, heightField, options = {}) => {
    const height = setting(heightField, options.height || 24);
    drawFittedText(text, { x: 14, y, width: 372, height }, {
      maximum: setting(fontField, options.maximum || 12),
      minimum: minimumFor(fontField),
      bold: Boolean(options.bold),
      noWrap: Boolean(options.noWrap),
      body: true,
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
  if (sample.nutrition && !nutrition.length) failures.push("Δεν υπάρχουν διατροφικά στοιχεία μετά την επικεφαλίδα");
  if (nutrition.length > 8) failures.push("Η διατροφική δήλωση ξεπερνά τις 8 σειρές");
  if (nutrition.length) {
    const headingHeight = setting("nutrition_heading_height_px", 19);
    drawFittedText("ΔΙΑΤΡΟΦΙΚΗ ΔΗΛΩΣΗ ΑΝΑ 100 g", { x: 14, y, width: 372, height: headingHeight }, {
      maximum: setting("nutrition_heading_font_px", 12), minimum: minimumFor("nutrition_heading_font_px"), bold: true, noWrap: true, body: true, label: "Επικεφαλίδα διατροφικής δήλωσης",
    }, failures);
    y += headingHeight;
    const trailingHeight = setting("dates_height_px", 24) + setting("lot_height_px", 23)
      + setting("storage_height_px", 28) + setting("origin_height_px", 21)
      + (sample.sourceLot ? setting("source_lot_height_px", 20) : 0)
      + (sample.usage ? setting("usage_height_px", 33) : 0);
    const rowBudget = 449 - y - setting("nutrition_gap_after_px", 4) - trailingHeight;
    const fittedRowHeight = Math.min(setting("nutrition_row_height_px", 22), Math.floor(rowBudget / nutrition.length));
    if (fittedRowHeight < 14) failures.push("Οι διατροφικές σειρές δεν χωρούν χωρίς να καλύψουν τα υπόλοιπα στοιχεία");
    const rowHeight = Math.max(14, fittedRowHeight);
    nutrition.forEach((entry, index) => {
      const cellWidth = 372;
      const x = 14;
      const cellY = y + index * rowHeight;
      if (!profileLayout || cellY + rowHeight <= 449) ctx.strokeRect(x, cellY, cellWidth, rowHeight);
      drawFittedText(entry, { x: x + 4, y: cellY, width: cellWidth - 8, height: rowHeight }, {
        maximum: setting("nutrition_cell_font_px", 11), minimum: minimumFor("nutrition_cell_font_px"), noWrap: true, body: true, label: `Διατροφικό στοιχείο ${index + 1}`,
      }, failures);
    });
    y += nutrition.length * rowHeight + setting("nutrition_gap_after_px", 4);
  }

  drawSection(`ΠΑΡΑΓΩΓΗ: ${sample.productionDate}     ΑΝΑΛΩΣΗ ΕΩΣ: ${sample.useByDate}`, "dates_font_px", "dates_height_px", { bold: true, noWrap: true, height: 24, maximum: 13, label: "Ημερομηνίες" });
  drawSection(`LOT: ${sample.lot}`, "lot_font_px", "lot_height_px", { noWrap: true, height: 23, maximum: 12, label: "LOT" });
  if (sample.sourceLot) drawSection(`ΠΑΡΤΙΔΑ ΠΗΓΗΣ: ${sample.sourceLot}`, "source_lot_font_px", "source_lot_height_px", { noWrap: true, height: 20, maximum: 11, label: "Παρτίδα πηγής" });
  drawSection(sample.storage, "storage_font_px", "storage_height_px", { bold: true, height: 28, maximum: 13, label: "Συντήρηση" });
  drawSection(`ΠΡΟΕΛΕΥΣΗ: ${sample.origin}`, "origin_font_px", "origin_height_px", { noWrap: true, height: 21, maximum: 11, label: "Προέλευση" });
  if (sample.usage) drawSection(sample.usage, "usage_font_px", "usage_height_px", { height: 33, maximum: 11, label: "Οδηγίες χρήσης" });

  // Keep the stored v1 layout reservation compatible with existing snapshots.
  // Actual one-per-row nutrition capacity is checked against the footer above.
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
  if (!profileLayout && protectedWorstCaseBottom > 449) failures.push(`Η πλήρης διάταξη φτάνει στα ${protectedWorstCaseBottom}px (όριο 449px)`);
  if (y > 449) failures.push(`Το κύριο περιεχόμενο φτάνει στα ${y}px (όριο 449px)`);
  ctx.beginPath();
  ctx.moveTo(14, 452);
  ctx.lineTo(386, 452);
  ctx.stroke();

  drawFittedText(sample.footerCaption, { x: 14, y: 456, width: 278, height: 18 }, {
    maximum: setting("footer_caption_font_px", 10), minimum: minimumFor("footer_caption_font_px"), noWrap: true, label: "Λεζάντα παραγωγού",
  }, failures);
  const hasCompanyLogo = profileLayout ? false : drawCompanyLogo(sample.logoAssetId, failures);
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
  previewFits = uniqueFailures.length === 0;
  bodySpaceBar.style.width = `${Math.max(0, Math.min(100, ((y - 7) / 442) * 100))}%`;
  bodySpaceBar.className = previewFits ? "" : "is-overflow";
  if (uniqueFailures.length === 0) {
    fitCard.dataset.fit = "yes";
    fitTitle.textContent = "Χωράει στο 50×70";
    fitDetail.textContent = `Κύριο περιεχόμενο έως ${y}px από διαθέσιμα 449px.`;
  } else {
    fitCard.dataset.fit = "no";
    fitTitle.textContent = "Χρειάζεται προσαρμογή";
    fitDetail.textContent = uniqueFailures.slice(0, 3).join(" · ");
  }
  updateDirtyState();
}

function schedulePreview() {
  autoFitDetail.textContent = `Προσαρμόζει μόνο το προφίλ ${editingProfile === "full" ? "Full" : "Simple"} στο ορατό περιεχόμενο. Το υποσέλιδο και το άλλο προφίλ παραμένουν αμετάβλητα.`;
  if (renderPending) return;
  renderPending = true;
  previewFits = false;
  if (state) updateDirtyState();
  window.requestAnimationFrame(renderPreview);
}

function bodySections(sample) {
  const sections = [];
  const add = (font, height, text, extra = {}) => sections.push({ font, height, texts: [text], count: 1, width: 372, ...extra });
  add("title_font_px", "title_height_px", sample.displayName, { bold: true });
  add("legal_name_font_px", "legal_name_height_px", sample.legalName);
  if (sample.ingredients) add("ingredients_font_px", "ingredients_height_px", `Συστατικά: ${sample.ingredients}`);
  if (sample.allergens) add("allergens_font_px", "allergens_height_px", `ΑΛΛΕΡΓΙΟΓΟΝΑ: ${sample.allergens}`, { bold: true });
  const nutrition = splitNutrition(sample.nutrition);
  if (sample.nutrition && !nutrition.length) throw new Error("Η διατροφική δήλωση περιέχει μόνο επικεφαλίδα.");
  if (nutrition.length > 8) throw new Error("Η διατροφική δήλωση ξεπερνά τις 8 σειρές. Έλεγξε το προϊόν.");
  if (nutrition.length) {
    add("nutrition_heading_font_px", "nutrition_heading_height_px", "ΔΙΑΤΡΟΦΙΚΗ ΔΗΛΩΣΗ ΑΝΑ 100 g", { bold: true, noWrap: true });
    add("nutrition_cell_font_px", "nutrition_row_height_px", "", { texts: nutrition, count: nutrition.length, width: 364, noWrap: true });
  }
  add("dates_font_px", "dates_height_px", `ΠΑΡΑΓΩΓΗ: ${sample.productionDate}     ΑΝΑΛΩΣΗ ΕΩΣ: ${sample.useByDate}`, { bold: true, noWrap: true });
  add("lot_font_px", "lot_height_px", `LOT: ${sample.lot}`, { noWrap: true });
  if (sample.sourceLot) add("source_lot_font_px", "source_lot_height_px", `ΠΑΡΤΙΔΑ ΠΗΓΗΣ: ${sample.sourceLot}`, { noWrap: true });
  add("storage_font_px", "storage_height_px", sample.storage, { bold: true });
  add("origin_font_px", "origin_height_px", `ΠΡΟΕΛΕΥΣΗ: ${sample.origin}`, { noWrap: true });
  if (sample.usage) add("usage_font_px", "usage_height_px", sample.usage);
  return sections;
}

function measuredSectionHeight(section, size, bounds) {
  ctx.font = fontString(size, section.bold);
  let lineCount = 1;
  for (const text of section.texts) {
    const lines = section.noWrap ? [String(text)] : wrappedLines(text, section.width);
    if (lines.some((line) => ctx.measureText(line).width > section.width)) return null;
    lineCount = Math.max(lineCount, lines.length);
  }
  const height = Math.max(bounds[section.height].minimum, Math.ceil(lineCount * size * 1.16) + 2);
  return height <= bounds[section.height].maximum ? height : null;
}

function autoFitProfile(settings, sample, bounds) {
  const candidate = cloneSettings(settings);
  const sections = bodySections(sample);
  const gapFields = [];
  const hasLogo = sample.logoAssetId !== "NONE";
  if (hasLogo) {
    candidate.logo_height_px = Math.max(bounds.logo_height_px.minimum, Math.min(bounds.logo_height_px.maximum, Number(settings.logo_height_px)));
    gapFields.push("logo_gap_after_px");
  }
  if (sample.allergens) gapFields.push("allergens_gap_after_px");
  if (splitNutrition(sample.nutrition).length) gapFields.push("nutrition_gap_after_px");
  gapFields.forEach((field) => { candidate[field] = bounds[field].minimum; });
  for (const section of sections) {
    candidate[section.font] = bounds[section.font].minimum;
    const height = measuredSectionHeight(section, candidate[section.font], bounds);
    if (height === null) throw new Error(`${labelForField(section.font)}: το πλήρες κείμενο δεν χωρά στο ελάχιστο ασφαλές μέγεθος.`);
    candidate[section.height] = height;
  }
  const bottom = () => 7 + (hasLogo ? candidate.logo_height_px : 0)
    + gapFields.reduce((total, field) => total + candidate[field], 0)
    + sections.reduce((total, section) => total + candidate[section.height] * section.count, 0);
  if (hasLogo && bottom() > 449) candidate.logo_height_px = Math.max(bounds.logo_height_px.minimum, candidate.logo_height_px - (bottom() - 449));
  if (bottom() > 449) throw new Error(`Το προϊόν απαιτεί τουλάχιστον ${bottom()} px. Δεν γίνεται ασφαλής αυτόματη προσαρμογή χωρίς απώλεια περιεχομένου.`);

  // Grow every visible field independently. Width, wrapping, row count and the
  // immutable footer boundary all participate in each candidate measurement.
  let changed = true;
  while (changed) {
    changed = false;
    for (const section of sections) {
      const nextSize = candidate[section.font] + 1;
      if (nextSize > bounds[section.font].maximum) continue;
      const nextHeight = measuredSectionHeight(section, nextSize, bounds);
      if (nextHeight === null) continue;
      if (bottom() + (nextHeight - candidate[section.height]) * section.count > 449) continue;
      candidate[section.font] = nextSize;
      candidate[section.height] = nextHeight;
      changed = true;
    }
  }
  // Allocate remaining space as breathing room; never change footer settings or
  // hidden fields. A row-height increment costs one pixel for every visible row.
  const spacers = [
    ...(hasLogo ? [{ field: "logo_height_px", count: 1 }] : []),
    ...gapFields.map((field) => ({ field, count: 1 })),
    ...sections.map((section) => ({ field: section.height, count: section.count })),
  ];
  changed = true;
  while (changed) {
    changed = false;
    for (const spacer of spacers) {
      if (candidate[spacer.field] >= bounds[spacer.field].maximum || bottom() + spacer.count > 449) continue;
      candidate[spacer.field] += 1;
      changed = true;
    }
  }
  return { settings: candidate, bottom: bottom(), sections: sections.length };
}

autoFitBtn.addEventListener("click", () => {
  if (!state || mutationPending) return;
  upgradeToProfiles();
  try {
    const result = autoFitProfile(workingSettings, sampleData(), currentBounds());
    workingProfiles[editingProfile] = result.settings;
    workingSettings = workingProfiles[editingProfile];
    buildControls();
    renderPreview();
    autoFitDetail.textContent = `${editingProfile === "full" ? "Full" : "Simple"}: προσαρμόστηκαν ${result.sections} ορατές ενότητες έως ${result.bottom} / 449 px. Το άλλο προφίλ και το υποσέλιδο δεν άλλαξαν.`;
    showToast(previewFits ? "Η αυτόματη προσαρμογή ολοκληρώθηκε. Αποθήκευσε τη νέα έκδοση όταν είσαι έτοιμος." : "Το κυρίως περιεχόμενο προσαρμόστηκε. Έλεγξε τις υπόλοιπες προειδοποιήσεις πριν αποθηκεύσεις.", previewFits ? "success" : "error");
  } catch (error) {
    buildControls();
    schedulePreview();
    autoFitDetail.textContent = error.message;
    showToast(error.message, "error");
  }
});

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
    badge.textContent = active ? "ΕΝΕΡΓΗ" : "ΠΡΟΣΧΕΔΙΟ";
    const contractBadge = document.createElement("span");
    contractBadge.className = "badge badge-muted";
    contractBadge.textContent = isProfileBundle(version.settings) ? "v2 · Full + Simple" : "v1 · Ιστορική";
    const creator = document.createElement("span");
    const creatorId = version.created_by_user_id;
    creator.textContent = version.created_by_username || version.created_by || (creatorId ? `user #${creatorId}` : "SYSTEM");
    const created = document.createElement("span");
    created.textContent = formatTimestamp(version.created_at);
    meta.append(badge, contractBadge, creator, created);
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
    if (!isProfileBundle(state.profiles_defaults)) throw new Error("Απαιτείται backend με υποστήριξη δύο προφίλ (contract 2).");
    state.profiles_defaults = cloneSettings(state.profiles_defaults);
    state.profiles_bounds = normalizeBounds(state.profiles_bounds || {}, state.profiles_defaults.full);
    runtimeBanner.hidden = false;
    runtimeBanner.className = `runtime-banner${state.schema8_enabled ? " is-live" : ""}`;
    runtimeBanner.textContent = state.schema8_enabled
      ? "Schema 8: ΕΝΕΡΓΟ. Οι νέες εκδόσεις δύο προφίλ μπορούν να ενεργοποιηθούν για νέες εργασίες. Τα υπάρχοντα στιγμιότυπα εκτύπωσης παραμένουν αμετάβλητα."
      : "Schema 8: ΑΝΕΝΕΡΓΟ. Μπορείς να σχεδιάσεις και να αποθηκεύσεις προσχέδια Full + Simple. Η ενεργοποίησή τους απαιτεί ρητή ενεργοποίηση του feature gate από τη διαχείριση· δεν αλλάζουν οι εκτυπώσεις.";
    if (!keepSelection || !selectedVersion()) selectedVersionId = activeVersionId();
    setWorkingVersion(selectedVersion() ? versionSettings(selectedVersion()) : activeSettings(), selectedVersion() ? versionContent(selectedVersion()) : activeContent());
    renderVersions();
    updateDirtyState();
    schedulePreview();
  } catch (error) {
    previewFits = false;
    state = null;
    updateDirtyState();
    fitCard.dataset.fit = "no";
    fitTitle.textContent = "Ο σχεδιαστής δεν φορτώθηκε";
    fitDetail.textContent = error.message;
    showToast(error.message, "error");
  } finally {
    shell.setAttribute("aria-busy", "false");
  }
}

async function mutate(path, body, successMessage, { selectCreated = false } = {}) {
  mutationPending = true;
  updateDirtyState();
  try {
    const result = await api(path, { method: "POST", body: JSON.stringify(body) });
    reasonInput.value = "";
    showToast(successMessage, "success");
    if (selectCreated && result.version) selectedVersionId = versionId(result.version);
    await loadState({ keepSelection: selectCreated });
  } catch (error) {
    if (error.status === 409) showToast("Η διάταξη άλλαξε από άλλη συνεδρία. Έγινε ανανέωση· έλεγξε ξανά πριν συνεχίσεις.", "error");
    else showToast(error.message, "error");
    // Keep edits after a validation/network failure; reload only on an actual conflict.
    if (error.status === 409) await loadState({ keepSelection: true });
  } finally {
    mutationPending = false;
    updateDirtyState();
  }
}

saveDraftBtn.addEventListener("click", async () => {
  if (mutationPending) return;
  upgradeToProfiles();
  renderPreview();
  if (!previewFits) return showToast("Η προεπισκόπηση δεν χωρά. Προσάρμοσε το προφίλ πριν από την αποθήκευση.", "error");
  const reason = requireReason();
  if (!reason) return;
  await mutate("", { settings: cloneSettings(workingProfiles), content: workingContent, reason, expected_version: versionToken() }, "Η νέα έκδοση Full + Simple αποθηκεύτηκε ως προσχέδιο.", { selectCreated: true });
});

activateBtn.addEventListener("click", async () => {
  const reason = requireReason();
  const version = selectedVersion();
  if (!reason || !version) return;
  if (isProfileBundle(version.settings) && !state.schema8_enabled) return showToast("Η ενεργοποίηση δύο προφίλ απαιτεί Schema 8: ΕΝΕΡΓΟ.", "error");
  renderPreview();
  if (!previewFits || mutationPending) return;
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

refreshBtn.addEventListener("click", () => {
  if (!selectedVersion() && !window.confirm("Η ανανέωση θα απορρίψει τις μη αποθηκευμένες αλλαγές. Συνέχεια;")) return;
  loadState({ keepSelection: true });
});
sampleSelect.addEventListener("change", () => {
  const product = selectedProduct();
  editingProfile = product && simpleEligibility(product) ? "simple" : "full";
  if (workingContractVersion === 2) workingSettings = workingProfiles[editingProfile];
  buildControls();
  schedulePreview();
});
profileButtons.forEach((button) => button.addEventListener("click", () => {
  if (!state || mutationPending) return;
  editingProfile = button.dataset.profile;
  upgradeToProfiles();
  workingSettings = workingProfiles[editingProfile];
  buildControls();
  schedulePreview();
}));
profileDefaultsBtn.addEventListener("click", () => {
  if (!state || mutationPending) return;
  upgradeToProfiles();
  workingProfiles[editingProfile] = cloneSettings(state.profiles_defaults[editingProfile]);
  workingSettings = workingProfiles[editingProfile];
  buildControls();
  schedulePreview();
  showToast(`Επαναφέρθηκαν μόνο οι ρυθμίσεις του προφίλ ${editingProfile === "full" ? "Full" : "Simple"}. Απαιτείται αποθήκευση.`);
});

if (products.length) {
  sampleSelect.value = `product:${products[0].id}`;
  editingProfile = simpleEligibility(products[0]) ? "simple" : "full";
}

loadState();
