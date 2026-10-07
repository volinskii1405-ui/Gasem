// Расширение Gasem для VS Code: подсветка (грамматика), языковой сервер
// gasem lsp (ошибки при наборе, переход к объявлению, подсказки, автодополнение,
// структура файла, выравнивание) и команды «Собрать» / «Запустить в QEMU».

const vscode = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

let client = null;

function gasemPath() {
  return vscode.workspace.getConfiguration("gasem").get("path") || "gasem";
}

async function startClient(context) {
  if (!vscode.workspace.getConfiguration("gasem").get("languageServer", true)) {
    return;
  }
  const command = gasemPath();
  const run = { command, args: ["lsp"], transport: TransportKind.stdio };
  client = new LanguageClient(
    "gasem",
    "Gasem",
    { run, debug: run },
    { documentSelector: [{ scheme: "file", language: "gasem" }, { scheme: "untitled", language: "gasem" }] }
  );
  try {
    await client.start();
  } catch (err) {
    client = null;
    vscode.window.showWarningMessage(
      `Gasem: не удалось запустить «${command} lsp» — проверка ошибок и подсказки выключены ` +
        `(подсветка работает). Укажите путь к программе gasem в настройке gasem.path.`
    );
  }
}

async function stopClient() {
  if (client) {
    const c = client;
    client = null;
    await c.stop();
  }
}

// Собрать или запустить текущий файл: gasem build / gasem run в панели терминала.
async function runGasem(mode) {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.languageId !== "gasem") {
    vscode.window.showInformationMessage("Gasem: откройте файл .gsm");
    return;
  }
  const doc = editor.document;
  if (doc.isUntitled) {
    vscode.window.showInformationMessage("Gasem: сначала сохраните файл");
    return;
  }
  await doc.save();
  const folder = vscode.workspace.getWorkspaceFolder(doc.uri);
  const task = new vscode.Task(
    { type: "gasem", mode, file: doc.fileName },
    folder || vscode.TaskScope.Workspace,
    mode === "run" ? "запустить" : "собрать",
    "gasem",
    new vscode.ProcessExecution(gasemPath(), [mode, doc.fileName], {
      cwd: require("path").dirname(doc.fileName),
    })
  );
  task.presentationOptions = { reveal: vscode.TaskRevealKind.Always, clear: true };
  await vscode.tasks.executeTask(task);
}

async function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("gasem.build", () => runGasem("build")),
    vscode.commands.registerCommand("gasem.run", () => runGasem("run")),
    vscode.commands.registerCommand("gasem.restart", async () => {
      await stopClient();
      await startClient(context);
    }),
    vscode.workspace.onDidChangeConfiguration(async (e) => {
      if (e.affectsConfiguration("gasem")) {
        await stopClient();
        await startClient(context);
      }
    })
  );
  await startClient(context);
}

function deactivate() {
  return stopClient();
}

module.exports = { activate, deactivate };
