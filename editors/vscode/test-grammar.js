// Разметить файл .gsm грамматикой Gasem тем же движком, что в VS Code.
// node test-grammar.js файл.gsm
const fs = require('fs');
const path = require('path');
const vsctm = require('vscode-textmate');
const oniguruma = require('vscode-oniguruma');
const wasm = fs.readFileSync(path.join(__dirname, 'node_modules/vscode-oniguruma/release/onig.wasm')).buffer;
const onigLib = oniguruma.loadWASM(wasm).then(() => ({
  createOnigScanner: (p) => new oniguruma.OnigScanner(p),
  createOnigString: (s) => new oniguruma.OnigString(s),
}));
const grammarPath = path.join(__dirname, 'syntaxes', 'gasem.tmLanguage.json');
const registry = new vsctm.Registry({
  onigLib,
  loadGrammar: async () => vsctm.parseRawGrammar(fs.readFileSync(grammarPath, 'utf8'), grammarPath),
});
const lines = fs.readFileSync(process.argv[2], 'utf8').split('\n');
registry.loadGrammar('source.gasem').then((grammar) => {
  let rule = vsctm.INITIAL;
  const out = [];
  for (const line of lines) {
    const r = grammar.tokenizeLine(line, rule);
    const parts = r.tokens
      .filter(t => line.substring(t.startIndex, t.endIndex).trim())
      .map(t => line.substring(t.startIndex, t.endIndex).trim() + '[' + (t.scopes[t.scopes.length - 1] || '') + ']');
    if (parts.length) console.log(parts.join(' '));
    rule = r.ruleStack;
  }
});
