# Gasem для VS Code

Подсветка синтаксиса, сниппеты и автоотступы для файлов `.gsm`.

## Установка

Скопируйте эту папку в каталог расширений VS Code и перезапустите редактор:

```sh
# Linux / macOS
cp -r editors/vscode ~/.vscode/extensions/gasem-0.1.0
# Windows (PowerShell)
Copy-Item -Recurse editors\vscode $env:USERPROFILE\.vscode\extensions\gasem-0.1.0
```

Или соберите пакет `.vsix` и установите его:

```sh
cd editors/vscode
npx @vscode/vsce package
code --install-extension gasem-0.1.0.vsix
```

## Что есть

- подсветка команд (включая `nxtb`, `chk`, `jf…`), регистров, меток, констант,
  данных (`b:`, `s:`, `w-N` …), строк, чисел, `do`/`let` (выражение внутри
  кавычек подсвечивается как код), `if`/`while`/`for`, разделителя ` - `;
- сниппеты: `boot` (загрузочный сектор), `if`, `ifelse`, `while`, `for`,
  `repeat`, `let`, `do`, `proc`, `str`;
- автоотступ внутри `if`/`while`/`for`/`repeat` и сворачивание блоков;
- `Ctrl+/` комментирует строки через `;`.

## Для разработчиков

Грамматика генерируется из таблиц компилятора, чтобы подсветка знала те же
команды и регистры:

```sh
python3 editors/vscode/generate.py
```

Проверить её тем же движком, что использует VS Code:

```sh
cd editors/vscode
npm install --no-save vscode-textmate vscode-oniguruma
node test-grammar.js ../../examples/hello.gsm
```
