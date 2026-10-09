// Модель InDesign для проверки скрипта правок в Node.js: истории (stories), поиск, замена, условия, заметки.
// Запуск: node indesign_mock.js скрипт.jsx сценарий.json → JSON с текстом историй, условиями, заметками и журналом.
var fs = require("fs"), vm = require("vm");
var script = fs.readFileSync(process.argv[2], "utf8");
var scen = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
var out = {conditions: [], notes: [], alerts: [], log: ""};

function Story(text, pageBreaks) { this.text = text; this.pageBreaks = pageBreaks || []; var self = this;
  this.characters = { get length() { return self.text.length; }, itemByRange: function (a, b) { return new TR(self, a, b + 1); } }; }
function TR(story, a, b) { this.story = story; this.a = a; this.b = b; }
Object.defineProperty(TR.prototype, "index", { get: function () { return this.a; } });
Object.defineProperty(TR.prototype, "parentStory", { get: function () { return this.story; } });
Object.defineProperty(TR.prototype, "contents", {
  get: function () { return this.story.text.substring(this.a, this.b); },
  set: function (v) { var s = this.story; s.text = s.text.substring(0, this.a) + v + s.text.substring(this.b); this.b = this.a + v.length; }
});
Object.defineProperty(TR.prototype, "parentTextFrames", { get: function () {
  var page = 1, br = this.story.pageBreaks; for (var i = 0; i < br.length; i++) { if (this.a >= br[i]) { page = i + 2; } }
  return [{ parentPage: { documentOffset: page - 1 } }]; } });
Object.defineProperty(TR.prototype, "insertionPoints", { get: function () { var self = this;
  return [{ notes: { add: function () { var n = { texts: [{ contents: "" }] }; out.notes.push({ at: self.a, note: n }); return n; } } }]; } });
TR.prototype.applyConditions = function (arr) { out.conditions.push({ text: this.contents, name: arr[0].name }); };

var stories = scen.stories.map(function (s) { return new Story(s.text, s.pageBreaks); });
var conds = {};
var doc = {
  name: "Каталог.indd", get filePath() { throw new Error("не сохранен"); },
  findText: function () {
    var what = app.findTextPreferences.findWhat.replace(/\^\^/g, "^"), res = [];
    if (!what) { return res; }
    stories.forEach(function (st) { var i = st.text.indexOf(what);
      while (i >= 0) { res.push(new TR(st, i, i + what.length)); i = st.text.indexOf(what, i + what.length); } });
    return res;
  },
  conditions: { itemByName: function (n) { return conds[n] || { isValid: false }; },
                add: function (p) { conds[p.name] = { isValid: true, name: p.name }; return conds[p.name]; } }
};
function control(p) { return { checkedState: p.checkedState }; }
var findPrefs = {}, changePrefs = {};
var app = {
  documents: { length: 1 }, activeDocument: doc, findChangeTextOptions: {},
  // как в InDesign: присваивание NothingEnum.nothing сбрасывает настройки поиска, сам объект остается
  get findTextPreferences() { return findPrefs; }, set findTextPreferences(v) { findPrefs = {}; },
  get changeTextPreferences() { return changePrefs; }, set changeTextPreferences(v) { changePrefs = {}; },
  dialogs: { add: function () { var cbs = [];
    return { dialogColumns: { add: function () { return { staticTexts: { add: function () {} },
      checkboxControls: { add: function (p) { var c = control(p); cbs.push(c); return c; } } }; } },
      show: function () { (scen.options || []).forEach(function (v, i) { if (cbs[i]) { cbs[i].checkedState = v; } }); return true; },
      destroy: function () {} }; } },
  doScript: function (fn) { fn(); }
};
var ctx = {
  app: app, NothingEnum: { nothing: null }, UIColors: { YELLOW: "y", GRASS_GREEN: "g" },
  ConditionIndicatorMethod: { USE_HIGHLIGHT: "h" }, ScriptLanguage: { JAVASCRIPT: 1 }, UndoModes: { ENTIRE_SCRIPT: 1 },
  Folder: { desktop: "/tmp" },
  File: function (p) { this.fsName = p; this.open = function () { return true; }; this.writeln = function (l) { out.log += l + "\n"; }; this.close = function () {}; },
  alert: function (m) { out.alerts.push(m); }
};
// директивы препроцессора ExtendScript (#target) Node не понимает — убираем
vm.runInNewContext(script.replace(/^#.*$/mg, ""), ctx);
out.stories = stories.map(function (s) { return s.text; });
console.log(JSON.stringify(out));
