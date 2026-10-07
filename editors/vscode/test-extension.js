// Проверка расширения без самого VS Code:
//  1) extension.js с подставным модулем vscode — регистрирует команды и запускает «gasem lsp»;
//  2) настоящий «gasem lsp» через vscode-jsonrpc — ту же библиотеку, что использует VS Code.
//
//   node test-extension.js путь/к/gasem
//
// Нужен npm install (vscode-languageclient).

const assert = require("assert");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

const gasem = process.argv[2] || "gasem";

async function testExtension() {
  const commands = {};
  const clients = [];
  const tasks = [];
  const fakeVscode = {
    workspace: {
      getConfiguration: () => ({ get: (k, d) => (k === "path" ? gasem : d) }),
      onDidChangeConfiguration: () => ({ dispose() {} }),
      getWorkspaceFolder: () => undefined,
    },
    window: {
      activeTextEditor: {
        document: { languageId: "gasem", isUntitled: false, fileName: "/tmp/x.gsm", uri: {}, save: async () => true },
      },
      showWarningMessage: (m) => { throw new Error("предупреждение: " + m); },
      showInformationMessage: () => {},
    },
    commands: { registerCommand: (name, fn) => { commands[name] = fn; return { dispose() {} }; } },
    tasks: { executeTask: async (t) => tasks.push(t) },
    Task: function (def, scope, name, source, exec) { Object.assign(this, { def, name, source, exec }); },
    ProcessExecution: function (cmd, args, opts) { Object.assign(this, { cmd, args, opts }); },
    TaskScope: { Workspace: 2 },
    TaskRevealKind: { Always: 1 },
  };
  class FakeClient {
    constructor(id, name, server, options) {
      Object.assign(this, { id, name, server, options });
      clients.push(this);
    }
    async start() { this.started = true; }
    async stop() { this.stopped = true; }
  }
  const load = Module._load;
  Module._load = function (request, ...rest) {
    if (request === "vscode") return fakeVscode;
    if (request === "vscode-languageclient/node") return { LanguageClient: FakeClient, TransportKind: { stdio: 0 } };
    return load.call(this, request, ...rest);
  };
  try {
    const ext = require("./extension.js");
    const context = { subscriptions: [] };
    await ext.activate(context);
    assert.deepStrictEqual(Object.keys(commands).sort(), ["gasem.build", "gasem.restart", "gasem.run"]);
    assert.strictEqual(clients.length, 1);
    assert.strictEqual(clients[0].server.run.command, gasem);
    assert.deepStrictEqual(clients[0].server.run.args, ["lsp"]);
    assert.ok(clients[0].started);
    assert.strictEqual(clients[0].options.documentSelector[0].language, "gasem");
    await commands["gasem.run"]();
    assert.strictEqual(tasks.length, 1);
    assert.deepStrictEqual(tasks[0].exec.args, ["run", "/tmp/x.gsm"]);
    await commands["gasem.restart"]();
    assert.strictEqual(clients.length, 2);
    assert.ok(clients[0].stopped);
    await ext.deactivate();
  } finally {
    Module._load = load;
  }
  console.log("extension.js: команды и запуск языкового сервера — ok");
}

async function testProtocol() {
  const rpc = require("vscode-jsonrpc/node");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gasem-lsp-"));
  const file = path.join(dir, "Привет.gsm");
  const text = "og 0x7C00\nSPEED = 6*7\nstart:\n    mov ax - SPEED\n    mov bx - [bx + cl]\n    jmp start\n";
  fs.writeFileSync(file, text);
  const uri = "file://" + file.split(path.sep).map(encodeURIComponent).join("/");

  const proc = cp.spawn(gasem, ["lsp"], { stdio: ["pipe", "pipe", "inherit"] });
  const conn = rpc.createMessageConnection(new rpc.StreamMessageReader(proc.stdout), new rpc.StreamMessageWriter(proc.stdin));
  const diagnostics = new Promise((resolve) =>
    conn.onNotification("textDocument/publishDiagnostics", (p) => { if (p.uri === uri) resolve(p.diagnostics); }));
  conn.listen();

  const init = await conn.sendRequest("initialize", { processId: process.pid, rootUri: null, capabilities: {} });
  assert.strictEqual(init.serverInfo.name, "gasem");
  assert.ok(init.capabilities.hoverProvider);
  conn.sendNotification("initialized", {});
  conn.sendNotification("textDocument/didOpen", { textDocument: { uri, languageId: "gasem", version: 1, text } });

  const diags = await diagnostics;
  assert.strictEqual(diags.length, 1);
  assert.match(diags[0].message, /регистр cl нельзя использовать в адресе/);
  assert.deepStrictEqual(diags[0].range, { start: { line: 4, character: 19 }, end: { line: 4, character: 21 } });

  const hover = await conn.sendRequest("textDocument/hover", { textDocument: { uri }, position: { line: 3, character: 14 } });
  assert.match(hover.contents.value, /SPEED = 6\*7/);
  const def = await conn.sendRequest("textDocument/definition", { textDocument: { uri }, position: { line: 5, character: 9 } });
  assert.strictEqual(def.range.start.line, 2);
  const comp = await conn.sendRequest("textDocument/completion", { textDocument: { uri }, position: { line: 3, character: 4 } });
  assert.ok(comp.items.some((i) => i.label === "SPEED"));
  const syms = await conn.sendRequest("textDocument/documentSymbol", { textDocument: { uri } });
  assert.deepStrictEqual(syms.map((s) => s.name), ["SPEED", "start"]);
  const edits = await conn.sendRequest("textDocument/formatting", { textDocument: { uri }, options: { tabSize: 4, insertSpaces: true } });
  assert.ok(Array.isArray(edits));

  await conn.sendRequest("shutdown");
  conn.sendNotification("exit");
  await new Promise((resolve) => proc.on("exit", resolve));
  conn.dispose();
  fs.rmSync(dir, { recursive: true });
  console.log("gasem lsp через vscode-jsonrpc: ошибки, подсказки, переход, автодополнение, структура — ok");
}

(async () => {
  await testExtension();
  await testProtocol();
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
