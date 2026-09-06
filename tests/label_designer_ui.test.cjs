"use strict";
// RAW LOGIC. REAL SYSTEMS.
// Created by Christos Fragoulis
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const fixture = JSON.parse(fs.readFileSync(0, "utf8"));

class Element {
  constructor(id = "") { this.id = id; this.value = ""; this.textContent = ""; this.children = []; this.dataset = {}; this.style = {}; this.listeners = {}; this.attributes = {}; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  focus() {}
  querySelectorAll() { return []; }
}
const elements = new Map();
const fullButton = new Element(); fullButton.dataset.profile = "full";
const simpleButton = new Element(); simpleButton.dataset.profile = "simple";
const drawing = { text: [], logos: [], lines: [] };
const canvasContext = {
  font: "10px Arial", measureText(text) { const size = Number(this.font.match(/(\d+)px/)[1]); return { width: String(text).length * size * .48 }; },
  fillText(...args) { drawing.text.push(args); }, drawImage(...args) { drawing.logos.push(args); },
  strokeRect(...args) { drawing.lines.push(args); }, fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, ellipse() {}, save() {}, restore() {}, rect() {}, clip() {},
  getImageData() { return { data: new Uint8ClampedArray(0) }; }, putImageData() {},
};
const document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element(id)); const element = elements.get(id); if (id === "labelPreview") element.getContext = () => canvasContext; return element; },
  querySelectorAll() { return [fullButton, simpleButton]; }, createElement() { return new Element(); },
};
document.getElementById("sampleSelect").value = "worst_case";
document.getElementById("label-designer-bootstrap").textContent = JSON.stringify({ products: fixture.products });
const sandbox = { document, fixture, console, Image: class extends Element { constructor() { super(); this.naturalWidth = 1200; this.naturalHeight = 500; } },
  window: { requestAnimationFrame() {}, clearTimeout() {}, setTimeout() {}, confirm() { return true; } }, fetch: async () => { throw new Error("Unexpected network call"); } };
vm.createContext(sandbox);
const source = fs.readFileSync(path.join(__dirname, "../app/static/label-designer.js"), "utf8").replace(/\nloadState\(\);\s*$/u, "");
vm.runInContext(source, sandbox);
const run = (code) => vm.runInContext(code, sandbox);
const plain = (value) => JSON.parse(JSON.stringify(value));
run("state = fixture.state; companyLogoReady = true; englishLogoReady = true; setWorkingVersion(state.active.settings, state.active.content);");

assert.equal(run("simpleEligibility({unit:'pcs',product:{plain_traceability:false,nutrition_exempt:true}})"), false, "pcs alone never selects SIMPLE");
assert.equal(run("simpleEligibility({unit:'pcs',product:{plain_traceability:true,nutrition_exempt:true,ingredients:' ',allergens:'',nutrition:''}})"), true, "all explicit flags and empty composition are required for SIMPLE");
assert.equal(run("simpleEligibility({product:{plain_traceability:true,nutrition_exempt:true,ingredients:'κρέας'}})"), false);
assert.equal(run("simpleEligibility({product:{plain_traceability:true,nutrition_exempt:false}})"), false);
assert.equal(run("splitNutrition('Ανά 100 g: Ενέργεια 873,23 kJ / 210 kcalΠρωτεΐνη 18 gΛιπαρά 14 g').length"), 3);

const initialSimple = plain(run("workingProfiles.simple"));
run("workingSettings.title_font_px = 44;");
assert.deepEqual(plain(run("workingProfiles.simple")), initialSimple, "editing FULL must not change SIMPLE");
run("sampleSelect.value='product:101'; editingProfile='full'; workingSettings=workingProfiles.full;");
const fullFit = plain(run("autoFitProfile(workingSettings, sampleData(), currentBounds())"));
assert.ok(fullFit.bottom <= 449);
for (const [field, value] of Object.entries(fullFit.settings)) {
  assert.ok(value >= fixture.state.profiles_bounds[field].minimum && value <= fixture.state.profiles_bounds[field].maximum, field);
}
run("workingProfiles.full = autoFitProfile(workingSettings, sampleData(), currentBounds()).settings; workingSettings = workingProfiles.full; renderPreview();");
assert.equal(run("previewFits"), true, "FULL auto-fit should render without clipping");
assert.equal(elements.get("saveDraftBtn").disabled, false);
assert.ok(drawing.logos.some((entry) => entry[2] >= 7 && entry[2] < 100), "v2 logo is in the top section");
assert.ok(drawing.logos.every((entry) => entry[2] < 452), "v2 has no footer logo");
assert.deepEqual(plain(run("workingProfiles.simple")), initialSimple, "auto-fit FULL must not change SIMPLE");
for (const field of Object.keys(fullFit.settings).filter((key) => /^(footer|approval)_/.test(key))) {
  assert.equal(fullFit.settings[field], fixture.state.profiles_defaults.full[field], `protected footer ${field}`);
}

run("sampleSelect.value='product:102'; editingProfile='simple'; workingSettings=workingProfiles.simple;");
const simpleFit = plain(run("autoFitProfile(workingSettings, sampleData(), currentBounds())"));
assert.ok(simpleFit.bottom <= 449);
assert.ok(simpleFit.settings.title_font_px > fixture.state.profiles_bounds.title_font_px.minimum, "short product grows legibly");
assert.equal(simpleFit.settings.ingredients_height_px, initialSimple.ingredients_height_px, "hidden field unchanged");
run("workingProfiles.simple=autoFitProfile(workingSettings,sampleData(),currentBounds()).settings; workingSettings=workingProfiles.simple; renderPreview();");
assert.equal(run("previewFits"), true);
run("workingSettings.title_height_px=100;workingSettings.legal_name_height_px=100;workingSettings.dates_height_px=100;workingSettings.lot_height_px=100;renderPreview();");
assert.equal(run("previewFits"), false);
assert.equal(elements.get("saveDraftBtn").disabled, true, "overflow blocks draft save");
assert.throws(() => run("autoFitProfile(workingSettings, {...sampleData(), displayName:'X'.repeat(1000)}, currentBounds())"), /ελάχιστο/);

run("setWorkingVersion(state.active.settings,state.active.content); renderPreview();");
assert.equal(elements.get("activateBtn").disabled, true, "gate OFF blocks profile activation");
run("setWorkingVersion(state.versions[1].settings,state.versions[1].content);");
assert.equal(run("workingContractVersion"), 1);
assert.equal(run("isProfileBundle(workspaceSettings())"), false);
run("sampleSelect.value='product:102'; renderPreview();");
assert.ok(drawing.logos.some((entry) => entry[2] >= 478), "legacy preview keeps footer logo");
run("upgradeToProfiles();");
assert.equal(run("Object.keys(workspaceSettings().full).length"), 34);
assert.equal(run("Object.keys(workspaceSettings().simple).length"), 34);
assert.equal(run("workingProfiles.full === workingProfiles.simple"), false);
console.log("Designer UI: eligibility, parser, independent profiles, bounded auto-fit, footer, overflow, schema gate and v1 preservation passed.");
